"""Uçtan uca iş akışı: tespit → rutin ayrımı → analiz → önem → rapor → e-posta; tekrar önleme ve hata yönetimi."""
from __future__ import annotations

from datetime import date, timedelta

import httpx
import respx
from sqlalchemy import select

from enerji_takip.db import session_scope
from enerji_takip.models import (
    AIAnalysis,
    EmailDeliveryLog,
    EmailReport,
    RegulatoryChange,
    RegulatoryDocument,
    RegulatorySource,
    ScanRun,
)
from tests.conftest import NOW, FakeClient, fake_chain, make_pipeline, register_all

DUY = "Elektrik Piyasası Dengeleme ve Uzlaştırma Yönetmeliğinde Değişiklik Yapılmasına Dair Yönetmelik"


def _changes(session):
    return {c.document.title: c for c in session.scalars(select(RegulatoryChange))}


def _reports(smtp):
    return [m for m in smtp.messages if "SİSTEM" not in m["msg"]["Subject"]]


def _alerts(smtp):
    return [m for m in smtp.messages if "SİSTEM" in m["msg"]["Subject"]]


def _html(mail):
    return mail["msg"].get_body(("html",)).get_content()


def test_enerji_mevzuati_tespit_analiz_ve_eposta(settings, smtp_server):
    with respx.mock(assert_all_called=False) as router:
        register_all(router)
        result = make_pipeline(settings, fake_chain()).run()

    assert result.status == "basarili", result.errors
    with session_scope() as s:
        changes = _changes(s)
        titles = set(changes)
        assert DUY in titles
        assert "Yenilenebilir Enerji Kaynak Garanti ve Destek Yönetmeliği" in titles
        assert any(t.startswith("Elektrik Piyasasında Dağıtım Bedellerinin") for t in titles)
        assert any("15000 Sayılı" in t for t in titles)
        assert any(t.startswith("Bazı Akaryakıt Ürünlerinde") for t in titles)          # PDF
        assert "Doğal Gaz Piyasası Tarifeler Yönetmeliğinde Değişiklik Yapılmasına Dair Yönetmelik" in titles  # mükerrer
        # Elenenler: üniversite, kamulaştırma, karayolu, AYM (enerji dışı), ilanlar
        assert not any(k in t for t in titles for k in ("Üniversitesi", "Kamulaştırılmasına", "Karayolu", "Anayasa", "ilânları"))

        # Rutin kurul kararları AI'ya gönderilmez
        rutin = [c for c in changes.values() if c.status == "rutin"]
        assert {c.document.title.split(" ve ")[-1] for c in rutin} == {"15001 Sayılı Kararı", "15002 Sayılı Kararı"}
        assert all(c.importance == "DUSUK" and c.note and not c.analyses for c in rutin)
        assert "lisansının tadil" in next(c for c in rutin if "15001" in c.document.title).note

        duy = changes[DUY]
        assert duy.change_type == "DEGISIKLIK"
        assert duy.amended_regulation == "Elektrik Piyasası Dengeleme ve Uzlaştırma Yönetmeliği"
        assert {"madde": "Madde 4", "islem": "değiştirildi"} in duy.amended_articles
        assert {"madde": "Madde 110", "islem": "eklendi"} in duy.amended_articles
        assert {"madde": "Geçici Madde 15", "islem": "yürürlükten kaldırıldı"} in duy.amended_articles
        assert duy.importance == "KRITIK" and duy.status == "analiz_edildi", duy.review_reasons
        assert "elektrik_piyasasi" in duy.categories
        assert duy.departments == ["Fiyatlar ve uzlaştırma", "Elektrik piyasası işleyişi", "Yenilenebilir enerji ve YEKDEM"]
        eff = {(e["kind"], e["provision"]): e["date"] for e in duy.effective_dates}
        assert eff[("yururluk", "Madde 1")] == "2026-11-01" and eff[("yururluk", "Diğer hükümler")] == "2026-10-23"

        kurul = next(c for t, c in changes.items() if "15000 Sayılı" in t)
        assert kurul.importance == "KRITIK" and "elektrik_piyasasi" in kurul.categories

        yek = changes["Yenilenebilir Enerji Kaynak Garanti ve Destek Yönetmeliği"]
        assert yek.effective_date == date(2027, 4, 23) and yek.importance == "YUKSEK"

        # İlk taramada kurum listesi/RSS kayıtları başlangıç sayılır
        base = s.scalars(select(RegulatoryDocument).where(RegulatoryDocument.is_baseline.is_(True))).all()
        assert {d.source.key for d in base} == {"epias_elektrik_duyurulari", "etkb_mevzuat_rss"}
        rg = s.scalar(select(RegulatorySource).where(RegulatorySource.key == "resmi_gazete"))
        assert rg.cursor == "2026-10-23"
        # AI çağrı sayacı tutuldu
        from enerji_takip.db import get_setting
        assert int(get_setting(s, "llm_calls:2026-10-23")) == s.query(AIAnalysis).count()

    mails = _reports(smtp_server)
    assert len(mails) == 1
    msg = mails[0]["msg"]
    assert msg["Subject"] == "[ENERJİ MEVZUAT] 2026-10-23 | Günlük Enerji Mevzuatı Değişiklikleri"
    assert sorted(mails[0]["rcpt"]) == ["arkadas@ornek.com", "ben@ornek.com"]
    html = _html(mails[0])
    for needle in ["Günlük Enerji Mevzuatı Raporu", "Piyasaya etkisi", "Benim açımdan önemi", "Takip önerileri",
                   "Etkilenen piyasa unsurları", "Rutin kurul kararları (2)", "Resmî kaynak", "yatırım tavsiyesi"]:
        assert needle in html, needle
    assert html.index(DUY) < html.index("Doğal Gaz Piyasası Tarifeler")      # önem sırası
    assert "https://www.resmigazete.gov.tr/eskiler/2026/10/20261023-9.htm" in html  # rutin kararın bağlantısı
    assert not _alerts(smtp_server)

    # Kontrol amaçlı: elenen belgeler ad + kaynak ile listelenir
    for needle in ["Kontrol için: elenen belgeler", "İlgisiz bulundu (", "Analiz dışı (ilan vb.) (", "Üniversitesi", "Resmî Gazete"]:
        assert needle in html, needle
    text = mails[0]["msg"].get_body(("plain",)).get_content()
    assert "KONTROL İÇİN: ELENEN BELGELER" in text and "Üniversitesi" in text
    # Rapora giren ilgili düzenlemeler elenenler listesinde tekrar yer almaz
    assert html.index("Kontrol için: elenen belgeler") > html.index(DUY)


