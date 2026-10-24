"""Fihrist ayrıştırıcı, ilgililik filtresi ve kural tabanlı çıkarım testleri."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from enerji_takip.detection.extract import (
    detect_change_type,
    extract_all,
    extract_amended_articles,
    extract_amended_regulation,
    extract_effective_dates,
    summarize_effective,
)
from enerji_takip.detection.relevance import ALAKALI, ALAKASIZ, ATLANDI, BELIRSIZ, Taxonomy
from enerji_takip.http import decode_html
from enerji_takip.sources.base import ParseError
from enerji_takip.sources.resmi_gazete import ResmiGazeteAdapter, classify_heading, parse_fihrist
from enerji_takip.config import DEFAULTS_DIR, Settings

FIX = Path(__file__).parent / "fixtures"
PUB = date(2026, 10, 23)
URL = "https://www.resmigazete.gov.tr/eskiler/2026/10/20261023.htm"


# ---- Fihrist ------------------------------------------------------------------
def test_fihrist_cp1254_bolumler_turler_ve_baglantilar():
    html = decode_html((FIX / "rg" / "20261023.htm").read_bytes(), None)
    docs, meta = parse_fihrist(html, URL, expected=PUB)
    assert meta["rg_number"] == "33999" and meta["rg_date"] == PUB
    by_id = {d.external_id: d for d in docs}
    assert len(docs) == 13
    assert by_id["20261023-1"].doc_type == "CB_KARARI" and by_id["20261023-1"].pdf_url.endswith("20261023-1.pdf")
    assert by_id["20261023-3"].doc_type == "YONETMELIK" and by_id["20261023-3"].section == "YURUTME_IDARE"
    assert by_id["20261023-7"].doc_type == "TEBLIG"
    assert by_id["20261023-8"].doc_type == "KURUL_KARARI" and by_id["20261023-10"].doc_type == "KURUL_KARARI"
    assert by_id["20261023-11"].doc_type == "AYM_KARARI" and by_id["20261023-11"].section == "YARGI"
    assert by_id["20261023-12-1"].section == "ILAN" and by_id["20261023-12-1"].skip_reason
    assert by_id["20261023-3"].url == "https://www.resmigazete.gov.tr/eskiler/2026/10/20261023-3.htm"
    assert "Uzlaştırma Yönetmeliğinde" in by_id["20261023-3"].title  # Türkçe karakterler doğru çözüldü


def test_fihrist_mukerrer_isaretlenir():
    html = (FIX / "rg" / "20261023M1.htm").read_text(encoding="utf-8")
    docs, _ = parse_fihrist(html, URL.replace(".htm", "M1.htm"), expected=PUB)
    assert len(docs) == 1 and docs[0].is_mukerrer and docs[0].external_id == "20261023M1-1"


def test_fihrist_yapi_degisirse_sessizce_bos_donmez():
    html = "<html><body>" + "<p>Yeni tasarım: içerik JavaScript ile yükleniyor.</p>" * 20 + "</body></html>"
    with pytest.raises(ParseError):
        parse_fihrist(html, URL, expected=PUB)


def test_baslik_siniflandirma():
    assert classify_heading("YÜRÜTME VE İDARE BÖLÜMÜ") == ("top", "YURUTME_IDARE")
    assert classify_heading("İLÂN BÖLÜMÜ") == ("top", "ILAN")
    assert classify_heading("YÖNETMELİKLER") == ("sub", "YONETMELIK")
    assert classify_heading("CUMHURBAŞKANI KARARLARI") == ("sub", "CB_KARARI")
    assert classify_heading("Bu bir cümledir ve başlık değildir.") is None


def test_tarama_tarihleri_bosluk_ve_yeniden_tarama(monkeypatch):
    s = Settings()
    s.rg_rescan_days, s.rg_max_backfill_days, s.rg_first_run_days = 2, 14, 1
    a = ResmiGazeteAdapter("resmi_gazete")
    new, rescan, skipped = a._dates_to_scan("2026-10-20", PUB, s)
    assert [d.day for d in new] == [21, 22, 23] and [d.day for d in rescan] == [19, 20] and not skipped
    new, rescan, skipped = a._dates_to_scan(None, PUB, s)
    assert new == [PUB] and rescan == []
    new, _, skipped = a._dates_to_scan("2026-09-01", PUB, s)
    assert len(new) == 14 and skipped[0] == date(2026, 9, 2)


# ---- İlgililik ----------------------------------------------------------------
@pytest.fixture(scope="module")
def tax():
    return Taxonomy.load(DEFAULTS_DIR / "taxonomy.yaml")


@pytest.mark.parametrize("title,doc_type,expected", [
    ("Elektrik Piyasası Dengeleme ve Uzlaştırma Yönetmeliğinde Değişiklik Yapılmasına Dair Yönetmelik", "YONETMELIK", ALAKALI),
    ("Enerji Piyasası Düzenleme Kurulunun 16/10/2026 Tarihli ve 15000 Sayılı Kararı", "KURUL_KARARI", ALAKALI),
    ("Yenilenebilir Enerji Kaynaklarının Belgelendirilmesi ve Desteklenmesine İlişkin Yönetmelik", "YONETMELIK", ALAKALI),
    ("Doğal Gaz Piyasası Tarifeler Yönetmeliğinde Değişiklik Yapılmasına Dair Yönetmelik", "YONETMELIK", ALAKALI),
    ("Elektrik Depolama Tesislerine İlişkin Yönetmelik", "YONETMELIK", ALAKALI),
    ("Örnek Üniversitesi Lisansüstü Eğitim ve Öğretim Yönetmeliği", "YONETMELIK", ALAKASIZ),
    ("Karayolu Taşıma Yönetmeliğinde Değişiklik Yapılmasına Dair Yönetmelik", "YONETMELIK", ALAKASIZ),
    ("Bazı Taşınmazların Acele Kamulaştırılmasına İlişkin Karar", "CB_KARARI", ALAKASIZ),
    ("Anayasa Mahkemesinin 10/9/2026 Tarihli Kararı", "AYM_KARARI", BELIRSIZ),
    ("Sera Gazı Emisyonlarının Takibi Hakkında Yönetmelik", "YONETMELIK", ALAKALI),
])
def test_baslik_ilgililigi(tax, title, doc_type, expected):
    assert tax.assess_title(title, doc_type, "YURUTME_IDARE").decision == expected


def test_kisa_kokler_tam_kelime_eslesir(tax):
    assert "ptf" in tax.match("PTF ve SMF arasındaki fark").get("elektrik_piyasasi", set())
    assert not tax.match("Adres ve kptfx kayıtları").get("elektrik_piyasasi")
    assert tax.match("dengelemenin ve uzlaştırmaya ilişkin")["elektrik_piyasasi"]  # kök + ek


def test_rutin_karar_ayrimi(tax):
    rutin = "Kurulun toplantısında; Örnek Rüzgar Enerji A.Ş.’nin üretim lisansının tadil edilmesine karar verilmiştir."
    genel = "Kurulun toplantısında; tüm piyasa katılımcıları için azami uzlaştırma fiyat limiti belirlenmiştir. Örnek A.Ş."
    uzun = rutin + " x" * 4000
    assert tax.is_routine(rutin, "KURUL_KARARI")[0]
    assert not tax.is_routine(genel, "KURUL_KARARI")[0]            # piyasa geneli göstergesi var
    assert not tax.is_routine(rutin, "YONETMELIK")[0]              # yalnızca kurul kararları
    assert not tax.is_routine(uzun, "KURUL_KARARI")[0]             # uzun metin rutin sayılmaz
    assert not tax.is_routine("Lisans tadil edilmesine karar verilmiştir.", "KURUL_KARARI")[0]  # şirket yok


def test_rss_ayristirma_ve_xml_saldirisi():
    from enerji_takip.sources.base import ParseError
    from enerji_takip.sources.rss import parse_feed

    items = parse_feed((FIX / "listing" / "etkb_rss_v2.xml").read_text(encoding="utf-8"))
    assert [i["title"] for i in items][:1] == ["Elektrik Depolama Tesislerine İlişkin Yönetmelik"]
    assert items[1]["link"].endswith("MevzuatNo=18646&MevzuatTur=7&MevzuatTertip=5")
    bomb = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aa"><!ENTITY b "&a;&a;">]><rss><item><title>&b;</title><link>l</link></item></rss>'
    with pytest.raises(ParseError):
        parse_feed(bomb)


def test_karar_numarasi_eslesmesi():
    from enerji_takip.detection.engine import decision_numbers

    assert decision_numbers("Enerji Piyasası Düzenleme Kurulunun 16/10/2026 Tarihli ve 15000 Sayılı Kararı") == {"15000"}
    assert decision_numbers("15000-3 sayılı Kurul Kararı ile") == {"15000"}
    assert decision_numbers("6446 sayılı Kanun") == set()


def test_ilan_bolumu_atlanir(tax):
    assert tax.assess_title("İhale ilânları", "ILAN", "ILAN", skip_reason="İlan bölümü").decision == ATLANDI


def test_metin_kontrolu(tax):
    prior = tax.assess_title("Anayasa Mahkemesinin Kararı", "AYM_KARARI", None)
    alakasiz = "Belediye gelirleri hakkındaki itirazın reddine karar verildi."
    enerji = "6446 sayılı Elektrik Piyasası Kanunu uyarınca dağıtım şirketlerinin tarife tabloları ve dengeleme ..."
    assert tax.assess_text(alakasiz, prior).decision == ALAKASIZ
    assert tax.assess_text(enerji, prior).decision == ALAKALI


# ---- Yürürlük tarihi ----------------------------------------------------------
@pytest.mark.parametrize("sentence,expected", [
    ("MADDE 9 – (1) Bu Yönetmelik yayımı tarihinde yürürlüğe girer.", [("Tüm hükümler", "2026-10-23", "kesin")]),
    ("MADDE 9 – (1) Bu Yönetmelik 1/1/2027 tarihinde yürürlüğe girer.", [("Tüm hükümler", "2027-01-01", "kesin")]),
    ("MADDE 9 – (1) Bu Tebliğ 1 Ocak 2027 tarihinde yürürlüğe girer.", [("Tüm hükümler", "2027-01-01", "kesin")]),
    ("MADDE 9 – (1) Bu Yönetmelik yayımı tarihinden itibaren altı ay sonra yürürlüğe girer.",
     [("Tüm hükümler", "2027-04-23", "hesaplanan")]),
    ("MADDE 9 – (1) Bu Yönetmelik yayımı tarihinden itibaren doksan gün sonra yürürlüğe girer.",
     [("Tüm hükümler", "2027-01-21", "hesaplanan")]),
    ("MADDE 9 – (1) Bu Tebliğ yayımını izleyen ayın başında yürürlüğe girer.", [("Tüm hükümler", "2026-11-01", "hesaplanan")]),
    ("MADDE 9 – (1) Bu Yönetmeliğin; a) 7 nci maddesi 1/1/2027 tarihinde, b) diğer hükümleri yayımı tarihinde, yürürlüğe girer.",
     [("Madde 7", "2027-01-01", "kesin"), ("Diğer hükümler", "2026-10-23", "kesin")]),
    ("MADDE 9 – (1) Bu Kanunun 3 üncü maddesi yayımı tarihinden itibaren bir yıl sonra, diğer maddeleri yayımı tarihinde yürürlüğe girer.",
     [("Madde 3", "2027-10-23", "hesaplanan"), ("Diğer hükümler", "2026-10-23", "kesin")]),
    ("MADDE 9 – (1) Bu Yönetmelik Bakanlıkça belirlenecek tarihte yürürlüğe girer.", [("Tüm hükümler", None, "belirsiz")]),
])
def test_yururluk_tarihi_cikarimi(sentence, expected):
    got = [(e.provision, e.date, e.basis) for e in extract_effective_dates(sentence, PUB) if e.kind == "yururluk"]
    assert got == expected


def test_yururluk_tarihi_tahmin_edilmez():
    items = [e.as_dict() for e in extract_effective_dates("MADDE 1 – Metinde yürürlük hükmü yok.", PUB)]
    assert summarize_effective(items) == (None, "belirsiz")


def test_uyum_suresi_kesin_ve_hesaplanan():
    text = ("MADDE 4 – (1) Aynı Yönetmeliğe aşağıdaki geçici madde eklenmiştir.\n"
            "“GEÇİCİ MADDE 9 – (1) Taşımacılar bu maddenin yürürlüğe girdiği tarihten itibaren altı ay içinde uyum sağlar.”\n"
            "MADDE 5 – (1) Bu Yönetmelik yayımı tarihinde yürürlüğe girer.")
    deadlines = [e for e in extract_effective_dates(text, PUB) if e.kind == "uyum_suresi"]
    assert [(d.provision, d.date, d.basis) for d in deadlines] == [("Geçici Madde 9", "2027-04-23", "hesaplanan")]


def test_farkli_yururluk_tarihlerinde_sure_baslangici_tahmin_edilmez():
    text = ("MADDE 4 – (1) Aynı Yönetmeliğe aşağıdaki geçici madde eklenmiştir.\n"
            "“GEÇİCİ MADDE 9 – (1) Taşımacılar bu maddenin yürürlüğe girdiği tarihten itibaren altı ay içinde uyum sağlar.”\n"
            "MADDE 5 – (1) Bu Yönetmeliğin; a) 2 nci maddesi 1/1/2027 tarihinde, b) diğer hükümleri yayımı tarihinde, yürürlüğe girer.")
    deadlines = [e for e in extract_effective_dates(text, PUB) if e.kind == "uyum_suresi"]
    assert deadlines[0].date is None and deadlines[0].basis == "belirsiz"


# ---- Değişiklik türü / değiştirilen mevzuat ----------------------------------
@pytest.mark.parametrize("title,ctype,target", [
    ("Elektrik Piyasası Lisans Yönetmeliğinde Değişiklik Yapılmasına Dair Yönetmelik", "DEGISIKLIK",
     "Elektrik Piyasası Lisans Yönetmeliği"),
    ("Elektrik Piyasasında Dağıtım Bedellerinin Belirlenmesine İlişkin Tebliğde Değişiklik Yapılmasına Dair Tebliğ",
     "DEGISIKLIK", "Elektrik Piyasasında Dağıtım Bedellerinin Belirlenmesine İlişkin Tebliğ"),
    ("Elektrik Piyasası Dengeleme ve Uzlaştırma Yönetmeliğinde Değişiklik Yapılmasına Dair Yönetmelik", "DEGISIKLIK",
     "Elektrik Piyasası Dengeleme ve Uzlaştırma Yönetmeliği"),
    ("Vergi Usul Kanunu Genel Tebliği (Sıra No: 509)'nde Değişiklik Yapılmasına Dair Tebliğ (Sıra No: 600)", "DEGISIKLIK",
     "Vergi Usul Kanunu Genel Tebliği (Sıra No: 509)"),
    ("Elektrik Piyasası Kapasite Mekanizması Yönetmeliğinin Yürürlükten Kaldırılmasına Dair Yönetmelik",
     "YURURLUKTEN_KALDIRMA", "Elektrik Piyasası Kapasite Mekanizması Yönetmeliği"),
    ("Elektrik Depolama Tesislerine İlişkin Yönetmelik", "YENI_YONETMELIK", None),
])
def test_degisiklik_turu_ve_hedef(title, ctype, target):
    assert detect_change_type(title, "YONETMELIK") == ctype
    assert extract_amended_regulation(title)[0] == target


def test_degisen_maddeler():
    html = (FIX / "rg" / "20261023-3.htm").read_text(encoding="utf-8")
    from enerji_takip.text_utils import html_to_text

    arts = extract_amended_articles(html_to_text(html))
    assert {"madde": "Madde 4", "islem": "değiştirildi"} in arts
    assert {"madde": "Madde 110", "islem": "eklendi"} in arts
    assert {"madde": "Geçici Madde 15", "islem": "yürürlükten kaldırıldı"} in arts
    ext = extract_all("X Yönetmeliğinde Değişiklik Yapılmasına Dair Yönetmelik", "YONETMELIK", html_to_text(html), PUB)
    assert "yururlukten_kaldirma" in ext.aspects and "madde_eklenmesi" in ext.aspects


def test_rss_baglanti_yoksa_guid_veya_goreli_yol_kullanilir_ve_tani_verilir():
    from enerji_takip.sources.rss import describe_feed, parse_feed

    xml = ('<rss version="2.0"><channel>'
           '<item><title>A projesi ÇED</title><guid>https://enerji.gov.tr/duyuru/1</guid></item>'
           '<item><title>B</title><guid isPermaLink="false">kimlik-2</guid><link>/Media/b.docx</link></item>'
           '<item><title>C</title><enclosure url="https://enerji.gov.tr/Media/c.pdf"/></item>'
           '</channel></rss>')
    got = parse_feed(xml, base_url="https://enerji.gov.tr/RSS/15?t=1")
    assert [e["link"] for e in got] == ["https://enerji.gov.tr/duyuru/1", "https://enerji.gov.tr/Media/b.docx",
                                         "https://enerji.gov.tr/Media/c.pdf"]
    bad = '<rss><channel><item><title>X</title><guid>kimlik</guid></item></channel></rss>'
    assert parse_feed(bad) == []
    assert "1 kayıt var ama başlık+bağlantı yok" in describe_feed(bad) and "guid='kimlik'" in describe_feed(bad)
    assert describe_feed("<rss><channel/></rss>") == "akışta <item>/<entry> yok"


def test_ilan_bolumu_baglantilari_gercek_belgeyle_karismaz():
    """Gerçek sayfada ilan alt başlıkları main.aspx?home=…/ilanlar/…-N.htm adresine gider; bu, N numaralı
    gerçek belgenin başlığına ve adresine karışıyordu (24.10.2026 canlı veriden)."""
    html = ('<html><body><p>21 Ekim 2026</p><p>Resmî Gazete</p><p>Sayı : 33407</p>'
            '<p><b>YÖNETMELİK</b></p>'
            '<p><a href="20261021-3.htm">Elektrik Piyasası Dengeleme ve Uzlaştırma Yönetmeliğinde Değişiklik '
            'Yapılmasına Dair Yönetmelik</a></p>'
            '<p><b>İLÂN BÖLÜMÜ</b></p><p>b - Artırma, Eksiltme ve İhale İlânları</p>'
            '<p><a href="http://www.resmigazete.gov.tr/main.aspx?home=http://www.resmigazete.gov.tr/ilanlar/eskiilanlar/'
            '2026/10/20261021.htm&amp;main=http://www.resmigazete.gov.tr/ilanlar/eskiilanlar/2026/10/20261021-3.htm">'
            'b - Artırma, Eksiltme ve İhale İlânları</a></p></body></html>')
    docs, _ = parse_fihrist(html, "https://www.resmigazete.gov.tr/eskiler/2026/10/20261021.htm", expected=date(2026, 10, 21))
    by_id = {d.external_id: d for d in docs}
    real = by_id["20261021-3"]
    assert real.title.endswith("Yönetmelik") and "İlânları" not in real.title
    assert real.url == "https://www.resmigazete.gov.tr/eskiler/2026/10/20261021-3.htm" and not real.skip_reason
    ilan = by_id["ilan-20261021-3"]
    assert ilan.skip_reason == "İlan bölümü" and ilan.doc_type == "ILAN"


def test_bozuk_pdf_metni_okunamadi_sayilir_ve_aiya_gitmez():
    from enerji_takip.sources.base import looks_garbled

    garbage = "\x07\b\x11\b\x05\x03\x07\x06\x11\x06\x11\x04\n\x11\x10\x12\x04 \b\n\x12\x11\x10\x11\x0e\x12\x0f\x03\x06\x05" * 3
    assert looks_garbled(garbage)
    assert not looks_garbled("Enerji Piyasası Düzenleme Kurulunun 15/10/2026 Tarihli ve 14967 Sayılı Kararı " * 5)
    assert not looks_garbled("1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 kWh TL 1,5 2,5 3,5 4,5")


def test_ai_yokken_uzun_ve_seyrek_konulu_metin_dahil_edilmez():
    from enerji_takip.config import config_path
    from enerji_takip.detection.relevance import Taxonomy

    tax = Taxonomy.load(config_path("taxonomy.yaml"))
    sparse = ("Meslek mensuplarının sicil ve disiplin işlemleri hakkında genel hükümler. " * 1200) + " elektrik enerji lisans"
    dense = "Elektrik piyasası lisans sahibi üretim tesisi iletim sistemi dengeleme uzlaştırma. " * 80
    assert tax.density(sparse) < 0.3
    assert tax.density(dense) > 0.3
