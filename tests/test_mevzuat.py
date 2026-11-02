"""Mevzuat Bilgi Sistemi konsolide metin izleme (mevzuat_konsolide)."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import respx
from sqlalchemy import select

from enerji_takip.db import session_scope
from enerji_takip.http import Fetcher
from enerji_takip.models import RegulatoryChange, RegulatoryDocument, RegulatorySource
from enerji_takip.sources.mevzuat import (
    MevzuatKonsolideAdapter,
    diff_articles,
    extract_markers,
    iframe_url,
    split_into_articles,
)
from enerji_takip.text_utils import html_to_text
from tests.conftest import fake_chain, make_pipeline

FIX = Path(__file__).parent / "fixtures" / "mevzuat"
V1 = (FIX / "24729_v1.html").read_text(encoding="utf-8")
V2 = (FIX / "24729_v2.html").read_text(encoding="utf-8")
URL = iframe_url(7, "24729")
URL_KANUN = iframe_url(1, "5627")
CONFIG = {"items": [{"mevzuat_no": 24729, "tur": 7, "name": "Enerji Verimliliği Denetim Yönetmeliği"}],
          "institution": "Mevzuat Bilgi Sistemi"}


def _html(body):
    return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})


def _scan(state, body, today=date(2026, 11, 2), config=CONFIG, extra=None):
    adapter = MevzuatKonsolideAdapter("mevzuat_bilgi_sistemi", config)
    with respx.mock(assert_all_called=False) as r:
        r.get("https://www.mevzuat.gov.tr/robots.txt").respond(404)
        r.get(URL).mock(return_value=body if isinstance(body, httpx.Response) else _html(body))
        for url, resp in (extra or {}).items():
            r.get(url).mock(return_value=resp)
        outcome = adapter.scan(Fetcher(), cursor=None, today=today, state=state)
    return adapter, outcome


def test_maddeler_isaretler_ve_tablo_ayrilir():
    text = html_to_text(V1)
    labels = [l for l, _ in split_into_articles(text)]
    assert labels == ["_giris", "MADDE 1", "MADDE 2", "MADDE 3", "MADDE 4", "MADDE 5", "MADDE 14",
                      "MADDE 19", "MADDE 20", "_tablo"]
    # <span> ile bölünmüş işaretler tek biçime getirilir
    assert "(Mülga:RG-27/12/2019-30991)" in extract_markers(text)
    assert "(Değişik ibare:RG-27/12/2019-30991)" in extract_markers(text)


def test_madde_basliklari_sonraki_maddeye_dahil_edilir():
    """Araya yeni madde eklenince önceki madde 'değişti' görünmemeli."""
    a1 = split_into_articles(html_to_text(V1))
    a2 = split_into_articles(html_to_text(V2))
    changes = {label: op for label, op, _, _ in diff_articles(a1, a2)}
    assert changes == {"MADDE 14": "değiştirildi", "EK MADDE 1": "eklendi", "_tablo": "değiştirildi"}
    assert dict(a1)["MADDE 2"].startswith("Kapsam MADDE 2")


def test_ilk_tarama_taban_kaydeder_degisiklik_uretmez():
    state: dict = {}
    _, outcome = _scan(state, V1)
    assert outcome.complete and outcome.documents == []
    assert outcome.coverage["ilk_kayit"] == 1 and "7.5.24729" in state
    _, again = _scan(state, V1)
    assert again.documents == [] and again.coverage["guncellenen"] == 0


def test_metin_degisince_eski_yeni_karsilastirmasi_uretilir():
    state: dict = {}
    _scan(state, V1)
    adapter, outcome = _scan(state, V2, today=date(2026, 11, 3))
    assert outcome.complete and len(outcome.documents) == 1
    fd = outcome.documents[0]
    assert fd.doc_type == "KONSOLIDE"
    assert fd.title.startswith("Enerji Verimliliği Denetim Yönetmeliği — konsolide metin güncellendi (")
    assert "MADDE 14" in fd.title and "EK MADDE 1" in fd.title
    assert (fd.rg_date, fd.rg_number) == (date(2026, 11, 1), "33418")
    assert fd.url.startswith("https://www.mevzuat.gov.tr/mevzuat?") and fd.pdf_url is None
    text = fd.inline_text
    assert "Değişen maddeler: MADDE 14 (değiştirildi), EK MADDE 1 (eklendi), Yayım ve değişiklik tablosu (değiştirildi)." in text
    assert "ESKİ METİN:" in text and "1 (bir) ay içerisinde" in text
    assert "YENİ METİN:" in text and "15 gün içerisinde elektronik ortamda" in text
    assert "(Ek:RG-01/11/2026-33418)" in text
    # Taban yeni metne geçti: aynı metin bir daha raporlanmaz
    _, again = _scan(state, V2, today=date(2026, 11, 4))
    assert again.documents == []


def test_yalnizca_tablo_degisirse_raporlanmaz():
    state: dict = {}
    _scan(state, V1)
    only_table = V1.replace("<p><span>30991</span></p>", "<p><span>30991 </span></p> <p>not</p>")
    _, outcome = _scan(state, only_table)
    assert outcome.documents == []


def test_bir_kalem_hatasi_digerlerini_engellemez():
    cfg = {"items": CONFIG["items"] + [{"mevzuat_no": 5627, "tur": 1, "name": "Enerji Verimliliği Kanunu"}]}
    state: dict = {}
    _, outcome = _scan(state, V1, config=cfg, extra={URL_KANUN: httpx.Response(500)})
    assert not outcome.complete and outcome.access_status == "erisilebilir"
    assert "7.5.24729" in state and "1.5.5627" not in state
    assert any("Enerji Verimliliği Kanunu" in e for e in outcome.errors)


def test_beklenmeyen_sayfa_hata_sayilir():
    _, outcome = _scan({}, "<html><body><p>Sayfa bulunamadı</p></body></html>")
    assert not outcome.complete and outcome.access_status == "erisilemiyor"
    assert "madde başlığı yok" in outcome.errors[0]


def test_bos_izleme_listesi_hata():
    adapter = MevzuatKonsolideAdapter("mevzuat_bilgi_sistemi", {"items": []})
    outcome = adapter.scan(Fetcher(), cursor=None, today=date(2026, 11, 2), state={})
    assert not outcome.complete


SOURCES = """
sources:
  - key: mevzuat_bilgi_sistemi
    name: Mevzuat Bilgi Sistemi – konsolide metinler
    url: https://www.mevzuat.gov.tr/
    category: Konsolide metinler
    method: mevzuat_konsolide
    config:
      institution: Mevzuat Bilgi Sistemi
      items:
        - {mevzuat_no: 24729, tur: 7, name: Enerji Verimliliği Denetim Yönetmeliği}
