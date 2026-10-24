"""Paketleme ve açık kaynak yayımı: sürüm, bağımlılık eşitliği, varsayılan dosyalar, kişisel veri sızıntısı."""
from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _names(lines) -> set[str]:
    return {re.split(r"[<>=\[ ;]", x.strip())[0].lower() for x in lines if x.strip() and not x.strip().startswith(("#", "-r"))}


def test_pyproject_bagimliliklari_requirements_ile_ayni():
    assert _names(PYPROJECT["project"]["dependencies"]) == _names((ROOT / "requirements.txt").read_text().splitlines())
    dev = _names((ROOT / "requirements-dev.txt").read_text().splitlines())
    assert _names(PYPROJECT["project"]["optional-dependencies"]["dev"]) == dev
    assert "mcp" in dev and _names(PYPROJECT["project"]["optional-dependencies"]["mcp"]) == {"mcp"}


def test_lisans_ve_komutlar():
    assert PYPROJECT["project"]["license"] == "MIT"
    assert (ROOT / "LICENSE").read_text(encoding="utf-8").startswith("MIT License")
    scripts = PYPROJECT["project"]["scripts"]
    assert scripts == {"enerji-takip": "enerji_takip.__main__:main", "enerji-mevzuat-mcp": "enerji_takip.mcp_server:main"}


def test_varsayilan_dosyalar_pakette_ve_env_ornegi_esit():
    from enerji_takip.config import DEFAULTS_DIR

    for name in ("sources.yaml", "taxonomy.yaml", "profile.yaml", "env.example"):
        assert (DEFAULTS_DIR / name).is_file(), name
    assert (DEFAULTS_DIR / "env.example").read_text(encoding="utf-8") == (ROOT / ".env.example").read_text(encoding="utf-8")


def test_init_config_kopyalar_ve_ustune_yazmaz(tmp_path):
    env = {"ENERJI_TAKIP_HOME": str(tmp_path), "PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"}
    run = lambda *a: subprocess.run([sys.executable, "-m", "enerji_takip", *a], env=env, cwd=tmp_path,  # noqa: E731
                                    capture_output=True, text=True, timeout=60)
    first = run("init-config")
    assert first.returncode == 0, first.stderr
    for name in ("sources.yaml", "taxonomy.yaml", "profile.yaml"):
        assert (tmp_path / "config" / name).is_file()
    assert (tmp_path / ".env").is_file()
    (tmp_path / "config" / "profile.yaml").write_text("aciklama: kendi profilim\n", encoding="utf-8")
    run("init-config")
    assert (tmp_path / "config" / "profile.yaml").read_text(encoding="utf-8") == "aciklama: kendi profilim\n"
    # Özel profil, varsayılanın önüne geçer
    out = subprocess.run([sys.executable, "-c", "from enerji_takip.config import config_path;print(config_path('profile.yaml'))"],
                         env=env, cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == str(tmp_path / "config" / "profile.yaml")


PUBLIC_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".toml", ".txt", ".html", ".j2", ".example", ""}
ALLOWED_EMAIL_DOMAINS = ("ornek.com", "example.com", "users.noreply.github.com")


def _public_files():
    skip = {".git", ".venv", "__pycache__", ".pytest_cache", "build", "dist", "data", "veri", "config"}
    for p in ROOT.rglob("*"):
        if p.is_file() and not (set(p.relative_to(ROOT).parts) & skip) and p.suffix in PUBLIC_SUFFIXES \
                and p.name != ".env" and not p.name.endswith(".egg-info"):
            yield p


def test_yayimlanacak_dosyalarda_gercek_eposta_yok():
    email = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+\.)+[a-z]{2,}")
    leaks = []
    for p in _public_files():
        for m in email.finditer(p.read_text(encoding="utf-8", errors="ignore")):
            addr = m.group(0)
            if not addr.endswith(ALLOWED_EMAIL_DOMAINS) and not addr.startswith(("adresiniz@", "ben@")):
                leaks.append((str(p.relative_to(ROOT)), addr))
    assert not leaks, leaks


@pytest.mark.parametrize("path", [".gitignore"])
def test_gizli_dosyalar_git_disinda(path):
    ignored = (ROOT / path).read_text(encoding="utf-8").split()
    for entry in (".env", "data/", "veri/"):
        assert entry in ignored, entry


def test_calisma_dizinindeki_sistem_config_klasoru_uygulama_dizini_sayilmaz(tmp_path, monkeypatch):
    """Windows'ta Claude Desktop sunucuyu C:\\Windows\\System32 içinde başlatır; orada 'config' adlı sistem klasörü var."""
    from enerji_takip.config import app_home

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    cwd = tmp_path / "system32"
    (cwd / "config").mkdir(parents=True)
    (cwd / "config" / "SAM").write_text("sistem dosyası")
    monkeypatch.delenv("ENERJI_TAKIP_HOME", raising=False)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.chdir(cwd)
    assert app_home() == fake_home / ".enerji-mevzuat-takip"
    # Gerçek bir yapılandırma dosyası varsa çalışma dizini kullanılır
    (cwd / "config" / "profile.yaml").write_text("aciklama: x\n")
    assert app_home() == cwd


def test_cp1252_konsolunda_turkce_cikti_cokmez():
    code = "import sys;from enerji_takip.__main__ import _utf8_console,_print;_utf8_console();_print('Durum: tamamlandı ğ ş İ')"
    env = {"PYTHONIOENCODING": "cp1252", "PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"}
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "tamamlandı".encode("utf-8") in out.stdout


def test_kilidi_kaldir_yarim_kalan_kaydi_kapatir(settings, monkeypatch):
    from types import SimpleNamespace

    from sqlalchemy import select

    import enerji_takip.pipeline as pl
    from enerji_takip.__main__ import main
    from enerji_takip.db import init_db, session_scope
    from enerji_takip.models import ScanRun

    init_db()
    with session_scope() as s:
        s.add(ScanRun(run_type="tarama", trigger="cli", status="calisiyor"))
    fake = SimpleNamespace(status="basarili", complete=True, new_documents=0, new_changes=0, analyzed=0,
                           report_subject=None, warnings=[], errors=[])
    monkeypatch.setattr(pl, "run_pipeline", lambda **kw: fake)
    assert main(["run", "--type", "tarama", "--no-send", "--kilidi-kaldir"]) == 0
    with session_scope() as s:
        assert [r.status for r in s.scalars(select(ScanRun))] == ["yarida_kaldi"]
