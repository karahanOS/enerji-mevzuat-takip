"""Resmî Gazete kaynağı.

Resmî Gazete'nin RSS veya API'si yoktur. Her günün içindekiler (fihrist) sayfası
sabit bir adres kalıbıyla yayımlanır:

    https://www.resmigazete.gov.tr/eskiler/YYYY/AA/YYYYAAGG.htm        (asıl sayı)
    https://www.resmigazete.gov.tr/eskiler/YYYY/AA/YYYYAAGGM1.htm      (1. mükerrer)
    belge:  .../YYYYAAGG-N.htm  veya  .../YYYYAAGG-N.pdf

Ayrıştırıcı CSS sınıflarına değil, belge sırasındaki bölüm başlıklarına ve bağlantı
adres kalıbına dayanır; böylece sayfa tasarımı değişse de çalışmaya devam eder.
Kalıp tümüyle bozulursa sessizce "0 belge" döndürmek yerine ParseError fırlatır.
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from ..http import FetchError, Fetcher, NotFound, RobotsDisallowed
from ..text_utils import normalize_space, parse_tr_date, tr_upper
from .base import DocumentText, FetchedDocument, ParseError, ScanOutcome, SourceAdapter

log = logging.getLogger(__name__)

BASE = "https://www.resmigazete.gov.tr"

ITEM_HREF_RE = re.compile(r"(?P<ymd>\d{8})(?P<muk>M\d+)?-(?P<no>\d+(?:-\d+)?)\.(?P<ext>htm|html|pdf)$", re.I)
MUKERRER_INDEX_RE = re.compile(r"(?P<ymd>\d{8})M(?P<n>\d+)\.htm$", re.I)
SAYI_RE = re.compile(r"Say[ıi]\s*:?\s*(\d{4,6})", re.I)
BLOCK_TAGS = ["p", "div", "td", "th", "li", "h1", "h2", "h3", "h4", "h5", "h6", "center"]

TOP_SECTIONS = {
    "YASAMA BÖLÜMÜ": "YASAMA",
    "YÜRÜTME VE İDARE BÖLÜMÜ": "YURUTME_IDARE",
    "YARGI BÖLÜMÜ": "YARGI",
    "İLÂN BÖLÜMÜ": "ILAN",
    "İLAN BÖLÜMÜ": "ILAN",
}

# Alt başlık → belge türü (tekil/çoğul biçimler)
SUBSECTIONS: list[tuple[str, str]] = [
    ("CUMHURBAŞKANLIĞI KARARNAME", "CB_KARARNAMESI"),
    ("CUMHURBAŞKANI KARAR", "CB_KARARI"),
    ("CUMHURBAŞKANLIĞI GENELGE", "GENELGE"),
    ("CUMHURBAŞKANLIĞINA VEKALETEN", "CB_KARARI"),
    ("MİLLETLERARASI ANDLAŞMA", "ANDLASMA"),
    ("MİLLETLERARASI ANTLAŞMA", "ANDLASMA"),
    ("ATAMA KARAR", "ATAMA"),
    ("KANUN", "KANUN"),
    ("YÖNETMELİK", "YONETMELIK"),
    ("TEBLİĞ", "TEBLIG"),
    ("GENELGE", "GENELGE"),
    ("YÖNERGE", "YONERGE"),
    ("KURUL KARAR", "KURUL_KARARI"),
    ("ANAYASA MAHKEMESİ KARAR", "AYM_KARARI"),
    ("YARGITAY", "YARGI_KARARI"),
    ("DANIŞTAY", "YARGI_KARARI"),
    ("UYUŞMAZLIK MAHKEMESİ", "YARGI_KARARI"),
    ("KARAR", "KARAR"),
]


def index_url(d: date, mukerrer: int = 0) -> str:
    ymd = d.strftime("%Y%m%d")
    suffix = f"M{mukerrer}" if mukerrer else ""
    return f"{BASE}/eskiler/{d:%Y}/{d:%m}/{ymd}{suffix}.htm"


def _heading_key(text: str) -> str:
    t = tr_upper(normalize_space(text)).strip(" :–-—")
    return re.sub(r"\s+", " ", t)


def classify_heading(text: str) -> tuple[str, str] | None:
    """Metin bir bölüm başlığı ise ('top'|'sub', değer) döndürür."""
    key = _heading_key(text)
    if not key or len(key) > 70:
        return None
    # Başlıklar büyük harftir; normal cümleleri başlık sanmamak için kontrol
    letters = [c for c in text if c.isalpha()]
    if letters and sum(1 for c in letters if c.isupper()) / len(letters) < 0.8:
        # İlan alt bölümleri "a - Yargı İlânları" biçiminde küçük harfle gelebilir
        if re.match(r"^[a-zçğ]\s*[-–]\s*\S", text.strip()) and "İLÂN" in key.replace("ILAN", "İLÂN"):
            return ("sub", "ILAN")
        return None
    for heading, value in TOP_SECTIONS.items():
        if key == heading:
            return ("top", value)
    for prefix, value in SUBSECTIONS:
        if key.startswith(prefix):
            return ("sub", value)
    return None


def doc_type_from_title(title: str) -> str:
    t = title.strip().rstrip(".").lower()
    t = t.replace("ı", "i").replace("ğ", "g")
    if re.search(r"yonetmeli(k|gi)$", t):
        return "YONETMELIK"
    if re.search(r"tebli(g|gi)( \(.*\))?$", t) or "seri no" in t:
        return "TEBLIG"
    if re.search(r"kanun(u)?$", t):
        return "KANUN"
    if "kararname" in t:
        return "CB_KARARNAMESI"
    if re.search(r"genelge(si)?( \(.*\))?$", t):
        return "GENELGE"
    if re.search(r"karar(i)?$", t):
        return "KARAR"
    return "DIGER"


def _clean_title(text: str) -> str:
    text = normalize_space(text)
    return re.sub(r"^[\s–—\-•·»]+", "", text).strip()


def parse_fihrist(html: str, page_url: str, expected: date | None = None) -> tuple[list[FetchedDocument], dict]:
    """Bir fihrist sayfasını belgelere ayrıştırır. (belgeler, başlık bilgisi) döndürür."""
    soup = BeautifulSoup(html, "lxml")
    body = soup.body or soup
    full_text = body.get_text(" ", strip=True)
    meta: dict = {"rg_number": None, "rg_date": None, "mukerrer_links": []}

    m = SAYI_RE.search(full_text)
    if m:
        meta["rg_number"] = m.group(1)
    meta["rg_date"] = parse_tr_date(full_text[:600]) or expected
    is_mukerrer = bool(re.search(r"M\d+\.htm$", page_url, re.I)) or "MÜKERRER" in tr_upper(full_text[:400])

    top: str | None = None
    sub: str | None = None
    docs: dict[str, FetchedDocument] = {}
    order: list[str] = []

    def handle_heading(text: str) -> None:
        nonlocal top, sub
        heading = classify_heading(text)
        if heading:
            kind, value = heading
            if kind == "top":
                top, sub = value, None
            else:
                sub = value

    def handle_anchor(node: Tag) -> None:
        href = node["href"].strip()
        abs_url = urljoin(page_url, href)
        if MUKERRER_INDEX_RE.search(abs_url):
            meta["mukerrer_links"].append(abs_url)
            return
        im = ITEM_HREF_RE.search(abs_url)
        if not im:
            return
        ext_id = f"{im.group('ymd')}{im.group('muk') or ''}-{im.group('no')}"
        title = _clean_title(node.get_text(" ", strip=True))
        ext = im.group("ext").lower()
        doc = docs.get(ext_id)
        if doc is None:
            doc_type = "ILAN" if top == "ILAN" else (sub or doc_type_from_title(title))
            doc = FetchedDocument(
                source_key="resmi_gazete",
                external_id=ext_id,
                title=title,
                url=abs_url,
                doc_type=doc_type,
                section=top,
                subsection=sub,
                rg_date=meta["rg_date"],
                rg_number=meta["rg_number"],
                is_mukerrer=is_mukerrer or bool(im.group("muk")),
                published_date=meta["rg_date"],
                skip_reason="İlan bölümü" if top == "ILAN" else None,
            )
            if ext == "pdf":
                doc.pdf_url = abs_url
            docs[ext_id] = doc
            order.append(ext_id)
        else:
            # Aynı belgenin ikinci bağlantısı (hem .htm hem .pdf ya da bölünmüş başlık)
            if ext == "pdf" and not doc.pdf_url:
                doc.pdf_url = abs_url
            elif ext in ("htm", "html") and doc.url.lower().endswith(".pdf"):
                doc.url = abs_url
            if title and title not in doc.title and len(doc.title) < 400:
                doc.title = f"{doc.title} {title}".strip()

    # Yaprak blokları (iç içe blok içermeyen p/div/td...) belge sırasıyla gez.
    # Word çıktısında başlıklar birden çok <span>'a bölünebildiği için blok içindeki
    # metin parçaları birleştirilerek başlık kontrolü yapılır.
    blocks = [b for b in body.find_all(BLOCK_TAGS) if not b.find(BLOCK_TAGS)]
    processed: set[int] = set()
    for block in blocks:
        buffer: list[str] = []
        for node in block.descendants:
            if isinstance(node, Tag) and node.name == "a" and node.get("href"):
                if buffer:
                    handle_heading(" ".join(buffer))
                    buffer = []
                processed.add(id(node))
                handle_anchor(node)
            elif isinstance(node, NavigableString) and not isinstance(node, Comment):
                if node.parent is not None and node.parent.name == "a":
                    continue
                if any(p.name == "a" for p in node.parents if isinstance(p, Tag)):
                    continue
                piece = str(node).strip()
                if piece:
                    buffer.append(piece)
        if buffer:
            handle_heading(" ".join(buffer))
    # Yaprak blok dışında kalan bağlantılar (yedek; bölüm bilgisi olmayabilir)
    for a in body.find_all("a", href=True):
        if id(a) not in processed:
            handle_anchor(a)

    result = [docs[k] for k in order]
    # Yürütme bölümündeki tür tahmini: başlık daha kesin bilgi veriyorsa onu kullan
    for doc in result:
        if doc.doc_type in ("KARAR", "DIGER"):
            guess = doc_type_from_title(doc.title)
            if guess != "DIGER":
                doc.doc_type = guess

    if not result and len(full_text) > 300:
        raise ParseError(
            f"Fihrist sayfasında hiç belge bağlantısı bulunamadı ({page_url}). "
            "Sayfa yapısı değişmiş olabilir; ayrıştırıcı güncellenmeli."
        )
    if expected and meta["rg_date"] and meta["rg_date"] != expected:
        log.warning("Fihrist tarihi beklenenden farklı: %s ≠ %s (%s)", meta["rg_date"], expected, page_url)
    return result, meta


_INSTITUTION_RE = re.compile(r"^\s*([A-ZÇĞİÖŞÜ][^\n:]{3,150}?(?:dan|den|tan|ten)):\s*$", re.M)


def extract_institution(text: str) -> str | None:
    m = _INSTITUTION_RE.search(text[:3000])
    return m.group(1).strip() if m else None


class ResmiGazeteAdapter(SourceAdapter):
    method = "rg_fihrist"

    def _dates_to_scan(self, cursor: str | None, today: date, settings) -> tuple[list[date], list[date], list[date]]:
        """(yeni tarihler, yeniden taranacak tarihler, atlanan tarihler)."""
        if cursor:
            last = date.fromisoformat(cursor)
            start = last + timedelta(days=1)
        else:
            start = today - timedelta(days=max(settings.rg_first_run_days, 1) - 1)
            last = start - timedelta(days=1)
        skipped: list[date] = []
        earliest_allowed = today - timedelta(days=settings.rg_max_backfill_days - 1)
        if start < earliest_allowed:
            d = start
            while d < earliest_allowed:
                skipped.append(d)
                d += timedelta(days=1)
            start = earliest_allowed
        new_dates = [start + timedelta(days=i) for i in range((today - start).days + 1)] if start <= today else []
        rescan = [] if not cursor else [
            last - timedelta(days=i) for i in range(settings.rg_rescan_days - 1, -1, -1)
            if last - timedelta(days=i) >= earliest_allowed
        ]
        return new_dates, rescan, skipped

    def _scan_date(self, fetcher: Fetcher, d: date) -> list[FetchedDocument]:
        url = index_url(d)
        result = fetcher.get(url)
        docs, meta = parse_fihrist(result.text, result.url, expected=d)
        # Mükerrer sayılar: fihristteki bağlantılar + M1, M2... denemesi
        mukerrer_urls = list(dict.fromkeys(meta["mukerrer_links"]))
        n = 1
        while n <= 5:
            u = index_url(d, n)
            if u not in mukerrer_urls:
                try:
                    fetcher.get(u)  # varlık kontrolü
                except NotFound:
                    break
                mukerrer_urls.append(u)
            n += 1
        for u in mukerrer_urls:
            try:
                r = fetcher.get(u)
            except NotFound:
                continue
            mdocs, _ = parse_fihrist(r.text, r.url, expected=d)
            docs.extend(mdocs)
        return docs

    def scan(self, fetcher: Fetcher, *, cursor: str | None, today: date, state: dict) -> ScanOutcome:
        from ..config import get_settings

        settings = get_settings()
        outcome = ScanOutcome()
        new_dates, rescan, skipped = self._dates_to_scan(cursor, today, settings)
        coverage = {"taranan": [], "basarisiz": {}, "yayimlanmamis": [], "atlanan": [d.isoformat() for d in skipped]}
        if skipped:
            outcome.complete = False
            outcome.errors.append(
                f"{len(skipped)} günlük boşluk azami geri tarama sınırını (RG_MAX_BACKFILL_DAYS="
                f"{settings.rg_max_backfill_days}) aşıyor; bu günler taranmadı: "
                f"{skipped[0].isoformat()} – {skipped[-1].isoformat()}"
            )
        missing_counts: dict = dict(state.get("rg_404", {}))
        contiguous_ok: date | None = date.fromisoformat(cursor) if cursor else None
        chain_broken = False

        for d in rescan + new_dates:
            is_new = d in new_dates
            try:
                docs = self._scan_date(fetcher, d)
            except RobotsDisallowed as exc:
                outcome.access_status = "robots_engeli"
                outcome.errors.append(str(exc))
                outcome.complete = False
                coverage["basarisiz"][d.isoformat()] = "robots.txt engeli"
                chain_broken = True
                break  # aynı site için diğer günleri denemenin anlamı yok
            except NotFound:
                if d == today:
                    coverage["yayimlanmamis"].append(d.isoformat())
                    outcome.warnings.append(f"{d.isoformat()} sayısı henüz yayımlanmamış görünüyor; sonraki çalıştırmada tekrar denenecek.")
                    chain_broken = True
                    continue
                if not is_new:
                    continue
                count = missing_counts.get(d.isoformat(), 0) + 1
                missing_counts[d.isoformat()] = count
                if count >= 3:
                    outcome.warnings.append(f"{d.isoformat()} için fihrist 3 denemede de bulunamadı; o gün yayın olmadığı kabul edildi.")
                    coverage["taranan"].append(d.isoformat())
                    if not chain_broken:
                        contiguous_ok = d
                    continue
                coverage["basarisiz"][d.isoformat()] = "fihrist bulunamadı (404)"
                outcome.errors.append(f"{d.isoformat()} fihristi bulunamadı (404); sonraki çalıştırmada yeniden denenecek.")
                outcome.complete = False
                chain_broken = True
                continue
            except (FetchError, ParseError) as exc:
                if is_new:
                    coverage["basarisiz"][d.isoformat()] = str(exc)
                    outcome.errors.append(f"{d.isoformat()}: {exc}")
                    outcome.complete = False
                    chain_broken = True
                else:
                    outcome.warnings.append(f"Yeniden tarama {d.isoformat()}: {exc}")
                if isinstance(exc, FetchError) and exc.status_code in (401, 403):
                    outcome.access_status = "erisim_reddedildi"
                continue
            outcome.documents.extend(docs)
            if is_new:
                coverage["taranan"].append(d.isoformat())
                missing_counts.pop(d.isoformat(), None)
                if not chain_broken:
                    contiguous_ok = d

        if outcome.errors and not coverage["taranan"] and outcome.access_status == "erisilebilir":
            outcome.access_status = "erisilemiyor"
        outcome.new_cursor = contiguous_ok.isoformat() if contiguous_ok else cursor
        state["rg_404"] = {k: v for k, v in missing_counts.items()}
        outcome.coverage = coverage
        return outcome

    def fetch_text(self, fetcher: Fetcher, doc: FetchedDocument) -> DocumentText:
        result = super().fetch_text(fetcher, doc)
        if result.text:
            result.institution = extract_institution(result.text)
        return result
