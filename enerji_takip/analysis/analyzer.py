"""Mevzuat analiz modülü: ücretsiz LLM zinciri, şema doğrulama, yeniden deneme, kaynağa bağlılık kontrolü."""
from __future__ import annotations

import logging
import re
import time
from datetime import date

import yaml
from pydantic import ValidationError
from sqlalchemy.orm import Session

from ..config import Settings
from ..detection.extract import DOC_TYPE_LABELS
from ..models import AIAnalysis, RegulatoryChange
from ..text_utils import TR_MONTH_NAMES, normalize_title
from .llm import LLMChain, LLMError, LLMQuotaError, Message
from .prompts import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    TRIAGE_SYSTEM,
    build_analysis_prompt,
    build_retry_prompt,
    build_triage_prompt,
    extract_json,
    truncate_for_llm,
)
from .schema import AnalysisResult, TriageResult, map_to_known

log = logging.getLogger(__name__)

MAX_ANALYSIS_ATTEMPTS_TOTAL = 6  # çalıştırmalar arası toplam
# Yeniden analizde temizlenecek (AI kaynaklı) inceleme gerekçeleri
AI_REASON_PREFIXES = ("AI ", "AI'", "Alıntı", "Yürürlük tarihi çelişkisi", "Analiz eksik",
                      "Mevzuat metni alınamadığı", "Önem seviyesi")


def load_profile(settings: Settings) -> dict:
    return yaml.safe_load(settings.profile_file.read_text(encoding="utf-8")) or {}


def _date_forms(iso: str) -> list[str]:
    d = date.fromisoformat(iso)
    return [
        f"{d.day}/{d.month}/{d.year}", f"{d.day:02d}/{d.month:02d}/{d.year}",
        f"{d.day}.{d.month}.{d.year}", f"{d.day:02d}.{d.month:02d}.{d.year}",
        f"{d.day} {TR_MONTH_NAMES[d.month]} {d.year}".lower(),
    ]


def _quote_in_text(quote: str, norm_text: str) -> bool:
    q = normalize_title(quote.strip(" \"'“”…."))
    if not q:
        return True
    if q in norm_text:
        return True
    words = q.split()
    if len(words) < 6:
        return False
    shingles = [" ".join(words[i:i + 5]) for i in range(len(words) - 4)]
    found = sum(1 for s in shingles if s in norm_text)
    return found / len(shingles) >= 0.9


def grounding_check(result: AnalysisResult, text: str, published: date | None, rule_dates: list[dict]) -> list[str]:
    """AI çıktısının kaynak metne bağlılığını denetler; sorunları listeler."""
    issues: list[str] = []
    norm_text = normalize_title(text)
    low_text = text.lower()
    for p in result.acik_hukumler:
        if p.alinti and not _quote_in_text(p.alinti, norm_text):
            issues.append(f"Alıntı metinde doğrulanamadı: “{p.alinti[:80]}”")
    for d in result.tarihler:
        if d.kesinlik == "kesin" and d.tarih:
            in_text = any(f.lower() in low_text for f in _date_forms(d.tarih))
            is_pub = published is not None and d.tarih == published.isoformat() and re.search(
                r"yayım(ı|landığı)\s+tarih", low_text)
            if not in_text and not is_pub:
                issues.append(f"AI'nın 'kesin' dediği {d.tarih} tarihi metinde bulunamadı")
    rule_kesin = {r["date"] for r in rule_dates if r.get("kind") == "yururluk" and r.get("basis") == "kesin" and r.get("date")}
    ai_kesin = {d.tarih for d in result.tarihler if d.tur == "yururluk" and d.kesinlik == "kesin" and d.tarih}
    if rule_kesin and ai_kesin and not (rule_kesin & ai_kesin):
        issues.append(f"Yürürlük tarihi çelişkisi: kural tabanlı {sorted(rule_kesin)} ≠ AI {sorted(ai_kesin)}")
    return issues


