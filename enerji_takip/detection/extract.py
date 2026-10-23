"""Kural tabanlı çıkarım: değişiklik türü, değiştirilen mevzuat, maddeler, yürürlük ve uyum tarihleri.

Bu çıkarımlar yapay zekâdan bağımsızdır ve iki amaca hizmet eder:
  * AI çalışmasa bile raporda doğru temel bilgiler bulunur;
  * AI'nın ürettiği tarihler bu sonuçlarla karşılaştırılır, çelişki varsa kayıt incelemeye düşer.
Kural: tarih TAHMİN EDİLMEZ. Metinden çıkarılamayan tarih "belirsiz" olarak kalır.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

from dateutil.relativedelta import relativedelta

from ..text_utils import TR_MONTHS, normalize_space, parse_tr_number, tr_lower

# ---- Değişiklik türleri ---------------------------------------------------

CHANGE_TYPE_LABELS = {
    "YENI_KANUN": "Yeni kanun",
    "YENI_YONETMELIK": "Yeni yönetmelik",
    "YENI_TEBLIG": "Yeni tebliğ",
    "YENI_GENELGE": "Yeni genelge",
    "DEGISIKLIK": "Mevcut mevzuatta değişiklik",
    "YURURLUKTEN_KALDIRMA": "Mevzuatın yürürlükten kaldırılması",
    "IDARI_KARAR": "Yeni idari karar",
    "KURUL_KARARI": "Yeni kurul kararı",
    "CB_KARARNAMESI": "Cumhurbaşkanlığı kararnamesi",
    "YARGI_KARARI": "Yargı kararı (Anayasa Mahkemesi vb.)",
    "ULUSLARARASI_ANLASMA": "Milletlerarası anlaşma",
    "KURUM_DUYURUSU": "Kurum duyurusu",
    "DIGER": "Diğer düzenleme",
}

ASPECT_LABELS = {
    "yeni_yukumluluk": "Yeni yükümlülük",
    "yukumluluk_degisikligi": "Mevcut yükümlülüğün değiştirilmesi",
    "yeni_uygulama_tarihi": "Yeni uygulama tarihi",
    "yaptirim": "Yaptırım / ceza düzenlemesi",
    "gecis_suresi_istisna": "Geçiş süresi veya istisna",
    "yururlukten_kaldirma": "Hüküm/mevzuat kaldırma",
    "madde_degisikligi": "Madde değişikliği",
    "madde_eklenmesi": "Madde/hüküm eklenmesi",
    "tanim_degisikligi": "Tanım değişikliği",
    "usul_degisikligi": "Usul/süreç değişikliği",
    "diger": "Diğer",
}

DOC_TYPE_LABELS = {
    "KANUN": "Kanun",
    "CB_KARARNAMESI": "Cumhurbaşkanlığı Kararnamesi",
    "CB_KARARI": "Cumhurbaşkanı Kararı",
    "GENELGE": "Genelge",
    "YONETMELIK": "Yönetmelik",
    "TEBLIG": "Tebliğ",
    "KURUL_KARARI": "Kurul Kararı",
    "AYM_KARARI": "Anayasa Mahkemesi Kararı",
    "YARGI_KARARI": "Yargı Kararı",
    "ANDLASMA": "Milletlerarası Andlaşma",
    "ATAMA": "Atama Kararı",
    "YONERGE": "Yönerge",
    "KARAR": "Karar",
    "ILAN": "İlan",
    "DUYURU": "Kurum Duyurusu",
    "DIGER": "Diğer",
}

_AMEND_TITLE_RE = re.compile(
    r"^(?P<target>.*?)\s*['’]?(?:nde|nda|de|da|te|ta)\s+(?:Bazı\s+)?Değişiklik\s+Yapılmas[ıi]",
    re.I,
)
_REPEAL_TITLE_RE = re.compile(
    r"^(?P<target>.*?)\s*['’]?(?:nın|nin|nun|nün|ın|in|un|ün)\s+Yürürlükten\s+Kaldırılmas[ıi]",
    re.I,
)
_RG_REF_RE = re.compile(
    r"(\d{1,2}/\d{1,2}/\d{4})\s+tarihli\s+ve\s+(\d{4,6})\s+sayılı\s+Resm[iî]\s+Gazete['’]?de\s+yayımlanan",
    re.I,
)


def _fix_stem(name: str) -> str:
    name = name.strip(" ,;")
    # Yönetmeliğ(in) → Yönetmelik ; Kararnameni → düzeltme gerekmez
    name = re.sub(r"eliğ$", "elik", name)
    return name


def detect_change_type(title: str, doc_type: str) -> str:
    t = tr_lower(title)
    if "yürürlükten kaldırılmas" in t:
        return "YURURLUKTEN_KALDIRMA"
    if re.search(r"değişiklik yapılmas|değiştirilmesine dair|değişiklik yapılmasına ilişkin", t):
        return "DEGISIKLIK"
    return {
        "KANUN": "YENI_KANUN",
        "YONETMELIK": "YENI_YONETMELIK",
        "TEBLIG": "YENI_TEBLIG",
        "GENELGE": "YENI_GENELGE",
        "KURUL_KARARI": "KURUL_KARARI",
        "CB_KARARI": "IDARI_KARAR",
        "KARAR": "IDARI_KARAR",
        "CB_KARARNAMESI": "CB_KARARNAMESI",
        "AYM_KARARI": "YARGI_KARARI",
        "YARGI_KARARI": "YARGI_KARARI",
        "ANDLASMA": "ULUSLARARASI_ANLASMA",
        "DUYURU": "KURUM_DUYURUSU",
    }.get(doc_type, "DIGER")


def extract_amended_regulation(title: str, text: str = "") -> tuple[str | None, str | None]:
    """(değiştirilen/kaldırılan mevzuatın adı, ilk yayımlandığı RG atfı)."""
    name = None
    for rx in (_AMEND_TITLE_RE, _REPEAL_TITLE_RE):
        m = rx.search(normalize_space(title))
        if m and m.group("target"):
            name = _fix_stem(m.group("target"))
            break
    ref = None
    m = _RG_REF_RE.search(text[:5000])
    if m:
        ref = f"{m.group(1)} tarihli ve {m.group(2)} sayılı Resmî Gazete"
    return name, ref


_ORD = r"(?:['’]?\s*(?:inci|ıncı|uncu|üncü|nci|ncı|ncu|ncü))"
_ARTICLE_RE = re.compile(
    rf"(?P<kind>[Gg]eçici\s+|[Ee]k\s+)?(?P<no>\d+){_ORD}\s+madde(?:si|lerinin|leri|sinin|sine|sinde|ye)?",
)
_VERB_RE = re.compile(
    r"(?P<v>değiştirilmiştir|eklenmiştir|ilave edilmiştir|yürürlükten kaldırılmıştır|kaldırılmıştır|"
    r"yeniden düzenlenmiştir|teselsül ettirilmiştir)",
    re.I,
)
_ARTICLE_SPLIT_RE = re.compile(r"(?<!GEÇİCİ )(?<!Geçici )(?<!EK )(?<!Ek )(?=\bMADDE\s+\d+\s*[–\-—])")
_QUOTED_NEW_ARTICLE_RE = re.compile(r"(?P<kind>GEÇİCİ|Geçici|EK|Ek)\s+MADDE\s+(?P<no>\d+)\s*[–\-—]")


def split_articles(text: str) -> list[str]:
    parts = _ARTICLE_SPLIT_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


def extract_amended_articles(text: str) -> list[dict]:
    """Değiştiren mevzuatın maddelerinden hedef maddeleri ve işlem türünü çıkarır."""
    results: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for block in split_articles(text):
        verbs = [tr_lower(m.group("v")) for m in _VERB_RE.finditer(block)]
        if not verbs:
            continue
        action = (
            "yürürlükten kaldırıldı" if any("kaldırıl" in v for v in verbs)
            else "eklendi" if any(v in ("eklenmiştir", "ilave edilmiştir") for v in verbs) and not any("değiştiril" in v for v in verbs)
            else "değiştirildi"
        )
        # "Bu Yönetmeliğin 3 üncü maddesi ..." gibi kendine atıfları atla
        cleaned = re.sub(r"\bBu\s+(Yönetmeliğin|Tebliğin|Kanunun|Kararın|Kararnamenin|Genelgenin)\b[^.]*", "", block)
        if action == "eklendi":
            q = _QUOTED_NEW_ARTICLE_RE.search(block)
            if q:
                label = ("Geçici Madde" if q.group("kind").lower().startswith("g") else "Ek Madde") + f" {q.group('no')}"
                if (label, action) not in seen:
                    seen.add((label, action))
                    results.append({"madde": label, "islem": action})
                continue
        for m in _ARTICLE_RE.finditer(cleaned[:600]):
            kind = (m.group("kind") or "").strip().lower()
            label = {"geçici": "Geçici Madde", "ek": "Ek Madde"}.get(kind, "Madde") + f" {m.group('no')}"
            key = (label, action)
            if key not in seen:
                seen.add(key)
                results.append({"madde": label, "islem": action})
            break  # her değişiklik maddesinin ilk hedef atfı yeterli
    return results


def extract_aspects(text: str, change_type: str) -> list[str]:
    t = tr_lower(text)
    aspects: list[str] = []
    if change_type == "DEGISIKLIK" or "değiştirilmiştir" in t:
        aspects.append("madde_degisikligi")
    if "eklenmiştir" in t or "ilave edilmiştir" in t:
        aspects.append("madde_eklenmesi")
    if "yürürlükten kaldırılmıştır" in t or change_type == "YURURLUKTEN_KALDIRMA":
        aspects.append("yururlukten_kaldirma")
    if re.search(r"idari para ceza|para cezası|cezalandırılır|yaptırım uygulanır|faaliyetten men|belgesi iptal", t):
        aspects.append("yaptirim")
    if re.search(r"geçici madde|geçiş süre|uyum sağla|istisna", t):
        aspects.append("gecis_suresi_istisna")
    return aspects


# ---- Yürürlük ve uyum tarihleri --------------------------------------------

@dataclass
class EffectiveDate:
    kind: str                 # yururluk | uyum_suresi
    provision: str            # "Tüm hükümler", "Madde 7", "Diğer hükümler"
    date: str | None          # ISO tarih veya None
    basis: str                # kesin | hesaplanan | belirsiz
    source_text: str = ""
    note: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


_DATE_NUM = r"(\d{1,2})[./](\d{1,2})[./](\d{4})"
_DATE_LONG = r"(\d{1,2})\s+(Ocak|Şubat|Mart|Nisan|Mayıs|Haziran|Temmuz|Ağustos|Eylül|Ekim|Kasım|Aralık)\s+(\d{4})"
_NUM_WORDS = r"(\d+|[a-zçğıöşü]+(?:\s+[a-zçğıöşü]+)?)"
_UNIT = r"(gün|ay|yıl)"


def _parse_explicit(fragment: str) -> date | None:
    m = re.search(_DATE_NUM, fragment)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    m = re.search(_DATE_LONG, fragment, re.I)
    if m:
        try:
            return date(int(m.group(3)), TR_MONTHS[tr_lower(m.group(2))], int(m.group(1)))
        except (ValueError, KeyError):
            return None
    return None


def _add(base: date, n: int, unit: str) -> date:
    if unit == "gün":
        return base + timedelta(days=n)
    if unit == "ay":
        return base + relativedelta(months=n)
    return base + relativedelta(years=n)


def resolve_date_phrase(fragment: str, pub: date | None) -> tuple[date | None, str, str]:
    """Bir yürürlük ifadesini (tarih, dayanak, not) olarak çözer. Tahmin yapmaz."""
    f = tr_lower(normalize_space(fragment))
    explicit_before = re.search(rf"({_DATE_NUM}|{_DATE_LONG})\s+tarihinde", fragment, re.I)
    if explicit_before:
        d = _parse_explicit(explicit_before.group(0))
        if d:
            return d, "kesin", ""
    m = re.search(rf"yayım(?:ı|ını|lanma)?\s*(?:tarihinden|tarihini izleyen günden)?\s*itibaren\s+{_NUM_WORDS}\s+{_UNIT}\s+sonra", f)
    if not m:
        m = re.search(rf"yayım(?:ı|ını)\s+(?:tarihinden|izleyen)\s+{_NUM_WORDS}\s+{_UNIT}\s+sonra", f)
    if m:
        n = parse_tr_number(m.group(1))
        if n is not None and pub:
            return _add(pub, n, m.group(2)), "hesaplanan", f"Yayım tarihine {n} {m.group(2)} eklenerek hesaplandı"
        return None, "belirsiz", "Süre ifadesi çözülemedi"
    if re.search(r"yayımını izleyen (ayın başında|aybaşında|ay başında)", f):
        if pub:
            first_next = (pub.replace(day=1) + relativedelta(months=1))
            return first_next, "hesaplanan", "Yayımı izleyen ayın ilk günü"
    if re.search(r"yayımını izleyen yılın başında", f):
        if pub:
            return date(pub.year + 1, 1, 1), "hesaplanan", "Yayımı izleyen yılın ilk günü"
    if re.search(r"yayımını (izleyen|takip eden) gün", f):
        if pub:
            return pub + timedelta(days=1), "hesaplanan", "Yayımı izleyen gün"
    if re.search(r"yayım(ı|landığı)\s+tarih(inde|te)", f):
        if pub:
            return pub, "kesin", "Yayımı tarihinde"
        return None, "belirsiz", "Yayım tarihi bilinmiyor"
    m = re.search(rf"({_DATE_NUM}|{_DATE_LONG})\s+tarihinden\s+(?:geçerli|itibaren)", fragment, re.I)
    if m:
        d = _parse_explicit(m.group(0))
        if d:
            return d, "kesin", "Belirtilen tarihten geçerli"
    return None, "belirsiz", "Yürürlük ifadesi otomatik çözülemedi; metne bakınız"


def _provision_label(fragment: str) -> str:
    f = normalize_space(fragment)
    m = re.search(rf"(?:(?P<kind>[Gg]eçici|[Ee]k)\s+)?(?P<no>\d+){_ORD}\s+madde", f)
    if m and not re.search(r"\bdiğer\b", tr_lower(f[: m.start()])):
        kind = (m.group("kind") or "").lower()
        prefix = {"geçici": "Geçici Madde", "ek": "Ek Madde"}.get(kind, "Madde")
        nos = re.findall(rf"(\d+){_ORD}", f[: f.find("tarih") if "tarih" in f else len(f)])
        if len(nos) > 1:
            return f"{prefix} " + ", ".join(dict.fromkeys(nos))
        return f"{prefix} {m.group('no')}"
    if re.search(r"diğer (hüküm|madde)", tr_lower(f)):
        return "Diğer hükümler"
    return "Tüm hükümler"


_BENT_SPLIT_RE = re.compile(r"(?:^|\s|;|,)(?=[a-zçğ]\)\s)")


def extract_effective_dates(text: str, pub: date | None) -> list[EffectiveDate]:
    results: list[EffectiveDate] = []
    blocks = split_articles(text)
    yur_blocks = [b for b in blocks if re.search(r"yürürlüğe\s+gir", tr_lower(b))]
    # Ana yürürlük maddesi genellikle sonda; "Yürürlük" başlıklı olanı tercih et
    for block in yur_blocks:
        low = tr_lower(block)
        # Geçici maddelerdeki "yürürlüğe girdiği tarihten itibaren" ifadeleri yürürlük hükmü değildir
        if not re.search(r"yürürlüğe\s+girer|yürürlüğe\s+girecek", low):
            continue
        body = re.sub(r"^MADDE\s+\d+\s*[–\-—]\s*(\(\d+\)\s*)?", "", block).strip()
        body = re.sub(r"\s*Yürütme\s*$", "", body)
        sentences = [s for s in re.split(r"(?<=girer)\.?\s+", body) if "yürürlüğe" in tr_lower(s) or re.search(r"[a-zçğ]\)\s", s)]
        for sentence in sentences or [body]:
            parts = [p for p in _BENT_SPLIT_RE.split(sentence) if p.strip()]
            bent_parts = [p for p in parts if re.match(r"\s*[a-zçğ]\)\s", p)]
            if len(bent_parts) >= 2:
                targets = bent_parts
            else:
                targets = [c for c in re.split(r",\s*(?=diğer\s)|;\s+", sentence) if c.strip()]
            for part in targets:
                d, basis, note = resolve_date_phrase(part, pub)
                results.append(
                    EffectiveDate(
                        kind="yururluk",
                        provision=_provision_label(part),
                        date=d.isoformat() if d else None,
                        basis=basis,
                        source_text=normalize_space(part)[:400],
                        note=note,
                    )
                )
    results.extend(_extract_deadlines(text, pub, results))
    return _dedupe(results)


def _deadline_label(block: str) -> str:
    m = _QUOTED_NEW_ARTICLE_RE.search(block)
    if m:
        return ("Geçici Madde" if m.group("kind").lower().startswith("g") else "Ek Madde") + f" {m.group('no')}"
    m = re.match(r"\s*MADDE\s+(\d+)", block)
    return f"Değiştiren metnin {m.group(1)}. maddesi" if m else "İlgili hüküm"


def _extract_deadlines(text: str, pub: date | None, effective: list[EffectiveDate]) -> list[EffectiveDate]:
    """Geçiş/uyum süreleri: 'yürürlüğe girdiği tarihten itibaren altı ay içinde', '31/12/2027 tarihine kadar'."""
    out: list[EffectiveDate] = []
    base = None
    dated = [e for e in effective if e.kind == "yururluk" and e.date]
    if len(dated) == 1 or (dated and len({e.date for e in dated}) == 1):
        base = date.fromisoformat(dated[0].date)
    for block in split_articles(text):
        low = tr_lower(block)
        for m in re.finditer(rf"(?:yürürlüğe girdiği|yayımı)\s+tarih(?:ten|inden)\s+itibaren\s+{_NUM_WORDS}\s+{_UNIT}\s+içinde", low):
            n = parse_tr_number(m.group(1))
            start = base if "yürürlüğe" in m.group(0) else pub
            snippet = normalize_space(block[max(0, m.start() - 150): m.end() + 150])
            if n is not None and start:
                out.append(EffectiveDate("uyum_suresi", _deadline_label(block), _add(start, n, m.group(2)).isoformat(),
                                         "hesaplanan", snippet, f"{n} {m.group(2)} süre; başlangıç {start.isoformat()}"))
            else:
                out.append(EffectiveDate("uyum_suresi", _deadline_label(block), None, "belirsiz", snippet,
                                         "Süre başlangıcı metinden kesinleştirilemedi"))
        for m in re.finditer(rf"({_DATE_NUM}|{_DATE_LONG})\s+tarihine\s+kadar", block, re.I):
            d = _parse_explicit(m.group(0))
            if d:
                snippet = normalize_space(block[max(0, m.start() - 150): m.end() + 120])
                out.append(EffectiveDate("uyum_suresi", _deadline_label(block), d.isoformat(), "kesin", snippet, ""))
    return out


def _dedupe(items: list[EffectiveDate]) -> list[EffectiveDate]:
    seen = set()
    out = []
    for e in items:
        key = (e.kind, e.provision, e.date, e.basis)
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def summarize_effective(items: list[dict]) -> tuple[date | None, str]:
    """Rapor için ana yürürlük tarihi ve durum: kesin | hesaplanan | kismi | belirsiz."""
    yur = [i for i in items if i.get("kind") == "yururluk"]
    if not yur:
        return None, "belirsiz"
    dated = [i for i in yur if i.get("date")]
    if not dated:
        return None, "belirsiz"
    dates = sorted({i["date"] for i in dated})
    first = date.fromisoformat(dates[0])
    if len(dated) < len(yur):
        return first, "kismi"
    if len(dates) > 1:
        return first, "farkli_tarihler"
    if any(i["basis"] == "hesaplanan" for i in dated):
        return first, "hesaplanan"
    return first, "kesin"


@dataclass
class Extraction:
    change_type: str
    amended_regulation: str | None
    amended_regulation_ref: str | None
    amended_articles: list[dict] = field(default_factory=list)
    aspects: list[str] = field(default_factory=list)
    effective_dates: list[dict] = field(default_factory=list)


def extract_all(title: str, doc_type: str, text: str, pub: date | None) -> Extraction:
    ctype = detect_change_type(title, doc_type)
    name, ref = extract_amended_regulation(title, text)
    articles = extract_amended_articles(text) if ctype in ("DEGISIKLIK", "YURURLUKTEN_KALDIRMA") else []
    return Extraction(
        change_type=ctype,
        amended_regulation=name,
        amended_regulation_ref=ref,
        amended_articles=articles,
        aspects=extract_aspects(text, ctype),
        effective_dates=[e.as_dict() for e in extract_effective_dates(text, pub)] if text else [],
    )
