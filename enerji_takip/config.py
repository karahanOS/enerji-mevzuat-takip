"""Uygulama yapılandırması.

Tüm gizli bilgiler (Gmail uygulama parolası, API anahtarları) yalnızca ortam
değişkenlerinden okunur; kodda sabit değer yoktur. Yerelde .env dosyası,
GitHub Actions'ta depo "secret"ları kullanılır.

Yapay zekâ yalnızca ücretsiz seçeneklerle çalışır: Gemini API ücretsiz katmanı,
Groq ücretsiz katmanı veya yerel Ollama. Hepsi OpenAI uyumlu uç nokta sunar.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from . import PROJECT_URL, __version__

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULTS_DIR = PACKAGE_DIR / "varsayilan"
CONFIG_NAMES = ("sources.yaml", "taxonomy.yaml", "profile.yaml")


def app_home() -> Path:
    """Veri, günlük ve kullanıcı yapılandırmasının bulunduğu dizin.

    1. ENERJI_TAKIP_HOME ortam değişkeni
    2. Çalışma dizini bir proje/şablon deposuysa (config/ altında sources|taxonomy|profile.yaml ya da .env var) orası
    3. Aksi halde ~/.enerji-mevzuat-takip (pip/uvx ile kurulumda, ör. MCP sunucusu)
    """
    env = os.environ.get("ENERJI_TAKIP_HOME", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    cwd = Path.cwd()
    # "config/" klasörünün varlığı tek başına yetmez: Windows'ta C:\Windows\System32\config gibi
    # sistem klasörleri aynı adı taşır. Bizim yapılandırma dosyalarımızdan biri bulunmalı.
    has_config = any((cwd / "config" / name).is_file() for name in CONFIG_NAMES)
    if has_config or (cwd / ".env").is_file() or (
            (cwd / "pyproject.toml").is_file() and (cwd / "enerji_takip").is_dir()):
        return cwd
    return Path.home() / ".enerji-mevzuat-takip"


def config_path(name: str) -> Path:
    """Kullanıcının özelleştirdiği dosya varsa onu, yoksa paketle gelen varsayılanı döndürür."""
    custom = app_home() / "config" / name
    return custom if custom.is_file() else DEFAULTS_DIR / name


def resolve_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else app_home() / path


load_dotenv(Path.cwd() / ".env", override=False)
load_dotenv(app_home() / ".env", override=False)

# Ücretsiz sağlayıcılar: OpenAI uyumlu taban adresleri
PROVIDER_BASE_URLS = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "groq": "https://api.groq.com/openai/v1",
    "ollama": "http://localhost:11434/v1",
}


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "evet", "on"}


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    return int(value) if value is not None else default


def _env_list(name: str, default: str = "") -> list[str]:
    value = _env(name, default) or ""
    return [item.strip() for item in value.replace(";", ",").split(",") if item.strip()]


@dataclass
class Settings:
    # Veri tabanı (SQLite: ücretsiz, sunucu gerektirmez)
    database_url: str = "sqlite:///data/enerji_takip.db"

    # Zamanlama
    timezone: str = "Europe/Istanbul"
    daily_report_time: str = "08:00"

    # E-posta (varsayılan: Gmail SMTP + uygulama parolası)
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_security: str = "starttls"  # starttls | ssl | none
    email_from: str | None = None
    email_to: list[str] = field(default_factory=list)
    email_cc: list[str] = field(default_factory=list)
    admin_email: list[str] = field(default_factory=list)
    email_max_attempts: int = 3
    email_retry_base_seconds: int = 20
    send_empty_reports: bool = True
    notify_recipients_on_failure: bool = True

    # Yapay zekâ — "sağlayıcı:model" zincirleri; ilk çalışan kullanılır, kota dolarsa sıradakine geçilir
    llm_analysis_chain: list[str] = field(default_factory=lambda: [
        "gemini:gemini-3.8-flash", "gemini:gemini-3.5-flash-lite", "groq:openai/gpt-oss-120b"])
    llm_triage_chain: list[str] = field(default_factory=lambda: [
        "gemini:gemini-3.5-flash-lite", "groq:openai/gpt-oss-20b"])
    gemini_api_key: str | None = None
    groq_api_key: str | None = None
    ollama_base_url: str = PROVIDER_BASE_URLS["ollama"]
    max_input_chars: dict = field(default_factory=lambda: {"gemini": 60000, "groq": 12000, "ollama": 12000})
    max_output_tokens: dict = field(default_factory=lambda: {"gemini": 6000, "groq": 3000, "ollama": 3000})
    llm_daily_max_calls: int = 40
    llm_max_attempts: int = 2
    llm_timeout_seconds: int = 120
    llm_triage_uncertain: bool = True

    # Tarama
    http_user_agent: str = f"enerji-mevzuat-takip/{__version__} (+{PROJECT_URL})"
    http_timeout_seconds: int = 30
    http_min_interval_seconds: float = 1.5
    respect_robots_txt: bool = True
    rg_rescan_days: int = 2
    rg_max_backfill_days: int = 14
    rg_first_run_days: int = 1
    ocr_enabled: bool = True
    ocr_max_pages: int = 8

    # Yapılandırma dosyaları (varsayılanlar pakette; config/ altına kopyalanarak özelleştirilir)
    sources_file: Path = field(default_factory=lambda: config_path("sources.yaml"))
    taxonomy_file: Path = field(default_factory=lambda: config_path("taxonomy.yaml"))
    profile_file: Path = field(default_factory=lambda: config_path("profile.yaml"))

    # Çıktılar
    report_preview_dir: Path = field(default_factory=lambda: app_home() / "data" / "raporlar")
    report_keep: int = 60
    log_dir: Path = field(default_factory=lambda: app_home() / "data" / "loglar")
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls()
        s.database_url = _env("DATABASE_URL", s.database_url)
        s.timezone = _env("TIMEZONE", s.timezone)
        s.daily_report_time = _env("DAILY_REPORT_TIME", s.daily_report_time)

        s.smtp_host = _env("SMTP_HOST")
        s.smtp_port = _env_int("SMTP_PORT", s.smtp_port)
        s.smtp_username = _env("SMTP_USERNAME")
        s.smtp_password = _env("SMTP_PASSWORD")
        s.smtp_security = (_env("SMTP_SECURITY", s.smtp_security) or "starttls").lower()
        s.email_from = _env("EMAIL_FROM", s.smtp_username)
        s.email_to = _env_list("EMAIL_TO")
        s.email_cc = _env_list("EMAIL_CC")
        s.admin_email = _env_list("ADMIN_EMAIL") or list(s.email_to)
        s.email_max_attempts = _env_int("EMAIL_MAX_ATTEMPTS", s.email_max_attempts)
        s.email_retry_base_seconds = _env_int("EMAIL_RETRY_BASE_SECONDS", s.email_retry_base_seconds)
        s.send_empty_reports = _env_bool("SEND_EMPTY_REPORTS", s.send_empty_reports)
        s.notify_recipients_on_failure = _env_bool("NOTIFY_RECIPIENTS_ON_FAILURE", s.notify_recipients_on_failure)

        if _env("LLM_ANALYSIS_CHAIN") is not None:
            s.llm_analysis_chain = [] if _env("LLM_ANALYSIS_CHAIN") == "none" else _env_list("LLM_ANALYSIS_CHAIN")
        if _env("LLM_TRIAGE_CHAIN") is not None:
            s.llm_triage_chain = [] if _env("LLM_TRIAGE_CHAIN") == "none" else _env_list("LLM_TRIAGE_CHAIN")
        s.gemini_api_key = _env("GEMINI_API_KEY")
        s.groq_api_key = _env("GROQ_API_KEY")
        s.ollama_base_url = _env("OLLAMA_BASE_URL", s.ollama_base_url)
        for provider in ("gemini", "groq", "ollama"):
            s.max_input_chars[provider] = _env_int(f"{provider.upper()}_MAX_INPUT_CHARS", s.max_input_chars[provider])
            s.max_output_tokens[provider] = _env_int(f"{provider.upper()}_MAX_OUTPUT_TOKENS", s.max_output_tokens[provider])
        s.llm_daily_max_calls = _env_int("LLM_DAILY_MAX_CALLS", s.llm_daily_max_calls)
        s.llm_max_attempts = _env_int("LLM_MAX_ATTEMPTS", s.llm_max_attempts)
        s.llm_timeout_seconds = _env_int("LLM_TIMEOUT_SECONDS", s.llm_timeout_seconds)
        s.llm_triage_uncertain = _env_bool("LLM_TRIAGE_UNCERTAIN", s.llm_triage_uncertain)

        s.http_user_agent = _env("HTTP_USER_AGENT", s.http_user_agent)
        s.http_timeout_seconds = _env_int("HTTP_TIMEOUT_SECONDS", s.http_timeout_seconds)
        s.http_min_interval_seconds = float(_env("HTTP_MIN_INTERVAL_SECONDS", str(s.http_min_interval_seconds)))
        s.respect_robots_txt = _env_bool("RESPECT_ROBOTS_TXT", s.respect_robots_txt)
        s.rg_rescan_days = _env_int("RG_RESCAN_DAYS", s.rg_rescan_days)
        s.rg_max_backfill_days = _env_int("RG_MAX_BACKFILL_DAYS", s.rg_max_backfill_days)
        s.rg_first_run_days = _env_int("RG_FIRST_RUN_DAYS", s.rg_first_run_days)
        s.ocr_enabled = _env("OCR_ENABLED", "true").lower() not in ("0", "false", "hayir", "no")
        s.ocr_max_pages = _env_int("OCR_MAX_PAGES", s.ocr_max_pages)

        for attr, var in (("sources_file", "SOURCES_FILE"), ("taxonomy_file", "TAXONOMY_FILE"),
                          ("profile_file", "PROFILE_FILE"), ("report_preview_dir", "REPORT_DIR"),
                          ("log_dir", "LOG_DIR")):
            if _env(var):
                setattr(s, attr, resolve_path(_env(var)))
        s.report_keep = _env_int("REPORT_KEEP", s.report_keep)
        s.log_level = (_env("LOG_LEVEL", s.log_level) or "INFO").upper()
        return s

    # ---- Doğrulama -------------------------------------------------------
    def email_config_problems(self) -> list[str]:
        problems = []
        if not self.smtp_host:
            problems.append("SMTP_HOST tanımlı değil (Gmail için smtp.gmail.com)")
        if not self.email_from:
            problems.append("EMAIL_FROM tanımlı değil")
        if self.smtp_username and not self.smtp_password:
            problems.append("SMTP_USERNAME var ama SMTP_PASSWORD (Gmail uygulama parolası) tanımlı değil")
        if self.smtp_security not in {"starttls", "ssl", "none"}:
            problems.append("SMTP_SECURITY yalnızca starttls, ssl veya none olabilir")
        return problems

    def provider_key(self, provider: str) -> str | None:
        return {"gemini": self.gemini_api_key, "groq": self.groq_api_key, "ollama": "ollama"}.get(provider)

    def provider_base_url(self, provider: str) -> str | None:
        if provider == "ollama":
            return self.ollama_base_url.rstrip("/")
        return PROVIDER_BASE_URLS.get(provider)

    def llm_config_problems(self) -> list[str]:
        problems = []
        for chain_name, chain in (("LLM_ANALYSIS_CHAIN", self.llm_analysis_chain),
                                  ("LLM_TRIAGE_CHAIN", self.llm_triage_chain)):
            usable = 0
            for entry in chain:
                provider, _, model = entry.partition(":")
                if provider not in PROVIDER_BASE_URLS or not model:
                    problems.append(f"{chain_name}: geçersiz giriş '{entry}' (biçim: gemini:model, groq:model, ollama:model)")
                elif self.provider_key(provider):
                    usable += 1
            if chain and not usable:
                problems.append(f"{chain_name}: kullanılabilir sağlayıcı yok (GEMINI_API_KEY / GROQ_API_KEY tanımlayın "
                                "veya Ollama kullanın); AI olmadan kural tabanlı çalışılacak")
        return problems


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def set_settings(settings: Settings) -> None:
    """Testlerde ve CLI'da ayarları değiştirmek için."""
    global _settings
    _settings = settings
