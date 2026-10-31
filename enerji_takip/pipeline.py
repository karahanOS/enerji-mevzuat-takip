"""Uçtan uca iş akışı: kaynak tarama → değişiklik tespiti → AI analizi → önem → rapor → e-posta.

Güvenilirlik ilkeleri:
  * Bir kaynaktaki hata diğer kaynakların taranmasını engellemez.
  * Eksik tarama raporda açıkça belirtilir; "her şey kontrol edildi" izlenimi verilmez.
  * Gönderilemeyen rapordaki değişiklikler "bildirildi" sayılmaz; bir sonraki rapora girer.
  * Aynı anda iki çalıştırma olmaz (veri tabanı kilidi).
"""
from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from .analysis.analyzer import MAX_ANALYSIS_ATTEMPTS_TOTAL, RegulatoryAnalyzer, load_profile
from .analysis.llm import LLMChain, build_chain
from .classification import apply_classification
from .config import Settings, get_settings
from .db import check_connection, init_db, session_scope, set_setting, get_setting, with_db_retry
from .detection.engine import ChangeDetectionEngine, DetectionStats
from .detection.extract import extract_all, summarize_effective
from .detection.relevance import load_taxonomy
from .http import Fetcher
from .models import (
    EmailDeliveryLog,
    EmailReport,
    EmailReportItem,
    MonitoringLog,
    RegulatoryChange,
    RegulatoryDocument,
    RegulatorySource,
    ScanRun,
    utcnow,
)
from .reporting.builder import ReportInput, SourceStatusRow, build_report, pending_changes, render_admin_alert, to_local
from .reporting.mailer import OutgoingEmail, send_with_retry
from .runtime import KEY_HEARTBEAT_ALERT, KEY_LLM_CALLS_PREFIX, effective_recipients, setup_logging
from .sources.base import FetchedDocument, ScanOutcome
from .sources.registry import build_adapter, sync_sources
from .text_utils import sha256

log = logging.getLogger(__name__)

ACCESS_LABELS = {
    "erisilebilir": "Erişilebilir",
    "erisilemiyor": "Erişilemedi",
    "robots_engeli": "robots.txt engeli",
    "erisim_reddedildi": "Erişim reddedildi",
    "hata": "Hata",
    "bilinmiyor": "Bilinmiyor",
}
_UNSET = object()


@dataclass
class RunResult:
    run_id: str | None = None
    status: str = "basarisiz"
    complete: bool = False
    new_documents: int = 0
    new_changes: int = 0
    analyzed: int = 0
    report_id: str | None = None
    report_status: str | None = None
    report_subject: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def local_today(settings: Settings, now: datetime | None = None) -> date:
    now = now or datetime.now(timezone.utc)
    return now.astimezone(ZoneInfo(settings.timezone)).date()


# ---------------------------------------------------------------------------
def _log(session: Session, run_id: str | None, event: str, message: str, *, level: str = "INFO",
         source_id: int | None = None, details: dict | None = None) -> None:
    getattr(log, level.lower(), log.info)(message)
    session.add(MonitoringLog(scan_run_id=run_id, source_id=source_id, level=level, event=event,
                              message=message[:4000], details=details or {}))


def _acquire_run(session: Session, run_type: str, trigger: str) -> ScanRun | None:
    running = session.scalars(select(ScanRun).where(ScanRun.status == "calisiyor")).all()
    for r in running:
        if r.started_at < utcnow() - timedelta(hours=3):
            r.status = "yarida_kaldi"
            r.finished_at = utcnow()
            r.errors = list(r.errors or []) + ["Çalıştırma 3 saatten uzun sürdü veya süreç kesildi"]
        else:
            return None
    run = ScanRun(run_type=run_type, trigger=trigger, status="calisiyor")
    session.add(run)
    session.commit()
    return run


