"""Komut satırı arayüzü.

    enerji-takip init-config                 # varsayılan yapılandırmayı düzenlemek üzere config/ altına kopyalar
    enerji-takip init-db
    enerji-takip check-config
    enerji-takip check-llm               # ücretsiz AI anahtarlarını dener (çağrı başına 1 istek)
    enerji-takip check-sources [--all]
    enerji-takip run [--no-send] [--since YYYY-AA-GG]
    enerji-takip test-email
    enerji-takip scheduler               # yerel bilgisayarda sürekli çalışan zamanlayıcı
    enerji-takip healthcheck             # son günlük çalıştırma 26 saatten eskiyse çıkış kodu 1
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

from .config import get_settings


def _print(msg: str = "") -> None:
    print(msg, flush=True)


def cmd_init_db(_args) -> int:
    from .db import init_db, session_scope
    from .sources.registry import sync_sources

    init_db()
    with session_scope() as s:
        rows = sync_sources(s, get_settings().sources_file)
    _print(f"Veri tabanı hazır; {len(rows)} kaynak tanımı yüklendi.")
    return 0


def cmd_init_config(args) -> int:
    import shutil

    from .config import CONFIG_NAMES, DEFAULTS_DIR, app_home

    target = app_home() / "config"
    target.mkdir(parents=True, exist_ok=True)
    for name in CONFIG_NAMES:
        dest = target / name
        if dest.exists() and not args.force:
            _print(f"• {dest} zaten var (üzerine yazmak için --force)")
            continue
        shutil.copyfile(DEFAULTS_DIR / name, dest)
        _print(f"✔ {dest}")
    env_example = DEFAULTS_DIR / "env.example"
    if env_example.exists() and not (app_home() / ".env").exists():
        shutil.copyfile(env_example, app_home() / ".env")
        _print(f"✔ {app_home() / '.env'} (doldurmanız gerekiyor)")
    _print(f"\nUygulama dizini: {app_home()}  (değiştirmek için ENERJI_TAKIP_HOME)")
    return 0


def cmd_check_config(_args) -> int:
    s = get_settings()
    from .config import app_home
    from .db import resolve_db_url

    _print(f"Uygulama dizini  : {app_home()}")
    _print(f"Veri tabanı      : {resolve_db_url(s.database_url).split('@')[-1]}")
    _print(f"Saat dilimi      : {s.timezone} | rapor saati: {s.daily_report_time}")
    _print(f"Alıcılar         : {', '.join(s.email_to) or '— (EMAIL_TO boş)'} | CC: {', '.join(s.email_cc) or '—'}")
    _print(f"Sistem uyarıları : {', '.join(s.admin_email) or '—'}")
    _print(f"AI analiz zinciri: {', '.join(s.llm_analysis_chain) or 'kapalı'}")
    _print(f"AI ön eleme      : {', '.join(s.llm_triage_chain) or 'kapalı'}")
    _print(f"Günlük AI tavanı : {s.llm_daily_max_calls} çağrı")
    problems = s.email_config_problems() + s.llm_config_problems()
    if not s.email_to:
        problems.append("EMAIL_TO tanımlı değil")
    if problems:
        _print("\nEksik/hatalı yapılandırma:")
        for p in problems:
            _print(f"  - {p}")
        return 1
    _print("\nYapılandırma tamam.")
    return 0


def cmd_check_llm(_args) -> int:
    """Zincirdeki her sağlayıcıya küçük bir istek atar; ücretsiz anahtar/model çalışıyor mu?"""
    from .analysis.llm import LLMError, Message, build_chain

    s = get_settings()
    failures = 0
    seen = set()
    for entry in s.llm_analysis_chain + s.llm_triage_chain:
        if entry in seen:
            continue
        seen.add(entry)
        chain = build_chain(s, [entry])
        if not chain.clients:
            _print(f"– {entry}: atlandı (API anahtarı tanımlı değil)")
            continue
        client = chain.clients[0]
        try:
            out = client.complete("Yalnızca JSON döndür.", [Message("user", 'Şunu aynen döndür: {"durum": "tamam"}')],
                                  max_tokens=50)
            _print(f"✔ {entry}: yanıt alındı ({out.strip()[:60]})")
        except LLMError as exc:
            failures += 1
            _print(f"✖ {entry}: {exc}")
    return 1 if failures else 0


def cmd_check_sources(args) -> int:
    """Kaynakların bu sunucudan gerçekten erişilebilir olduğunu ve ayrıştırıldığını doğrular."""
    from .db import init_db, session_scope
    from .http import Fetcher, FetchError, RobotsDisallowed
    from .sources.registry import build_adapter, sync_sources
    from .sources.resmi_gazete import index_url, parse_fihrist
    from .pipeline import local_today

    settings = get_settings()
    init_db()
    fetcher = Fetcher(settings)
    failures = 0
    with session_scope() as session:
        sources = sync_sources(session, settings.sources_file)
        for src in sources:
            if not src.enabled and not args.all:
                continue
            tag = "" if src.enabled else " [devre dışı]"
            try:
                if src.method == "rg_fihrist":
                    today = local_today(settings)
                    for d in (today, today - timedelta(days=1)):
                        url = index_url(d)
                        try:
                            res = fetcher.get(url)
                        except FetchError as exc:
                            if d == today and getattr(exc, "status_code", None) == 404:
                                continue
                            raise
                        docs, meta = parse_fihrist(res.text, res.url, expected=d)
                        _print(f"✔ {src.name}{tag}: {url} → Sayı {meta['rg_number']}, {len(docs)} belge")
                        for doc in docs[:6]:
                            _print(f"     · [{doc.doc_type}] {doc.title[:90]}  ({doc.url})")
                        break
                    else:
                        raise FetchError("Bugün ve dün için fihrist alınamadı")
                elif src.method == "html_listing" and src.config.get("list_urls") and src.config.get("item_link_pattern"):
                    adapter = build_adapter(src)
                    for list_url in src.config["list_urls"]:
                        res = fetcher.get(list_url)
                        docs = adapter._parse(res.text, res.url)
                        mark = "✔" if docs else "✖"
                        _print(f"{mark} {src.name}{tag}: {list_url} → kalıba uyan {len(docs)} kayıt")
                        for doc in docs[:4]:
                            _print(f"     · {doc.title[:90]}  ({doc.url})")
                        if not docs:
                            failures += src.enabled
                elif src.method == "rss" and src.config.get("feed_urls"):
                    from .sources.rss import parse_feed

                    for feed_url in src.config["feed_urls"]:
                        res = fetcher.get(feed_url)
                        entries = parse_feed(res.text or res.content.decode("utf-8", "replace"))
                        mark = "✔" if entries else "✖"
                        _print(f"{mark} {src.name}{tag}: {feed_url} → {len(entries)} kayıt")
                        for e in entries[:4]:
                            _print(f"     · {e['title'][:90]}  ({e['link']})")
                        if not entries:
                            failures += src.enabled
                else:
                    allowed = fetcher.robots_allowed(src.url)
                    res = fetcher.get(src.url)
                    _print(f"• {src.name}{tag}: robots.txt izin={'evet' if allowed else 'hayır'}, HTTP {res.status_code}, "
                           f"{len(res.text)} karakter (ayrıştırıcı tanımlı değil)")
            except RobotsDisallowed as exc:
                _print(f"✖ {src.name}{tag}: {exc}")
                failures += src.enabled
            except Exception as exc:
                _print(f"✖ {src.name}{tag}: {type(exc).__name__}: {exc}")
                failures += src.enabled
    fetcher.close()
    _print(f"\n{failures} etkin kaynakta sorun." if failures else "\nTüm etkin kaynaklar erişilebilir ve ayrıştırılabiliyor.")
    return 1 if failures else 0


def cmd_run(args) -> int:
    from .db import init_db, session_scope
    from .models import RegulatorySource
    from .pipeline import run_pipeline
    from .sources.registry import sync_sources
    from sqlalchemy import select

    if args.since:
        since = date.fromisoformat(args.since)
        init_db()
        with session_scope() as s:
            sync_sources(s, get_settings().sources_file)
            rg = s.scalar(select(RegulatorySource).where(RegulatorySource.key == "resmi_gazete"))
            rg.cursor = (since - timedelta(days=1)).isoformat()
        _print(f"Resmî Gazete {since.isoformat()} tarihinden itibaren taranacak.")
    result = run_pipeline(run_type=args.type, trigger="cli", send=not args.no_send)
    _print(f"Durum: {result.status} | tam tarama: {'evet' if result.complete else 'HAYIR'} | yeni belge: "
           f"{result.new_documents} | yeni değişiklik: {result.new_changes} | AI analizi: {result.analyzed}")
    if result.report_subject:
        _print(f"Rapor: {result.report_subject} → {result.report_status}")
    for w in result.warnings:
        _print(f"  uyarı: {w}")
    for e in result.errors:
        _print(f"  hata: {e}")
    return 0 if result.status in ("basarili", "kismi") else 1


def cmd_test_email(_args) -> int:
    from .db import init_db, session_scope
    from .reporting.mailer import OutgoingEmail, send_with_retry
    from .runtime import effective_recipients

    settings = get_settings()
    init_db()
    with session_scope() as s:
        to, cc = effective_recipients(s, settings)
    ok = send_with_retry(settings, OutgoingEmail(
        subject="[ENERJİ MEVZUAT] Test e-postası",
        text="Bu bir test e-postasıdır. SMTP yapılandırması çalışıyor.",
        html="<p>Bu bir <strong>test</strong> e-postasıdır. SMTP yapılandırması çalışıyor.</p>",
        to=to, cc=cc), on_attempt=lambda a, st, err: _print(f"deneme {a}: {st} {err or ''}"))
    return 0 if ok else 1


def cmd_scheduler(_args) -> int:
    from .runtime import setup_logging
    from .scheduler import run_blocking

    setup_logging(get_settings())
    run_blocking()
    return 0


def cmd_healthcheck(_args) -> int:
    from .db import init_db
    from .pipeline import check_heartbeat

    init_db()
    ok, msg = check_heartbeat()
    _print(msg)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="enerji-takip", description="Enerji Mevzuatı Takip ve Bildirim Sistemi")
    sub = p.add_subparsers(dest="cmd", required=True)
    ic = sub.add_parser("init-config", help="Varsayılan yapılandırmayı config/ altına kopyalar")
    ic.add_argument("--force", action="store_true")
    ic.set_defaults(fn=cmd_init_config)
    sub.add_parser("init-db").set_defaults(fn=cmd_init_db)
    sub.add_parser("check-config").set_defaults(fn=cmd_check_config)
    sub.add_parser("check-llm").set_defaults(fn=cmd_check_llm)
    cs = sub.add_parser("check-sources")
    cs.add_argument("--all", action="store_true", help="Devre dışı kaynakları da kontrol et")
    cs.set_defaults(fn=cmd_check_sources)
    r = sub.add_parser("run")
    r.add_argument("--type", choices=["gunluk", "kritik", "tarama"], default="gunluk",
                   help="tarama: yalnızca kaynakları tara (AI ve e-posta yok)")
    r.add_argument("--no-send", action="store_true", help="E-posta gönderme; raporu data/reports altına yaz")
    r.add_argument("--since", help="Resmî Gazete'yi bu tarihten (YYYY-AA-GG) itibaren tara")
    r.set_defaults(fn=cmd_run)
    sub.add_parser("test-email").set_defaults(fn=cmd_test_email)
    sub.add_parser("scheduler").set_defaults(fn=cmd_scheduler)
    sub.add_parser("healthcheck").set_defaults(fn=cmd_healthcheck)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
