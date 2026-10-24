"""Kaynak izleme modülü için ortak tipler."""
from __future__ import annotations

import io
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date

from ..http import FetchError, FetchResult, Fetcher
from ..text_utils import html_to_text, normalize_space

log = logging.getLogger(__name__)


class ParseError(Exception):
    """Sayfa alındı ama beklenen yapıda değil (site tasarımı değişmiş olabilir)."""


@dataclass
class FetchedDocument:
    source_key: str
    external_id: str
    title: str
    url: str
    doc_type: str = "DIGER"
    section: str | None = None
    subsection: str | None = None
    institution: str | None = None
    rg_date: date | None = None
    rg_number: str | None = None
    is_mukerrer: bool = False
    published_date: date | None = None
    pdf_url: str | None = None
    skip_reason: str | None = None  # ör. İlan bölümü → analiz dışı


@dataclass
class ScanOutcome:
    documents: list[FetchedDocument] = field(default_factory=list)
    complete: bool = True
    new_cursor: str | None = None
    coverage: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    access_status: str = "erisilebilir"


@dataclass
class DocumentText:
    text: str
    status: str  # tam | kisa | okunamadi
    institution: str | None = None
    note: str | None = None


def pdf_to_text(content: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    parts = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception as exc:  # bozuk sayfa diğerlerini engellemesin
            log.warning("PDF sayfası okunamadı: %s", exc)
    return normalize_space("\n".join(parts))


def looks_garbled(text: str) -> bool:
    """PDF'lerde yazı tipi kodlaması bozuksa çıkarılan metin kontrol karakterlerinden oluşur."""
    sample = [c for c in text[:4000] if not c.isspace()]
    if len(sample) < 20:
        return False
    bad = sum(1 for c in sample if ord(c) < 32 or c == "\ufffd" or 0x7F <= ord(c) < 0xA0)
    return bad / len(sample) > 0.05


def result_to_text(result: FetchResult) -> str:
    if result.is_pdf:
        return pdf_to_text(result.content)
    return html_to_text(result.text)


SHORT_PDF_CHARS = 300  # bunun altındaki PDF metni şüpheli sayılır, OCR denenir


def _ocr_fallback(result: FetchResult) -> str:
    from ..config import get_settings
    from .ocr import ocr_pdf

    st = get_settings()
    if not st.ocr_enabled or not result.is_pdf:
        return ""
    return ocr_pdf(result.content, max_pages=st.ocr_max_pages)


class SourceAdapter(ABC):
    """Her kaynak türü bu arayüzü uygular."""

    method: str = ""

    def __init__(self, source_key: str, config: dict | None = None):
        self.source_key = source_key
        self.config = config or {}

    @abstractmethod
    def scan(self, fetcher: Fetcher, *, cursor: str | None, today: date, state: dict) -> ScanOutcome:
        """Yeni belgeleri listeler. Hata durumunda istisna fırlatmak yerine outcome.errors doldurur."""

    def fetch_text(self, fetcher: Fetcher, doc: FetchedDocument) -> DocumentText:
        urls = [doc.url] + ([doc.pdf_url] if doc.pdf_url and doc.pdf_url != doc.url else [])
        last_error = None
        best = ""
        for url in urls:
            try:
                result = fetcher.get(url)
                text = result_to_text(result)
            except (FetchError, Exception) as exc:  # PDF ayrıştırma hataları dahil
                last_error = exc
                continue
            if text and looks_garbled(text):
                text = _ocr_fallback(result)
                if not text or looks_garbled(text):
                    last_error = "PDF metni okunamadı (yazı tipi kodlaması bozuk); resmî bağlantıdan açın"
                    continue
            elif result.is_pdf and len(text) < SHORT_PDF_CHARS:
                # Yalnızca başlık satırı gibi çok kısa metin: gövde görüntü olabilir; OCR daha uzun metin verirse onu kullan
                ocr_text = _ocr_fallback(result)
                if ocr_text and not looks_garbled(ocr_text) and len(ocr_text) > max(len(text) * 1.5, len(text) + 100):
                    text = ocr_text
            if len(text) > len(best):
                best = text
            if len(text) >= 400:
                return DocumentText(text=text, status="tam")
        if best:
            return DocumentText(text=best, status="kisa", note="Metin beklenenden kısa")
        return DocumentText(text="", status="okunamadi", note=str(last_error) if last_error else "Metin boş")