def send_admin_alert(session: Session | None, settings: Settings, title: str, lines: list[str],
                     details: dict | None = None) -> bool:
    """Sistem yöneticisine hata bildirimi. Veri tabanı yoksa da çalışır (session=None)."""
    if not settings.admin_email:
        log.error("YÖNETİCİ BİLDİRİMİ (ADMIN_EMAIL tanımlı değil, yalnızca günlüğe yazıldı): %s | %s", title, lines)
        return False
    body = render_admin_alert(title, lines, details)
    mail = OutgoingEmail(
        subject=f"[ENERJİ MEVZUAT – SİSTEM] {title}", text=body, html=None, to=settings.admin_email, cc=[]
    )

    def record(attempt: int, status: str, error: str | None) -> None:
        if session is not None:
            session.add(EmailDeliveryLog(kind="yonetici", attempt=attempt,
                                         recipients={"to": settings.admin_email}, status=status, error=error))

    ok = send_with_retry(settings, mail, on_attempt=record)
    if session is not None:
        session.commit()
    return ok


# ---------------------------------------------------------------------------
class Pipeline:
    def __init__(self, settings: Settings | None = None, fetcher: Fetcher | None = None,
                 analysis_chain: LLMChain | None | object = _UNSET, triage_chain: LLMChain | None | object = _UNSET,
                 now: datetime | None = None):
        self.settings = settings or get_settings()
        self.fetcher = fetcher or Fetcher(self.settings)
        self._analysis_chain = analysis_chain
        self._triage_chain = triage_chain
        self.now = now
        self.taxonomy = load_taxonomy(str(self.settings.taxonomy_file))
        self.profile = load_profile(self.settings)
        self.analysis_chain: LLMChain | None = None
        self.triage_chain: LLMChain | None = None

    def _setup_llm(self, session: Session, today: date) -> None:
        """Ücretsiz LLM zincirlerini kurar; günlük çağrı sayacı veri tabanında tutulur (çalıştırmalar arası)."""
        key = f"{KEY_LLM_CALLS_PREFIX}{today.isoformat()}"

        def count() -> int:
            return int(get_setting(session, key) or 0)

        def inc() -> None:
            set_setting(session, key, str(count() + 1))

        for attr, chain_arg, entries in (("analysis_chain", self._analysis_chain, self.settings.llm_analysis_chain),
                                         ("triage_chain", self._triage_chain, self.settings.llm_triage_chain)):
            chain = build_chain(self.settings, entries) if chain_arg is _UNSET else chain_arg
            if chain is not None:
                chain.daily_cap = self.settings.llm_daily_max_calls
                chain._count, chain._inc = count, inc
            setattr(self, attr, chain if chain else None)

    # ---- ana giriş -----------------------------------------------------------
    def run(self, *, run_type: str = "gunluk", trigger: str = "zamanlanmis", send: bool = True,
            today: date | None = None) -> RunResult:
        setup_logging(self.settings)
        result = RunResult()
        try:
            self._ensure_db()
        except Exception as exc:
            msg = f"Veri tabanına bağlanılamadı: {exc}"
            result.errors.append(msg)
            log.exception(msg)
            send_admin_alert(None, self.settings, "Günlük görev başlatılamadı (veri tabanı)", [msg])
            return result

        today = today or local_today(self.settings, self.now)
        with session_scope() as session:
            run = _acquire_run(session, run_type, trigger)
            if run is None:
                result.status = "atlandi"
                result.errors.append("Başka bir tarama çalışıyor; bu çalıştırma atlandı.")
                log.warning(result.errors[-1])
                return result
            result.run_id = run.id
            try:
                self._run(session, run, result, run_type, send, today)
            except Exception as exc:
                session.rollback()
                tb = traceback.format_exc()
                log.exception("Çalıştırma beklenmeyen hatayla durdu")
                result.status = "basarisiz"
                result.errors.append(f"Beklenmeyen hata: {exc}")
                run = session.get(ScanRun, run.id)
                run.status = "basarisiz"
                run.finished_at = utcnow()
                run.errors = list(run.errors or []) + [str(exc), tb[-2000:]]
                session.commit()
                send_admin_alert(session, self.settings, "Günlük görev hata ile sonlandı",
                                 [str(exc)], {"çalıştırma": run.id, "iz": tb[-1500:]})
        return result

    @with_db_retry(attempts=3, base_delay=3.0)
    def _ensure_db(self) -> None:
        check_connection()
        init_db()

    # ---- adımlar -------------------------------------------------------------
    def _run(self, session: Session, run: ScanRun, result: RunResult, run_type: str, send: bool, today: date):
        sources = sync_sources(session, self.settings.sources_file)
        session.commit()
        self._setup_llm(session, today)
        engine = ChangeDetectionEngine(
            session, self.fetcher, self.taxonomy,
            triage=self._triage_fn(session) if (self.triage_chain and self.settings.llm_triage_uncertain) else None,
        )

        totals = DetectionStats()
        source_rows: list[SourceStatusRow] = []
        warnings: list[str] = []
        primary_failures: list[str] = []
        complete = True
        coverage: dict = {}

        for source in sources:
            if not source.enabled:
                continue
            outcome = self._scan_source(session, run, source, today)
            coverage[source.key] = {"tamam": outcome.complete, **outcome.coverage}
            if not outcome.complete:
                complete = False
                label = f"{source.name}: " + "; ".join(outcome.errors[:3])
                warnings.append(label)
                if source.is_primary:
                    primary_failures.extend(outcome.errors)
            warnings.extend(f"{source.name}: {w}" for w in outcome.warnings)

            baseline = source.method in ("html_listing", "rss", "epdk_kararlar") and not source.baseline_done
            stats = engine.process(source, build_adapter(source), outcome.documents, baseline=baseline)
            if baseline and outcome.complete:
                source.baseline_done = True
            for err in stats.errors:
                _log(session, run.id, "belge_hatasi", err, level="ERROR", source_id=source.id)
                result.errors.append(err)
            _log(session, run.id, "tespit", (
                f"{source.name}: {stats.seen} belge görüldü, {stats.new_documents} yeni, "
                f"{stats.new_changes} ilgili değişiklik, {stats.irrelevant} ilgisiz, {stats.skipped} analiz dışı"
                + (f", {stats.baseline} başlangıç kaydı" if stats.baseline else "")
            ), source_id=source.id, details=stats.__dict__ | {"new_change_ids": stats.new_change_ids})
            for f in ("seen", "new_documents", "new_changes", "irrelevant", "skipped", "duplicates", "baseline"):
                setattr(totals, f, getattr(totals, f) + getattr(stats, f))
            session.commit()

        for source in sources:
            source_rows.append(self._source_row(source))

        self._retry_missing_texts(session, run)
        if run_type == "tarama":
            # Yalnızca tarama (MCP sunucusu): AI analizi ve e-posta yok; kural tabanlı önem atanır,
            # analiz kullanıcının kendi asistanına bırakılır.
            analyzed = 0
            for chg in session.scalars(select(RegulatoryChange).where(RegulatoryChange.importance.is_(None))):
                apply_classification(chg, None, today, self.taxonomy)
            session.commit()
        else:
            analyzed = self._analyze_pending(session, run, today)

        run = session.get(ScanRun, run.id)
        run.docs_seen = totals.seen
        run.docs_new = totals.new_documents
        run.changes_new = totals.new_changes
        run.coverage = coverage
        result.new_documents = totals.new_documents
        result.new_changes = totals.new_changes
        result.analyzed = analyzed
        result.complete = complete
        result.warnings = warnings
        session.commit()

        # ---- rapor ----
        report_status = None
        if run_type in ("gunluk", "kritik"):
            report_status = self._report(session, run, result, run_type, send, today, complete, source_rows,
                                         warnings, totals)

        # ---- yönetici bildirimleri ----
        alert_lines = []
        if primary_failures:
            alert_lines.append("Birincil kaynak (Resmî Gazete) taranamadı: " + "; ".join(primary_failures[:5]))
        if not complete and not primary_failures:
            alert_lines.append("Bazı kaynaklar taranamadı: " + "; ".join(warnings[:5]))
        if result.errors:
            alert_lines.append(f"{len(result.errors)} işlem hatası: " + "; ".join(result.errors[:5]))
        if report_status == "basarisiz":
            alert_lines.append("Rapor e-postası tüm denemelere rağmen gönderilemedi; değişiklikler bir sonraki rapora aktarılacak.")
        if alert_lines and run_type != "tarama" and (run_type != "kritik" or primary_failures):
            send_admin_alert(session, self.settings, f"Tarama sorunları ({today.isoformat()})", alert_lines,
                             {"çalıştırma": run.id})

        run = session.get(ScanRun, run.id)
        run.errors = list(run.errors or []) + result.errors + [w for w in warnings if w not in (run.errors or [])]
        run.status = "basarili" if complete and not result.errors and report_status != "basarisiz" else "kismi"
        run.finished_at = utcnow()
        result.status = run.status
        session.commit()
        log.info("Çalıştırma bitti: %s | yeni değişiklik=%s | rapor=%s", run.status, result.new_changes, report_status)

    def _scan_source(self, session: Session, run: ScanRun, source: RegulatorySource, today: date) -> ScanOutcome:
        adapter = build_adapter(source)
        state = dict((source.config or {}).get("_state", {}))
        source.last_checked_at = utcnow()
        try:
            outcome = adapter.scan(self.fetcher, cursor=source.cursor, today=today, state=state)
        except Exception as exc:  # beklenmeyen hata da yalnızca bu kaynağı etkiler
            log.exception("Kaynak taranamadı: %s", source.key)
            outcome = ScanOutcome(complete=False, errors=[f"Beklenmeyen hata: {exc}"], access_status="hata")
        source.access_status = outcome.access_status
        source.cursor = outcome.new_cursor or source.cursor
        if outcome.complete:
            source.last_success_at = utcnow()
            source.last_error = None
        else:
            source.last_error = "; ".join(outcome.errors)[:2000]
        cfg = dict(source.config or {})
        cfg["_state"] = state
        source.config = cfg
        _log(session, run.id, "kaynak_tarama",
             f"{source.name}: {'tamamlandı' if outcome.complete else 'EKSİK'} ({len(outcome.documents)} belge)",
             level="INFO" if outcome.complete else "ERROR", source_id=source.id,
             details={"coverage": outcome.coverage, "errors": outcome.errors, "warnings": outcome.warnings})
        session.commit()
        return outcome

    def _source_row(self, source: RegulatorySource) -> SourceStatusRow:
        if not source.enabled:
            return SourceStatusRow(source.name, "İzlenmiyor (" + (source.access_status or "devre dışı").replace("_", " ") + ")",
                                   True, "—", disabled=True)
        ok = source.access_status == "erisilebilir" and not source.last_error
        detail = (source.last_error or "")[:200]
        return SourceStatusRow(
            name=source.name,
            status=ACCESS_LABELS.get(source.access_status, source.access_status) if ok or not detail else "Eksik / hata",
            ok=ok,
            last_success=to_local(source.last_success_at, self.settings.timezone),
            detail=detail,
        )

    def _triage_fn(self, session: Session):
        analyzer = RegulatoryAnalyzer(session, self.analysis_chain, self.settings, self.profile, self.triage_chain)
        return analyzer.triage

    def _retry_missing_texts(self, session: Session, run: ScanRun) -> None:
        """Metni alınamamış, henüz raporlanmamış değişikliklerin metnini yeniden dener."""
        since = utcnow() - timedelta(days=3)
        rows = session.scalars(
            select(RegulatoryChange).join(RegulatoryDocument).where(
                RegulatoryDocument.text_status == "okunamadi",
                RegulatoryChange.first_detected_at >= since,
                RegulatoryChange.id.not_in(select(EmailReportItem.change_id).join(EmailReport).where(
                    EmailReport.status == "gonderildi")),
            )
        ).all()
        for chg in rows:
            doc = chg.document
            if chg.first_detected_at > utcnow() - timedelta(minutes=30):
                continue  # bu çalıştırmada zaten denendi
            adapter = build_adapter(doc.source)
            fd = FetchedDocument(source_key=doc.source.key, external_id=doc.external_id or "", title=doc.title,
                                 url=doc.url, pdf_url=doc.pdf_url, doc_type=doc.doc_type)
            dtext = adapter.fetch_text(self.fetcher, fd)
            if not dtext.text:
                continue
            doc.full_text, doc.text_hash, doc.text_status = dtext.text, sha256(dtext.text), dtext.status
            ext = extract_all(doc.title, doc.doc_type, dtext.text, doc.published_date)
            chg.amended_regulation = ext.amended_regulation or chg.amended_regulation
            chg.amended_regulation_ref = ext.amended_regulation_ref
            chg.amended_articles = ext.amended_articles
            chg.effective_dates = ext.effective_dates
            chg.effective_date, chg.effective_date_status = summarize_effective(ext.effective_dates)
            chg.change_aspects = ext.aspects
            chg.review_reasons = [r for r in (chg.review_reasons or []) if "metni okunamadı" not in r]
            chg.needs_review = bool(chg.review_reasons)
            chg.url_verified = True
            chg.status = "yeni"
            _log(session, run.id, "metin_yeniden", f"Metin yeniden alındı: {doc.title[:120]}")
        session.commit()

    def _analyze_pending(self, session: Session, run: ScanRun, today: date) -> int:
        reported = select(EmailReportItem.change_id).join(EmailReport).where(
            EmailReport.status == "gonderildi", EmailReport.report_type == "gunluk")
        candidates = session.scalars(
            select(RegulatoryChange).where(RegulatoryChange.id.not_in(reported)).where(
                RegulatoryChange.status.in_(["yeni", "analiz_basarisiz", "inceleme"])
            )
        ).all()
        analyzer = RegulatoryAnalyzer(session, self.analysis_chain, self.settings, self.profile, self.triage_chain)
        count = 0
        for chg in candidates:
            has_valid = chg.latest_valid_analysis is not None
            if chg.status == "inceleme" and (has_valid or self.analysis_chain is None or not chg.document.full_text):
                # zaten analiz edilmiş ya da AI ile tekrar denemenin anlamı yok → yalnızca önemi güncelle
                apply_classification(chg, chg.latest_valid_analysis.parsed if has_valid else None, today, self.taxonomy)
                session.commit()
                continue
            if chg.status == "analiz_basarisiz" and chg.analysis_attempts >= MAX_ANALYSIS_ATTEMPTS_TOTAL:
                apply_classification(chg, None, today, self.taxonomy)
                session.commit()
                continue
            try:
                result = analyzer.analyze(chg)
                count += result is not None
            except Exception as exc:
                log.exception("Analiz hatası")
                chg.status = "analiz_basarisiz"
                chg.needs_review = True
                chg.review_reasons = list(chg.review_reasons or []) + [f"AI analizi sırasında beklenmeyen hata: {exc}"]
            latest = chg.latest_valid_analysis
            apply_classification(chg, latest.parsed if latest else None, today, self.taxonomy)
            _log(session, run.id, "analiz", f"Analiz: {chg.document.title[:120]} → {chg.status}, önem {chg.importance}")
            session.commit()
        return count

    def _previous_success(self, session: Session, run: ScanRun, run_type: str) -> datetime | None:
        prev = session.scalars(
            select(ScanRun).where(ScanRun.id != run.id, ScanRun.run_type == run_type,
                                  ScanRun.status.in_(["basarili", "kismi"]))
            .order_by(ScanRun.started_at.desc()).limit(1)
        ).first()
        return prev.started_at if prev else None

    def _report(self, session, run, result, run_type, send, today, complete, source_rows, warnings, totals) -> str | None:
        changes = pending_changes(session, run_type)
        if run_type == "kritik" and not changes:
            return None
        if run_type == "gunluk" and not changes:
            if complete and not self.settings.send_empty_reports:
                return None
            if not complete and not self.settings.notify_recipients_on_failure:
                return None
        inp = ReportInput(
            report_type=run_type,
            report_date=today,
            period_start=self._previous_success(session, run, run_type),
            period_end=utcnow(),
            is_complete=complete,
            source_rows=source_rows if run_type == "gunluk" else [],
            warnings=warnings,
            docs_scanned=totals.new_documents,
            docs_filtered=totals.irrelevant,
            docs_skipped=totals.skipped,
            excluded_since=run.started_at,
            excluded_max=self.settings.report_excluded_max,
            ai_usage=(f"{get_setting(session, KEY_LLM_CALLS_PREFIX + today.isoformat()) or 0}"
                      f" / {self.settings.llm_daily_max_calls}") if self.analysis_chain else "AI kapalı",
        )
        report = build_report(session, inp, changes, self.taxonomy, self.settings.timezone)
        report.scan_run_id = run.id
        session.commit()
        result.report_id = report.id
        result.report_subject = report.subject
        preview_dir = self.settings.report_preview_dir
        preview_dir.mkdir(parents=True, exist_ok=True)
        (preview_dir / f"{today.isoformat()}_{run_type}_{report.id[:8]}.html").write_text(report.html_body, encoding="utf-8")
        for old_file in sorted(preview_dir.glob("*.html"))[:-max(1, self.settings.report_keep)]:
            old_file.unlink(missing_ok=True)  # en eski önizlemeleri temizle

        if not send:
            report.status = "gonderilmedi"
            session.commit()
            result.report_status = report.status
            return report.status

        to, cc = effective_recipients(session, self.settings)
        mail = OutgoingEmail(subject=report.subject, text=report.text_body, html=report.html_body, to=to, cc=cc)

        def record(attempt: int, status: str, error: str | None) -> None:
            session.add(EmailDeliveryLog(report_id=report.id, kind="rapor", attempt=attempt,
                                         recipients={"to": to, "cc": cc}, status=status, error=error))
            session.commit()

        ok = send_with_retry(self.settings, mail, on_attempt=record)
        report.status = "gonderildi" if ok else "basarisiz"
        report.sent_at = utcnow() if ok else None
        session.commit()
        _log(session, run.id, "eposta", f"Rapor {'gönderildi' if ok else 'GÖNDERİLEMEDİ'}: {report.subject}",
             level="INFO" if ok else "ERROR")
        session.commit()
        result.report_status = report.status
        return report.status


