"""Türkçe metin yardımcıları: büyük/küçük harf, normalizasyon, HTML→metin, tarih."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date

from bs4 import BeautifulSoup

TR_MONTHS = {
    "ocak": 1, "şubat": 2, "mart": 3, "nisan": 4, "mayıs": 5, "haziran": 6,
    "temmuz": 7, "ağustos": 8, "eylül": 9, "ekim": 10, "kasım": 11, "aralık": 12,
}
TR_MONTH_NAMES = {v: k.capitalize() for k, v in TR_MONTHS.items()}
TR_WEEKDAYS = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]


def tr_lower(text: str) -> str:
    """Türkçe kurallarına uygun küçük harf (I→ı, İ→i)."""
    return text.replace("I", "ı").replace("İ", "i").lower()


def tr_upper(text: str) -> str:
    return text.replace("i", "İ").replace("ı", "I").upper()


def normalize_space(text: str) -> str:
    text = text.replace("\xa0", " ").replace("​", "")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def normalize_for_match(text: str) -> str:
    """Anahtar kelime eşleştirmesi için: Türkçe küçük harf, tek boşluk, düzgün kesme işareti."""
    text = unicodedata.normalize("NFC", text)
    text = text.replace("’", "'").replace("‘", "'").replace("`", "'")
    text = text.replace("â", "a").replace("Â", "A").replace("î", "i").replace("û", "u")
    text = tr_lower(text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_title(title: str) -> str:
    """Tekilleştirme için başlık normalizasyonu."""
    t = normalize_for_match(title)
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def html_to_text(html: str) -> str:
    """Word kaynaklı Resmî Gazete HTML'ini okunabilir düz metne çevirir."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "head", "noscript"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    blocks = []
    for el in soup.find_all(["p", "div", "td", "li", "h1", "h2", "h3", "h4", "tr"]):
        # Yalnızca yaprak blokları al (iç içe tekrarları önlemek için)
        if el.find(["p", "div", "td", "li", "tr"]):
            continue
        txt = el.get_text(" ", strip=True)
        if txt:
            blocks.append(txt)
    if not blocks:
        return normalize_space(soup.get_text("\n", strip=True))
    return normalize_space("\n".join(blocks))


_TR_DATE_RE = re.compile(
    r"(\d{1,2})\s+(Ocak|Şubat|Mart|Nisan|Mayıs|Haziran|Temmuz|Ağustos|Eylül|Ekim|Kasım|Aralık)\s+(\d{4})",
    re.IGNORECASE,
)
_NUM_DATE_RE = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b")


def parse_tr_date(text: str) -> date | None:
    """'23 Ekim 2026', '23.10.2026' veya '23/10/2026' biçimlerini çözer."""
    m = _TR_DATE_RE.search(text)
    if m:
        try:
            return date(int(m.group(3)), TR_MONTHS[tr_lower(m.group(2))], int(m.group(1)))
        except (ValueError, KeyError):
            pass
    m = _NUM_DATE_RE.search(text)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    return None


def format_tr_date(d: date | None) -> str:
    if d is None:
        return "—"
    return f"{d.day:02d}.{d.month:02d}.{d.year}"


def format_tr_long(d: date) -> str:
    return f"{d.day} {TR_MONTH_NAMES[d.month]} {d.year} {TR_WEEKDAYS[d.weekday()]}"


_UNITS = {
    "bir": 1, "iki": 2, "üç": 3, "dört": 4, "beş": 5, "altı": 6, "yedi": 7, "sekiz": 8, "dokuz": 9,
}
_TENS = {
    "on": 10, "yirmi": 20, "otuz": 30, "kırk": 40, "elli": 50, "altmış": 60, "yetmiş": 70,
    "seksen": 80, "doksan": 90,
}


def parse_tr_number(text: str) -> int | None:
    """'altı', 'on beş', 'otuz', 'yüz seksen', '90' gibi ifadeleri sayıya çevirir."""
    text = normalize_for_match(text).strip()
    if text.isdigit():
        return int(text)
    total = 0
    found = False
    for word in text.replace("-", " ").split():
        if word == "yüz":
            total = (total or 1) * 100
            found = True
        elif word in _TENS:
            total += _TENS[word]
            found = True
        elif word in _UNITS:
            total += _UNITS[word]
            found = True
        else:
            # birleşik yazımlar: "onbeş", "yüzseksen"
            matched = False
            for token in sorted(list(_TENS) + list(_UNITS) + ["yüz"], key=len, reverse=True):
                if word.startswith(token):
                    rest = parse_tr_number(word[len(token):]) if word[len(token):] else 0
                    if rest is None:
                        continue
                    base = 100 if token == "yüz" else _TENS.get(token) or _UNITS.get(token)
                    total += base + rest if token != "yüz" else 100 + rest
                    found = matched = True
                    break
            if not matched:
                return None
    return total if found else None
