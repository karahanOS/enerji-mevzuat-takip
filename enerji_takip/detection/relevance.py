"""İlgililik filtresi: hangi belgeler enerji takibi için analiz edilmeye değer?

İki aşamalıdır ve maliyeti düşük tutar:
  1) Başlık taraması (ücretsiz): taksonomi kökleri + olumsuz kalıplar
  2) Belirsiz belgelerde tam metin taraması; gerekirse ucuz bir LLM ön eleme
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from ..text_utils import normalize_for_match

ALAKALI = "alakali"
BELIRSIZ = "belirsiz"
ALAKASIZ = "alakasiz"
ATLANDI = "atlandi"
BASLANGIC = "baslangic"


@dataclass
class RelevanceResult:
    decision: str
    score: float = 0.0
    categories: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class _Category:
    key: str
    label: str
    core: bool
    weak: bool
    patterns: list[tuple[str, re.Pattern]]
    law_patterns: list[tuple[str, re.Pattern]]


def _stem_pattern(stem: str) -> re.Pattern:
    whole = stem.startswith("=")
    s = normalize_for_match(stem.lstrip("="))
    body = re.escape(s).replace(r"\ ", r"\s+")
    if whole:
        return re.compile(rf"(?<!\w){body}(?!\w)")
    return re.compile(rf"(?<!\w){body}")


class Taxonomy:
    def __init__(self, data: dict):
        self.categories: list[_Category] = []
        for key, cat in (data.get("categories") or {}).items():
            self.categories.append(
                _Category(
                    key=key,
                    label=cat.get("label", key),
                    core=bool(cat.get("core")),
                    weak=bool(cat.get("weak")),
                    patterns=[(s.lstrip("="), _stem_pattern(s)) for s in cat.get("stems", [])],
                    law_patterns=[
                        (f"{n} sayılı", re.compile(rf"(?<!\d){n}\s+sayılı"))
                        for n in cat.get("law_numbers", [])
                    ],
                )
            )
        self.negative = [(p, _stem_pattern(p)) for p in data.get("negative_title_patterns", [])]
        self.skip_sections = set(data.get("skip_sections", []))
        self.text_check_types = set(data.get("text_check_doc_types", []))
        self.text_min_hits = int(data.get("text_min_distinct_hits", 3))
        routine = data.get("routine") or {}
        self.routine_doc_types = set(routine.get("doc_types", []))
        self.routine_max_chars = int(routine.get("max_chars", 6000))
        self.routine_company = [_stem_pattern(p) for p in routine.get("company_markers", [])]
        self.routine_patterns = [(p, _stem_pattern(p)) for p in routine.get("patterns", [])]
        self.routine_blockers = [(p, _stem_pattern(p)) for p in routine.get("market_wide_markers", [])]

    @classmethod
    def load(cls, path: Path) -> "Taxonomy":
        return cls(yaml.safe_load(path.read_text(encoding="utf-8")) or {})

    def label(self, key: str) -> str:
        for c in self.categories:
            if c.key == key:
                return c.label
        return key

    def is_core(self, key: str) -> bool:
        return any(c.key == key and c.core for c in self.categories)

    def match(self, text: str) -> dict[str, set[str]]:
        norm = normalize_for_match(text)
        hits: dict[str, set[str]] = {}
        for cat in self.categories:
            for stem, pat in cat.patterns + cat.law_patterns:
                if pat.search(norm):
                    hits.setdefault(cat.key, set()).add(stem)
        return hits

    def _strong(self, hits: dict[str, set[str]]) -> dict[str, set[str]]:
        weak = {c.key for c in self.categories if c.weak}
        return {k: v for k, v in hits.items() if k not in weak}

    def assess_title(self, title: str, doc_type: str, section: str | None, *, skip_reason: str | None = None,
                     is_gazette: bool = True) -> RelevanceResult:
        if skip_reason or (section and section in self.skip_sections):
            return RelevanceResult(ATLANDI, reason=skip_reason or f"{section} bölümü analiz dışı")
        hits = self.match(title)
        strong = self._strong(hits)
        norm_title = normalize_for_match(title)
        negatives = [p for p, pat in self.negative if pat.search(norm_title)]
        keywords = sorted({k for v in hits.values() for k in v})
        cats = sorted(strong) or sorted(hits)
        score = 3.0 * sum(len(v) for v in strong.values()) + 1.0 * sum(
            len(v) for k, v in hits.items() if k not in strong
        )
        if strong and not negatives:
            return RelevanceResult(ALAKALI, score, cats, keywords, "Başlıkta ilgili konu kökleri bulundu")
        if strong and negatives:
            return RelevanceResult(BELIRSIZ, score, cats, keywords,
                                   f"Başlıkta hem ilgili kök hem olumsuz kalıp var ({', '.join(negatives)})")
        if negatives:
            return RelevanceResult(ALAKASIZ, score, cats, keywords, f"Başlık olumsuz kalıba uyuyor ({', '.join(negatives)})")
        if hits:
            return RelevanceResult(BELIRSIZ, score, cats, keywords, "Başlıkta yalnızca zayıf sinyal var")
        if is_gazette and doc_type in self.text_check_types:
            return RelevanceResult(BELIRSIZ, 0.0, [], [], "Başlık konu belirtmiyor; metin kontrol edilecek")
        return RelevanceResult(ALAKASIZ, 0.0, [], [], "Başlıkta ilgili konu bulunamadı")

    def is_routine(self, text: str, doc_type: str) -> tuple[bool, str]:
        """Tek bir şirkete/tesise özgü rutin kurul kararı mı? (lisans tadili, süre uzatımı vb.)"""
        if doc_type not in self.routine_doc_types or not text or len(text) > self.routine_max_chars:
            return False, ""
        norm = normalize_for_match(text)
        if not any(p.search(norm) for p in self.routine_company):
            return False, ""
        blockers = [b for b, pat in self.routine_blockers if pat.search(norm)]
        if blockers:
            return False, ""
        hits = [p for p, pat in self.routine_patterns if pat.search(norm)]
        if not hits:
            return False, ""
        return True, "Tek şirkete özgü karar: " + ", ".join(hits[:3])

    def assess_text(self, text: str, prior: RelevanceResult) -> RelevanceResult:
        hits = self.match(text)
        strong = self._strong(hits)
        distinct = sum(len(v) for v in strong.values())
        law_hits = sum(1 for v in strong.values() for k in v if k.endswith("sayılı"))
        core_hits = sum(len(v) for k, v in strong.items() if self.is_core(k))
        keywords = sorted(set(prior.keywords) | {k for v in hits.values() for k in v})
        cats = sorted(set(prior.categories) | set(strong))
        score = prior.score + distinct + 2 * law_hits
        if distinct >= self.text_min_hits or (law_hits >= 1 and core_hits >= 1) or core_hits >= 2:
            return RelevanceResult(ALAKALI, score, cats, keywords,
                                   f"Metinde {distinct} farklı ilgili kök bulundu")
        if distinct >= 1:
            return RelevanceResult(BELIRSIZ, score, cats, keywords,
                                   f"Metinde zayıf eşleşme ({distinct} kök); ön eleme gerekli")
        return RelevanceResult(ALAKASIZ, score, cats, keywords, "Metinde ilgili konu kökü bulunamadı")


@lru_cache(maxsize=4)
def load_taxonomy(path: str) -> Taxonomy:
    return Taxonomy.load(Path(path))
