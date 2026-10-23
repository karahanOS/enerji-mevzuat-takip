"""Test altyapısı.

Testler gerçek ağa çıkmaz: Resmî Gazete ve kurum sitelerinin yanıtları, gerçek sayfaların
yapısını taklit eden ve açıkça "TEST VERİSİ" olarak etiketlenmiş fixture dosyalarından
(respx ile) sunulur. E-postalar yerel bir SMTP sunucusuna (aiosmtpd) gönderilir; LLM
yanıtları, gerçek istemcinin arayüzünü taklit eden sahte bir istemciden gelir.
"""
from __future__ import annotations

import io
import json
import re
import socket
from datetime import datetime, timezone
from email import message_from_bytes, policy
from pathlib import Path

import httpx
import pytest
import respx

from enerji_takip import config as config_mod
from enerji_takip.analysis.llm import LLMChain, LLMError, LLMQuotaError
from enerji_takip.analysis.prompts import TRIAGE_SYSTEM
from enerji_takip.config import Settings, set_settings
from enerji_takip.db import init_db, reset_engine
from enerji_takip.http import Fetcher
from enerji_takip.pipeline import Pipeline

FIX = Path(__file__).parent / "fixtures"
RG = "https://www.resmigazete.gov.tr"
EPIAS_LIST = "https://www.epias.com.tr/spot-elektrik-piyasasi/elektrik-piyasasi-duyurulari"
ETKB_RSS = "https://enerji.gov.tr/RSS/15?t=1"
NOW = datetime(2026, 10, 23, 5, 0, tzinfo=timezone.utc)  # 08:00 İstanbul

TEST_SOURCES = """
sources:
  - key: resmi_gazete
    name: Resmî Gazete
    url: https://www.resmigazete.gov.tr/
    category: Birincil kaynak
    method: rg_fihrist
    primary: true
  - key: epias_elektrik_duyurulari
    name: EPİAŞ – Elektrik Piyasası Duyuruları
    url: https://www.epias.com.tr/spot-elektrik-piyasasi/elektrik-piyasasi-duyurulari
    category: Piyasa işletmecisi
    method: html_listing
    config:
      list_urls: [https://www.epias.com.tr/spot-elektrik-piyasasi/elektrik-piyasasi-duyurulari]
      item_link_pattern: 'epias\\.com\\.tr/(tum-duyurular|genel)/.+'
      exclude_title_pattern: 'Uzlaştırma Bildirimi|Planlı Bakım'
      content_selector: .entry-content
      doc_type: DUYURU
      institution: EPİAŞ
  - key: etkb_mevzuat_rss
    name: ETKB – Mevzuat RSS
    url: https://enerji.gov.tr/RSS/15?t=1
    category: Bakanlık
    method: rss
    config:
      feed_urls: ['https://enerji.gov.tr/RSS/15?t=1']
      doc_type: DUYURU
      institution: ETKB
  - key: epdk_elektrik_kurul_kararlari
    name: EPDK – Elektrik Kurul Kararları
    url: https://www.epdk.gov.tr/
    category: Düzenleyici
    method: html_listing
    enabled: false
    status_if_disabled: javascript_gerekli
"""


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    import enerji_takip.analysis.llm as llm_mod
    import enerji_takip.http as http_mod

    monkeypatch.setattr(http_mod.time, "sleep", lambda *_: None)
    monkeypatch.setattr(llm_mod.time, "sleep", lambda *_: None)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Collector:
    def __init__(self):
        self.messages: list[dict] = []

    async def handle_DATA(self, server, session, envelope):
        msg = message_from_bytes(envelope.content, policy=policy.default)
        self.messages.append({"rcpt": list(envelope.rcpt_tos), "msg": msg})
        return "250 OK"


@pytest.fixture
def smtp_server():
    from aiosmtpd.controller import Controller

    handler = _Collector()
    port = free_port()
    ctrl = Controller(handler, hostname="127.0.0.1", port=port)
    ctrl.start()
    handler.port = port
    yield handler
    ctrl.stop()


