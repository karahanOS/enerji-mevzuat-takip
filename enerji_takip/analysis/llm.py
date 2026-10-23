"""Ücretsiz LLM sağlayıcıları için tek istemci ve yedekleme zinciri.

Gemini (ücretsiz katman), Groq (ücretsiz katman) ve yerel Ollama aynı OpenAI uyumlu
`/chat/completions` arayüzünü sunar. Zincirdeki ilk sağlayıcının kotası dolar veya
hata verirse sıradakine geçilir. Ücretsiz kotayı aşmamak için günlük bir çağrı
tavanı uygulanır (LLM_DAILY_MAX_CALLS).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

import httpx

from ..config import Settings

log = logging.getLogger(__name__)


class LLMError(Exception):
    """Kalıcı hata (geçersiz istek, yetki vb.) — sıradaki sağlayıcı denenir."""


class LLMQuotaError(LLMError):
    """Ücretsiz kota veya günlük tavan doldu — sıradaki sağlayıcı denenir."""


@dataclass
class Message:
    role: str  # user | assistant
    content: str


class OpenAICompatClient:
    """Tek bir sağlayıcı + model."""

    def __init__(self, provider: str, model: str, base_url: str, api_key: str, *,
                 max_input_chars: int, max_output_tokens: int, timeout: int):
        self.provider = provider
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.max_input_chars = max_input_chars
        self.max_output_tokens = max_output_tokens
        self._json_mode = True
        self._client = httpx.Client(timeout=timeout)

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"

    def complete(self, system: str, messages: list[Message], *, max_tokens: int | None = None) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}]
            + [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": min(max_tokens or self.max_output_tokens, self.max_output_tokens),
            "temperature": 0.1,
        }
        if self._json_mode:
            payload["response_format"] = {"type": "json_object"}
        for attempt in range(1, 4):
            try:
                resp = self._client.post(f"{self.base_url}/chat/completions", json=payload,
                                         headers={"Authorization": f"Bearer {self.api_key}"})
            except httpx.HTTPError as exc:
                if attempt == 3:
                    raise LLMError(f"{self.label}: bağlantı hatası: {exc}") from exc
                time.sleep(3 * attempt)
                continue
            if resp.status_code == 429:
                wait = _retry_after(resp)
                if wait is not None and wait <= 20 and attempt < 3:
                    log.info("%s hız sınırı; %ss bekleniyor", self.label, wait)
                    time.sleep(wait)
                    continue
                raise LLMQuotaError(f"{self.label}: ücretsiz kota/hız sınırı (429) {resp.text[:200]}")
            if resp.status_code == 400 and self._json_mode and "response_format" in resp.text:
                # Bazı model/sağlayıcılar JSON modunu desteklemez; düz metinle tekrar dene
                self._json_mode = False
                payload.pop("response_format", None)
                continue
            if resp.status_code in (500, 502, 503, 504):
                if attempt == 3:
                    raise LLMError(f"{self.label}: sunucu hatası {resp.status_code}")
                time.sleep(5 * attempt)
                continue
            if resp.status_code >= 400:
                raise LLMError(f"{self.label}: HTTP {resp.status_code}: {resp.text[:300]}")
            try:
                content = resp.json()["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise LLMError(f"{self.label}: beklenmeyen yanıt biçimi") from exc
            if not content:
                raise LLMError(f"{self.label}: boş yanıt")
            return content
        raise LLMError(f"{self.label}: istek tamamlanamadı")


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


class LLMChain:
    """Sıralı istemciler + günlük çağrı tavanı."""

    def __init__(self, clients: list[OpenAICompatClient], *, daily_cap: int = 0,
                 usage_counter: Callable[[], int] | None = None, usage_inc: Callable[[], None] | None = None):
        self.clients = clients
        self.daily_cap = daily_cap
        self._count = usage_counter
        self._inc = usage_inc

    def __bool__(self) -> bool:
        return bool(self.clients)

    def check_budget(self) -> None:
        if self.daily_cap and self._count is not None and self._count() >= self.daily_cap:
            raise LLMQuotaError(f"Günlük AI çağrı tavanına ulaşıldı (LLM_DAILY_MAX_CALLS={self.daily_cap})")

    def record_call(self) -> None:
        if self._inc is not None:
            self._inc()

    def call(self, client: OpenAICompatClient, system: str, messages: list[Message], *,
             max_tokens: int | None = None) -> str:
        self.check_budget()
        self.record_call()
        return client.complete(system, messages, max_tokens=max_tokens)

    def complete_any(self, system: str, build: Callable[[OpenAICompatClient], list[Message]], *,
                     max_tokens: int | None = None) -> tuple[str, OpenAICompatClient]:
        """Zincirdeki istemcileri sırayla dener; (yanıt, kullanılan istemci) döndürür."""
        errors: list[Exception] = []
        for client in self.clients:
            try:
                return self.call(client, system, build(client), max_tokens=max_tokens), client
            except LLMQuotaError as exc:
                errors.append(exc)
                if "Günlük AI çağrı tavanı" in str(exc):
                    break
            except LLMError as exc:
                errors.append(exc)
        if not errors:
            raise LLMError("Kullanılabilir AI sağlayıcısı yok")
        message = " | ".join(str(e) for e in errors)
        if all(isinstance(e, LLMQuotaError) for e in errors):
            raise LLMQuotaError(message)
        raise LLMError(message)


def build_chain(settings: Settings, entries: list[str], usage_counter=None, usage_inc=None) -> LLMChain:
    clients = []
    for entry in entries:
        provider, _, model = entry.partition(":")
        key = settings.provider_key(provider)
        base = settings.provider_base_url(provider)
        if not model or not base or not key:
            continue
        clients.append(OpenAICompatClient(
            provider, model, base, key,
            max_input_chars=settings.max_input_chars.get(provider, 12000),
            max_output_tokens=settings.max_output_tokens.get(provider, 3000),
            timeout=settings.llm_timeout_seconds,
        ))
    return LLMChain(clients, daily_cap=settings.llm_daily_max_calls, usage_counter=usage_counter, usage_inc=usage_inc)
