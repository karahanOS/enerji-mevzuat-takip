"""Genel amaçlı kurum duyuru/karar listesi izleyicisi.

RSS veya API sunmayan kurum sitelerinde (SGK, EPDK, KVKK, UAB, ÇSGB, Rekabet)
liste sayfasındaki bağlantılar, yapılandırmadaki adres kalıbıyla süzülür.
Daha önce görülmemiş her bağlantı yeni belge kabul edilir.

Duyuru sayfası yalnızca "Duyuru metni için tıklayınız" gibi bir bağlantı içeriyorsa (ör. UHDGM),
``attachment_link_pattern`` ile eşleşen ilk ek (PDF/DOCX) indirilir ve metni sayfa metnine eklenir.

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
        self.last_pattern_matches = 0
        for a in soup.find_all("a", href=True):
            url = urldefrag(urljoin(page_url, a["href"].strip()))[0]
            if not pattern.search(url):
                continue
            self.last_pattern_matches += 1
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
                if not docs and self.last_pattern_matches:
                    # Kalıba uyan bağlantılar var ama hepsi hariç tutuldu (ör. yalnızca personel alımı duyuruları):
                    # sayfa yapısı sağlam, yeni ilgili kayıt yok demektir.
                    coverage["liste_sayfalari"][list_url] = (f"0 kayıt ({self.last_pattern_matches} bağlantı "
                                                             "hariç tutma kalıbıyla elendi)")
                    continue
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
        soup = BeautifulSoup(result.text, "lxml")
        if selector:
            node = soup.select_one(selector)
            if node is not None:
                text = html_to_text(str(node))
        if not text:
            text = html_to_text(result.text)
        attachment = self._attachment_url(soup, result.url)
        if attachment:
            att = super().fetch_text(fetcher, FetchedDocument(
                source_key=doc.source_key, external_id=attachment, title=doc.title, url=attachment,
                doc_type=doc.doc_type, institution=doc.institution))
            if att.text:
                if not doc.pdf_url:
                    doc.pdf_url = attachment
                text = (text + "\n\n--- EK BELGE METNİ ---\n" + att.text).strip()
        status = "tam" if len(text) >= 400 else ("kisa" if text else "okunamadi")
        return DocumentText(text=text, status=status)

    def _attachment_url(self, soup: BeautifulSoup, page_url: str) -> str | None:
        pattern = self.config.get("attachment_link_pattern")
        if not pattern:
            return None
        rx = re.compile(pattern, re.I)
        for a in soup.find_all("a", href=True):
            url = urldefrag(urljoin(page_url, a["href"].strip()))[0]
            if rx.search(url):
                return url
        return None
