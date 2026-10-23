"""RSS / Atom akışı izleyicisi.

Akış sunan kaynaklarda HTML ayrıştırmaya göre daha kararlıdır. XML, dış varlık ve
"billion laughs" saldırılarına karşı defusedxml ile ayrıştırılır. İlk çalıştırmada
akıştaki kayıtlar başlangıç (baseline) sayılır ve raporlanmaz.
"""
from __future__ import annotations

import re
from datetime import date
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin

from defusedxml import ElementTree as SafeET

from ..http import FetchError, Fetcher, RobotsDisallowed
from ..text_utils import normalize_space, parse_tr_date
from .base import FetchedDocument, ParseError, ScanOutcome
from .listing import HtmlListingAdapter


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


_URL = re.compile(r"^https?://", re.I)


def parse_feed(xml_text: str, *, base_url: str = "") -> list[dict]:
    """Akıştaki kayıtları döndürür. Bağlantı sırasıyla <link>, <guid> (URL ise), <enclosure>, <comments> ve
    <id> etiketlerinden alınır; göreli bağlantılar base_url ile tamamlanır."""
    try:
        root = SafeET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except Exception as exc:  # defusedxml de ParseError fırlatır
        raise ParseError(f"Akış XML olarak okunamadı: {exc}") from exc
    items = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        entry: dict = {}
        links: dict[str, str] = {}
        for child in node:
            name = _local(child.tag)
            if name == "title":
                entry["title"] = normalize_space("".join(child.itertext()))
            elif name in ("link", "guid", "id", "comments", "enclosure", "url"):
                value = (child.get("href") or child.get("url") or (child.text or "")).strip()
                if value and name not in links:
                    links[name] = value
            elif name in ("pubdate", "published", "updated", "date"):
                entry["date"] = (child.text or "").strip()
            elif name in ("description", "summary"):
                entry["summary"] = normalize_space(re.sub(r"<[^>]+>", " ", "".join(child.itertext())))[:500]
        for name in ("link", "guid", "id", "enclosure", "url", "comments"):
            value = links.get(name)
            if value and (_URL.match(value) or (base_url and value.startswith("/"))):
                entry["link"] = urljoin(base_url, value) if base_url else value
                break
        if entry.get("title") and entry.get("link"):
            items.append(entry)
    return items


def describe_feed(xml_text: str) -> str:
    """Kayıt bulunamadığında nedenini anlatan kısa tanı: kaç <item> var, ilkinin etiketleri neler."""
    try:
        root = SafeET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except Exception:
        return "XML değil"
    nodes = [n for n in root.iter() if _local(n.tag) in ("item", "entry")]
    if not nodes:
        return "akışta <item>/<entry> yok"
    first = nodes[0]
    tags = ", ".join(f"{_local(c.tag)}={(c.text or c.get('href') or '')[:40]!r}" for c in list(first)[:8])
    return f"{len(nodes)} kayıt var ama başlık+bağlantı yok; ilk kaydın etiketleri: {tags}"


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
                raw = result.text or result.content.decode("utf-8", "replace")
                entries = parse_feed(raw, base_url=feed_url)
                if not entries:
                    raise ParseError(
                        f"Akışta kayıt bulunamadı ({feed_url}) [HTTP {result.status_code}, {result.content_type or '?'}, "
                        f"{len(result.content)} bayt; {describe_feed(raw)}]")
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
