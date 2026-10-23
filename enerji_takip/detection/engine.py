"""Değişiklik tespit motoru: yeni belgeleri kaydeder, tekilleştirir, ilgilileri değişiklik kaydına çevirir."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable
from urllib.parse import urlparse, urlunparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..http import Fetcher
from ..models import RegulatoryChange, RegulatoryDocument, RegulatorySource, utcnow
from ..sources.base import FetchedDocument, SourceAdapter
from ..text_utils import normalize_for_match, normalize_title, sha256
from .extract import extract_all, summarize_effective
from .relevance import ALAKALI, ALAKASIZ, ATLANDI, BASLANGIC, BELIRSIZ, RelevanceResult, Taxonomy

log = logging.getLogger(__name__)

OFFICIAL_DOMAIN_SUFFIXES = (".gov.tr",)

TriageFn = Callable[[str, str], tuple[str, str]]  # (başlık, metin) → (karar, gerekçe)


@dataclass
class DetectionStats:
    seen: int = 0
    new_documents: int = 0
    new_changes: int = 0
    skipped: int = 0
    irrelevant: int = 0
    duplicates: int = 0
    baseline: int = 0
    text_failures: int = 0
    new_change_ids: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def canonical_url(url: str) -> str:
    p = urlparse(url.strip())
    path = p.path.rstrip("/") or "/"
    return urlunparse(("https", p.netloc.lower().removeprefix("www."), path, "", p.query, ""))


def document_dedup_key(fd: FetchedDocument) -> str:
    if fd.source_key == "resmi_gazete":
        return sha256(f"rg|{fd.external_id}")
    return sha256(f"url|{canonical_url(fd.url)}")


def change_dedup_key(doc: RegulatoryDocument, change_type: str) -> str:
    """Aynı belge = aynı değişiklik. Aynı mevzuatta sonradan yayımlanan yeni değişiklik
    farklı bir belge (farklı RG sayısı/adres) olduğundan ayrı kayıt oluşturur."""
    return sha256(f"chg|{doc.dedup_key}|{change_type}")


def is_official_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host.endswith(OFFICIAL_DOMAIN_SUFFIXES)


_DECISION_NO_RE = re.compile(r"(?<!\d)(\d{4,6})(?:-\d+)?\s+sayılı\s+(?:kurul\s+)?karar", re.I)


def decision_numbers(title: str) -> set[str]:
    """'… Tarihli ve 15000 Sayılı Kararı' / '15000-3 Sayılı Kurul Kararı' → {'15000'}"""
    return set(_DECISION_NO_RE.findall(normalize_for_match(title)))


def find_cross_source_duplicate(session: Session, doc: RegulatoryDocument) -> RegulatoryChange | None:
    """Kurum sitesindeki bir duyuru, son 10 günde tespit edilmiş bir düzenlemeyle aynı başlığı taşıyorsa
    ya da aynı kurul kararı numarasına (başlıkta veya metnin başında) atıf yapıyorsa tek kayıt tutulur."""
    norm = normalize_title(doc.title)
    numbers = decision_numbers(doc.title)
    if doc.rg_date is None and doc.full_text:
        # Kurum duyurusu metninde atıf yapılan kurul kararı (ör. "15000 sayılı kararı uyarınca")
        numbers |= decision_numbers(doc.full_text[:2000])
    if len(norm) < 25 and not numbers:
        return None
    since = utcnow() - timedelta(days=10)
    candidates = session.scalars(
        select(RegulatoryChange)
        .join(RegulatoryDocument)
        .where(RegulatoryDocument.id != doc.id, RegulatoryChange.first_detected_at >= since)
    )
    for chg in candidates:
        other = normalize_title(chg.document.title)
        if len(norm) >= 25 and (other == norm or (len(norm) > 40 and (norm in other or other in norm))):
            return chg
        if numbers and numbers & decision_numbers(chg.document.title):
            return chg
    return None


def routine_gist(text: str) -> str:
    """Rutin kararın tek cümlelik özü (ilk 'karar verilmiştir' cümlesi)."""
    sentences = re.split(r"(?<=[.;])\s+", re.sub(r"\s+", " ", text))
    for s in sentences:
        low = s.lower()
        if any(k in low for k in ("verilmesine", "tadil", "uzatılmasına", "sona erdiril", "iptal", "karar veril")):
            return s.strip()[:300]
    return sentences[0].strip()[:300] if sentences else ""


class ChangeDetectionEngine:
    def __init__(self, session: Session, fetcher: Fetcher, taxonomy: Taxonomy, triage: TriageFn | None = None):
        self.session = session
        self.fetcher = fetcher
        self.taxonomy = taxonomy
        self.triage = triage

    def process(self, source: RegulatorySource, adapter: SourceAdapter, docs: list[FetchedDocument],
                *, baseline: bool = False) -> DetectionStats:
        stats = DetectionStats()
        seen_keys: set[str] = set()
        for fd in docs:
            stats.seen += 1
            key = document_dedup_key(fd)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            existing = self.session.scalar(select(RegulatoryDocument).where(RegulatoryDocument.dedup_key == key))
            if existing is not None:
                existing.last_checked_at = utcnow()
                for chg in existing.changes:
                    chg.last_checked_at = utcnow()
                continue
            # Her belge kendi işlemiyle kaydedilir: bir belgedeki hata öncekileri geri almaz,
            # sonrakilerin işlenmesini de engellemez.
            try:
                self._process_new(source, adapter, fd, key, baseline, stats)
                self.session.commit()
            except Exception as exc:
                log.exception("Belge işlenemedi: %s", fd.url)
                self.session.rollback()
                stats.errors.append(f"Belge işlenemedi ({fd.url}): {exc}")
        self.session.commit()
        return stats

    def _process_new(self, source, adapter, fd: FetchedDocument, key: str, baseline: bool, stats: DetectionStats):
        doc = RegulatoryDocument(
            source_id=source.id,
            dedup_key=key,
            external_id=fd.external_id,
            title=fd.title,
            doc_type=fd.doc_type,
            section=fd.subsection or fd.section,
            institution=fd.institution,
            rg_date=fd.rg_date,
            rg_number=fd.rg_number,
            is_mukerrer=fd.is_mukerrer,
            published_date=fd.published_date or fd.rg_date,
            url=fd.url,
            pdf_url=fd.pdf_url,
        )
        self.session.add(doc)
        self.session.flush()
        stats.new_documents += 1

        if baseline:
            doc.is_baseline = True
            doc.relevance = BASLANGIC
            doc.relevance_reason = "İlk taramada listede bulunan kayıt (başlangıç); raporlanmaz"
            stats.baseline += 1
            return

        rel = self.taxonomy.assess_title(
            fd.title, fd.doc_type, fd.section, skip_reason=fd.skip_reason,
            is_gazette=(fd.source_key == "resmi_gazete"),
        )
        if rel.decision in (ATLANDI, ALAKASIZ):
            self._apply_relevance(doc, rel)
            stats.skipped += rel.decision == ATLANDI
            stats.irrelevant += rel.decision == ALAKASIZ
            return

        # Tam metni al (hem belirsizleri çözmek hem de analiz için)
        dtext = adapter.fetch_text(self.fetcher, fd)
        doc.full_text = dtext.text or None
        doc.text_hash = sha256(dtext.text) if dtext.text else None
        doc.text_status = dtext.status
        doc.fetch_error = dtext.note if dtext.status != "tam" else None
        if dtext.institution and not doc.institution:
            doc.institution = dtext.institution
        if dtext.status == "okunamadi":
            stats.text_failures += 1

        review_reasons: list[str] = []
        if rel.decision == ALAKALI and dtext.text:
            # Başlıktan ilgili bulunan belgede kategorileri metinle zenginleştir (ör. EPDK kararı → elektrik piyasası)
            text_hits = self.taxonomy._strong(self.taxonomy.match(dtext.text))
            rel.categories = sorted(set(rel.categories) | set(text_hits))
            rel.keywords = sorted(set(rel.keywords) | {k for v in text_hits.values() for k in v})
        if rel.decision == BELIRSIZ:
            if dtext.text:
                rel = self.taxonomy.assess_text(dtext.text, rel)
            if rel.decision == BELIRSIZ:
                rel, note = self._triage(fd.title, dtext.text, rel)
                if note:
                    review_reasons.append(note)
        self._apply_relevance(doc, rel)
        if rel.decision != ALAKALI:
            stats.irrelevant += 1
            return

        dup = find_cross_source_duplicate(self.session, doc)
        if dup is not None:
            doc.relevance = "kopya"
            doc.relevance_reason = f"Aynı düzenleme başka kaynakta zaten tespit edildi (değişiklik {dup.id})"
            stats.duplicates += 1
            return

        routine, routine_why = self.taxonomy.is_routine(dtext.text or "", fd.doc_type)
        ext = extract_all(fd.title, fd.doc_type, dtext.text or "", doc.published_date)
        eff_date, eff_status = summarize_effective(ext.effective_dates)
        if dtext.status == "okunamadi":
            review_reasons.append("Mevzuat metni okunamadı; özet yalnızca başlığa dayanıyor")
        elif dtext.status == "kisa":
            review_reasons.append("Mevzuat metni eksik/kısa olabilir")

        chg = RegulatoryChange(
            document_id=doc.id,
            dedup_key=change_dedup_key(doc, ext.change_type),
            change_type=ext.change_type,
            change_aspects=ext.aspects,
            amended_regulation=ext.amended_regulation,
            amended_regulation_ref=ext.amended_regulation_ref,
            amended_articles=ext.amended_articles,
            effective_dates=ext.effective_dates,
            effective_date=eff_date,
            effective_date_status=eff_status,
            categories=rel.categories,
            status="rutin" if routine else "yeni",
            needs_review=bool(review_reasons) and not routine,
            review_reasons=[] if routine else review_reasons,
            note=routine_gist(dtext.text or "") if routine else None,
            importance="DUSUK" if routine else None,
            importance_reasons=[routine_why] if routine else [],
            url_verified=dtext.status != "okunamadi" and is_official_url(fd.url),
        )
        if self.session.scalar(select(RegulatoryChange).where(RegulatoryChange.dedup_key == chg.dedup_key)):
            stats.duplicates += 1
            return
        self.session.add(chg)
        self.session.flush()
        stats.new_changes += 1
        stats.new_change_ids.append(chg.id)

    def _triage(self, title: str, text: str, rel: RelevanceResult) -> tuple[RelevanceResult, str | None]:
        if self.triage is None:
            core_hit = any(self.taxonomy.is_core(c) for c in rel.categories)
            if core_hit:
                rel.decision = ALAKALI
                rel.reason += " | AI ön eleme kapalı; ana faaliyet kökü bulunduğu için dahil edildi"
                return rel, "İlgililik belirsiz (AI ön eleme yapılmadı)"
            rel.decision = ALAKASIZ
            rel.reason += " | AI ön eleme kapalı; ana faaliyet kökü yok"
            return rel, None
        try:
            decision, why = self.triage(title, text)
        except Exception as exc:  # AI hatası belgeyi kaybettirmesin
            log.warning("AI ön eleme başarısız: %s", exc)
            rel.decision = ALAKALI
            rel.reason += f" | AI ön eleme başarısız ({exc}); güvenli tarafta kalınarak dahil edildi"
            return rel, "İlgililik belirsiz (AI ön eleme hatası)"
        if decision == "hayir":
            rel.decision = ALAKASIZ
            rel.reason += f" | AI ön eleme: ilgisiz — {why}"
            return rel, None
        rel.decision = ALAKALI
        rel.reason += f" | AI ön eleme: {decision} — {why}"
        return rel, ("İlgililik AI tarafından 'belirsiz' bulundu" if decision == "belirsiz" else None)

    @staticmethod
    def _apply_relevance(doc: RegulatoryDocument, rel: RelevanceResult) -> None:
        doc.relevance = rel.decision
        doc.relevance_score = rel.score
        doc.matched_categories = rel.categories
        doc.matched_keywords = rel.keywords
        doc.relevance_reason = rel.reason