"""


def test_pipeline_konsolide_guncelleme_degisiklik_kaydi(settings, smtp_server, tmp_path):
    src = tmp_path / "sources_mevzuat.yaml"
    src.write_text(SOURCES, encoding="utf-8")
    settings.sources_file = src
    for body, day in ((V1, date(2026, 11, 2)), (V2, date(2026, 11, 3))):
        with respx.mock(assert_all_called=False) as r:
            r.get("https://www.mevzuat.gov.tr/robots.txt").respond(404)
            r.get(URL).mock(return_value=_html(body))
            result = make_pipeline(settings, fake_chain()).run(run_type="tarama", today=day)
        assert result.complete
    with session_scope() as s:
        changes = list(s.scalars(select(RegulatoryChange)))
        assert len(changes) == 1
        chg = changes[0]
        assert chg.change_type == "KONSOLIDE_GUNCELLEME"
        assert chg.amended_regulation == "Enerji Verimliliği Denetim Yönetmeliği"
        assert ("EK MADDE 1", "eklendi") in [(x["madde"], x["islem"]) for x in chg.amended_articles]
        doc = s.get(RegulatoryDocument, chg.document_id)
        assert doc.rg_number == "33418" and "YENİ METİN:" in doc.full_text
        assert chg.amended_regulation_ref == "değişikliği yapan: 1/11/2026 tarihli ve 33418 sayılı Resmî Gazete"
        source = s.scalar(select(RegulatorySource).where(RegulatorySource.key == "mevzuat_bilgi_sistemi"))
        assert source.access_status == "erisilebilir"


def test_karsilastirma_yoksa_guncel_tam_metin_alinir():
    from enerji_takip.sources.base import FetchedDocument

    adapter = MevzuatKonsolideAdapter("mevzuat_bilgi_sistemi", CONFIG)
    fd = FetchedDocument(source_key="mevzuat_bilgi_sistemi", external_id="mevzuat:7.5.24729:abc:",
                         title="x", url="https://www.mevzuat.gov.tr/mevzuat?MevzuatNo=24729&MevzuatTur=7&MevzuatTertip=5",
                         doc_type="KONSOLIDE")
    with respx.mock(assert_all_called=False) as r:
        r.get("https://www.mevzuat.gov.tr/robots.txt").respond(404)
        r.get(URL).mock(return_value=_html(V2))
        dt = adapter.fetch_text(Fetcher(), fd)
    assert "EK MADDE 1" in dt.text and dt.note
