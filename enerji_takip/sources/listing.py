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


_TRAILING_DATE_RE = re.compile(r"\s(\d{1,2}[./]\d{1,2}[./]\d{4})\s*$")


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
            container = self._item_container(a, pattern, page_url, url)
            needs_detail = False
            if len(title) < min_len:
                # Bazı listelerde bağlantı metni "Devamı" gibi kısa olur; başlık, yalnızca bu kaydı içeren üst blokta.
                # Üst blok başka kayıtları da içeriyorsa metni alınmaz (birden çok duyurunun başlığı birleşirdi,
                # 25.11.2026 ETKB); başlık detay sayfasından okunur.
                if container is not None:
                    title = normalize_space(container.get_text(" ", strip=True))[:300]
                elif self.config.get("detail_title_pattern"):
                    title = f"{self.config.get('institution') or 'Kurum'} duyurusu ({url})"
                    needs_detail = True
                else:  # detay sayfasından başlık okunamıyorsa eski davranış: en yakın üst bloğun metni
                    parent = a.find_parent(["li", "tr", "article", "div"])
                    if parent is not None:
                        title = normalize_space(parent.get_text(" ", strip=True))[:300]
            if len(title) < min_len or (exclude and exclude.search(title)):
                continue
            pub = None
            if self.config.get("title_trailing_date"):
                # "Başlık 05.11.2026": sondaki tarih yayım tarihidir, başlıktan ayrılır
                m = _TRAILING_DATE_RE.search(title)
                if m and len(title[:m.start()].strip()) >= min_len:
                    pub = parse_tr_date(m.group(1))
                    title = title[:m.start()].strip()
            if pub is None and container is not None:
                pub = parse_tr_date(container.get_text(" ", strip=True)[:400])
            if url in found:
                prev = found[url]
                if prev.needs_detail_title and not needs_detail:
                    prev.title, prev.needs_detail_title = title, False
                    prev.published_date = prev.published_date or pub
                elif not needs_detail and len(title) > len(prev.title):
                    prev.title = title
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
                needs_detail_title=needs_detail,
            )
        return list(found.values())

    @staticmethod
    def _item_container(a, pattern: re.Pattern, page_url: str, url: str):
        """Bağlantının kaydına ait en yakın üst blok; yalnızca bu kaydın bağlantısını içeriyorsa döner."""
        container = a.find_parent(["li", "tr", "article", "div"])
        if container is None:
            return None
        others = {urldefrag(urljoin(page_url, x["href"].strip()))[0] for x in container.find_all("a", href=True)}
        others = {u for u in others if pattern.search(u)}
        return container if others <= {url} else None

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
        title_rx = self.config.get("detail_title_pattern")
        if title_rx and doc.needs_detail_title:
            m = re.search(title_rx, text, re.M)
            if m and normalize_space(m.group("title")):
                doc.title = normalize_space(m.group("title"))[:300]
                doc.needs_detail_title = False
                exclude = self.config.get("exclude_title_pattern")
                if exclude and re.search(exclude, doc.title, re.I):
                    doc.skip_reason = "Başlık hariç tutulan türden (exclude_title_pattern)"
        text = self._trim(text)
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

    def _trim(self, text: str) -> str:
        """Sayfa menüsünü ve alt bilgisini metinden atar (``text_start_after`` / ``text_end_before``).

        EPDK gibi sitelerde menü "Akaryakıt, LPG, Lisanssız Üretim…" gibi konu sözcükleri içerir; kırpılmazsa her
        duyuru bu sözcüklerle eşleşir. İşaret bulunamazsa metin olduğu gibi bırakılır (sayfa düzeni değişse de
        hiçbir belge boş kalmaz)."""
        start = self.config.get("text_start_after")
        if start:
            matches = list(re.finditer(start, text, re.M))
            if matches:
                trimmed = text[matches[-1].end():]
                if trimmed.strip():
                    text = trimmed
        end = self.config.get("text_end_before")
        if end:
            match = re.search(end, text, re.M)
            if match and text[:match.start()].strip():
                text = text[:match.start()]
        return text.strip()

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
