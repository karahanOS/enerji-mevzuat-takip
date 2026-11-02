"""Değişen maddelerin ESKİ hâli: izleme tabanı, mevzuat.gov.tr canlı arama, zamanlama kontrolü, rapor ve teyit."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import respx
from sqlalchemy import select

from enerji_takip.db import session_scope
from enerji_takip.detection.previous import SEARCH_URL, already_applied, attach_previous, find_previous
from enerji_takip.http import Fetcher
from enerji_takip.models import (
    EmailReport,
    EmailReportItem,
    RegulatoryChange,
    RegulatoryDocument,
    RegulatorySource,
)
from enerji_takip.sources.mevzuat import MevzuatKonsolideAdapter, iframe_url
from tests.conftest import fake_chain, make_pipeline, register_all

FIX = Path(__file__).parent / "fixtures" / "mevzuat"
V1 = (FIX / "24729_v1.html").read_text(encoding="utf-8")
V2 = (FIX / "24729_v2.html").read_text(encoding="utf-8")
DUY_OLD = (FIX / "12985_eski.html").read_text(encoding="utf-8")
EVD = "Enerji Verimliliği Denetim Yönetmeliği"
DUY = "Elektrik Piyasası Dengeleme ve Uzlaştırma Yönetmeliği"
ARTS = [{"madde": "Madde 14", "islem": "değiştirildi", "yeni": "MADDE 3 – Aynı Yönetmeliğin 14 üncü maddesi …"},
        {"madde": "Ek Madde 1", "islem": "eklendi", "yeni": "MADDE 4 – …"}]


def _html(body):
    return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})


def _search_hit(no, tur, name):
    return httpx.Response(200, json={"draw": 1, "recordsTotal": 1, "recordsFiltered": 1, "data": [
        {"mevzuatNo": str(no), "mevzuatTur": tur, "mevzuatTertip": "5",
         "mevAdi": f"<span style='background-color:yellow'>{name.upper()}</span>"}]})


def _watch_source(session, body):
    """İzleme listesinde EVD olan bir mevzuat_konsolide kaynağı; tabanı verilen metinden."""
    cfg = {"items": [{"mevzuat_no": 24729, "tur": 7, "name": EVD}]}
    state: dict = {}
    with respx.mock(assert_all_called=False) as r:
        r.get("https://www.mevzuat.gov.tr/robots.txt").respond(404)
        r.get(iframe_url(7, "24729")).mock(return_value=_html(body))
        MevzuatKonsolideAdapter("mevzuat_bilgi_sistemi", cfg).scan(Fetcher(), cursor=None, today=date(2026, 10, 30), state=state)
    src = RegulatorySource(key="mevzuat_bilgi_sistemi", name="Mevzuat Bilgi Sistemi", url="https://www.mevzuat.gov.tr/",
                           category="Konsolide", method="mevzuat_konsolide", config={**cfg, "_state": state})
    session.add(src)
    session.commit()
    return src


def test_izleme_tabanindan_eski_madde_metni(settings):
    with session_scope() as s:
        _watch_source(s, V1)
        out, prev = attach_previous(s, Fetcher(), ARTS, EVD, "33418", live=False)
    assert prev.status == "bulundu"
    m14, ek1 = out
    assert "1 (bir) ay içerisinde ödenir" in m14["eski"]
    assert "sistemde saklanan" in m14["eski_kaynak"]
    assert m14["konsolide_url"].startswith("https://www.mevzuat.gov.tr/mevzuat?MevzuatNo=24729")
    assert ek1["eski"] is None and ek1["eski_not"].startswith("Yeni madde")
    assert m14["yeni"] == ARTS[0]["yeni"]  # yeni metin korunur


def test_taban_degisikligi_zaten_iceriyorsa_eski_sayilmaz(settings):
    with session_scope() as s:
        _watch_source(s, V2)  # V2: (Değişik:RG-01/11/2026-33418) işlenmiş
        out, prev = attach_previous(s, Fetcher(), ARTS, EVD, "33418", live=False)
    assert prev.status == "guncellenmis" and out[0]["eski"] is None and "zaten içeriyor" in out[0]["eski_not"]


def test_listede_yoksa_canli_aramadan_alinir(settings):
    with session_scope() as s, respx.mock(assert_all_called=False) as r:
        r.get("https://www.mevzuat.gov.tr/robots.txt").respond(404)
        search = r.post(SEARCH_URL).mock(return_value=_search_hit(12985, 7, DUY))
        r.get(iframe_url(7, "12985")).mock(return_value=_html(DUY_OLD))
        out, prev = attach_previous(s, Fetcher(), [{"madde": "Madde 4", "islem": "değiştirildi"},
                                                   {"madde": "Geçici Madde 15", "islem": "yürürlükten kaldırıldı"}],
                                    DUY, "33999")
    assert prev.status == "bulundu" and prev.source == "mevzuat_gov_canli"
    body = json.loads(search.calls[0].request.content)
    assert body["parameters"]["AranacakYer"] == "Baslik"  # arama ifadesi base64
    assert "yüksek olanı esas alınarak" in out[0]["eski"]
    assert "eski yönteme göre hesaplanır" in out[1]["eski"]
    assert "işlenmeden önce" in out[0]["eski_kaynak"]


def test_sitede_degisiklik_islenmisse_eski_kullanilmaz(settings):
    with session_scope() as s, respx.mock(assert_all_called=False) as r:
        r.get("https://www.mevzuat.gov.tr/robots.txt").respond(404)
        r.post(SEARCH_URL).mock(return_value=_search_hit(24729, 7, "Başka Ad"))
        prev_missing = find_previous(s, Fetcher(), "Hiç Olmayan Yönetmelik", "1")
    assert prev_missing.status == "bulunamadi"
    with session_scope() as s, respx.mock(assert_all_called=False) as r:
        r.get("https://www.mevzuat.gov.tr/robots.txt").respond(404)
        r.post(SEARCH_URL).mock(return_value=_search_hit(24729, 7, EVD))
        r.get(iframe_url(7, "24729")).mock(return_value=_html(V2))
        prev = find_previous(s, Fetcher(), EVD, "33418")
    assert prev.status == "guncellenmis"


def test_erisim_hatasi_tespiti_engellemez(settings, monkeypatch):
    from enerji_takip import http as http_mod

    monkeypatch.setattr(http_mod.time, "sleep", lambda *_: None)
    with session_scope() as s, respx.mock(assert_all_called=False) as r:
        r.get("https://www.mevzuat.gov.tr/robots.txt").respond(404)
        r.post(SEARCH_URL).respond(503)
        out, prev = attach_previous(s, Fetcher(), ARTS, EVD, "33418")
    assert prev.status == "hata" and out[0]["eski"] is None and "erişilemedi" in out[0]["eski_not"]


def test_kanun_degisikligi_kanun_numarasiyla_kontrol_edilir():
    assert already_applied(["(Ek:19/7/2025-7554/18 md.)"], rg_number="32960", law_number="7554")
    assert not already_applied(["(Ek:19/7/2025-7554/18 md.)"], rg_number="32960", law_number="7600")
    assert already_applied(["(Değişik:RG-01/11/2026-33418)"], rg_number="33418")


def _mevzuat_routes(r):
    r.get("https://www.mevzuat.gov.tr/robots.txt").respond(404)
    r.post(SEARCH_URL).mock(return_value=_search_hit(12985, 7, DUY))
    r.get(iframe_url(7, "12985")).mock(return_value=_html(DUY_OLD))


def test_eposta_eskiden_simdi_tablosu(settings, smtp_server):
    settings.previous_text_lookup = True
    with respx.mock(assert_all_called=False) as router:
        _mevzuat_routes(router)
        register_all(router)
        make_pipeline(settings, fake_chain()).run()
    with session_scope() as s:
        chg = next(c for c in s.scalars(select(RegulatoryChange)) if c.amended_regulation == DUY)
        by = {a["madde"]: a for a in chg.amended_articles}
        assert "yüksek olanı esas alınarak" in by["Madde 4"]["eski"]
        assert "yüzde üç fazlası" in by["Madde 4"]["yeni"]
    mail = next(m for m in smtp_server.messages if "SİSTEM" not in m["msg"]["Subject"])
    html = mail["msg"].get_body(("html",)).get_content()
    text = mail["msg"].get_body(("plain",)).get_content()
    assert "Eskiden / Şimdi" in html and "Yapmanız gereken:" in html
    assert "Madde 4: eski hüküm (TEST)" in html and "Hesaplamayı güncelleyin (TEST)" in html
    assert "Önceki metin kaynağı:" in html and "yüksek olanı esas alınarak" in html
    assert "Eskiden: Madde 4: eski hüküm (TEST)" in text


def test_eski_metin_yoksa_raporda_belirtilir(settings, smtp_server):
    with respx.mock(assert_all_called=False) as router:
        register_all(router)
        make_pipeline(settings, fake_chain()).run()  # previous_text_lookup kapalı
    mail = next(m for m in smtp_server.messages if "SİSTEM" not in m["msg"]["Subject"])
    html = mail["msg"].get_body(("html",)).get_content()
    assert "Değişen maddelerin önceki hâli alınamadı" in html
    assert "yüzde üç fazlası" in html  # yeni metin yine gösterilir


# ---- 3: konsolide güncelleme, RG kaydını teyit eder ---------------------------
KONS_SOURCES = """
sources:
  - key: mevzuat_bilgi_sistemi
    name: Mevzuat Bilgi Sistemi – konsolide metinler
    url: https://www.mevzuat.gov.tr/
    category: Konsolide metinler
    method: mevzuat_konsolide
    config:
      items:
        - {mevzuat_no: 24729, tur: 7, name: Enerji Verimliliği Denetim Yönetmeliği}
