"""HTTP katmanı: robots.txt uyumu, nazik hız sınırı, yeniden deneme ve Türkçe karakter kodlaması."""
from __future__ import annotations

import logging
import re
import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx

from .config import Settings, get_settings

log = logging.getLogger(__name__)


class FetchError(Exception):
    """Kaynağa erişilemedi (ağ hatası, 5xx, zaman aşımı)."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class NotFound(FetchError):
    """404 — sayfa yok (ör. mükerrer sayı yayımlanmamış)."""


class RobotsDisallowed(FetchError):
    """Sitenin robots.txt dosyası bu adresin otomatik taranmasına izin vermiyor."""


@dataclass
class FetchResult:
    url: str
    status_code: int
    content: bytes
    text: str
    content_type: str

    @property
    def is_pdf(self) -> bool:
        return "pdf" in self.content_type or self.url.lower().endswith(".pdf")


_META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?([\w-]+)""", re.IGNORECASE)


def decode_html(content: bytes, header_charset: str | None) -> str:
    """Sunucu başlığı → meta etiketi → utf-8 → windows-1254 sırasıyla çözümler."""
    candidates: list[str] = []
    if header_charset:
        candidates.append(header_charset)
    m = _META_CHARSET_RE.search(content[:4096])
    if m:
        candidates.append(m.group(1).decode("ascii", "ignore"))
    candidates += ["utf-8", "windows-1254"]
    for enc in candidates:
        enc = enc.lower().replace("iso-8859-9", "windows-1254")
        try:
            return content.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("windows-1254", errors="replace")


class Fetcher:
    def __init__(self, settings: Settings | None = None, client: httpx.Client | None = None):
        self.settings = settings or get_settings()
        self.client = client or httpx.Client(
            timeout=self.settings.http_timeout_seconds,
            follow_redirects=True,
            headers={
                "User-Agent": self.settings.http_user_agent,
                "Accept-Language": "tr-TR,tr;q=0.9",
            },
        )
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def close(self) -> None:
        self.client.close()

    # ---- robots.txt ----------------------------------------------------
    def robots_allowed(self, url: str) -> bool:
        if not self.settings.respect_robots_txt:
            return True
        parsed = urlparse(url)
        host = f"{parsed.scheme}://{parsed.netloc}"
        if host not in self._robots:
            self._robots[host] = self._load_robots(host)
        parser = self._robots[host]
        if parser is None:  # robots.txt yok (4xx) → RFC 9309: her şey serbest
            return True
        return parser.can_fetch(self.settings.http_user_agent, url)

    def _load_robots(self, host: str) -> urllib.robotparser.RobotFileParser | None:
        robots_url = urljoin(host, "/robots.txt")
        self._throttle(host)
        try:
            resp = self.client.get(robots_url)
        except httpx.HTTPError as exc:
            # RFC 9309: robots.txt'ye ulaşılamıyorsa tarama yapılmamalı
            raise FetchError(f"robots.txt alınamadı ({robots_url}): {exc}") from exc
        if 400 <= resp.status_code < 500:
            return None
        if resp.status_code >= 500:
            raise FetchError(f"robots.txt sunucu hatası {resp.status_code} ({robots_url})", resp.status_code)
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(decode_html(resp.content, resp.charset_encoding).splitlines())
        return parser

    # ---- istek ---------------------------------------------------------
    def _throttle(self, host: str) -> None:
        last = self._last_request.get(host)
        if last is not None:
            wait = self.settings.http_min_interval_seconds - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_request[host] = time.monotonic()

    def get(self, url: str, attempts: int = 3) -> FetchResult:
        if not self.robots_allowed(url):
            raise RobotsDisallowed(
                f"robots.txt bu adresin otomatik taranmasına izin vermiyor: {url}"
            )
        parsed = urlparse(url)
        host = f"{parsed.scheme}://{parsed.netloc}"
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            self._throttle(host)
            try:
                resp = self.client.get(url)
            except httpx.HTTPError as exc:
                last_error = exc
                log.warning("İstek hatası (%s/%s) %s: %s", attempt, attempts, url, exc)
            else:
                if resp.status_code == 404:
                    raise NotFound(f"Bulunamadı (404): {url}", 404)
                if resp.status_code in (401, 403):
                    raise FetchError(f"Erişim reddedildi ({resp.status_code}): {url}", resp.status_code)
                if resp.status_code >= 500 or resp.status_code == 429:
                    last_error = FetchError(f"Sunucu hatası {resp.status_code}: {url}", resp.status_code)
                    log.warning("Sunucu hatası (%s/%s) %s: %s", attempt, attempts, url, resp.status_code)
                elif resp.status_code >= 400:
                    raise FetchError(f"HTTP {resp.status_code}: {url}", resp.status_code)
                else:
                    ctype = resp.headers.get("content-type", "").lower()
                    text = "" if ("pdf" in ctype or url.lower().endswith(".pdf")) else decode_html(
                        resp.content, resp.charset_encoding
                    )
                    return FetchResult(str(resp.url), resp.status_code, resp.content, text, ctype)
            if attempt < attempts:
                time.sleep(min(2 ** attempt, 10))
        if isinstance(last_error, FetchError):
            raise last_error
        raise FetchError(f"Kaynağa erişilemedi: {url} ({last_error})")