@pytest.fixture
def settings(tmp_path, smtp_server):
    src = tmp_path / "sources.yaml"
    src.write_text(TEST_SOURCES, encoding="utf-8")
    s = Settings()
    s.database_url = f"sqlite:///{tmp_path / 'test.db'}"
    s.sources_file = src
    s.http_min_interval_seconds = 0
    s.smtp_host, s.smtp_port, s.smtp_security = "127.0.0.1", smtp_server.port, "none"
    s.email_from = "ben@ornek.com"
    s.email_to = ["ben@ornek.com"]
    s.email_cc = ["arkadas@ornek.com"]
    s.admin_email = ["ben@ornek.com"]
    s.email_retry_base_seconds = 0
    s.llm_analysis_chain, s.llm_triage_chain = [], []
    s.log_dir = tmp_path / "loglar"
    s.report_preview_dir = tmp_path / "raporlar"
    s.rg_first_run_days = 1
    set_settings(s)
    reset_engine(s.database_url)
    init_db()
    yield s
    config_mod._settings = None


# ---- Sahte LLM ---------------------------------------------------------------
def _quote_from(text: str) -> str:
    m = re.search(r"MADDE 1 – \(1\) (.+?)(?:\.|$)", text, re.M)
    if not m:
        m = re.search(r"toplantısında; (.+?)(?:\.|$)", text, re.M)
    words = (m.group(1) if m else text.strip().split("\n")[-1]).split()
    return " ".join(words[:16])


def default_analysis(prompt: str) -> dict:
    title = re.search(r'"baslik": "(.*?)"', prompt).group(1)
    text = prompt.split("<mevzuat_metni>")[1].split("</mevzuat_metni>")[0]
    base = {
        "ilgili_mi": "evet",
        "ilgililik_gerekcesi": "Enerji piyasasıyla ilgili.",
        "ozet": f"{title} yayımlandı. Düzenleme enerji mevzuatında değişiklikler içermektedir. "
                "Piyasa katılımcılarının süreçlerini etkileyebilir.",
        "ne_degisti": "Metinde belirtilen maddeler değişti.",
        "onceki_duzenleme": "Metinde önceki düzenleme yer almıyor; karşılaştırma için mevzuatın önceki hali incelenmeli.",
        "yeni_duzenleme": "Yeni hükümler getirildi.",
        "acik_hukumler": [{"hukum": "Birinci hüküm", "madde": "Madde 1", "alinti": _quote_from(text)}],
        "cikarimlar": ["Piyasa fiyat oluşumuna dolaylı etkisi olabilir."],
        "piyasa_etkisi": {"duzey": "belirsiz", "aciklama": "Etki metinden kesin belirlenemedi."},
        "kisisel_ilgi": "Piyasa analizleri açısından takip edilmeli.",
        "ilgili_alanlar": ["Elektrik piyasası işleyişi"],
        "etkilenen_piyasa_unsurlari": [],
        "kimler_etkilenir": [],
        "yeni_yukumluluk": {"var_mi": "hayir", "aciklama": "", "madde": ""},
        "yukumluluk_degisikligi": {"var_mi": "hayir", "aciklama": "", "madde": ""},
        "tarihler": [],
        "yaptirim": {"var_mi": "hayir", "aciklama": "", "madde": ""},
        "gecis_suresi_istisna": {"var_mi": "hayir", "aciklama": "", "madde": ""},
        "degisiklik_nitelikleri": ["diger"],
        "takip_onerileri": ["Resmî metni okuyun."],
        "onem_onerisi": "ORTA",
        "onem_gerekcesi": "Sınırlı etki.",
        "guven": "orta",
        "metin_eksik_mi": False,
    }
    if "Dengeleme ve Uzlaştırma" in title:
        base.update({
            "piyasa_etkisi": {"duzey": "genel", "aciklama": "Dengesizlik bedeli tüm dengeden sorumlu tarafları etkiler."},
            "kisisel_ilgi": "SMF tahmin modelinde dengesizlik maliyeti hesabı güncellenmeli.",
            "ilgili_alanlar": ["Fiyatlar ve uzlaştırma", "Elektrik piyasası işleyişi", "YEKDEM"],
            "etkilenen_piyasa_unsurlari": ["PTF", "SMF", "Dengesizlik bedeli"],
            "kimler_etkilenir": ["Dengeden sorumlu taraflar", "Tedarik şirketleri"],
            "yeni_yukumluluk": {"var_mi": "evet", "aciklama": "GİP pozisyonlarını 1 saat önce bildirme.", "madde": "Madde 2"},
            "tarihler": [
                {"tur": "yururluk", "hukum": "Madde 1", "tarih": "2026-11-01", "metindeki_ifade": "1 inci maddesi 1/11/2026 tarihinde", "kesinlik": "kesin"},
                {"tur": "yururluk", "hukum": "Diğer hükümler", "tarih": "2026-10-23", "metindeki_ifade": "diğer hükümleri yayımı tarihinde", "kesinlik": "kesin"},
            ],
            "degisiklik_nitelikleri": ["piyasa_kurali_degisikligi", "fiyat_tarife_degisikligi", "yeni_yukumluluk"],
            "takip_onerileri": ["Dengesizlik maliyeti hesaplamasını yeni formüle göre güncelle."],
            "onem_onerisi": "KRITIK",
            "guven": "yuksek",
        })
    elif "15000 Sayılı" in title:
        base.update({
            "piyasa_etkisi": {"duzey": "genel", "aciklama": "Azami fiyat limiti tüm GÖP ve GİP işlemlerini etkiler."},
            "ilgili_alanlar": ["Fiyatlar ve uzlaştırma"],
            "etkilenen_piyasa_unsurlari": ["PTF", "GİP"],
            "tarihler": [{"tur": "yururluk", "hukum": "Tüm hükümler", "tarih": "2026-11-01", "metindeki_ifade": "1/11/2026 tarihinde", "kesinlik": "kesin"}],
            "degisiklik_nitelikleri": ["fiyat_tarife_degisikligi"],
            "onem_onerisi": "KRITIK",
        })
    elif "Garanti ve Destek" in title:
        base.update({
            "piyasa_etkisi": {"duzey": "sektorel", "aciklama": "YEKDEM katılımcısı tesisleri etkiler."},
            "ilgili_alanlar": ["Yenilenebilir enerji ve YEKDEM"],
            "yeni_yukumluluk": {"var_mi": "evet", "aciklama": "Yerli aksam belgelendirme.", "madde": "Madde 2"},
            "tarihler": [{"tur": "yururluk", "hukum": "Tüm hükümler", "tarih": "2027-04-23", "metindeki_ifade": "yayımı tarihinden itibaren altı ay sonra", "kesinlik": "metinden_hesaplanan"}],
            "degisiklik_nitelikleri": ["destek_tesvik_degisikligi"],
            "onem_onerisi": "YUKSEK",
        })
    return base


