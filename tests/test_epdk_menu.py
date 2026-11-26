"""EPDK duyuruları: menü/alt bilgi kırpma."""
from __future__ import annotations

import re
from pathlib import Path

import httpx
import respx
import yaml

from enerji_takip.config import Settings
from enerji_takip.detection.relevance import Taxonomy
from enerji_takip.http import Fetcher
from enerji_takip.sources.base import FetchedDocument
from enerji_takip.sources.listing import HtmlListingAdapter

ROOT = Path(__file__).resolve().parents[1]
CFG = next(s for s in yaml.safe_load((ROOT / "enerji_takip" / "varsayilan" / "sources.yaml").read_text(encoding="utf-8"))["sources"]
           if s["key"] == "epdk_duyurular")["config"]
URL = "https://www.epdk.gov.tr/Detay/Icerik/4-16488/opet-petrolculuk-anonim-sirketi-mersin-tesisi-dep"

# Gerçek sayfanın (25.11.2026) yapısı: uzun menü → başlık → duyuru metni → "Birimi :" → alt bilgi
MENU = "".join(f"<li><a href='#'>{x}</a></li>" for x in (
    "ELEKTRİK PİYASASI", "Lisanssız Üretim", "YEKDEM", "DOĞAL GAZ PİYASASI", "Spot Kapasite", "PETROL PİYASASI",
    "Akaryakıt İzleme Raporu", "LPG PİYASASI", "Dağıtıcılar Arası LPG Ticareti", "TÜKETİCİ KÖŞESİ",
    "AKARYAKIT FİYATLARI", "BASIN AÇIKLAMALARI"))
PAGE = f"""<html><body><nav><ul>{MENU}</ul></nav>
<div><h2>Opet Petrolcülük Anonim Şirketi Mersin Tesisi Depolama Tarifesi Hakkında</h2>
<p>Opet Petrolcülük Anonim Şirketinin Petrol Piyasası Fiyatlandırma Sistemi Yönetmeliği kapsamında Mersin adresinde
faaliyette bulunan depolama tesisine ait onaya tabi depolama tarifesinin tadil edilmesine ilişkin tarife değişikliği
başvurusu ekte yer almaktadır.</p>
<p>Ekteki tarife değişikliği başvurusuna, söz konusu tesisten en az bir kez hizmet almış kişilerce, 08/12/2026 günü
mesai saati sonuna kadar itiraz edilebilecektir.</p>
<p>25.11.2026 - 08.12.2026 Birimi : Tarifeler Dairesi Görüntülenme Sayısı: 135</p></div>
<footer><p>HAKKIMIZDA</p><p>Kurum Unvanı: T.C. ENERJİ PİYASASI DÜZENLEME KURUMU</p><p>Lisanssız Üretim</p></footer>
</body></html>"""


def _text(page: str) -> str:
    s = Settings()
    s.http_min_interval_seconds = 0
    s.respect_robots_txt = False
    adapter = HtmlListingAdapter("epdk_duyurular", CFG)
    with respx.mock() as router:
        router.get(URL).mock(return_value=httpx.Response(200, text=page, headers={"content-type": "text/html"}))
        doc = FetchedDocument(source_key="epdk_duyurular", external_id=URL, title="Opet", url=URL,
                              doc_type="DUYURU", institution="EPDK")
        return adapter.fetch_text(Fetcher(s), doc).text


def test_menu_ve_alt_bilgi_kirpilir():
    text = _text(PAGE)
    assert text.startswith("Opet Petrolcülük Anonim Şirketi Mersin Tesisi")
    assert "08/12/2026" in text
    for noise in ("Lisanssız Üretim", "YEKDEM", "Spot Kapasite", "Kurum Unvanı", "Birimi"):
        assert noise not in text


def test_isaret_bulunamazsa_metin_kaybolmaz():
    text = _text(PAGE.replace("BASIN AÇIKLAMALARI", "BASIN DUYURULARI").replace("Birimi :", "Birim -"))
    assert "08/12/2026" in text and "Lisanssız Üretim" in text  # düzen değişti → kırpma yok, metin tam


def test_kirpilmis_metin_menu_sozcukleriyle_eslesmez():
    """Önce her EPDK duyurusu menüden "elektrik piyasa, lisanssız üretim, doğal gaz…" ile eşleşiyordu."""
    tax = Taxonomy.load(ROOT / "enerji_takip" / "varsayilan" / "taxonomy.yaml")
    keywords = {k for v in tax.match(_text(PAGE)).values() for k in v}
    assert not any(w in k for k in keywords for w in ("lisanssız", "yekdem", "elektrik piyasa", "doğal gaz"))
