"""Veri tabanı katmanı: bağlantı, oturum yönetimi ve bağlantı kesintisinde yeniden deneme."""
from __future__ import annotations

import functools
import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, TypeVar

from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from .config import app_home, get_settings
from .models import Base, SystemSetting, utcnow

log = logging.getLogger(__name__)

_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None

T = TypeVar("T")


def resolve_db_url(url: str) -> str:
    """Göreli sqlite yolunu uygulama dizinine (ENERJI_TAKIP_HOME / proje / ~/.enerji-mevzuat-takip) göre çözer."""
    prefix = "sqlite:///"
    if url.startswith(prefix) and not url.startswith(prefix + "/") and ":memory:" not in url:
        return prefix + str(app_home() / url[len(prefix):])
    return url


def _make_engine(url: str) -> Engine:
    kwargs: dict = {"future": True, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        url = resolve_db_url(url)
        if url.startswith("sqlite:///") and ":memory:" not in url:
            Path(url[len("sqlite:///"):]).parent.mkdir(parents=True, exist_ok=True)
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):  # pragma: no cover - basit
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")  # WAL kullanılmaz: tek dosya git'e işlenebilsin
            cur.close()
    return engine


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        _engine = _make_engine(get_settings().database_url)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def reset_engine(url: str | None = None) -> Engine:
    """Testlerde farklı bir veri tabanına geçmek için."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = _make_engine(url or get_settings().database_url)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def init_db() -> None:
    Base.metadata.create_all(get_engine())


def check_connection() -> None:
    with get_engine().connect() as conn:
        conn.execute(text("SELECT 1"))


@contextmanager
def session_scope() -> Iterator[Session]:
    get_engine()
    assert _SessionLocal is not None
    session: Session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def with_db_retry(attempts: int = 3, base_delay: float = 2.0) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Geçici bağlantı hatalarında (OperationalError) işlemi yeniden dener."""

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs) -> T:
            last_exc: Exception | None = None
            for attempt in range(1, attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except (OperationalError, DBAPIError) as exc:
                    if isinstance(exc, DBAPIError) and not exc.connection_invalidated and not isinstance(
                        exc, OperationalError
                    ):
                        raise
                    last_exc = exc
                    log.warning("Veri tabanı hatası (%s/%s): %s", attempt, attempts, exc)
                    if attempt < attempts:
                        time.sleep(base_delay * attempt)
            assert last_exc is not None
            raise last_exc

        return wrapper

    return decorator


# ---- Sistem ayarları (web arayüzünden düzenlenebilir) -------------------

def get_setting(session: Session, key: str) -> str | None:
    row = session.get(SystemSetting, key)
    return row.value if row else None


def set_setting(session: Session, key: str, value: str) -> None:
    row = session.get(SystemSetting, key)
    if row is None:
        session.add(SystemSetting(key=key, value=value))
    else:
        row.value = value
        row.updated_at = utcnow()


def all_settings(session: Session) -> dict[str, str]:
    return {row.key: row.value for row in session.scalars(select(SystemSetting))}


def ensure_data_dir() -> Path:
    data = app_home() / "data"
    data.mkdir(parents=True, exist_ok=True)
    return data
