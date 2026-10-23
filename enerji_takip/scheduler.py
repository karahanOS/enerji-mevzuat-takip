"""Zamanlayıcı: her gün belirlenen saatte (varsayılan 08:00 Europe/Istanbul) tam tarama + rapor.

* misfire_grace_time + coalesce: süreç kısa süre kapalı kalırsa kaçan çalıştırma bir kez yapılır.
* Açılışta telafi: bugünün raporu henüz üretilmemiş ve rapor saati geçmişse hemen çalıştırılır.
* Saatlik sağlık kontrolü: son başarılı günlük çalıştırma 26 saatten eskiyse yöneticiye e-posta.
* Bu mod bilgisayarınızın açık kalmasını gerektirir; sunucusuz seçenek GitHub Actions'tır.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from .config import Settings, get_settings
from .db import init_db, session_scope
from .models import ScanRun
from .runtime import effective_report_time, validate_time

log = logging.getLogger(__name__)

DAILY_JOB_ID = "gunluk_rapor"


def _daily_job(trigger: str = "zamanlanmis") -> None:
    from .pipeline import run_pipeline

    result = run_pipeline(run_type="gunluk", trigger=trigger)
    log.info("Günlük görev sonucu: %s", result.status)


def _heartbeat_job() -> None:
    from .pipeline import check_heartbeat

    ok, msg = check_heartbeat()
    (log.info if ok else log.error)("Sağlık kontrolü: %s", msg)


def daily_trigger(time_str: str, tz: str) -> CronTrigger:
    hh, mm = validate_time(time_str).split(":")
    return CronTrigger(hour=int(hh), minute=int(mm), timezone=ZoneInfo(tz))


def build_scheduler(settings: Settings | None = None, *, blocking: bool = False):
    settings = settings or get_settings()
    init_db()
    with session_scope() as session:
        report_time = effective_report_time(session, settings)
    tz = ZoneInfo(settings.timezone)
    sched = (BlockingScheduler if blocking else BackgroundScheduler)(timezone=tz)
    common = {"misfire_grace_time": 3 * 3600, "coalesce": True, "max_instances": 1, "replace_existing": True}
    sched.add_job(_daily_job, daily_trigger(report_time, settings.timezone), id=DAILY_JOB_ID,
                  name="Günlük mevzuat taraması ve rapor", **common)
    sched.add_job(_heartbeat_job, CronTrigger(minute=17, timezone=tz), id="saglik_kontrolu",
                  name="Sağlık kontrolü", **common)
    log.info("Zamanlayıcı hazır: günlük rapor %s (%s)", report_time, settings.timezone)
    return sched


def reschedule_daily(sched, time_str: str, settings: Settings | None = None) -> datetime | None:
    settings = settings or get_settings()
    job = sched.reschedule_job(DAILY_JOB_ID, trigger=daily_trigger(time_str, settings.timezone))
    return job.next_run_time


def needs_catchup(settings: Settings | None = None, now: datetime | None = None) -> bool:
    """Bugünün günlük çalıştırması yapılmamışsa ve rapor saati geçtiyse True."""
    settings = settings or get_settings()
    tz = ZoneInfo(settings.timezone)
    now_local = (now or datetime.now(timezone.utc)).astimezone(tz)
    with session_scope() as session:
        report_time = effective_report_time(session, settings)
        hh, mm = map(int, report_time.split(":"))
        due = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now_local < due:
            return False
        start_of_day_utc = now_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).replace(tzinfo=None)
        ran = session.scalars(
            select(ScanRun).where(ScanRun.run_type == "gunluk", ScanRun.started_at >= start_of_day_utc,
                                  ScanRun.status.in_(["basarili", "kismi", "calisiyor"]))
        ).first()
        return ran is None


def run_blocking() -> None:
    settings = get_settings()
    sched = build_scheduler(settings, blocking=True)
    if needs_catchup(settings):
        log.warning("Bugünün günlük raporu üretilmemiş; telafi çalıştırması başlatılıyor.")
        sched.add_job(_daily_job, kwargs={"trigger": "telafi"}, id="telafi",
                      run_date=datetime.now(timezone.utc) + timedelta(seconds=5))
    sched.start()