def run_pipeline(**kwargs) -> RunResult:
    init_kwargs = {k: kwargs.pop(k) for k in ("settings", "fetcher", "analysis_chain", "triage_chain", "now")
                   if k in kwargs}
    return Pipeline(**init_kwargs).run(**kwargs)


def check_heartbeat(settings: Settings | None = None, max_age_hours: int = 26) -> tuple[bool, str]:
    """Son başarılı günlük çalıştırma çok eskiyse yöneticiyi uyarır (günde en fazla bir kez)."""
    settings = settings or get_settings()
    with session_scope() as session:
        last = session.scalars(
            select(ScanRun).where(ScanRun.run_type == "gunluk", ScanRun.status.in_(["basarili", "kismi"]))
            .order_by(ScanRun.started_at.desc()).limit(1)
        ).first()
        if last is None:
            return True, "Henüz tamamlanmış günlük çalıştırma yok"
        age = utcnow() - last.started_at
        if age <= timedelta(hours=max_age_hours):
            return True, f"Son başarılı günlük çalıştırma {age.total_seconds() / 3600:.1f} saat önce"
        msg = f"Günlük görev {age.total_seconds() / 3600:.0f} saattir başarıyla çalışmadı (son: {to_local(last.started_at, settings.timezone)})"
        last_alert = get_setting(session, KEY_HEARTBEAT_ALERT)
        today_s = local_today(settings).isoformat()
        if last_alert != today_s:
            send_admin_alert(session, settings, "Günlük görev çalışmıyor", [msg])
            set_setting(session, KEY_HEARTBEAT_ALERT, today_s)
        return False, msg