def test_ertesi_gun_yalnizca_yeni_kayitlar_ve_kaynaklar_arasi_tekillestirme(settings, smtp_server):
    with respx.mock(assert_all_called=False) as router:
        register_all(router)
        make_pipeline(settings, fake_chain()).run()
    with session_scope() as s:
        first = s.query(RegulatoryChange).count()
    smtp_server.messages.clear()

    with respx.mock(assert_all_called=False) as router:
        register_all(router, epias="v2", etkb="v2")
        router.get("https://www.resmigazete.gov.tr/eskiler/2026/10/20261024.htm").mock(return_value=httpx.Response(
            200, text="<html><body><p>24 Ekim 2026</p><p>Sayı : 34000</p><p><b>İLÂN BÖLÜMÜ</b></p>"
                      "<p><a href='20261024-1.htm'>ilan</a></p></body></html>"))
        result = make_pipeline(settings, fake_chain(), now=NOW + timedelta(days=1)).run()

    assert result.status == "basarili", result.errors
    with session_scope() as s:
        changes = list(s.scalars(select(RegulatoryChange)))
        new = changes[first:]
        titles = {c.document.title for c in new}
        assert titles == {"Elektrik Depolama Tesislerine İlişkin Yönetmelik"}  # RSS'teki yeni kayıt
        # EPİAŞ duyurusu, 15000 sayılı karar RG'de zaten tespit edildiği için kopya sayıldı
        epias = s.scalar(select(RegulatoryDocument).where(RegulatoryDocument.title.like("Gün Öncesi Piyasası Azami%")))
        assert epias.relevance == "kopya"
        ext = [d.external_id for d in s.scalars(select(RegulatoryDocument))]
        assert len(ext) == len(set(ext))
    mails = _reports(smtp_server)
    assert len(mails) == 1 and "2026-10-24" in mails[0]["msg"]["Subject"]
    html = _html(mails[0])
    assert "Elektrik Depolama Tesislerine" in html and DUY not in html


