"""RSS / Atom akışı izleyicisi.

Akış sunan kaynaklarda HTML ayrıştırmaya göre daha kararlıdır. XML, dış varlık ve
"billion laughs" saldırılarına karşı defusedxml ile ayrıştırılır. İlk çalıştırmada
akıştaki kayıtlar başlangıç (baseline) sayılır ve raporlanmaz.
"""
from __future__ import annotations

import re
from datetime import date
from email.utils import parsedate_to_datetime

from defusedxml import ElementTree as SafeET

from ..http import FetchError, Fetcher, RobotsDisallowed
from ..text_utils import normalize_space, parse_tr_date
from .base import FetchedDocument, ParseError, ScanOutcome
from .listing import HtmlListingAdapter


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_feed(xml_text: str) -> list[dict]:
    try:
        root = SafeET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except Exception as exc:  # defusedxml de ParseError fırlatır
        raise ParseError(f"Akış XML olarak okunamadı: {exc}") from exc
    items = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        entry: dict = {}
        for child in node:
            name = _local(child.tag)
            if name == "title":
                entry["title"] = normalize_space("".join(child.itertext()))
            elif name == "link":
                entry["link"] = (child.get("href") or (child.text or "")).strip()
            elif name in ("pubdate", "published", "updated", "date"):
                entry["date"] = (child.text or "").strip()
            elif name in ("description", "summary"):
                entry["summary"] = normalize_space(re.sub(r"<[^>]+>", " ", "".join(child.itertext())))[:500]
        if entry.get("title") and entry.get("link"):
            items.append(entry)
    return items


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).date()
    except (TypeError, ValueError, IndexError):
        pass
    if re.match(r"\d{4}-\d{2}-\d{2}", value):
        return date.fromisoformat(value[:10])
    return parse_tr_date(value)


class RssAdapter(HtmlListingAdapter):
    """Akıştaki her yeni bağlantı yeni belge sayılır; metin alma HTML liste adaptörüyle aynıdır."""

    method = "rss"

    def scan(self, fetcher: Fetcher, *, cursor: str | None, today: date, state: dict) -> ScanOutcome:
        outcome = ScanOutcome()
        coverage = {"akislar": {}}
        exclude = re.compile(self.config["exclude_title_pattern"], re.I) if self.config.get("exclude_title_pattern") else None
        for feed_url in self.config.get("feed_urls") or []:
            try:
                result = fetcher.get(feed_url)
                entries = parse_feed(result.text or result.content.decode("utf-8", "replace"))
                if not entries:
                    raise ParseError(f"Akışta kayıt bulunamadı ({feed_url})")
            except RobotsDisallowed as exc:
                outcome.access_status = "robots_engeli"
                outcome.complete = False
                outcome.errors.append(str(exc))
                coverage["akislar"][feed_url] = "robots.txt engeli"
                continue
            except (FetchError, ParseError) as exc:
                outcome.complete = False
                outcome.errors.append(str(exc))
                coverage["akislar"][feed_url] = f"hata: {exc}"
                if outcome.access_status == "erisilebilir":
                    outcome.access_status = "erisilemiyor"
                continue
            kept = 0
            for e in entries:
                if exclude and exclude.search(e["title"]):
                    continue
                outcome.documents.append(FetchedDocument(
                    source_key=self.source_key, external_id=e["link"], title=e["title"], url=e["link"],
                    doc_type=self.config.get("doc_type", "DUYURU"), institution=self.config.get("institution"),
                    published_date=_parse_date(e.get("date")),
                ))
                kept += 1
            coverage["akislar"][feed_url] = f"{kept} kayıt"
        outcome.coverage = coverage
        outcome.new_cursor = today.isoformat() if outcome.complete else cursor
        return outcome
