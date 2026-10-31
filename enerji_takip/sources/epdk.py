"""EPDK Kurul Kararları izleyicisi (sayfa içi AJAX uç noktası).

Karar listesi sayfa kaynağında yoktur: kategori ağacındaki her yaprak (``data-id``) tıklanınca
``POST /Detay/GetFastAccessList {"fId": "<id>"}`` çağrılır ve JSON döner. Bu bağdaştırıcı
sayfayı bir kez alır (oturum çerezi için), yaprakların kimliklerini çıkarır ve her biri için aynı
isteği yapar. Belge adresi: ``/Detay/DownloadDocument?id=<ContentId>``. Dosya türü ``IsFileType``'tan
değil içerikten (PDF / docx) anlaşılır; canlı veride çoğu karar Word (docx), bazıları PDF'tir. Kararlar karar numarasıyla tekilleştirilir; ilk çalıştırmada bulunanlar başlangıç
(baseline) sayılır ve raporlanmaz.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup

from ..http import FetchError, Fetcher, RobotsDisallowed
from ..text_utils import normalize_space, parse_tr_date
from .base import FetchedDocument, ParseError, ScanOutcome
from .listing import HtmlListingAdapter

_DOTNET_DATE = re.compile(r"/Date\((-?\d+)")


def parse_leaves(html: str) -> list[tuple[str, str]]:
    """Sayfadaki yaprak düğümleri: [(fId, etiket)]. Yaprak = onclick'inde ShowDetailList olan bağlantı."""
    soup = BeautifulSoup(html, "lxml")
    leaves, seen = [], set()
    for a in soup.find_all("a", attrs={"data-id": True}):
        if "ShowDetailList" not in (a.get("onclick") or ""):
            continue
        fid = str(a["data-id"]).strip()
        if not fid or fid in seen:
            continue
        seen.add(fid)
        leaves.append((fid, normalize_space(a.get_text(" ", strip=True))))
    return leaves


def _parse_date(item: dict, text_key: str, value_key: str) -> date | None:
    d = parse_tr_date(str(item.get(text_key) or ""))
    if d:
        return d
    m = _DOTNET_DATE.search(str(item.get(value_key) or ""))
    if m:
        # .NET tarihi UTC milisaniyedir; İstanbul gece yarısı bir önceki gün 21:00 UTC olur
        return (datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc) + timedelta(hours=3)).date()
    return None


class EpdkKararlarAdapter(HtmlListingAdapter):
    method = "epdk_kararlar"

    def _file_url(self, item: dict, kind: int) -> str | None:
        base = self.config.get("download_url", "https://www.epdk.gov.tr/Detay/DownloadDocument?id=")
        files = [f for f in item.get("FastAccessDetail") or [] if f.get("ContentId")]
        for f in files:
            if f.get("IsFileType") == kind:
                return base + quote(f["ContentId"], safe="=/")
        if kind is None and files:  # IsFileType boş olan kayıtlar (canlı veride ~63 kayıt)
            return base + quote(files[0]["ContentId"], safe="=/")
        return None

    def parse_items(self, payload: dict, label: str, *, since: date | None) -> tuple[list[FetchedDocument], int]:
        """JSON yanıtından belgeler; (belgeler, belgesiz_kayıt_sayısı)."""
        prefer = int(self.config.get("prefer_file_type", 2))
        other = 1 if prefer == 2 else 2
        docs, no_file = [], 0
        for item in payload.get("model") or []:
            number = normalize_space(str(item.get("Number") or ""))
            title = normalize_space(str(item.get("Title") or ""))
            if not number or not title:
                continue
            decided = _parse_date(item, "Date", "DateValue")
            if since and decided and decided < since:
                continue
            url = (self._file_url(item, prefer) or self._file_url(item, other) or self._file_url(item, None)
                   or item.get("Url"))
            if not url:
                no_file += 1
                continue
            alt = self._file_url(item, other)
            rg_date = _parse_date(item, "RgDate", "RgDateValue")
            docs.append(FetchedDocument(
                source_key=self.source_key,
                external_id=f"epdk-karar:{number}",
                title=f"{title} ({number} sayılı Kurul Kararı)",
                url=url,
                pdf_url=alt if alt != url else None,
                doc_type="KURUL_KARARI",
                section=label or None,
                institution=self.config.get("institution", "Enerji Piyasası Düzenleme Kurumu"),
                published_date=decided,
                rg_date=rg_date,
                rg_number=str(item.get("RgNumber") or "") or None,
            ))
        return docs, no_file

    def scan(self, fetcher: Fetcher, *, cursor: str | None, today: date, state: dict) -> ScanOutcome:
        outcome = ScanOutcome()
        page_url = self.config["page_url"]
        post_url = self.config.get("post_url") or urljoin(page_url, "/Detay/GetFastAccessList")
        since = today - timedelta(days=int(self.config.get("max_age_days", 120)))
        coverage: dict = {"yapraklar": 0, "hatali_yapraklar": 0, "belgesiz_kayit": 0}
        try:
            page = fetcher.get(page_url)
            leaves = parse_leaves(page.text)
            if self.config.get("leaf_ids"):
                wanted = {str(x) for x in self.config["leaf_ids"]}
                leaves = [lf for lf in leaves if lf[0] in wanted]
            if not leaves:
                raise ParseError(f"Sayfada karar kategorisi (data-id + ShowDetailList) bulunamadı ({page_url}); sayfa yapısı değişmiş olabilir.")
        except RobotsDisallowed as exc:
            outcome.access_status, outcome.complete = "robots_engeli", False
            outcome.errors.append(str(exc))
            outcome.coverage = {"hata": "robots.txt engeli"}
            return outcome
        except (FetchError, ParseError) as exc:
            outcome.access_status, outcome.complete = "erisilemiyor", False
            outcome.errors.append(str(exc))
            outcome.coverage = {"hata": str(exc)}
            outcome.new_cursor = cursor
            return outcome

        coverage["yapraklar"] = len(leaves)
        by_number: dict[str, FetchedDocument] = {}
        for fid, label in leaves:
            try:
                res = fetcher.post_json(post_url, {"fId": fid}, referer=page_url)
                payload = json.loads(res.text or res.content.decode("utf-8", "replace"))
                if not isinstance(payload, dict) or "model" not in payload:
                    raise ParseError(f"Beklenmeyen yanıt biçimi (fId={fid})")
                docs, no_file = self.parse_items(payload, label, since=since)
            except RobotsDisallowed as exc:
                outcome.access_status, outcome.complete = "robots_engeli", False
                outcome.errors.append(str(exc))
                break
            except (FetchError, ParseError, ValueError) as exc:
                coverage["hatali_yapraklar"] += 1
                outcome.complete = False
                outcome.errors.append(f"{label or fid}: {exc}")
                continue
            coverage["belgesiz_kayit"] += no_file
            for d in docs:
                by_number.setdefault(d.external_id, d)
        outcome.documents = list(by_number.values())
        if not outcome.complete and outcome.access_status == "erisilebilir" and coverage["hatali_yapraklar"] >= len(leaves):
            outcome.access_status = "erisilemiyor"
        if coverage["belgesiz_kayit"]:
            outcome.warnings.append(f"{coverage['belgesiz_kayit']} karar kaydında indirilebilir belge yok (atlandı)")
        coverage["belge"] = len(outcome.documents)
        outcome.coverage = coverage
        outcome.new_cursor = today.isoformat() if outcome.complete else cursor
        return outcome
