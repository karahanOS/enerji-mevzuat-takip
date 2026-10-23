"""E-posta rapor üreticisi: bildirilmemiş değişiklikleri toplar, Türkçe HTML + düz metin rapor oluşturur."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..classification import IMPACT_LABELS, LEVEL_LABELS, LEVEL_ORDER
from ..detection.extract import ASPECT_LABELS, CHANGE_TYPE_LABELS, DOC_TYPE_LABELS
from ..detection.relevance import Taxonomy
from ..models import EmailReport, EmailReportItem, RegulatoryChange
from ..text_utils import format_tr_date

TEMPLATE_DIR = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)

LEVEL_COLORS = {"KRITIK": "#b42318", "YUKSEK": "#c4320a", "ORTA": "#b54708", "DUSUK": "#475467"}
EFFECTIVE_STATUS_LABELS = {
    "kesin": "",
    "hesaplanan": "",
    "kismi": " (bazı hükümler için tarih belirsiz)",
    "farkli_tarihler": " (hükümlere göre farklı tarihler)",
    "belirsiz": "",
}
BASIS_LABELS = {"kesin": "", "hesaplanan": "hesaplandı", "belirsiz": "belirsiz"}
EXTRA_ASPECT_LABELS = {
    "fiyat_tarife_degisikligi": "Fiyat / tarife değişikliği",
    "piyasa_kurali_degisikligi": "Piyasa kuralı değişikliği",
    "destek_tesvik_degisikligi": "Destek / teşvik değişikliği",
    "lisans_rejimi_degisikligi": "Lisans rejimi değişikliği",
}

DISCLAIMER = (
    "Bu e-posta kişisel kullanım için otomatik bir mevzuat izleme sistemi tarafından oluşturulmuştur; hukuki görüş "
    "veya yatırım tavsiyesi değildir. Özet ve etki değerlendirmeleri ücretsiz yapay zekâ modelleriyle hazırlanmıştır "
    "ve hata içerebilir; bağlayıcı olan Resmî Gazete'de / ilgili kurum sitesinde yayımlanan metindir."
)


@dataclass
class SourceStatusRow:
    name: str
    status: str
    ok: bool
    last_success: str
    detail: str = ""
    disabled: bool = False


@dataclass
class ReportInput:
    report_type: str  # gunluk | kritik
    report_date: date
    period_start: datetime | None
    period_end: datetime
    is_complete: bool
    source_rows: list[SourceStatusRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    docs_scanned: int = 0
    docs_filtered: int = 0
    docs_skipped: int = 0
    ai_usage: str = ""


def to_local(dt: datetime | None, tz: str) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(tz)).strftime("%d.%m.%Y %H:%M")


def pending_changes(session: Session, report_type: str) -> list[RegulatoryChange]:
    """Bu türde başarıyla gönderilmiş hiçbir rapora girmemiş değişiklikler."""
    sent_ids = select(EmailReportItem.change_id).join(EmailReport).where(
        EmailReport.status == "gonderildi",
        EmailReport.report_type.in_(["gunluk"] if report_type == "gunluk" else ["gunluk", "kritik"]),
    )
    q = select(RegulatoryChange).where(
        RegulatoryChange.id.not_in(sent_ids),
        RegulatoryChange.status.in_(["analiz_edildi", "inceleme", "analiz_basarisiz", "rutin"]),
    )
    if report_type == "kritik":
        q = q.where(RegulatoryChange.importance == "KRITIK")
    changes = list(session.scalars(q))
    changes.sort(key=lambda c: (
        -LEVEL_ORDER.index(c.importance) if c.importance in LEVEL_ORDER else 0,
        c.document.rg_date or c.document.published_date or date.min,
    ))
    return changes


def _fmt(iso: str | None) -> str:
    return format_tr_date(date.fromisoformat(iso)) if iso else "belirsiz"


def _item_view(n: int, chg: RegulatoryChange, taxonomy: Taxonomy) -> dict:
    doc = chg.document
    analysis = chg.latest_valid_analysis
    a = analysis.parsed if analysis else None
    rg = None
    if doc.rg_date:
        rg = f"{format_tr_date(doc.rg_date)} / Sayı {doc.rg_number or '—'}" + (" (Mükerrer)" if doc.is_mukerrer else "")

    eff_lines = []
    for e in chg.effective_dates or []:
        if e.get("kind") != "yururluk":
            continue
        when = _fmt(e.get("date")) if e.get("date") else "Belirsiz — metne bakınız"
        basis = BASIS_LABELS.get(e.get("basis"), "")
        eff_lines.append(f"{e.get('provision')}: {when}" + (f" ({basis})" if basis and e.get("date") else ""))
    if not eff_lines and a:
        for t in a.get("tarihler", []):
            if t.get("tur") == "yururluk":
                eff_lines.append(f"{t.get('hukum') or 'Tüm hükümler'}: {_fmt(t.get('tarih'))} (AI; {t.get('kesinlik')})")
    if not eff_lines:
        eff_lines = ["Belirlenemedi — resmî metindeki yürürlük maddesine bakınız"]

    compliance, rule_dates = [], set()
    for e in chg.effective_dates or []:
        if e.get("kind") == "uyum_suresi":
            compliance.append(f"{e.get('provision')}: {_fmt(e.get('date'))}" + (f" — {e['note']}" if e.get("note") else ""))
            if e.get("date"):
                rule_dates.add(e["date"])
    if a:
        for t in a.get("tarihler", []):
            if t.get("tarih") in rule_dates:
                continue
            if t.get("tur") in ("uyum_son_tarihi", "gecis_suresi_sonu", "basvuru_suresi"):
                compliance.append(f"{t.get('hukum')}: {_fmt(t.get('tarih'))} ({t.get('kesinlik')}) — "
                                  f"“{(t.get('metindeki_ifade') or '')[:140]}”")

    if a:
        impact = a["piyasa_etkisi"]
        view = {
            "summary": a["ozet"],
            "impact": impact["aciklama"],
            "impact_level": IMPACT_LABELS.get(impact["duzey"], impact["duzey"]),
            "personal": a.get("kisisel_ilgi") or "",
            "market_items": a.get("etkilenen_piyasa_unsurlari") or [],
            "who": a.get("kimler_etkilenir") or [],
            "actions": a.get("takip_onerileri") or [],
            "provisions": (a.get("acik_hukumler") or [])[:5],
            "inferences": (a.get("cikarimlar") or [])[:5],
            "ai_model": f"{analysis.provider}:{analysis.model}",
        }
    else:
        view = {
            "summary": "Yapay zekâ özeti oluşturulamadı. Aşağıdaki bilgiler kural tabanlı çıkarımdır; resmî metni inceleyiniz.",
            "impact": "Belirlenemedi (AI analizi yok). Eşleşen anahtar kelimeler: "
                      + (", ".join(doc.matched_keywords[:8]) if doc.matched_keywords else "—"),
            "impact_level": "Belirsiz", "personal": "", "market_items": [], "who": [],
            "actions": ["Resmî metni okuyun."], "provisions": [], "inferences": [], "ai_model": "",
        }

    labels = {**ASPECT_LABELS, **EXTRA_ASPECT_LABELS}
    return {
        "n": n,
        "id": chg.id,
        "title": doc.title,
        "doc_type": DOC_TYPE_LABELS.get(doc.doc_type, doc.doc_type),
        "change_type": CHANGE_TYPE_LABELS.get(chg.change_type, chg.change_type),
        "aspects": [labels[x] for x in (chg.change_aspects or []) if x in labels],
        "institution": doc.institution,
        "rg": rg,
        "published": format_tr_date(doc.published_date),
        "effective": eff_lines,
        "effective_note": EFFECTIVE_STATUS_LABELS.get(chg.effective_date_status, ""),
        "importance": LEVEL_LABELS.get(chg.importance or "", "—"),
        "importance_color": LEVEL_COLORS.get(chg.importance or "", "#475467"),
        "importance_reasons": chg.importance_reasons or [],
        "areas": chg.departments or ["Belirlenemedi"],
        "categories": [taxonomy.label(c) for c in (chg.categories or [])],
        "amended": chg.amended_regulation,
        "amended_ref": chg.amended_regulation_ref,
        "articles": [f"{x['madde']} ({x['islem']})" for x in (chg.amended_articles or [])],
        "compliance": compliance,
        "url": doc.url,
        "pdf_url": doc.pdf_url if doc.pdf_url and doc.pdf_url != doc.url else None,
        "url_verified": chg.url_verified,
        "needs_review": chg.needs_review,
        "review_reasons": chg.review_reasons or [],
        "ai_ok": a is not None,
        "source_name": doc.source.name if doc.source else "",
        **view,
    }


def _routine_view(chg: RegulatoryChange) -> dict:
    doc = chg.document
    return {
        "title": doc.title,
        "gist": chg.note or "",
        "rg": f"{format_tr_date(doc.rg_date)} / {doc.rg_number}" if doc.rg_date else (doc.source.name if doc.source else ""),
        "url": doc.url,
    }


def build_report(session: Session, inp: ReportInput, changes: list[RegulatoryChange], taxonomy: Taxonomy,
                 tz: str) -> EmailReport:
    main = [c for c in changes if c.status != "rutin"]
    routine = [c for c in changes if c.status == "rutin"]
    items = [_item_view(i + 1, c, taxonomy) for i, c in enumerate(main)]
    counts = {lvl: sum(1 for c in main if c.importance == lvl) for lvl in LEVEL_ORDER}
    if inp.report_type == "kritik":
        subject = f"[ENERJİ MEVZUAT] {inp.report_date.isoformat()} | KRİTİK Enerji Mevzuatı Değişikliği"
    else:
        subject = f"[ENERJİ MEVZUAT] {inp.report_date.isoformat()} | Günlük Enerji Mevzuatı Değişiklikleri"
    if not inp.is_complete:
        subject += " | EKSİK TARAMA"
    ctx = {
        "subject": subject,
        "report_type": inp.report_type,
        "report_date": format_tr_date(inp.report_date),
        "period_start": to_local(inp.period_start, tz) if inp.period_start else "İlk tarama",
        "period_end": to_local(inp.period_end, tz),
        "total": len(main),
        "routine_count": len(routine),
        "critical": counts["KRITIK"],
        "high": counts["YUKSEK"],
        "medium": counts["ORTA"],
        "low": counts["DUSUK"],
        "review_count": sum(1 for c in main if c.needs_review),
        "items": items,
        "routine": [_routine_view(c) for c in routine],
        "is_complete": inp.is_complete,
        "sources": inp.source_rows,
        "warnings": inp.warnings,
        "docs_scanned": inp.docs_scanned,
        "docs_filtered": inp.docs_filtered,
        "docs_skipped": inp.docs_skipped,
        "ai_usage": inp.ai_usage,
        "disclaimer": DISCLAIMER,
        "level_colors": LEVEL_COLORS,
    }
    html = _env.get_template("report.html.j2").render(**ctx)
    text = _env.get_template("report.txt.j2").render(**ctx)
    report = EmailReport(
        report_type=inp.report_type,
        report_date=inp.report_date,
        period_start=inp.period_start,
        period_end=inp.period_end,
        subject=subject,
        html_body=html,
        text_body=text,
        total_count=len(changes),
        critical_count=counts["KRITIK"],
        high_count=counts["YUKSEK"],
        is_complete=inp.is_complete,
    )
    session.add(report)
    session.flush()
    for c in changes:
        session.add(EmailReportItem(report_id=report.id, change_id=c.id))
    session.flush()
    return report


def render_admin_alert(title: str, lines: list[str], details: dict | None = None) -> str:
    return _env.get_template("admin_alert.txt.j2").render(title=title, lines=lines, details=details or {})
