"""ETKB duyuruları: birleşen başlıklar, sondaki tarih, detay sayfasından başlık ve aynı kaynakta yanlış kopya."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import respx
import yaml

from enerji_takip.config import Settings
from enerji_takip.db import init_db, session_scope
from enerji_takip.detection.engine import find_cross_source_duplicate
from enerji_takip.http import Fetcher
from enerji_takip.models import RegulatoryChange, RegulatoryDocument, RegulatorySource
from enerji_takip.sources.listing import HtmlListingAdapter

ROOT = Path(__file__).resolve().parents[1]
CFG = next(s for s in yaml.safe_load((ROOT / "enerji_takip" / "varsayilan" / "sources.yaml").read_text(encoding="utf-8"))
           ["sources"] if s["key"] == "etkb_duyurular")["config"]
LIST = "https://enerji.gov.tr/medya-duyurular-liste"
D = "https://enerji.gov.tr/duyuru-detay?id="

# TEST VERİSİ — 25.11.2026 sonrası yapı: bağlantıların metni boş, kayıtlar tek bir blokta art arda
FLAT = f"""<html><body><div class="duyurular">
<span>2026 Yılı ETKB-TÜBİTAK Lise Öğrencileri Enerji Verimliliği Proje Yarışması Başvurusu</span><span>25.11.2026</span>
<a href="duyuru-detay?id=30680"><i class="ikon"></i></a>
<span>Güneş Enerjisine Dayalı Aday YEKA İlanı</span><span>25.11.2026</span>
<a href="duyuru-detay?id=30679"><i class="ikon"></i></a>
<span>Mardin ili, Midyat ilçesi … G25 Mardin GES Projesine ilişkin 1/5.000 ölçekli Nazım İmar Planı</span><span>25.11.2026</span>
<a href="duyuru-detay?id=30678"><i class="ikon"></i></a>
</div></body></html>"""

# Eski yapı: her kayıt kendi bloğunda, bağlantı metni "Başlık tarih"
CARDS = f"""<html><body><ul>
<li><a href="duyuru-detay?id=30666">2026 Yılı Ekim Ayı Lisanssız Rüzgar Enerji Santrali Başvurularının Teknik Değerlendirme Sonuçları 05.11.2026</a></li>
<li><div><span>24.10.2026</span><a href="duyuru-detay?id=30663">Güneş Enerjisine Dayalı Aday YEKA İlanı 27.10.2026</a></div></li>
</ul></body></html>"""


def _detail(title: str, body: str) -> str:
    menu = "".join(f"<li>{x}</li>" for x in ("Enerji Verimliliği", "İklim Değişikliği ve Uluslararası Müzakereler",
                                              "Boru Hatları ve Projeleri", "Duyurular", "Anasayfa", "Medya"))
    return (f"<html><body><ul>{menu}<li>Duyurular</li></ul><h1>{title}</h1><p>Paylaş</p><p>{body}</p>"
            f"<p>Belge &amp; Dosyalar</p><footer>T.C. Enerji ve Tabii Kaynaklar Bakanlığı © Tüm Hakları Saklıdır.</footer>"
            f"</body></html>")


def _fetcher():
    s = Settings()
    s.http_min_interval_seconds = 0
    s.respect_robots_txt = False
    return Fetcher(s)


def test_birlesik_blokta_baslik_birlestirilmez_detaydan_alinir():
    adapter = HtmlListingAdapter("etkb_duyurular", CFG)
    docs = {d.url: d for d in adapter._parse(FLAT, LIST)}
    assert set(docs) == {D + "30680", D + "30679", D + "30678"}
    for d in docs.values():  # başka kayıtların başlıkları birbirine yapışmaz; başlık detay sayfasından gelecek
        assert d.needs_detail_title and d.title.endswith(f"duyurusu ({d.url})")
        assert d.published_date is None
    yeka = docs[D + "30679"]
    with respx.mock() as router:
        router.get(D + "30679").mock(return_value=httpx.Response(200, text=_detail(
            "Güneş Enerjisine Dayalı Aday YEKA İlanı",
            "Yenilenebilir Enerji Kaynak Alanları Yönetmeliği kapsamında aşağıda verilen alan Aday YEKA olarak belirlenmiştir. "
            "Kamuoyuna saygıyla duyurulur."), headers={"content-type": "text/html"}))
        text = adapter.fetch_text(_fetcher(), yeka).text
    assert yeka.title == "Güneş Enerjisine Dayalı Aday YEKA İlanı" and not yeka.needs_detail_title
    assert yeka.skip_reason is None
    assert text.startswith("Güneş Enerjisine Dayalı Aday YEKA İlanı") and "Kamuoyuna" in text
    for noise in ("İklim Değişikliği", "Boru Hatları", "Tüm Hakları Saklıdır"):
        assert noise not in text


def test_detaydan_gelen_baslik_haric_turdense_atlanir():
    adapter = HtmlListingAdapter("etkb_duyurular", CFG)
    imar = {d.url: d for d in adapter._parse(FLAT, LIST)}[D + "30678"]
    with respx.mock() as router:
        router.get(D + "30678").mock(return_value=httpx.Response(200, text=_detail(
            "Mardin ili, Midyat ilçesi, Gülgöze Mahallesi … G25 Mardin GES Projesine ilişkin Nazım İmar Planı",
            "Plan askıya çıkarılmıştır."), headers={"content-type": "text/html"}))
        adapter.fetch_text(_fetcher(), imar)
    assert imar.skip_reason and "İmar Planı" in imar.title


def test_sondaki_tarih_yayim_tarihidir_ve_basliktan_ayrilir():
    docs = {d.url: d for d in HtmlListingAdapter("etkb_duyurular", CFG)._parse(CARDS, LIST)}
    lis = docs[D + "30666"]
    assert lis.title == "2026 Yılı Ekim Ayı Lisanssız Rüzgar Enerji Santrali Başvurularının Teknik Değerlendirme Sonuçları"
    assert lis.published_date == date(2026, 11, 5) and not lis.needs_detail_title
    assert docs[D + "30663"].published_date == date(2026, 10, 27)  # blokta başka tarih olsa da başlıktaki esas


def test_ayni_kaynaktaki_farkli_duyuru_kopya_sayilmaz(settings):
    init_db()
    title = "2026 Yılı ETKB-TÜBİTAK Lise Öğrencileri Enerji Verimliliği Proje Yarışması Başvurusu ve diğerleri"
    with session_scope() as s:
        etkb = RegulatorySource(key="etkb_x", name="ETKB", url="u", category="c", method="html_listing")
        rg = RegulatorySource(key="rg_x", name="RG", url="u", category="c", method="rg_fihrist")
        s.add_all([etkb, rg])
        s.flush()
        first = RegulatoryDocument(source_id=etkb.id, dedup_key="a", external_id="1", title=title, url=D + "1")
        s.add(first)
        s.flush()
        s.add(RegulatoryChange(document_id=first.id, dedup_key="ca", change_type="DUYURU"))
        same_source = RegulatoryDocument(source_id=etkb.id, dedup_key="b", external_id="2", title=title, url=D + "2")
        other_source = RegulatoryDocument(source_id=rg.id, dedup_key="c", external_id="3", title=title, url="rg")
        s.add_all([same_source, other_source])
        s.flush()
        assert find_cross_source_duplicate(s, same_source) is None
        assert find_cross_source_duplicate(s, other_source) is not None


def test_motor_baslik_bekleyen_kaydi_detaydan_degerlendirir(settings):
    from enerji_takip.detection.engine import ChangeDetectionEngine
    from enerji_takip.detection.relevance import Taxonomy

    init_db()
    adapter = HtmlListingAdapter("etkb_duyurular", CFG)
    docs = adapter._parse(FLAT, LIST)
    pages = {
        "30680": _detail("2026 Yılı ETKB-TÜBİTAK Lise Öğrencileri Enerji Verimliliği Proje Yarışması Başvurusu",
                         "Lise öğrencileri için enerji verimliliği proje yarışması başvuruları 8 Ocak 2027'ye kadar."),
        "30679": _detail("Güneş Enerjisine Dayalı Aday YEKA İlanı",
                         "Yenilenebilir Enerji Kaynak Alanları Yönetmeliği kapsamında güneş enerjisine dayalı "
                         "yenilenebilir enerji kaynak alanı (YEKA) ilanı için çalışmalara başlanmıştır."),
        "30678": _detail("Mardin ili … G25 Mardin GES Projesine ilişkin 1/5.000 ölçekli Nazım İmar Planı", "Askı ilanı."),
    }
    with session_scope() as s, respx.mock() as router:
        for no, html in pages.items():
            router.get(D + no).mock(return_value=httpx.Response(200, text=html, headers={"content-type": "text/html"}))
        src = RegulatorySource(key="etkb_duyurular", name="ETKB", url=LIST, category="c", method="html_listing")
        s.add(src)
        s.flush()
        tax = Taxonomy.load(ROOT / "enerji_takip" / "varsayilan" / "taxonomy.yaml")
        ChangeDetectionEngine(s, _fetcher(), tax).process(src, adapter, docs)
        rows = {d.url[-5:]: d for d in s.query(RegulatoryDocument).all()}
    assert rows["30679"].title == "Güneş Enerjisine Dayalı Aday YEKA İlanı" and rows["30679"].relevance == "alakali"
    assert rows["30678"].relevance == "atlandi"
    assert all(r.relevance != "kopya" for r in rows.values())
