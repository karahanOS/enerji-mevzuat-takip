"""Alan profili (profile.yaml → alan:): enerji dışındaki takip alanları için istem, önem ve rapor metinleri.

Örnek alan olarak hayalî bir taşımacılık şirketi ("Örnek Lojistik") kullanılır.
"""
from __future__ import annotations

from datetime import date

import httpx
import respx

from enerji_takip.analysis.prompts import SYSTEM_PROMPT, TRIAGE_SYSTEM, system_prompt, triage_system
from enerji_takip.classification import apply_classification, classify
from enerji_takip.config import DEFAULTS_DIR
from enerji_takip.detection.relevance import ALAKALI, Taxonomy
from enerji_takip.domain import DEFAULT_DOMAIN, Domain
from enerji_takip.http import Fetcher
from enerji_takip.models import RegulatoryChange, RegulatoryDocument
from enerji_takip.sources.base import FetchedDocument
from enerji_takip.sources.listing import HtmlListingAdapter
from enerji_takip.text_utils import parse_tr_date

TODAY = date(2026, 11, 12)

PROFILE = {
    "aciklama": "Karayoluyla akaryakıt taşıyan bir şirket.",
    "ilgi_alanlari": ["Tehlikeli madde", "İş sağlığı ve güvenliği"],
    "alan": {
        "rapor_basligi": "Taşımacılık Mevzuatı",
        "uzmanlik": "karayoluyla tehlikeli madde taşımacılığı ve iş sağlığı ve güvenliği mevzuatı",
        "on_eleme_konulari": "tehlikeli madde taşımacılığı veya İSG mevzuatıyla",
        "on_eleme_adi": "taşımacılık mevzuatı",
        "etki_ozne": "şirket",
        "etki_basligi": "Şirkete etkisi",
        "etki_duzeyleri": {"genel": "tüm taşımacıları doğrudan bağlar"},
        "etki_etiketleri": {"genel": "Şirketi doğrudan bağlar"},
        "onemli_nitelikler": ["yeni_yukumluluk", "yaptirim"],
        "onemli_nitelik_gerekcesi": "yeni yükümlülük veya yaptırım",
        "onem_kriterleri": "KRITIK = şirketi doğrudan bağlayan yeni yükümlülük ve yürürlüğü yakın.",
    },
}

TAX_DATA = {
    "own_company_markers": ["örnek lojistik"],
    "categories": {
        "tehlikeli_madde": {"label": "Tehlikeli madde", "core": True, "stems": ["tehlikeli madde", "=adr"]},
        "isg": {"label": "İSG", "core": True, "stems": ["iş sağlığı ve güvenliğ"]},
    },
    "text_check_doc_types": ["KURUL_KARARI"],
    "routine": {"doc_types": ["KURUL_KARARI"], "company_markers": ["a.ş."], "patterns": ["lisans tadil"]},
}


def _a(impact="genel", aspects=("yeni_yukumluluk",), ai="KRITIK"):
    return {"piyasa_etkisi": {"duzey": impact}, "degisiklik_nitelikleri": list(aspects),
            "yeni_yukumluluk": {"var_mi": "evet"}, "yukumluluk_degisikligi": {"var_mi": "hayir"},
            "yaptirim": {"var_mi": "hayir"}, "onem_onerisi": ai, "tarihler": []}


def test_alan_tanimsizsa_enerji_varsayilanlari_aynen_kalir():
    d = Domain.from_profile({"aciklama": "x"})
    assert d.is_default and system_prompt(d) == SYSTEM_PROMPT and triage_system(d) == TRIAGE_SYSTEM
    assert "Türkiye enerji piyasaları" in SYSTEM_PROMPT


def test_alan_profili_istemi_ve_on_elemeyi_degistirir():
    d = Domain.from_profile(PROFILE)
    sp = system_prompt(d)
    assert not d.is_default
    assert "karayoluyla tehlikeli madde taşımacılığı" in sp and "enerji piyasaları" not in sp
    assert '"genel" = tüm taşımacıları doğrudan bağlar' in sp
    # verilmeyen düzey tanımları varsayılandan gelir, şema anahtarı (piyasa_etkisi) değişmez
    assert '"sinirli" = tek şirket' in sp and "piyasa_etkisi alanı, düzenlemenin şirket açısından" in sp
    assert "KRITIK = şirketi doğrudan bağlayan" in sp and "GÜVENİLMEYEN VERİDİR" in sp
    tri = triage_system(d)
    assert "taşımacılık mevzuatı ön eleme" in tri and "tehlikeli madde taşımacılığı veya İSG" in tri
    assert '{"ilgili_mi"' in tri