def test_eksik_gun_telafisi_ve_uyari(settings, smtp_server):
    with session_scope() as s:
        from enerji_takip.sources.registry import sync_sources
        sync_sources(s, settings.sources_file)
        s.scalar(select(RegulatorySource).where(RegulatorySource.key == "resmi_gazete")).cursor = "2026-10-20"
    with respx.mock(assert_all_called=False) as router:
        register_all(router, fail_dates={"20261022": 503})
        result = make_pipeline(settings, fake_chain()).run()
    assert not result.complete and result.status == "kismi"
    with session_scope() as s:
        assert s.scalar(select(RegulatorySource).where(RegulatorySource.key == "resmi_gazete")).cursor == "2026-10-21"
    assert _reports(smtp_server)[0]["msg"]["Subject"].endswith("| EKSİK TARAMA")
    assert _alerts(smtp_server)

    smtp_server.messages.clear()
    with respx.mock(assert_all_called=False) as router:
        register_all(router)
        assert make_pipeline(settings, fake_chain()).run().complete
    with session_scope() as s:
        assert s.scalar(select(RegulatorySource).where(RegulatorySource.key == "resmi_gazete")).cursor == "2026-10-23"


def test_kaynak_erisilemezse_bos_rapor_gonderilmez(settings, smtp_server):
    settings.notify_recipients_on_failure = False
    with respx.mock(assert_all_called=False) as router:
        router.get("https://www.resmigazete.gov.tr/robots.txt").mock(return_value=httpx.Response(404))
        router.get(url__regex=r"https://www\.resmigazete\.gov\.tr/eskiler/.*").mock(side_effect=httpx.ConnectError("x"))
        router.get(url__regex=r"https://www\.epias\.com\.tr/.*").mock(return_value=httpx.Response(503))
        router.get(url__regex=r"https://enerji\.gov\.tr/.*").mock(return_value=httpx.Response(503))
        result = make_pipeline(settings, fake_chain()).run()
    assert not result.complete and result.report_status is None
    assert not _reports(smtp_server) and len(_alerts(smtp_server)) == 1


def test_robots_engeline_uyulur(settings, smtp_server):
    with respx.mock(assert_all_called=False) as router:
        register_all(router, robots="User-agent: *\nDisallow: /eskiler/\n")
        result = make_pipeline(settings, fake_chain()).run()
        assert not [c for c in router.calls if "/eskiler/" in str(c.request.url)]
    assert not result.complete
    with session_scope() as s:
        assert s.scalar(select(RegulatorySource).where(RegulatorySource.key == "resmi_gazete")).access_status == "robots_engeli"


def test_bir_kaynak_hatasi_digerlerini_engellemez(settings, smtp_server):
    with respx.mock(assert_all_called=False) as router:
        register_rg_only = __import__("tests.conftest", fromlist=["x"])
        register_rg_only.register_rg_routes(router)
        register_rg_only.register_epias_routes(router, status=500)
        register_rg_only.register_etkb_routes(router)
        result = make_pipeline(settings, fake_chain()).run()
    assert not result.complete
    with session_scope() as s:
        assert s.query(RegulatoryChange).count() >= 6
        epias = s.scalar(select(RegulatorySource).where(RegulatorySource.key == "epias_elektrik_duyurulari"))
        assert epias.access_status == "erisilemiyor"


