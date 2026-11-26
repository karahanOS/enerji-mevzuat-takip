"""Geri tarama sınırı: --since sınır dışındaysa sessizce gün atlanmaz; sistem bildirimi raporun etiketini taşır."""
from __future__ import annotations

from datetime import timedelta

from enerji_takip import __main__ as cli
from enerji_takip import pipeline
from enerji_takip.pipeline import RunResult, local_today
from enerji_takip.reporting.builder import render_admin_alert


def test_since_sinir_disindaysa_calismadan_hata_verir(settings, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(pipeline, "run_pipeline", lambda **kw: calls.append(kw) or RunResult(status="basarili"))
    too_old = local_today(settings) - timedelta(days=settings.rg_max_backfill_days + 1)
    assert cli.main(["run", "--no-send", "--since", too_old.isoformat()]) == 2
    assert calls == [] and "geri tarama sınırının dışında" in capsys.readouterr().out


def test_since_tam_sinirda_kabul_edilir(settings, monkeypatch):
    calls = []
    monkeypatch.setattr(pipeline, "run_pipeline", lambda **kw: calls.append(kw) or RunResult(status="basarili"))
    edge = local_today(settings) - timedelta(days=settings.rg_max_backfill_days)
    assert cli.main(["run", "--no-send", "--since", edge.isoformat()]) == 0
    assert len(calls) == 1


def test_sistem_bildirimi_etiketi_ve_dipnotu():
    body = render_admin_alert("Tarama sorunları", ["x"], label="MEVZUAT TAKİP")
    assert body.startswith("MEVZUAT TAKİP — SİSTEM BİLDİRİMİ")
    assert "azami geri tarama sınırını" in body
