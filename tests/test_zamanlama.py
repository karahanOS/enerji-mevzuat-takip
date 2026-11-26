"""Zamanlanmış tetiklemeler günde tek rapor üretir (--gunde-bir): dış zamanlayıcı + gecikmeli GitHub cron'u."""
from __future__ import annotations

from datetime import timedelta

import yaml

from enerji_takip import __main__ as cli
from enerji_takip import pipeline
from enerji_takip.db import init_db, session_scope
from enerji_takip.models import EmailReport
from enerji_takip.pipeline import RunResult, daily_report_sent, local_today
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _report(day, status="gonderildi", report_type="gunluk"):
    with session_scope() as s:
        s.add(EmailReport(report_type=report_type, report_date=day, subject="x", html_body="x", text_body="x",
                          status=status))


def _calls(monkeypatch):
    calls = []

    def fake(**kw):
        calls.append(kw)
        return RunResult(status="basarili")

    monkeypatch.setattr(pipeline, "run_pipeline", fake)
    return calls


def test_bugunun_raporu_gittiyse_zamanlanmis_calistirma_atlanir(settings, monkeypatch, capsys):
    init_db()
    _report(local_today(settings))
    calls = _calls(monkeypatch)
    assert cli.main(["run", "--gunde-bir"]) == 0
    assert calls == [] and "zaten gönderilmiş" in capsys.readouterr().out


def test_dunku_rapor_ya_da_gonderilemeyen_rapor_engellemez(settings, monkeypatch):
    init_db()
    today = local_today(settings)
    _report(today - timedelta(days=1))
    _report(today, status="basarisiz")
    _report(today, report_type="kritik")
    with session_scope() as s:
        assert not daily_report_sent(s, today)
    calls = _calls(monkeypatch)
    assert cli.main(["run", "--gunde-bir"]) == 0
    assert len(calls) == 1


def test_elle_calistirma_her_zaman_calisir(settings, monkeypatch):
    init_db()
    _report(local_today(settings))
    calls = _calls(monkeypatch)
    cli.main(["run"])
    assert len(calls) == 1


def test_is_akisi_zamanlanmis_tetiklemelerde_gunde_bir_kullanir():
    wf = (ROOT / ".github" / "workflows" / "enerji-mevzuat-takip.yml").read_text(encoding="utf-8")
    data = yaml.safe_load(wf)
    inputs = data[True]["workflow_dispatch"]["inputs"]  # YAML 'on' anahtarını True okur
    assert inputs["zamanlanmis"]["default"] is False
    assert '"${{ github.event_name }}" = "schedule" ] || [ "${{ inputs.zamanlanmis }}" = "true" ]' in wf
    assert 'ARGS="--gunde-bir $ARGS"' in wf