def test_eposta_gonderilemezse_degisiklikler_kaybolmaz(settings, smtp_server):
    settings.smtp_port = 1
    with respx.mock(assert_all_called=False) as router:
        register_all(router)
        assert make_pipeline(settings, fake_chain()).run().report_status == "basarisiz"
    with session_scope() as s:
        assert s.query(EmailDeliveryLog).filter(EmailDeliveryLog.kind == "rapor").count() == settings.email_max_attempts
    settings.smtp_port = smtp_server.port
    with respx.mock(assert_all_called=False) as router:
        register_all(router)
        assert make_pipeline(settings, fake_chain()).run().report_status == "gonderildi"
    assert DUY in _html(_reports(smtp_server)[0])


def test_eszamanli_calistirma_engellenir(settings):
    with session_scope() as s:
        s.add(ScanRun(run_type="gunluk", status="calisiyor"))
    assert make_pipeline(settings, fake_chain()).run().status == "atlandi"


def test_ai_olmadan_kural_tabanli_rapor(settings, smtp_server):
    with respx.mock(assert_all_called=False) as router:
        register_all(router)
        result = make_pipeline(settings, None).run()
    assert result.report_status == "gonderildi"
    html = _html(_reports(smtp_server)[0])
    assert "Yapay zekâ özeti oluşturulamadı" in html and "AI kapalı" in html
    with session_scope() as s:
        duy = _changes(s)[DUY]
        assert duy.status == "inceleme" and duy.importance in ("YUKSEK", "ORTA")


def test_elenen_belge_listesi_kapatilabilir(settings, smtp_server):
    settings.report_excluded_max = 0
    with respx.mock(assert_all_called=False) as router:
        register_all(router)
        make_pipeline(settings, fake_chain()).run()
    mails = _reports(smtp_server)
    assert mails and "Kontrol için: elenen belgeler" not in _html(mails[0])


def test_analiz_disi_ama_ilgili_gorunen_belgeler_ayri_listelenir(settings, smtp_server):
    """İlan bölümünde olup başlığı enerji konusuyla eşleşen belge, rapor kontrol listesinde öne çıkarılır."""
    from datetime import datetime, timedelta, timezone

    from enerji_takip.config import config_path
    from enerji_takip.db import init_db, session_scope
    from enerji_takip.detection.relevance import Taxonomy
    from enerji_takip.models import RegulatoryDocument, RegulatorySource
    from enerji_takip.reporting.builder import excluded_documents

    init_db()
    tax = Taxonomy.load(config_path("taxonomy.yaml"))
    with session_scope() as s:
        src = RegulatorySource(key="resmi_gazete", name="Resmî Gazete", url="https://x.test", category="resmi", method="html")
        s.add(src)
        s.flush()
        for i, (title, rel) in enumerate([
            ("Elektrik Üretim Tesisi Yapımı İhale İlanı", "atlandi"),
            ("Üniversite Yemek Hizmeti İhale İlanı", "atlandi"),
            ("Optisyenlik Yönetmeliği", "alakasiz"),
        ]):
            s.add(RegulatoryDocument(source_id=src.id, dedup_key=f"k{i}", external_id=str(i), title=title, url=f"https://x.test/{i}",
                                     doc_type="ILAN", relevance=rel, section="ILAN",
                                     first_seen_at=datetime.now(timezone.utc)))
    with session_scope() as s:
        out = excluded_documents(s, datetime.now(timezone.utc) - timedelta(hours=1), 50, tax)
    assert [d["title"] for d in out["skipped_relevant"]] == ["Elektrik Üretim Tesisi Yapımı İhale İlanı"]
    assert out["skipped_relevant"][0]["keywords"] and out["skipped_relevant"][0]["source"] == "Resmî Gazete"
    assert [d["title"] for d in out["skipped"]] == ["Üniversite Yemek Hizmeti İhale İlanı"]
    assert out["skipped_total"] == 1 and out["skipped_relevant_total"] == 1 and out["irrelevant_total"] == 1