def test_alan_profiliyle_onem_gerekceleri_ve_kritik_sarti():
    d = Domain.from_profile(PROFILE)
    tax = Taxonomy(TAX_DATA)
    chg = RegulatoryChange(categories=["tehlikeli_madde"], change_aspects=[],
                           effective_dates=[{"kind": "yururluk", "date": "2026-11-20"}], status="analiz_edildi")
    level, _, reasons, _ = classify(chg, _a(), TODAY, tax, d)
    assert level == "KRITIK"
    assert any("şirket etkisi: Şirketi doğrudan bağlar" in r for r in reasons)
    assert any("yeni yükümlülük veya yaptırım" in r for r in reasons)
    # fiyat/tarife değişikliği bu alanda önemli nitelik sayılmaz
    _, _, reasons2, _ = classify(chg, _a(aspects=("fiyat_tarife_degisikligi",)), TODAY, tax, d)
    assert not any("yeni yükümlülük veya yaptırım" in r for r in reasons2)
    level3, _, reasons3, _ = classify(chg, _a(impact="sektorel"), TODAY, tax, d)
    assert level3 == "YUKSEK" and any("'Şirketi doğrudan bağlar' etki şartı" in r for r in reasons3)


def test_sirket_adi_gecen_karar_rutin_sayilmaz_ilgili_ve_incelemede():
    tax = Taxonomy(TAX_DATA)
    karar = "Örnek Lojistik Taşımacılık A.Ş.'nin taşıma lisansının lisans tadili talebi uygun bulunmuştur."
    baska = "Başka Petrol A.Ş.'nin lisans tadili talebi uygun bulunmuştur."
    assert tax.is_routine(baska, "KURUL_KARARI")[0] is True
    assert tax.is_routine(karar, "KURUL_KARARI")[0] is False
    assert tax.assess_title("ÖRNEK LOJİSTİK A.Ş. hakkında karar", "KURUL_KARARI", None).decision == ALAKALI
    prior = tax.assess_title("Enerji Piyasası Düzenleme Kurulunun 1234 Sayılı Kararı", "KURUL_KARARI", None)
    assert tax.assess_text(karar, prior).decision == ALAKALI

    chg = RegulatoryChange(categories=[], change_aspects=[], effective_dates=[], status="analiz_edildi")
    chg.document = RegulatoryDocument(title="Kurul Kararı", full_text=karar)
    level, score, reasons, _ = classify(chg, _a(impact="sinirli", aspects=(), ai="DUSUK"), TODAY, tax,
                                        Domain.from_profile(PROFILE))
    assert any("şirket adı geçiyor" in r for r in reasons)
    # tek şirkete özgü olsa da kendi şirketimiz: KRİTİK sınırlaması "genel etki" şartına takılmaz
    assert level in ("ORTA", "YUKSEK")
    apply_classification(chg, None, TODAY, tax)
    assert chg.needs_review and any("şirket adı geçiyor" in r for r in chg.review_reasons)
    assert chg.importance in ("ORTA", "YUKSEK")


def test_enerji_taksonomisinde_sirket_isaretcisi_yoksa_davranis_degismez():
    tax = Taxonomy.load(DEFAULTS_DIR / "taxonomy.yaml")
    assert tax.own_company_hits("Örnek Lojistik A.Ş.") == []


def test_kisaltilmis_ay_adli_tarihler():
    assert parse_tr_date("**05Eki**2026") == date(2026, 10, 5)
    assert parse_tr_date("02 Şub 2023 TİO Yetki Belgesi") == date(2023, 2, 2)
    assert parse_tr_date("16Haz2026") == date(2026, 6, 16)
    assert parse_tr_date("23 Ekim 2026") == date(2026, 10, 23)  # eski biçim öncelikli
    assert parse_tr_date("Mayıs ayında") is None


