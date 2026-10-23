"""Kaynak kayıt defteri: config/sources.yaml ↔ RegulatorySource tablosu."""
from __future__ import annotations

from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import RegulatorySource
from .base import SourceAdapter
from .listing import HtmlListingAdapter
from .resmi_gazete import ResmiGazeteAdapter
from .rss import RssAdapter

ADAPTERS: dict[str, type[SourceAdapter]] = {
    ResmiGazeteAdapter.method: ResmiGazeteAdapter,
    HtmlListingAdapter.method: HtmlListingAdapter,
    RssAdapter.method: RssAdapter,
}

REQUIRED_FIELDS = ("key", "name", "url", "category", "method")


def load_source_definitions(path: Path) -> list[dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    sources = data.get("sources", [])
    for s in sources:
        missing = [f for f in REQUIRED_FIELDS if not s.get(f)]
        if missing:
            raise ValueError(f"sources.yaml: '{s.get('key', '?')}' kaydında eksik alan: {missing}")
        if s["method"] not in ADAPTERS and s.get("enabled", True):
            raise ValueError(f"sources.yaml: '{s['key']}' için bilinmeyen yöntem '{s['method']}'")
    return sources


def sync_sources(session: Session, path: Path) -> list[RegulatorySource]:
    """YAML'daki tanımları veri tabanına yazar; çalışma durumu alanlarına dokunmaz."""
    defs = load_source_definitions(path)
    existing = {s.key: s for s in session.scalars(select(RegulatorySource))}
    rows = []
    for d in defs:
        row = existing.get(d["key"])
        if row is None:
            row = RegulatorySource(key=d["key"])
            session.add(row)
        row.name = d["name"]
        row.url = d["url"]
        row.category = d["category"]
        row.method = d["method"]
        row.frequency = d.get("frequency", "gunluk")
        row.enabled = bool(d.get("enabled", True))
        row.is_primary = bool(d.get("primary", False))
        row.notes = d.get("notes")
        cfg = dict(d.get("config") or {})
        # Çalışma durumu (ör. rg_404 sayaçları) config içinde '_state' altında saklanır
        if row.config and "_state" in row.config:
            cfg["_state"] = row.config["_state"]
        row.config = cfg
        if not row.enabled:
            row.access_status = d.get("status_if_disabled", "devre_disi")
        rows.append(row)
    session.flush()
    return rows


def build_adapter(source: RegulatorySource) -> SourceAdapter:
    cls = ADAPTERS[source.method]
    cfg = {k: v for k, v in (source.config or {}).items() if k != "_state"}
    return cls(source.key, cfg)