class RegulatoryAnalyzer:
    def __init__(self, session: Session, analysis_chain: LLMChain | None, settings: Settings, profile: dict,
                 triage_chain: LLMChain | None = None):
        self.session = session
        self.chain = analysis_chain if analysis_chain else None
        self.triage_chain = triage_chain if triage_chain else None
        self.settings = settings
        self.profile = profile

    # ---- ön eleme -------------------------------------------------------
    def triage(self, title: str, text: str) -> tuple[str, str]:
        if self.triage_chain is None:
            raise LLMError("Ön eleme için AI yapılandırılmamış")
        prompt = build_triage_prompt(profile=self.profile, title=title, text=text or "")
        raw, _ = self.triage_chain.complete_any(TRIAGE_SYSTEM, lambda c: [Message("user", prompt)], max_tokens=300)
        result = TriageResult.model_validate(extract_json(raw))
        return result.ilgili_mi, result.gerekce

    # ---- tam analiz -----------------------------------------------------
    def analyze(self, change: RegulatoryChange) -> AnalysisResult | None:
        doc = change.document
        change.review_reasons = [r for r in (change.review_reasons or []) if not r.startswith(AI_REASON_PREFIXES)]
        if self.chain is None:
            change.status = "inceleme"
            self._add_reason(change, "AI analizi yapılandırılmadı; yalnızca kural tabanlı bilgiler var")
            change.needs_review = True
            return None
        if not doc.full_text:
            change.status = "inceleme"
            self._add_reason(change, "Mevzuat metni alınamadığı için AI analizi yapılmadı (eksik belgeden sonuç çıkarılmaz)")
            change.needs_review = True
            return None

        meta = {
            "baslik": doc.title,
            "tur": DOC_TYPE_LABELS.get(doc.doc_type, doc.doc_type),
            "kurum": doc.institution,
            "resmi_gazete_tarihi": doc.rg_date,
            "resmi_gazete_sayisi": doc.rg_number,
            "mukerrer": doc.is_mukerrer,
            "yayim_tarihi": doc.published_date,
            "kaynak_url": doc.url,
        }
        hints = {
            "degisiklik_turu": change.change_type,
            "degistirilen_mevzuat": change.amended_regulation,
            "degistirilen_mevzuat_ilk_yayim": change.amended_regulation_ref,
            "degistirilen_maddeler": change.amended_articles,
            "yururluk_ve_uyum_tarihleri": change.effective_dates,
        }
        failures: list[str] = []
        quota_only = True
        attempts = max(1, self.settings.llm_max_attempts)
        for client in self.chain.clients:
            text, truncated = truncate_for_llm(doc.full_text, client.max_input_chars)
            messages = [Message("user", build_analysis_prompt(profile=self.profile, meta=meta, rule_hints=hints,
                                                              text=text, truncated=truncated))]
            for attempt in range(1, attempts + 1):
                change.analysis_attempts += 1
                started = time.monotonic()
                record = AIAnalysis(provider=client.provider, model=client.model, prompt_version=PROMPT_VERSION,
                                    attempt=change.analysis_attempts, input_chars=len(text), truncated=truncated)
                change.analyses.append(record)
                try:
                    raw = self.chain.call(client, SYSTEM_PROMPT, messages)
                except LLMQuotaError as exc:
                    record.validation_errors = [f"Kota: {exc}"]
                    if "Günlük AI çağrı tavanı" in str(exc):
                        failures.append(str(exc))
                        return self._fail(change, failures, quota=True)
                    failures.append(f"{client.label}: kota doldu")
                    break  # sıradaki sağlayıcıya geç
                except LLMError as exc:
                    record.validation_errors = [f"LLM hatası: {exc}"]
                    failures.append(f"{client.label}: {exc}")
                    quota_only = False
                    break
                finally:
                    record.duration_ms = int((time.monotonic() - started) * 1000)
                record.raw_output = raw
                try:
                    result = AnalysisResult.model_validate(extract_json(raw))
                except (ValueError, ValidationError) as exc:
                    errors = self._errors(exc)
                    record.validation_errors = errors
                    quota_only = False
                    log.warning("%s çıktısı şemaya uymadı (%s/%s): %s", client.label, attempt, attempts, errors[:3])
                    if attempt == attempts:
                        failures.append(f"{client.label}: {attempts} denemede şema doğrulamasından geçmedi")
                    messages = messages + [Message("assistant", raw), Message("user", build_retry_prompt(errors))]
                    continue
                record.parsed = result.model_dump()
                record.is_valid = True
                record.grounding_issues = grounding_check(result, doc.full_text, doc.published_date, change.effective_dates)
                self._apply(change, result, record.grounding_issues, truncated)
                return result
        return self._fail(change, failures, quota=quota_only and bool(failures))

    def _fail(self, change: RegulatoryChange, failures: list[str], *, quota: bool) -> None:
        change.status = "analiz_basarisiz"
        change.needs_review = True
        if quota:
            self._add_reason(change, "AI ücretsiz kotası doldu; analiz bir sonraki çalıştırmada yeniden denenecek ("
                             + "; ".join(failures[:3]) + ")")
        else:
            self._add_reason(change, "AI analizi başarısız: " + "; ".join(failures[:3]))
        return None

    def _apply(self, change: RegulatoryChange, r: AnalysisResult, issues: list[str], truncated: bool) -> None:
        change.departments = map_to_known(r.ilgili_alanlar, self.profile.get("ilgi_alanlari") or []) or r.ilgili_alanlar[:3]
        change.ai_importance = r.onem_onerisi
        aspects = list(dict.fromkeys((change.change_aspects or []) + [a for a in r.degisiklik_nitelikleri if a != "diger"]))
        for flag, key in ((r.yeni_yukumluluk, "yeni_yukumluluk"), (r.yaptirim, "yaptirim"),
                          (r.gecis_suresi_istisna, "gecis_suresi_istisna"),
                          (r.yukumluluk_degisikligi, "yukumluluk_degisikligi")):
            if flag.var_mi == "evet" and key not in aspects:
                aspects.append(key)
        change.change_aspects = aspects
        for issue in issues:
            self._add_reason(change, issue)
        if r.guven == "dusuk":
            self._add_reason(change, "AI analizin güvenini 'düşük' olarak bildirdi")
        if r.metin_eksik_mi or truncated:
            self._add_reason(change, "Analiz eksik/kısaltılmış metne dayanıyor")
        if r.ilgili_mi == "hayir":
            self._add_reason(change, "AI bu düzenlemeyi ilgisiz buldu; filtre sonucu ile çelişiyor")
        change.needs_review = bool(change.review_reasons)
        change.status = "inceleme" if change.needs_review else "analiz_edildi"

    @staticmethod
    def _add_reason(change: RegulatoryChange, reason: str) -> None:
        reasons = list(change.review_reasons or [])
        if reason not in reasons:
            reasons.append(reason)
        change.review_reasons = reasons

    @staticmethod
    def _errors(exc: Exception) -> list[str]:
        if isinstance(exc, ValidationError):
            return [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        return [str(exc)]
