"""Bozuk kodlamalı PDF için OCR yedeği: araçlar yoksa atlanır, varsa metin kullanılır."""
from __future__ import annotations

from datetime import date

from enerji_takip.config import Settings, get_settings, set_settings
from enerji_takip.http import FetchResult
from enerji_takip.sources import base, ocr
from enerji_takip.sources.base import FetchedDocument, SourceAdapter

GARBLED = "\x01\x02\x03\x04" * 40
READABLE = "Karar No: 14967 Asgari Veriler Listesinin onaylanmasına karar verilmiştir. " * 8


class _Fetcher:
    def get(self, url):
        return FetchResult(url=url, status_code=200, content=b"%PDF-1.4", text="", content_type="application/pdf")


class _Adapter(SourceAdapter):
    def scan(self, *a, **k):  # pragma: no cover
        raise NotImplementedError


def _doc():
    return FetchedDocument(source_key="epdk", external_id="1", title="Karar", url="https://x.test/k.pdf", rg_date=date(2026, 10, 20))


def _patch(monkeypatch, ocr_text):
    monkeypatch.setattr(base, "pdf_to_text", lambda content: GARBLED)
    monkeypatch.setattr(ocr, "ocr_pdf", lambda content, **kw: ocr_text)
    st = Settings()
    st.ocr_enabled = True
    set_settings(st)


def test_bozuk_pdf_ocr_ile_okunur(monkeypatch):
    _patch(monkeypatch, READABLE)
    out = _Adapter("epdk").fetch_text(_Fetcher(), _doc())
    assert out.status == "tam" and "14967" in out.text


def test_ocr_bossa_okunamadi_kalir(monkeypatch):
    _patch(monkeypatch, "")
    out = _Adapter("epdk").fetch_text(_Fetcher(), _doc())
    assert out.status == "okunamadi" and "bozuk" in out.note


def test_ocr_kapaliysa_cagrilmaz(monkeypatch):
    _patch(monkeypatch, READABLE)
    monkeypatch.setattr(get_settings(), "ocr_enabled", False)
    out = _Adapter("epdk").fetch_text(_Fetcher(), _doc())
    assert out.status == "okunamadi"


def test_araclar_yoksa_ocr_bos_doner(monkeypatch):
    monkeypatch.setattr(ocr.shutil, "which", lambda name: None)
    assert ocr.ocr_available() is False
    assert ocr.ocr_pdf(b"%PDF") == ""
