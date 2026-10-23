"""E-posta servisi, zamanlayıcı, sağlık kontrolü, veri tabanı yeniden deneme, komut satırı ve ücretsizlik denetimi."""
from __future__ import annotations

import re
import smtplib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import respx
from sqlalchemy.exc import OperationalError

from enerji_takip.db import session_scope, with_db_retry
from enerji_takip.models import ScanRun, utcnow
from enerji_takip.reporting import mailer
from enerji_takip.reporting.mailer import OutgoingEmail, send_once, send_with_retry
from tests.conftest import fake_chain, make_pipeline, register_all

ROOT = Path(__file__).resolve().parents[1]


def test_eposta_cc_ve_turkce_karakter(settings, smtp_server):
    send_once(settings, OutgoingEmail("[ENERJİ MEVZUAT] Deneme ğüşıöç", "PTF ve SMF — ğüşıöç", "<p>İçerik</p>",
                                      ["ben@ornek.com"], ["arkadas@ornek.com"]))
    m = smtp_server.messages[0]
    assert sorted(m["rcpt"]) == ["arkadas@ornek.com", "ben@ornek.com"]
    assert m["msg"]["Subject"] == "[ENERJİ MEVZUAT] Deneme ğüşıöç"
    assert m["msg"].get_body(("plain",)).get_content().strip() == "PTF ve SMF — ğüşıöç"


def test_eposta_gecici_hatada_yeniden_dener(settings, smtp_server, monkeypatch):
    calls = {"n": 0}
    real = mailer.send_once

    def flaky(s, mail):
        calls["n"] += 1
        if calls["n"] < 3:
            raise smtplib.SMTPServerDisconnected("koptu")
        return real(s, mail)

    monkeypatch.setattr(mailer, "send_once", flaky)
    log = []
    assert send_with_retry(settings, OutgoingEmail("K", "m", None, ["ben@ornek.com"], []),
                           on_attempt=lambda a, st, e: log.append(st), sleep=lambda *_: None)
    assert log == ["hata", "hata", "basarili"]


def test_eposta_yapilandirma_hatasi_yeniden_denenmez(settings):
    settings.smtp_host = None
    log = []
    assert not send_with_retry(settings, OutgoingEmail("K", "m", None, ["ben@ornek.com"], []),
                               on_attempt=lambda a, st, e: log.append(e))
    assert len(log) == 1 and "smtp.gmail.com" in log[0]


def test_zamanlayici_0800_istanbul(settings):
    from enerji_takip.scheduler import DAILY_JOB_ID, build_scheduler, reschedule_daily

    sched = build_scheduler(settings)
    sched.start(paused=True)
    try:
        job = sched.get_job(DAILY_JOB_ID)
        nxt = job.trigger.get_next_fire_time(None, datetime(2026, 10, 23, 7, 59, tzinfo=timezone(timedelta(hours=3))))
        assert (nxt.hour, nxt.minute, nxt.utcoffset()) == (8, 0, timedelta(hours=3))
        assert job.coalesce and job.max_instances == 1 and job.misfire_grace_time == 3 * 3600
        reschedule_daily(sched, "06:30", settings)
        nxt = sched.get_job(DAILY_JOB_ID).trigger.get_next_fire_time(
            None, datetime(2026, 10, 23, 0, 0, tzinfo=timezone(timedelta(hours=3))))
        assert (nxt.hour, nxt.minute) == (6, 30)
    finally:
        sched.shutdown(wait=False)


def test_zamanlanmis_gorev_calisir(settings, smtp_server, monkeypatch):
    from enerji_takip import pipeline, scheduler

    monkeypatch.setattr(pipeline, "build_chain", lambda s, entries: fake_chain())
    monkeypatch.setattr(pipeline, "local_today", lambda s, now=None: datetime(2026, 10, 23).date())
    with respx.mock(assert_all_called=False) as router:
        register_all(router)
        scheduler._daily_job()
    with session_scope() as s:
        run = s.query(ScanRun).one()
        assert run.trigger == "zamanlanmis" and run.status == "basarili"
    assert any("Günlük Enerji Mevzuatı" in m["msg"]["Subject"] for m in smtp_server.messages)


