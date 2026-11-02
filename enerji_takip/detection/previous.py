"""Değişen maddelerin ESKİ hâli: "Eskiden ne yapıyordum, şimdi ne yapacağım?" karşılaştırması için.

Resmî Gazete'deki değişiklik metni yalnızca yeni hükmü verir. Eski hüküm iki yerden alınır:

1. **İzleme listesi tabanı** — mevzuat `mevzuat_konsolide` kaynağının izleme listesindeyse, sistemin sakladığı
   son konsolide metin. Değişiklik siteye işlenene kadar taban değişmediği için eski metin her zaman hazırdır.
2. **mevzuat.gov.tr (canlı)** — listede yoksa mevzuat adıyla aranır ve o anki konsolide metin alınır.
   Site değişiklikleri Resmî Gazete'den günler/haftalar sonra işler; o güne kadar sitedeki metin eskidir.

Her iki durumda da metnin bu değişikliği zaten içerip içermediği değişiklik işaretlerinden
(``(Değişik:RG-01/11/2026-33418)``) kontrol edilir; içeriyorsa "eski" metin olarak kullanılmaz.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..http import FetchError, Fetcher
from ..models import RegulatorySource
from ..text_utils import html_to_text, normalize_for_match, tr_upper
from ..sources.mevzuat import (
    BASE,
    _unpack,
    extract_markers,
    iframe_url,
    page_url,
    split_into_articles,
)

log = logging.getLogger(__name__)

SEARCH_URL = f"{BASE}/anasayfa/MevzuatDatatable"
MAX_OLD_CHARS = 3000

SRC_WATCH = "izleme_listesi"
SRC_LIVE = "mevzuat_gov_canli"
SOURCE_LABELS = {
    SRC_WATCH: "mevzuat.gov.tr konsolide metni (sistemde saklanan, değişiklik öncesi)",
    SRC_LIVE: "mevzuat.gov.tr konsolide metni (değişiklik siteye işlenmeden önce alındı)",
}


@dataclass
class PreviousVersion:
    status: str  # bulundu | guncellenmis | bulunamadi | hata | kapali
    note: str = ""
    source: str | None = None
    page: str | None = None
    articles: dict[str, str] = field(default_factory=dict)


def _norm_name(name: str) -> str:
    n = normalize_for_match(re.sub(r"<[^>]+>", " ", name or ""))
    # i/ı farkı yok sayılır: büyük harfli başlıklarda İ/I dönüşümü kaynağa göre tutarsız olabilir
    return " ".join(re.sub(r"[^\w ]", " ", n.replace("ı", "i")).split())


def article_key(label: str) -> str:
    """'Geçici Madde 40' / 'GEÇİCİ MADDE 40' / 'Madde 7/A' → karşılaştırma anahtarı."""
    return re.sub(r"\s+", " ", tr_upper(label)).replace(" / ", "/").strip()


def _clip(text: str) -> str:
    return text if len(text) <= MAX_OLD_CHARS else text[:MAX_OLD_CHARS].rstrip() + " […]"


def already_applied(markers: list[str], rg_number: str | None, law_number: str | None = None) -> bool:
    """Konsolide metin bu değişikliği zaten içeriyor mu? (yönetmelik: RG sayısı; kanun: değiştiren kanun no)"""
    for mk in markers:
        if rg_number and re.search(rf"RG\s*-\s*\d{{1,2}}/\d{{1,2}}/\d{{4}}\s*-\s*{re.escape(rg_number)}\b", mk):
            return True
        if law_number and re.search(rf"\d{{4}}\s*-\s*{re.escape(law_number)}\s*/", mk):
            return True
    return False


def _watch_list(session: Session) -> list[tuple[RegulatorySource, dict]]:
    rows = []
    for src in session.scalars(select(RegulatorySource).where(RegulatorySource.method == "mevzuat_konsolide")):
        for it in (src.config or {}).get("items") or []:
            rows.append((src, it))
    return rows


def _from_watch_list(session: Session, name_key: str) -> tuple[dict | None, dict | None, RegulatorySource | None]:
    for src, it in _watch_list(session):
        if _norm_name(it.get("name") or "") != name_key:
            continue
        no = str(it.get("mevzuat_no") or it.get("no"))
        key = f"{int(it['tur'])}.{int(it.get('tertip', 5))}.{no}"
        state = ((src.config or {}).get("_state") or {}).get(key)
        return it, state, src
    return None, None, None


def search_mevzuat(fetcher: Fetcher, name: str) -> dict | None:
    """mevzuat.gov.tr başlık araması; adı birebir (Türkçe büyük/küçük harf duyarsız) eşleşen tek kaydı döndürür."""
    col = {"data": None, "name": "", "searchable": True, "orderable": False, "search": {"value": "", "regex": False}}
    payload = {
        "draw": 1, "columns": [col, col, col], "order": [], "start": 0, "length": 20,
        "search": {"value": "", "regex": False},
        "parameters": {"AranacakIfade": base64.b64encode(name.encode("utf-8")).decode("ascii"),
                       "AranacakYer": "Baslik", "TamCumle": False, "MevzuatTur": 0, "GenelArama": True},
    }
    res = fetcher.post_json(SEARCH_URL, payload, referer=f"{BASE}/")
    data = json.loads(res.text or res.content.decode("utf-8", "replace"))
    want = _norm_name(name)
    hits = [d for d in data.get("data") or [] if _norm_name(d.get("mevAdi") or "") == want]
    if len(hits) != 1:
        return None
    d = hits[0]
    return {"no": str(d["mevzuatNo"]), "tur": int(d["mevzuatTur"]), "tertip": int(d.get("mevzuatTertip") or 5)}


def find_previous(session: Session, fetcher: Fetcher, regulation: str | None, rg_number: str | None,
                  law_number: str | None = None, *, live: bool = True) -> PreviousVersion:
    if not regulation:
        return PreviousVersion("bulunamadi", "Değiştirilen mevzuatın adı başlıktan çıkarılamadı")
    name_key = _norm_name(regulation)
    item, state, _src = _from_watch_list(session, name_key)
    if item is not None and state and state.get("a"):
        pg = page_url(int(item["tur"]), str(item.get("mevzuat_no") or item.get("no")), int(item.get("tertip", 5)))
        if already_applied(state.get("m") or [], rg_number, law_number):
            return PreviousVersion("guncellenmis", "Saklanan konsolide metin bu değişikliği zaten içeriyor", page=pg)
        try:
            arts = dict(_unpack(state["a"]))
        except Exception:
            arts = {}
        if arts:
            return PreviousVersion("bulundu", source=SRC_WATCH, page=pg,
                                   note=f"izleme listesi tabanı ({state.get('t') or 'tarih yok'})", articles=arts)
    if not live:
        return PreviousVersion("bulunamadi", "Mevzuat izleme listesinde değil; canlı arama kapalı")
    try:
        if item is not None:
            ref = {"no": str(item.get("mevzuat_no") or item.get("no")), "tur": int(item["tur"]),
                   "tertip": int(item.get("tertip", 5))}
        else:
            ref = search_mevzuat(fetcher, regulation)
        if ref is None:
            return PreviousVersion("bulunamadi", f"mevzuat.gov.tr'de '{regulation}' adıyla tek bir kayıt bulunamadı")
        res = fetcher.get(iframe_url(ref["tur"], ref["no"], ref["tertip"]))
        text = html_to_text(res.text)
    except (FetchError, ValueError, KeyError) as exc:
        log.warning("Önceki metin alınamadı (%s): %s", regulation, exc)
        return PreviousVersion("hata", f"mevzuat.gov.tr'ye erişilemedi: {exc}")
    pg = page_url(ref["tur"], ref["no"], ref["tertip"])
    if already_applied(extract_markers(text), rg_number, law_number):
        return PreviousVersion("guncellenmis", "mevzuat.gov.tr'deki metne bu değişiklik zaten işlenmiş", page=pg)
    arts = dict(split_into_articles(text))
    if len(arts) <= 1:
        return PreviousVersion("bulunamadi", "mevzuat.gov.tr sayfasında madde metni okunamadı", page=pg)
    return PreviousVersion("bulundu", source=SRC_LIVE, page=pg, note="mevzuat.gov.tr (canlı)", articles=arts)


_LAW_NO_RE = re.compile(r"Kanun\s+No\.?\s*:?\s*(\d{4})", re.I)


def attach_previous(session: Session, fetcher: Fetcher, articles: list[dict], regulation: str | None,
                    rg_number: str | None, text: str = "", *, live: bool = True) -> tuple[list[dict], PreviousVersion | None]:
    """amended_articles kayıtlarına 'eski', 'eski_kaynak', 'eski_not', 'konsolide_url' ekler (yeni liste döner)."""
    if not articles:
        return articles, None
    m = _LAW_NO_RE.search(text[:3000])
    prev = find_previous(session, fetcher, regulation, rg_number, m.group(1) if m else None, live=live)
    out = []
    for a in articles:
        a = dict(a)
        a["konsolide_url"] = prev.page
        if prev.status != "bulundu":
            a["eski"] = None
            a["eski_not"] = prev.note
        else:
            old = prev.articles.get(article_key(a["madde"]))
            a["eski_kaynak"] = SOURCE_LABELS.get(prev.source, prev.source)
            if old:
                a["eski"] = _clip(old)
            elif a.get("islem") == "eklendi":
                a["eski"] = None
                a["eski_not"] = "Yeni madde: önceki metinde karşılığı yok"
            else:
                a["eski"] = None
                a["eski_not"] = f"'{a['madde']}' önceki konsolide metinde bulunamadı"
        out.append(a)
    return out, prev
