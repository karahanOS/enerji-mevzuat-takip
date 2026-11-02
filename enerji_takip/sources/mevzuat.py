"""Mevzuat Bilgi Sistemi (mevzuat.gov.tr): izleme listesindeki kanun/yönetmeliklerin konsolide metni.

Site arayüzü JavaScript ile çalışır, ancak metnin kendisi sunucuda üretilen bir iframe sayfasındadır:
``/anasayfa/MevzuatFihristDetayIframe?MevzuatTur=<tür>&MevzuatNo=<no>&MevzuatTertip=<tertip>``
(çerez/oturum gerekmez). Bu adaptör izleme listesindeki her mevzuatın metnini madde bazında saklar;
sonraki taramalarda madde metni değiştiyse "konsolide metin güncellendi" belgesi üretir. Belgenin
metni değişen maddelerin ESKİ / YENİ hâlidir, böylece değişikliğin mevcut metne nasıl işlendiği
(eski–yeni karşılaştırması) raporda görülür.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import zlib
from datetime import date
from urllib.parse import urlencode

from ..http import FetchError, Fetcher
from ..text_utils import html_to_text, normalize_space, sha256, tr_upper
from .base import DocumentText, FetchedDocument, ParseError, ScanOutcome, SourceAdapter

log = logging.getLogger(__name__)

BASE = "https://www.mevzuat.gov.tr"
IFRAME_PATH = "/anasayfa/MevzuatFihristDetayIframe"

TUR_LABELS = {1: "Kanun", 2: "KHK", 3: "Tüzük", 4: "Kanun Hükmünde Kararname", 5: "Uygulama Yönetmeliği",
              7: "Yönetmelik", 9: "Tebliğ", 19: "Cumhurbaşkanlığı Kararnamesi",
              20: "Cumhurbaşkanlığı Kararı", 21: "Kurum ve Kuruluş Yönetmeliği"}

# Madde başlığı satırları: "MADDE 4-", "MADDE 4 –", "GEÇİCİ MADDE 12 –", "EK MADDE 3-", "Madde 7/A -"
ARTICLE_HEAD_RE = re.compile(
    r"^(?P<label>(?:(?:EK|Ek|GEÇİCİ|Geçici)\s+)?(?:MADDE|Madde)\s+\d+(?:\s*/\s*[A-ZÇĞİÖŞÜa-zçğıöşü])?)\s*[–—\-:]",
    re.M,
)
# Değişiklik işaretleri: "(Değişik:RG-19/10/2026-33405)", "(Ek: 4/6/2016-6719/15 md.)", "(Mülga:…)", "(İptal:…)"
# Sitede işaretler <span> ile bölünür; metne çevrilince "( Değişik:RG -27/12/2019-30991)" gibi boşluklar oluşur.
MARKER_RE = re.compile(
    r"\(\s*(?:Değişik|Ek|Mülga|İptal|Yeniden\s+düzenleme)(?:\s+(?:ibare|cümle|fıkra|bent|madde|başlık))?"
    r"\s*:[^()]{1,200}?\)"
)
# Metnin sonundaki "…Yayımlandığı Resmî Gazete…" tablosu: yeni değişiklikte tabloya satır eklenir;
# bu son maddenin değişmiş gibi görünmemesi için ayrı bölüm sayılır.
TABLE_RE = re.compile(r"^[^\n]{0,80}Yayımlandığı\s+Resm[îi]\s+Gazete", re.M)
_RG_MARKER_RE = re.compile(r"RG\s*-\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*-\s*(\d{5})")

MAX_ARTICLE_CHARS = 4000   # raporda madde başına eski/yeni metin üst sınırı
MAX_DIFF_CHARS = 40000     # belge metni üst sınırı


def iframe_url(tur: int, no: str, tertip: int = 5) -> str:
    return f"{BASE}{IFRAME_PATH}?" + urlencode({"MevzuatTur": tur, "MevzuatNo": no, "MevzuatTertip": tertip})


def page_url(tur: int, no: str, tertip: int = 5) -> str:
    return f"{BASE}/mevzuat?" + urlencode({"MevzuatNo": no, "MevzuatTur": tur, "MevzuatTertip": tertip})


def _norm(text: str) -> str:
    text = re.sub(r"\s+", " ", normalize_space(text)).strip()
    return MARKER_RE.sub(lambda m: _clean_marker(m.group(0)), text)


_HEADING_LINE_RE = re.compile(r"^(?![a-zçğıöşü]{1,2}\)|\(|\d)[^\n]{1,150}(?<![.;:,])$")


def _heading_start(text: str, start: int, floor: int, max_lines: int = 3) -> int:
    """Madde başlığından hemen önceki kısa başlık satırlarını ('Kapsam', 'İKİNCİ BÖLÜM' …) maddeye dahil eder;
    böylece araya yeni madde eklendiğinde önceki madde değişmiş görünmez."""
    pos = start
    for _ in range(max_lines):
        prev_end = text.rfind("\n", floor, pos)
        if prev_end < 0:
            break
        line_start = text.rfind("\n", floor, prev_end) + 1
        if line_start <= floor:
            break
        line = text[line_start:prev_end].strip()
        if not line or not _HEADING_LINE_RE.match(line):
            break
        pos = line_start
    return pos


def split_into_articles(text: str) -> list[tuple[str, str]]:
    """Konsolide metni (etiket, madde metni) listesine böler. Başlangıçtaki giriş bölümü '_giris' olur.
    Aynı etiket tekrar ederse (ör. iki ayrı 'MADDE 5') ikincisi 'MADDE 5 #2' olarak ayrılır."""
    heads = list(ARTICLE_HEAD_RE.finditer(text))
    out: list[tuple[str, str]] = []
    if not heads:
        return [("_giris", _norm(text))] if text.strip() else []
    tail = TABLE_RE.search(text, heads[-1].end())
    text_end = tail.start() if tail else len(text)
    starts = [_heading_start(text, m.start(), heads[i - 1].end() if i else 0) for i, m in enumerate(heads)]
    if starts[0] > 0 and text[: starts[0]].strip():
        out.append(("_giris", _norm(text[: starts[0]])))
    seen: dict[str, int] = {}
    for i, m in enumerate(heads):
        end = starts[i + 1] if i + 1 < len(heads) else text_end
        label = re.sub(r"\s*/\s*", "/", tr_upper(re.sub(r"\s+", " ", m.group("label"))))
        seen[label] = seen.get(label, 0) + 1
        if seen[label] > 1:
            label = f"{label} #{seen[label]}"
        out.append((label, _norm(text[starts[i]:end])))
    if tail:
        out.append(("_tablo", _norm(text[tail.start():])))
    return out


def _clean_marker(raw: str) -> str:
    mk = re.sub(r"\s+", " ", raw)
    mk = re.sub(r"\(\s+", "(", mk)
    mk = re.sub(r"\s+\)", ")", mk)
    mk = re.sub(r"\s*([:\-])\s*", r"\1", mk)
    return mk


def extract_markers(text: str) -> list[str]:
    return sorted({_clean_marker(m.group(0)) for m in MARKER_RE.finditer(text)})


def _pack(articles: list[tuple[str, str]]) -> str:
    return base64.b64encode(zlib.compress(json.dumps(articles, ensure_ascii=False).encode("utf-8"), 9)).decode("ascii")


def _unpack(blob: str) -> list[tuple[str, str]]:
    return [tuple(x) for x in json.loads(zlib.decompress(base64.b64decode(blob)).decode("utf-8"))]


def _clip(text: str, limit: int = MAX_ARTICLE_CHARS) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + " […]"


def diff_articles(old: list[tuple[str, str]], new: list[tuple[str, str]]) -> list[tuple[str, str, str | None, str | None]]:
    """(etiket, işlem, eski, yeni) — işlem: değiştirildi | eklendi | çıkarıldı. Sıra yeni metne göredir."""
    old_map, new_map = dict(old), dict(new)
    result = []
    for label, text in new:
        if label not in old_map:
            result.append((label, "eklendi", None, text))
        elif old_map[label] != text:
            result.append((label, "değiştirildi", old_map[label], text))
    for label, text in old:
        if label not in new_map:
            result.append((label, "çıkarıldı", text, None))
    return result


def _display(label: str) -> str:
    return {"_giris": "Giriş/başlık bölümü", "_tablo": "Yayım ve değişiklik tablosu"}.get(label, label)


def build_diff_text(name: str, kind: str, no: str, changes, new_markers: list[str]) -> str:
    lines = [f"{name} ({kind}, No {no}) — Mevzuat Bilgi Sistemi'ndeki konsolide metin güncellendi.",
             "Değişen maddeler: " + ", ".join(f"{_display(l)} ({op})" for l, op, _, _ in changes) + "."]
    if new_markers:
        lines.append("Yeni değişiklik işaretleri: " + "; ".join(new_markers[:20]))
    lines.append("")
    for label, op, old, new in changes:
        lines.append(f"=== {_display(label)} — {op} ===")
        if old is not None:
            lines.append("ESKİ METİN: " + _clip(old))
        if new is not None:
            lines.append("YENİ METİN: " + _clip(new))
        lines.append("")
    text = "\n".join(lines).strip()
    return text if len(text) <= MAX_DIFF_CHARS else text[:MAX_DIFF_CHARS].rstrip() + "\n[… metin kısaltıldı]"


def _rg_from_markers(markers: list[str]) -> tuple[date | None, str | None]:
    """Yeni işaretlerdeki en son RG atfı (tarih, sayı)."""
    best: tuple[date, str] | None = None
    for mk in markers:
        for d, m, y, sayi in _RG_MARKER_RE.findall(mk):
            try:
                dt = date(int(y), int(m), int(d))
            except ValueError:
                continue
            if best is None or dt > best[0]:
                best = (dt, sayi)
    return (best[0], best[1]) if best else (None, None)


class MevzuatKonsolideAdapter(SourceAdapter):
    """config: items: [{mevzuat_no, tur, tertip?, name}], min_text_chars? (varsayılan 500)."""

    method = "mevzuat_konsolide"

    def _items(self) -> list[dict]:
        items = []
        for raw in self.config.get("items") or []:
            # YAML'da çıplak 'no' anahtarı boolean False okunur; bu yüzden alan adı 'mevzuat_no'.
            no = raw.get("mevzuat_no") or raw.get("no")
            if not no or not raw.get("tur"):
                raise ValueError(f"mevzuat_konsolide: her kalemde 'mevzuat_no' ve 'tur' zorunlu: {raw}")
            items.append({"no": str(no), "tur": int(raw["tur"]), "tertip": int(raw.get("tertip", 5)),
                          "name": raw.get("name") or f"{TUR_LABELS.get(int(raw['tur']), 'Mevzuat')} {no}"})
        return items

    def scan(self, fetcher: Fetcher, *, cursor: str | None, today: date, state: dict) -> ScanOutcome:
        outcome = ScanOutcome()
        items = self._items()
        if not items:
            outcome.complete = False
            outcome.errors.append("İzleme listesi (config.items) boş")
            outcome.access_status = "hata"
            return outcome
        min_chars = int(self.config.get("min_text_chars", 500))
        ok = baseline_new = updated = 0
        failures = 0
        for it in items:
            key = f"{it['tur']}.{it['tertip']}.{it['no']}"
            url = iframe_url(it["tur"], it["no"], it["tertip"])
            try:
                res = fetcher.get(url)
                text = html_to_text(res.text)
                if len(text) < min_chars or not ARTICLE_HEAD_RE.search(text):
                    raise ParseError(f"Beklenen mevzuat metni gelmedi ({len(text)} karakter, madde başlığı yok)")
            except (FetchError, ParseError) as exc:
                failures += 1
                outcome.errors.append(f"{it['name']}: {exc}")
                continue
            ok += 1
            articles = split_into_articles(text)
            markers = extract_markers(text)
            digest = sha256(json.dumps(articles, ensure_ascii=False))
            prev = state.get(key)
            entry = {"h": digest, "m": markers, "a": _pack(articles), "t": today.isoformat(), "n": it["name"]}
            if prev is None:
                state[key] = entry
                baseline_new += 1
                continue
            if prev.get("h") == digest:
                prev["t"] = today.isoformat()
                continue
            try:
                old_articles = _unpack(prev["a"])
            except Exception:  # bozuk/eski kayıt: yeni metni taban kabul et
                state[key] = entry
                outcome.warnings.append(f"{it['name']}: önceki metin okunamadı; yeni taban kaydedildi")
                continue
            changes = diff_articles(old_articles, articles)
            new_markers = [m for m in markers if m not in set(prev.get("m") or [])]
            state[key] = entry
            if not any(l not in ("_giris", "_tablo") for l, _, _, _ in changes):
                continue  # yalnızca başlık/tablo değişti: madde metni aynı, raporlanmaz (taban güncellendi)
            updated += 1
            kind = TUR_LABELS.get(it["tur"], "Mevzuat")
            labels = [_display(l) for l, _, _, _ in changes]
            title = (f"{it['name']} — konsolide metin güncellendi ("
                     + ", ".join(labels[:4]) + (f" ve {len(labels) - 4} madde daha" if len(labels) > 4 else "") + ")")
            rg_date, rg_number = _rg_from_markers(new_markers)
            ext_id = f"mevzuat:{key}:{digest[:16]}"
            outcome.documents.append(FetchedDocument(
                source_key=self.source_key, external_id=ext_id, title=title,
                url=page_url(it["tur"], it["no"], it["tertip"]), doc_type="KONSOLIDE",
                institution=self.config.get("institution"), published_date=today,
                rg_date=rg_date, rg_number=rg_number,
                inline_text=build_diff_text(it["name"], kind, it["no"], changes, new_markers),
            ))
        outcome.coverage = {"izlenen": len(items), "okunan": ok, "ilk_kayit": baseline_new, "guncellenen": updated}
        if failures:
            outcome.complete = False
            outcome.access_status = "erisilemiyor" if ok == 0 else "erisilebilir"
        return outcome

    def fetch_text(self, fetcher: Fetcher, doc: FetchedDocument) -> DocumentText:
        if doc.inline_text:
            return DocumentText(text=doc.inline_text, status="tam")
        # Karşılaştırma yoksa (ör. metin yeniden deneme) güncel tam metne düş
        m = re.match(r"mevzuat:(\d+)\.(\d+)\.([^:]+):", doc.external_id or "")
        if m:
            from dataclasses import replace

            doc = replace(doc, url=iframe_url(int(m.group(1)), m.group(3), int(m.group(2))))
        dt = super().fetch_text(fetcher, doc)
        if dt.text:
            dt.note = "Eski–yeni karşılaştırması yok; güncel konsolide metin alındı"
        return dt
