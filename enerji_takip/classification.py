"""Önem sınıflandırma motoru (enerji piyasası bakışıyla).

Önem yalnızca mevzuat türüne göre belirlenmez. Puan; piyasa geneline etki,
yükümlülük ve yaptırım, yürürlüğe kalan süre, ana ilgi alanı (elektrik piyasası,
yenilenebilir) ve AI önerisinden hesaplanır. Her katkı gerekçe olarak saklanır.
Tek bir şirkete özgü rutin kararlar ayrıca "rutin" olarak işaretlenir ve DÜŞÜK kalır.
"""
from __future__ import annotations

from datetime import date

from .detection.relevance import Taxonomy
from .models import RegulatoryChange

LEVEL_ORDER = ["DUSUK", "ORTA", "YUKSEK", "KRITIK"]
LEVEL_LABELS = {"KRITIK": "KRİTİK", "YUKSEK": "YÜKSEK", "ORTA": "ORTA", "DUSUK": "DÜŞÜK"}
IMPACT_POINTS = {"genel": 3, "sektorel": 2, "belirsiz": 1, "sinirli": 0, "yok": -3}
IMPACT_LABELS = {"genel": "Piyasa geneli", "sektorel": "Sektörel", "sinirli": "Sınırlı (tek şirket/proje)",
                 "belirsiz": "Belirsiz", "yok": "Piyasa etkisi yok"}
MARKET_ASPECTS = {"fiyat_tarife_degisikligi", "piyasa_kurali_degisikligi", "destek_tesvik_degisikligi",
                  "lisans_rejimi_degisikligi"}


def _days_until(target: str | date | None, today: date) -> int | None:
    if target is None:
        return None
    d = date.fromisoformat(target) if isinstance(target, str) else target
    return (d - today).days


def classify(change: RegulatoryChange, analysis: dict | None, today: date, taxonomy: Taxonomy) -> tuple[str, int, list[str], bool]:
    """(seviye, puan, gerekçeler, inceleme_gerekli) döndürür."""
    if change.status == "rutin":
        return "DUSUK", 0, ["Tek şirkete/tesise özgü rutin karar (kural tabanlı tespit)"], False

    score = 0
    reasons: list[str] = []
    review = False

    core = [c for c in (change.categories or []) if taxonomy.is_core(c)]
    if core:
        score += 1
        reasons.append("+1 ana ilgi alanı (" + ", ".join(taxonomy.label(c) for c in core) + ")")

    aspects = set(change.change_aspects or [])
    upcoming = [
        _days_until(e.get("date"), today)
        for e in (change.effective_dates or [])
        if e.get("date") and e.get("kind") in ("yururluk", "uyum_suresi")
    ]
    if analysis:
        for t in analysis.get("tarihler", []):
            if t.get("tarih") and t.get("tur") in ("yururluk", "uyum_son_tarihi", "gecis_suresi_sonu"):
                upcoming.append(_days_until(t["tarih"], today))
    upcoming = [d for d in upcoming if d is not None and d >= -1]
    nearest = min(upcoming) if upcoming else None

    if analysis is None:
        # AI yoksa temkinli kural tabanlı sınıflandırma; inceleme istenir
        if core:
            score += 2
            reasons.append("+2 AI analizi yok; ana ilgi alanında düzenleme (temkinli)")
        if change.change_type in ("YENI_KANUN", "YENI_YONETMELIK") or (
                change.change_type == "DEGISIKLIK" and change.amended_regulation and "yönetmelik" in change.amended_regulation.lower()):
            score += 1
            reasons.append("+1 yönetmelik/kanun düzeyinde düzenleme")
        if nearest is not None and nearest <= 30:
            score += 1
            reasons.append(f"+1 yürürlük/uyum tarihine {nearest} gün")
        review = True
        level = "YUKSEK" if score >= 4 else "ORTA" if score >= 2 else "DUSUK"
        return level, score, reasons, review

    impact = (analysis.get("piyasa_etkisi") or {}).get("duzey", "belirsiz")
    points = IMPACT_POINTS.get(impact, 1)
    score += points
    reasons.append(f"{points:+d} piyasa etkisi: {IMPACT_LABELS.get(impact, impact)}")

    market_change = bool(aspects & MARKET_ASPECTS) or bool(set(analysis.get("degisiklik_nitelikleri", [])) & MARKET_ASPECTS)
    if market_change:
        score += 2
        reasons.append("+2 fiyat/tarife, piyasa kuralı, destek veya lisans rejimi değişikliği")

    obligation = (analysis.get("yeni_yukumluluk") or {}).get("var_mi") == "evet" or \
        (analysis.get("yukumluluk_degisikligi") or {}).get("var_mi") == "evet"
    if obligation:
        score += 1
        reasons.append("+1 piyasa katılımcıları için yeni/değişen yükümlülük")
    if (analysis.get("yaptirim") or {}).get("var_mi") == "evet":
        score += 1
        reasons.append("+1 yaptırım öngörülüyor")

    if nearest is not None and impact in ("genel", "sektorel"):
        if nearest <= 7:
            score += 2
            reasons.append(f"+2 {max(nearest, 0)} gün içinde yürürlükte")
        elif nearest <= 30:
            score += 1
            reasons.append(f"+1 {nearest} gün içinde yürürlükte")

    ai_level = analysis.get("onem_onerisi")
    ai_points = {"KRITIK": 2, "YUKSEK": 1, "ORTA": 0, "DUSUK": -1}.get(ai_level, 0)
    if ai_points:
        score += ai_points
        reasons.append(f"{ai_points:+d} AI önerisi: {LEVEL_LABELS.get(ai_level, ai_level)}")

    level = "KRITIK" if score >= 9 else "YUKSEK" if score >= 6 else "ORTA" if score >= 3 else "DUSUK"

    if level == "KRITIK" and impact != "genel":
        level = "YUKSEK"
        reasons.append("KRİTİK için piyasa geneline etki şartı sağlanmadı → YÜKSEK")
    if level == "KRITIK" and not ((nearest is not None and nearest <= 30) or ai_level == "KRITIK"):
        level = "YUKSEK"
        reasons.append("Yürürlüğe " + (f"{nearest} gün var" if nearest is not None else "ilişkin kesin tarih yok")
                       + "; acil değil → YÜKSEK")

    if ai_level in LEVEL_ORDER and abs(LEVEL_ORDER.index(ai_level) - LEVEL_ORDER.index(level)) >= 2:
        review = True
        reasons.append(f"Sistem ({LEVEL_LABELS[level]}) ile AI ({LEVEL_LABELS[ai_level]}) önemi 2+ seviye farklı → inceleme")
    return level, score, reasons, review


def apply_classification(change: RegulatoryChange, analysis: dict | None, today: date, taxonomy: Taxonomy) -> None:
    level, score, reasons, review = classify(change, analysis, today, taxonomy)
    change.importance = level
    change.importance_score = score
    change.importance_reasons = reasons
    if review and analysis is not None:
        rr = list(change.review_reasons or [])
        msg = "Önem seviyesi sistem ile AI arasında belirgin farklı"
        if msg not in rr:
            rr.append(msg)
        change.review_reasons = rr
        change.needs_review = True
        if change.status == "analiz_edildi":
            change.status = "inceleme"
