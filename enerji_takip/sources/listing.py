"""Genel amaçlı kurum duyuru/karar listesi izleyicisi.

RSS veya API sunmayan kurum sitelerinde (SGK, EPDK, KVKK, UAB, ÇSGB, Rekabet)
liste sayfasındaki bağlantılar, yapılandırmadaki adres kalıbıyla süzülür.
Daha önce görülmemiş her bağlantı yeni belge kabul edilir.

İlk çalıştırmada liste sayfasında zaten bulunan kayıtlar "başlangıç (baseline)"
olarak işaretlenir ve raporlanmaz; böylece ilk e-posta eski duyurularla dolmaz.
"""
from __future__ import annotations

import re
from datetime import date
from urllib.parse import urldefrag, urljoin

from bs4 import BeautifulSoup

from ..http import FetchError, Fetcher, RobotsDisallowed
from ..text_utils import html_to_text, normalize_space, parse_tr_date
from .base import DocumentText, FetchedDocument, ParseError, ScanOutcome, SourceAdapter


class HtmlListingAdapter(SourceAdapter):
    method = "html_listing"

    def _parse(self, html: str, page_url: str) -> list[FetchedDocument]:
        pattern = re.compile(self.config["item_link_pattern"])
        exclude = re.compile(self.config["exclude_title_pattern"], re.I) if self.config.get("exclude_title_pattern") else None
        min_len = int(self.config.get("title_min_length", 12))
        soup = BeautifulSoup(html, "lxml")
        found: dict[str, FetchedDocument] = {}
        for a in soup.find_all("a", href=True):
            url = urldefrag(urljoin(page_url, a["href"].strip()))[0]
            if not pattern.search(url):
                continue
            title = normalize_space(a.get_text(" ", strip=True)) or normalize_space(a.get("title", ""))
            if len(title) < min_len:
                # Bazı listelerde bağlantı metni "Devamı" gibi kısa olur; başlık üst blokta
                parent = a.find_parent(["li", "tr", "article", "div"])
                if parent:
                    title = normalize_space(parent.get_text(" ", strip=True))[:300]
            if len(title) < min_len or (exclude and exclude.search(title)):
                continue
            pub = None
            container = a.find_parent(["li", "tr", "article", "div"])
            if container is not None:
                pub = parse_tr_date(container.get_text(" ", strip=True)[:400])
            if url in found:
                if len(title) > len(found[url].title):
                    found[url].title = title
                continue
            found[url] = FetchedDocument(
                source_key=self.source_key,
                external_id=url,
                title=title,
                url=url,
                doc_type=self.config.get("doc_type", "DUYURU"),
                section=self.config.get("section"),
                institution=self.config.get("institution"),
                published_date=pub,
            )
        return list(found.values())

    def scan(self, fetcher: Fetcher, *, cursor: str | None, today: date, state: dict) -> ScanOutcome:
        outcome = ScanOutcome()
        urls = self.config.get("list_urls") or []
        coverage = {"liste_sayfalari": {}}
        for list_url in urls:
            try:
                result = fetcher.get(list_url)
                docs = self._parse(result.text, result.url)
                if not docs:
                    raise ParseError(
                        f"Liste sayfasında kalıba uyan bağlantı bulunamadı ({list_url}); sayfa yapısı değişmiş olabilir."
                    )
            except RobotsDisallowed as exc:
                outcome.access_status = "robots_engeli"
                outcome.complete = False
                outcome.errors.append(str(exc))
                coverage["liste_sayfalari"][list_url] = "robots.txt engeli"
                continue
            except (FetchError, ParseError) as exc:
                outcome.complete = False
                outcome.errors.append(str(exc))
                coverage["liste_sayfalari"][list_url] = f"hata: {exc}"
                if outcome.access_status == "erisilebilir":
                    outcome.access_status = "erisilemiyor"
                continue
            coverage["liste_sayfalari"][list_url] = f"{len(docs)} kayıt"
            outcome.documents.extend(docs)
        outcome.coverage = coverage
        outcome.new_cursor = today.isoformat() if outcome.complete else cursor
        return outcome

    def fetch_text(self, fetcher: Fetcher, doc: FetchedDocument) -> DocumentText:
        if doc.url.lower().endswith(".pdf"):
            return super().fetch_text(fetcher, doc)
        try:
            result = fetcher.get(doc.url)
        except FetchError as exc:
            return DocumentText(text="", status="okunamadi", note=str(exc))
        if result.is_pdf:
            return super().fetch_text(fetcher, doc)
        selector = self.config.get("content_selector")
        text = ""
        if selector:
            soup = BeautifulSoup(result.text, "lxml")
            node = soup.select_one(selector)
            if node is not None:
                text = html_to_text(str(node))
        if not text:
            text = html_to_text(result.text)
        status = "tam" if len(text) >= 400 else ("kisa" if text else "okunamadi")
        return DocumentText(text=text, status=status)
