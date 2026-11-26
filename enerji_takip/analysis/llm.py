"""Ücretsiz LLM sağlayıcıları için tek istemci ve yedekleme zinciri.

Gemini (ücretsiz katman), Groq (ücretsiz katman) ve yerel Ollama aynı OpenAI uyumlu
`/chat/completions` arayüzünü sunar. Zincirdeki ilk sağlayıcının kotası dolar veya
hata verirse sıradakine geçilir. Ücretsiz kotayı aşmamak için günlük bir çağrı
tavanı uygulanır (LLM_DAILY_MAX_CALLS).

Geçici hatalara dayanıklılık: 5xx/zaman aşımında artan bekleme (8 sn, 24 sn …) ile
yeniden deneme, 429'da sağlayıcının istediği süre kadar bekleme (Gemini gövdedeki
retryDelay dahil), sağlayıcı başına asgari istek aralığı ve art arda düşen modeli
çalıştırmanın kalanında atlayan bir devre kesici.
"""
from __future__ import annotations

import logging
import random
import re
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


class LLMTransientError(LLMError):
    """Geçici hata (5xx, zaman aşımı, bağlantı) — bir süre sonra yeniden denenebilir."""


TRANSIENT_STATUSES = (500, 502, 503, 504)
_DAILY_QUOTA_MARKERS = ("perday", "per day", "per_day", "daily")
_RETRY_DELAY_RE = re.compile(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"')

# Testlerde gerçek bekleme yapılmasın diye değiştirilebilir
_sleep = time.sleep
_monotonic = time.monotonic
# Aynı sağlayıcıya (aynı API anahtarına) giden istekler arası asgari aralık için son istek zamanı
_last_request_at: dict[str, float] = {}


@dataclass
class Message:
    role: str  # user | assistant
    content: str


class OpenAICompatClient:
    """Tek bir sağlayıcı + model."""

    def __init__(self, provider: str, model: str, base_url: str, api_key: str, *,
                 max_input_chars: int, max_output_tokens: int, timeout: int,
                 http_retries: int = 3, backoff_base: float = 8.0, backoff_max: float = 60.0,
                 rate_wait_max: float = 65.0, min_interval: float = 0.0, breaker_threshold: int = 2):
        self.provider = provider
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.max_input_chars = max_input_chars
        self.max_output_tokens = max_output_tokens
        self.http_retries = max(1, http_retries)
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.rate_wait_max = rate_wait_max
        self.min_interval = min_interval
        self.breaker_threshold = breaker_threshold
        self.consecutive_failures = 0  # art arda geçici hatayla biten istek sayısı (bu çalıştırmada)
        self._json_mode = True
        self._client = httpx.Client(timeout=timeout)

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"

    @property
    def available(self) -> bool:
        """Devre kesici: art arda düşen model çalıştırmanın kalanında beklenmeden atlanır."""
        return self.breaker_threshold <= 0 or self.consecutive_failures < self.breaker_threshold

    def reset_health(self) -> None:
        self.consecutive_failures = 0

    def _backoff(self, attempt: int) -> float:
        base = min(self.backoff_max, self.backoff_base * (3 ** (attempt - 1)))
        return base * random.uniform(0.8, 1.2)

    def _pace(self) -> None:
        if self.min_interval <= 0:
            return
        last = _last_request_at.get(self.provider)
        if last is not None:
            wait = self.min_interval - (_monotonic() - last)
            if wait > 0:
                _sleep(wait)
        _last_request_at[self.provider] = _monotonic()

    def complete(self, system: str, messages: list[Message], *, max_tokens: int | None = None) -> str:
        try:
            content = self._complete(system, messages, max_tokens=max_tokens)
        except LLMTransientError:
            self.consecutive_failures += 1
            raise
        self.consecutive_failures = 0
        return content

    def _complete(self, system: str, messages: list[Message], *, max_tokens: int | None = None) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}]
            + [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": min(max_tokens or self.max_output_tokens, self.max_output_tokens),
            "temperature": 0.1,
        }
        if self._json_mode:
            payload["response_format"] = {"type": "json_object"}
        attempt = 0
        timeouts = 0
        json_fallback_used = False
        while attempt < self.http_retries:
            attempt += 1
            last = attempt >= self.http_retries
            self._pace()
            try:
                resp = self._client.post(f"{self.base_url}/chat/completions", json=payload,
                                         headers={"Authorization": f"Bearer {self.api_key}"})
            except httpx.HTTPError as exc:
                if isinstance(exc, httpx.TimeoutException):
                    timeouts += 1
                # Zaman aşımı zaten uzun sürer (LLM_TIMEOUT_SECONDS); aynı istekte ikinciden sonra vazgeç
                if last or timeouts >= 2:
                    raise LLMTransientError(f"{self.label}: bağlantı hatası: {exc}") from exc
                wait = self._backoff(attempt)
                log.info("%s bağlantı hatası (%s); %.0f sn sonra yeniden denenecek", self.label, exc, wait)
                _sleep(wait)
                continue
            if resp.status_code == 429:
                if _is_daily_quota(resp):
                    raise LLMQuotaError(f"{self.label}: günlük ücretsiz kota doldu (429) {resp.text[:200]}")
                wait = _retry_after(resp)
                if wait is None:
                    wait = self._backoff(attempt)
                if wait <= self.rate_wait_max and not last:
                    log.info("%s hız sınırı; %.0f sn bekleniyor", self.label, wait)
                    _sleep(wait)
                    continue
                raise LLMQuotaError(f"{self.label}: ücretsiz kota/hız sınırı (429) {resp.text[:200]}")
            if resp.status_code == 400 and self._json_mode and "response_format" in resp.text and not json_fallback_used:
                # Bazı model/sağlayıcılar JSON modunu desteklemez; düz metinle tekrar dene (deneme hakkından yemez)
                self._json_mode = False
                json_fallback_used = True
                payload.pop("response_format", None)
                attempt -= 1
                continue
            if resp.status_code in TRANSIENT_STATUSES:
                if last:
                    raise LLMTransientError(f"{self.label}: sunucu hatası {resp.status_code} "
                                            f"({self.http_retries} denemede)")
                wait = self._backoff(attempt)
                log.info("%s sunucu hatası %s; %.0f sn sonra yeniden denenecek", self.label, resp.status_code, wait)
                _sleep(wait)
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
        raise LLMTransientError(f"{self.label}: istek tamamlanamadı")


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after")
    if value is not None:
        try:
            return float(value)
        except ValueError:
            pass
    # Gemini bekleme süresini gövdede bildirir: "retryDelay": "37s"
    match = _RETRY_DELAY_RE.search(resp.text or "")
    return float(match.group(1)) if match else None


def _is_daily_quota(resp: httpx.Response) -> bool:
    text = (resp.text or "").lower().replace(" ", "")
    return any(marker.replace(" ", "") in text for marker in _DAILY_QUOTA_MARKERS)


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

    def reset_health(self) -> None:
        for client in self.clients:
            if hasattr(client, "reset_health"):
                client.reset_health()

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
            if not getattr(client, "available", True):
                errors.append(LLMTransientError(f"{client.label}: art arda hata verdiği için bu çalıştırmada atlandı"))
                continue
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
            http_retries=settings.llm_http_retries,
            backoff_base=settings.llm_backoff_base_seconds,
            rate_wait_max=settings.llm_rate_wait_max_seconds,
            min_interval=settings.llm_min_interval_seconds.get(provider, 0.0),
            breaker_threshold=settings.llm_breaker_threshold,
        ))
    return LLMChain(clients, daily_cap=settings.llm_daily_max_calls, usage_counter=usage_counter, usage_inc=usage_inc)