def test_acilista_telafi(settings):
    from enerji_takip.scheduler import needs_catchup

    ist = timezone(timedelta(hours=3))
    assert not needs_catchup(settings, now=datetime(2026, 10, 23, 7, 0, tzinfo=ist))
    assert needs_catchup(settings, now=datetime(2026, 10, 23, 9, 0, tzinfo=ist))
    with session_scope() as s:
        s.add(ScanRun(run_type="gunluk", status="basarili", started_at=datetime(2026, 10, 23, 5, 0)))
    assert not needs_catchup(settings, now=datetime(2026, 10, 23, 9, 0, tzinfo=ist))


def test_gunluk_gorev_calismazsa_uyari(settings, smtp_server):
    from enerji_takip.pipeline import check_heartbeat

    with session_scope() as s:
        s.add(ScanRun(run_type="gunluk", status="basarili", started_at=utcnow() - timedelta(hours=50)))
    assert not check_heartbeat(settings)[0]
    check_heartbeat(settings)  # aynı gün ikinci uyarı gönderilmez
    assert len([m for m in smtp_server.messages if "SİSTEM" in m["msg"]["Subject"]]) == 1


def test_veritabani_gecici_hatada_yeniden_dener():
    calls = {"n": 0}

    @with_db_retry(attempts=3, base_delay=0)
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise OperationalError("SELECT 1", {}, Exception("locked"))
        return "ok"

    assert flaky() == "ok"


def test_check_llm_komutu(settings, monkeypatch, capsys):
    from enerji_takip import __main__ as cli

    settings.gemini_api_key, settings.groq_api_key = "g", None
    settings.llm_analysis_chain = ["gemini:gemini-3.8-flash", "groq:openai/gpt-oss-120b"]
    settings.llm_triage_chain = ["gemini:gemini-3.5-flash-lite"]
    with respx.mock() as router:
        router.post("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions").mock(side_effect=[
            httpx.Response(200, json={"choices": [{"message": {"content": '{"durum": "tamam"}'}}]}),
            httpx.Response(429, headers={"retry-after": "3600"}),
        ])
        code = cli.main(["check-llm"])
    out = capsys.readouterr().out
    assert "✔ gemini:gemini-3.8-flash" in out and "groq:openai/gpt-oss-120b: atlandı" in out
    assert "✖ gemini:gemini-3.5-flash-lite" in out and code == 1


# ---- Ücretsizlik ve güvenlik denetimleri -----------------------------------------
FREE_PACKAGES = {  # paket → lisans (hepsi ücretsiz, açık kaynak)
    "httpx": "BSD-3", "beautifulsoup4": "MIT", "lxml": "BSD-3", "defusedxml": "PSF", "SQLAlchemy": "MIT",
    "pydantic": "MIT", "python-dotenv": "BSD-3", "PyYAML": "MIT", "Jinja2": "BSD-3", "APScheduler": "MIT",
    "pypdf": "BSD-3", "python-dateutil": "Apache-2.0/BSD", "tzdata": "Apache-2.0",
}


def test_yalnizca_ucretsiz_bagimliliklar():
    reqs = [re.split(r"[<>=\[ ]", line.strip())[0] for line in (ROOT / "requirements.txt").read_text().splitlines()
            if line.strip() and not line.startswith("#")]
    assert set(reqs) == set(FREE_PACKAGES), set(reqs) ^ set(FREE_PACKAGES)


def test_yalnizca_ucretsiz_ai_saglayicilari_ve_web_yok():
    from enerji_takip.config import PROVIDER_BASE_URLS

    assert set(PROVIDER_BASE_URLS) == {"gemini", "groq", "ollama"}
    code = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "enerji_takip").rglob("*.py"))
    for paid in ("api.anthropic.com", "api.openai.com", "fastapi", "uvicorn", "psycopg"):
        assert paid not in code, paid


def test_kimlik_bilgileri_kodda_yok():
    secret = re.compile(r"(password|passwd|api_key|secret|token)\w*\s*[:=]\s*[\"'][^\"']+[\"']", re.I)
    for path in (ROOT / "enerji_takip").rglob("*.py"):
        assert not secret.search(path.read_text(encoding="utf-8")), path
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    for key in ("SMTP_PASSWORD", "GEMINI_API_KEY", "GROQ_API_KEY"):
        assert re.search(rf"^{key}=$", env, re.M), key