class FakeClient:
    """OpenAICompatClient arayüzünü taklit eder."""

    def __init__(self, provider="fake", model="model-a", script=None, triage_answer="hayir", max_input_chars=60000):
        self.provider, self.model = provider, model
        self.max_input_chars, self.max_output_tokens = max_input_chars, 4000
        self.script = list(script or [])
        self.triage_answer = triage_answer
        self.calls: list[dict] = []

    @property
    def label(self):
        return f"{self.provider}:{self.model}"

    def complete(self, system, messages, *, max_tokens=None):
        self.calls.append({"system": system, "messages": messages})
        if system == TRIAGE_SYSTEM:
            return json.dumps({"ilgili_mi": self.triage_answer, "gerekce": "test"})
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            if callable(item):
                item = item(messages)
            if item is not None:
                return item
        return json.dumps(default_analysis(messages[0].content), ensure_ascii=False)


def fake_chain(*clients) -> LLMChain:
    return LLMChain(list(clients) or [FakeClient()])


# ---- Sahte web ----------------------------------------------------------------
def make_pdf(lines: list[str]) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    font = "Helvetica"
    try:
        pdfmetrics.registerFont(TTFont("DejaVu", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
        font = "DejaVu"
    except Exception:
        pass
    c.setFont(font, 10)
    y = 800
    for line in lines:
        c.drawString(40, y, line)
        y -= 16
    c.save()
    return buf.getvalue()


OTV_PDF_LINES = [
    "TEST VERİSİ – kurgusal Cumhurbaşkanı Kararı",
    "Bazı Akaryakıt Ürünlerinde Uygulanan Özel Tüketim Vergisi Tutarlarının Belirlenmesine İlişkin Karar",
    "MADDE 1 – (1) Motorin ve LPG için uygulanan özel tüketim vergisi tutarları ekli listede",
    "belirtildiği şekilde değiştirilmiştir.",
    "MADDE 2 – (1) Bu Karar yayımı tarihinde yürürlüğe girer.",
]
AYM_PDF_LINES = ["TEST VERİSİ – kurgusal Anayasa Mahkemesi kararı",
                 "Mahkeme, belediyelerin doğal gaz altyapı katkı payı hakkındaki itiraz başvurusunu inceledi.",
                 "İtirazın reddine karar verildi."]


def register_rg_routes(router: respx.MockRouter, *, robots: str = "User-agent: *\nAllow: /\n",
                       fail_dates: dict | None = None) -> None:
    fail_dates = fail_dates or {}
    router.get(f"{RG}/robots.txt").mock(return_value=httpx.Response(200, text=robots))
    for ymd, status in fail_dates.items():
        router.get(f"{RG}/eskiler/{ymd[:4]}/{ymd[4:6]}/{ymd}.htm").mock(return_value=httpx.Response(status))
    for path in sorted((FIX / "rg").glob("*.htm")):
        if path.stem in fail_dates:
            continue  # respx aynı kalıptaki rotayı günceller; hata rotası ezilmesin
        router.get(f"{RG}/eskiler/2026/10/{path.name}").mock(
            return_value=httpx.Response(200, content=path.read_bytes(), headers={"content-type": "text/html"}))
    router.get(f"{RG}/eskiler/2026/10/20261023-1.pdf").mock(
        return_value=httpx.Response(200, content=make_pdf(OTV_PDF_LINES), headers={"content-type": "application/pdf"}))
    router.get(f"{RG}/eskiler/2026/10/20261023-11.pdf").mock(
        return_value=httpx.Response(200, content=make_pdf(AYM_PDF_LINES), headers={"content-type": "application/pdf"}))
    router.get(url__regex=rf"{re.escape(RG)}/.*").mock(return_value=httpx.Response(404))


def register_epias_routes(router: respx.MockRouter, version: str = "v1", status: int = 200) -> None:
    router.get("https://www.epias.com.tr/robots.txt").mock(return_value=httpx.Response(404))
    if status != 200:
        router.get(EPIAS_LIST).mock(return_value=httpx.Response(status))
    else:
        router.get(EPIAS_LIST).mock(return_value=httpx.Response(
            200, text=(FIX / "listing" / f"epias_{version}.html").read_text(encoding="utf-8"),
            headers={"content-type": "text/html; charset=utf-8"}))
    router.get(url__regex=r"https://www\.epias\.com\.tr/tum-duyurular/.*").mock(return_value=httpx.Response(
        200, text=(FIX / "listing" / "epias_detail.html").read_text(encoding="utf-8"),
        headers={"content-type": "text/html; charset=utf-8"}))


def register_etkb_routes(router: respx.MockRouter, version: str = "v1") -> None:
    router.get("https://enerji.gov.tr/robots.txt").mock(return_value=httpx.Response(404))
    router.get(ETKB_RSS).mock(return_value=httpx.Response(
        200, content=(FIX / "listing" / f"etkb_rss_{version}.xml").read_bytes(),
        headers={"content-type": "application/rss+xml; charset=utf-8"}))
    router.get(url__regex=r"https://enerji\.gov\.tr/Media/.*").mock(return_value=httpx.Response(
        200, text=(FIX / "listing" / "etkb_depolama.html").read_text(encoding="utf-8"),
        headers={"content-type": "text/html; charset=utf-8"}))


def register_all(router, *, epias="v1", etkb="v1", **rg_kwargs):
    register_rg_routes(router, **rg_kwargs)
    register_epias_routes(router, epias)
    register_etkb_routes(router, etkb)


def make_pipeline(settings, chain=None, now=NOW, triage=None) -> Pipeline:
    return Pipeline(settings=settings, fetcher=Fetcher(settings), analysis_chain=chain, triage_chain=triage, now=now)


__all__ = ["LLMError", "LLMQuotaError"]