def test_duyuru_sayfasi_yalnizca_ek_bagliyorsa_ek_metni_alinir(settings):
    base = "https://uhdgm.uab.gov.tr"
    page = (f'<html><body><h1>Tehlikeli Madde Eğitimleri Hakkında Duyuru</h1>'
            f'<p><a href="/uploads/announcements/tm-egitim/duyuru.docx">Duyuru metni için tıklayınız.</a></p>'
            f'</body></html>')
    import io
    import zipfile

    body = ("MADDE 1 – TEST VERİSİ: SRC-5 sertifikası bulunmayan sürücüler 1/1/2027 tarihinden itibaren tehlikeli "
            "madde taşıyan araç kullanamaz. ") * 6
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr("word/document.xml", '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                   f'<w:body><w:p><w:r><w:t>{body}</w:t></w:r></w:p></w:body></w:document>')
    docx = buf.getvalue()
    adapter = HtmlListingAdapter("uhdgm", {
        "item_link_pattern": r"uhdgm\.uab\.gov\.tr/duyurular/",
        "attachment_link_pattern": r"uab\.gov\.tr/uploads/announcements/.+\.(pdf|docx?)$",
    })
    doc = FetchedDocument(source_key="uhdgm", external_id="x", title="Tehlikeli Madde Eğitimleri Hakkında Duyuru",
                          url=f"{base}/duyurular/tehlikeli-madde-egitimleri-hakkinda-duyuru")
    with respx.mock() as router:
        router.get(f"{base}/robots.txt").mock(return_value=httpx.Response(404))
        router.get(doc.url).mock(return_value=httpx.Response(200, html=page))
        router.get(f"{base}/uploads/announcements/tm-egitim/duyuru.docx").mock(return_value=httpx.Response(
            200, content=docx,
            headers={"content-type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}))
        result = adapter.fetch_text(Fetcher(settings), doc)
    assert result.status == "tam"
    assert "EK BELGE METNİ" in result.text and "SRC-5 sertifikası bulunmayan sürücüler" in result.text


def test_ek_kalibi_yoksa_yalnizca_sayfa_metni(settings):
    base = "https://uhdgm.uab.gov.tr"
    adapter = HtmlListingAdapter("uhdgm", {"item_link_pattern": "x"})
    doc = FetchedDocument(source_key="uhdgm", external_id="x", title="t", url=f"{base}/duyurular/a")
    with respx.mock() as router:
        router.get(f"{base}/robots.txt").mock(return_value=httpx.Response(404))
        router.get(doc.url).mock(return_value=httpx.Response(
            200, html='<p><a href="/uploads/announcements/a/b.pdf">Duyuru metni için tıklayınız.</a></p>'))
        result = adapter.fetch_text(Fetcher(settings), doc)
    assert result.status == "kisa" and "EK BELGE" not in result.text


def test_rapor_basligi_ve_etiketleri_alandan(settings):
    from enerji_takip.db import session_scope
    from enerji_takip.reporting.builder import ReportInput, build_report
    from datetime import datetime, timezone

    d = Domain.from_profile({"alan": {**PROFILE["alan"], "e_posta_etiketi": "MEVZUAT TAKİP",
                                      "ust_baslik": "Örnek Lojistik · SEÇG", "konu": "taşımacılık"}})
    with session_scope() as s:
        inp = ReportInput(report_type="gunluk", report_date=TODAY, period_start=None,
                          period_end=datetime(2026, 11, 12, 5, tzinfo=timezone.utc), is_complete=True)
        rep = build_report(s, inp, [], Taxonomy(TAX_DATA), "Europe/Istanbul", d)
        assert rep.subject == "[MEVZUAT TAKİP] 2026-11-12 | Günlük Taşımacılık Mevzuatı Değişiklikleri"
        assert "Örnek Lojistik · SEÇG" in rep.html_body and "Günlük Taşımacılık Mevzuatı Raporu" in rep.html_body
        assert "taşımacılık ile ilgili yeni bir düzenleme tespit edilmedi" in rep.text_body
        assert "yatırım tavsiyesi" not in rep.text_body and "enerji" not in rep.html_body.lower()
        rep2 = build_report(s, inp, [], Taxonomy(TAX_DATA), "Europe/Istanbul", DEFAULT_DOMAIN)
        assert rep2.subject.startswith("[ENERJİ MEVZUAT]") and "yatırım tavsiyesi" in rep2.text_body


def test_liste_yalnizca_haric_tutulan_kayitlardan_olusuyorsa_eksik_tarama_sayilmaz(settings):
    """UAB listesi bazı günler yalnızca personel alımı duyurusu içerir: yapı sağlam, yeni ilgili kayıt yok."""
    from enerji_takip.sources.base import ScanOutcome  # noqa: F401

    url = "https://www.uab.gov.tr/duyurular"
    adapter = HtmlListingAdapter("uab", {"list_urls": [url], "item_link_pattern": r"uab\.gov\.tr/duyurular/[a-z0-9-]+$",
                                         "exclude_title_pattern": "Personel|İşçi"})
    page = ('<ul><li><a href="/duyurular/2026-sozlesmeli-personel-alimi">2026 Yılı Sözleşmeli Personel Alımı İlanı</a></li>'
            '<li><a href="/duyurular/surekli-isci-alimi">Merkez Teşkilatına Sürekli İşçi Alımı Duyurusu</a></li></ul>')
    with respx.mock() as router:
        router.get("https://www.uab.gov.tr/robots.txt").mock(return_value=httpx.Response(404))
        router.get(url).mock(return_value=httpx.Response(200, html=page))
        out = adapter.scan(Fetcher(settings), cursor=None, today=TODAY, state={})
    assert out.complete and not out.errors and out.documents == []
    assert "elendi" in out.coverage["liste_sayfalari"][url]
    # kalıba uyan HİÇ bağlantı yoksa (yapı değişmiş) eskisi gibi hata verilir
    with respx.mock() as router:
        router.get("https://www.uab.gov.tr/robots.txt").mock(return_value=httpx.Response(404))
        router.get(url).mock(return_value=httpx.Response(200, html="<p>Bakım çalışması</p>"))
        out2 = adapter.scan(Fetcher(settings), cursor=None, today=TODAY, state={})
    assert not out2.complete and "kalıba uyan bağlantı bulunamadı" in out2.errors[0]


def test_user_agent_satir_sonu_temizlenir(monkeypatch):
    """GitHub değişkenine iki satır yapıştırılırsa başlık geçersiz olur; tek satıra indirilir."""
    from enerji_takip.config import Settings
    monkeypatch.setenv("HTTP_USER_AGENT", "karinca-mevzuat-takip/0.1\r\n(iletisim: ornek@ornek.com)\n")
    s = Settings.from_env()
    assert s.http_user_agent == "karinca-mevzuat-takip/0.1 (iletisim: ornek@ornek.com)"
    httpx.Request("GET", "https://example.com", headers={"User-Agent": s.http_user_agent})