"""


def _prior_rg_change(session, *, with_old: bool, reported: bool, regulation: str = EVD) -> str:
    rg = session.scalar(select(RegulatorySource).where(RegulatorySource.key == "resmi_gazete"))
    if rg is None:
        rg = RegulatorySource(key="resmi_gazete", name="Resmî Gazete", url="https://www.resmigazete.gov.tr/",
                              category="Birincil", method="rg_fihrist")
        session.add(rg)
        session.flush()
    doc = RegulatoryDocument(source_id=rg.id, dedup_key=f"rg-test-{regulation}", external_id="x",
                             title=f"{regulation}nde Değişiklik Yapılmasına Dair Yönetmelik", doc_type="YONETMELIK",
                             rg_date=date(2026, 11, 1), rg_number="33418", url="https://www.resmigazete.gov.tr/x.htm")
    session.add(doc)
    session.flush()
    art = {"madde": "Madde 14", "islem": "değiştirildi", "yeni": "MADDE 1 – … 15 gün içerisinde …"}
    if with_old:
        art["eski"] = "MADDE 14 – eski metin (önceden alınmış)"
    chg = RegulatoryChange(document_id=doc.id, dedup_key=f"chg-{regulation}", change_type="DEGISIKLIK",
                           amended_regulation=regulation, amended_articles=[art], status="yeni")
    session.add(chg)
    session.flush()
    if reported:
        rep = EmailReport(status="gonderildi", report_date=date(2026, 11, 1), subject="x", html_body="", text_body="")
        session.add(rep)
        session.flush()
        session.add(EmailReportItem(report_id=rep.id, change_id=chg.id))
    session.commit()
    return chg.id


def _run_konsolide(settings, tmp_path):
    src = tmp_path / "s.yaml"
    src.write_text(KONS_SOURCES, encoding="utf-8")
    settings.sources_file = src
    for body, day in ((V1, date(2026, 11, 2)), (V2, date(2026, 11, 20))):
        with respx.mock(assert_all_called=False) as r:
            r.get("https://www.mevzuat.gov.tr/robots.txt").respond(404)
            r.get(iframe_url(7, "24729")).mock(return_value=_html(body))
            make_pipeline(settings, fake_chain()).run(run_type="tarama", today=day)


def test_konsolide_guncelleme_rg_kaydina_eski_metni_ekler(settings, smtp_server, tmp_path):
    with session_scope() as s:
        cid = _prior_rg_change(s, with_old=False, reported=False)
    _run_konsolide(settings, tmp_path)
    with session_scope() as s:
        assert [c.id for c in s.scalars(select(RegulatoryChange))] == [cid]  # ayrı kayıt açılmadı
        art = s.get(RegulatoryChange, cid).amended_articles[0]
        assert "1 (bir) ay içerisinde ödenir" in art["eski"] and art["teyit"]
        kons = s.scalar(select(RegulatoryDocument).where(RegulatoryDocument.doc_type == "KONSOLIDE"))
        assert kons.relevance == "kopya" and "teyit etti" in kons.relevance_reason


def test_eski_metinli_rg_kaydi_yalnizca_teyit_edilir(settings, smtp_server, tmp_path):
    with session_scope() as s:
        cid = _prior_rg_change(s, with_old=True, reported=True)
    _run_konsolide(settings, tmp_path)
    with session_scope() as s:
        assert s.query(RegulatoryChange).count() == 1
        art = s.get(RegulatoryChange, cid).amended_articles[0]
        assert art["eski"] == "MADDE 14 – eski metin (önceden alınmış)" and art["teyit"]


def test_eski_metinsiz_bildirilmis_rg_kaydi_icin_gecikmeli_karsilastirma_gelir(settings, smtp_server, tmp_path):
    with session_scope() as s:
        cid = _prior_rg_change(s, with_old=False, reported=True)
    _run_konsolide(settings, tmp_path)
    with session_scope() as s:
        changes = list(s.scalars(select(RegulatoryChange)))
        assert len(changes) == 2
        kons = next(c for c in changes if c.id != cid)
        assert kons.change_type == "KONSOLIDE_GUNCELLEME"
        assert "eski metin olmadan bildirilmişti" in kons.amended_regulation_ref


def test_ayni_rg_sayisindaki_baska_mevzuat_baglanmaz(settings, smtp_server, tmp_path):
    with session_scope() as s:
        _prior_rg_change(s, with_old=False, reported=False, regulation="Elektrik Piyasası Lisans Yönetmeliği")
    _run_konsolide(settings, tmp_path)
    with session_scope() as s:
        kons = next(c for c in s.scalars(select(RegulatoryChange)) if c.change_type == "KONSOLIDE_GUNCELLEME")
        assert kons.amended_regulation_ref == "değişikliği yapan: 1/11/2026 tarihli ve 33418 sayılı Resmî Gazete"
