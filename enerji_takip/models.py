"""Veri tabanı modelleri (SQLAlchemy 2.0).

Tüm zaman damgaları UTC (timezone-naive) olarak saklanır; gösterimde
Europe/Istanbul'a çevrilir. Varsayılan veri tabanı SQLite'tır.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class RegulatorySource(Base):
    """İzlenen mevzuat kaynağı (Resmî Gazete, kurum duyuru sayfası vb.)."""

    __tablename__ = "regulatory_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(500))
    category: Mapped[str] = mapped_column(String(100))
    method: Mapped[str] = mapped_column(String(50))  # rg_fihrist | html_listing | ...
    frequency: Mapped[str] = mapped_column(String(50), default="gunluk")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    access_status: Mapped[str] = mapped_column(String(30), default="bilinmiyor")
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cursor: Mapped[str | None] = mapped_column(String(100), nullable=True)
    baseline_done: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    documents: Mapped[list["RegulatoryDocument"]] = relationship(back_populates="source")


class RegulatoryDocument(Base):
    """Bir kaynakta görülen tekil belge (ör. Resmî Gazete'deki bir yönetmelik)."""

    __tablename__ = "regulatory_documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_id: Mapped[int] = mapped_column(ForeignKey("regulatory_sources.id"), index=True)
    dedup_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    external_id: Mapped[str | None] = mapped_column(String(100), index=True)
    title: Mapped[str] = mapped_column(Text)
    doc_type: Mapped[str] = mapped_column(String(60), default="DIGER")
    section: Mapped[str | None] = mapped_column(String(120))
    institution: Mapped[str | None] = mapped_column(String(300))
    rg_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    rg_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    is_mukerrer: Mapped[bool] = mapped_column(Boolean, default=False)
    published_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    url: Mapped[str] = mapped_column(String(600))
    pdf_url: Mapped[str | None] = mapped_column(String(600), nullable=True)
    full_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    text_status: Mapped[str] = mapped_column(String(30), default="alinmadi")
    relevance: Mapped[str] = mapped_column(String(20), default="belirsiz")
    relevance_score: Mapped[float] = mapped_column(default=0.0)
    matched_categories: Mapped[list] = mapped_column(JSON, default=list)
    matched_keywords: Mapped[list] = mapped_column(JSON, default=list)
    relevance_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_baseline: Mapped[bool] = mapped_column(Boolean, default=False)
    fetch_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    source: Mapped[RegulatorySource] = relationship(back_populates="documents")
    changes: Mapped[list["RegulatoryChange"]] = relationship(back_populates="document")


class RegulatoryChange(Base):
    """Şirket açısından ilgili bulunan, bildirilecek mevzuat değişikliği."""

    __tablename__ = "regulatory_changes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("regulatory_documents.id"), index=True)
    dedup_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    change_type: Mapped[str] = mapped_column(String(60))
    change_aspects: Mapped[list] = mapped_column(JSON, default=list)
    amended_regulation: Mapped[str | None] = mapped_column(Text, nullable=True)
    amended_regulation_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    amended_articles: Mapped[list] = mapped_column(JSON, default=list)
    effective_dates: Mapped[list] = mapped_column(JSON, default=list)
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_date_status: Mapped[str] = mapped_column(String(30), default="belirsiz")
    categories: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(30), default="yeni", index=True)
    importance: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)
    importance_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    importance_reasons: Mapped[list] = mapped_column(JSON, default=list)
    ai_importance: Mapped[str | None] = mapped_column(String(10), nullable=True)
    departments: Mapped[list] = mapped_column(JSON, default=list)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    review_reasons: Mapped[list] = mapped_column(JSON, default=list)
    analysis_attempts: Mapped[int] = mapped_column(Integer, default=0)
    url_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)  # rutin kararlar için kısa öz
    first_detected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    document: Mapped[RegulatoryDocument] = relationship(back_populates="changes")
    analyses: Mapped[list["AIAnalysis"]] = relationship(
        back_populates="change", order_by="AIAnalysis.created_at"
    )

    @property
    def latest_valid_analysis(self) -> "AIAnalysis | None":
        valid = [a for a in self.analyses if a.is_valid]
        return valid[-1] if valid else None


class AIAnalysis(Base):
    __tablename__ = "ai_analyses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    change_id: Mapped[str] = mapped_column(ForeignKey("regulatory_changes.id"), index=True)
    provider: Mapped[str] = mapped_column(String(30))
    model: Mapped[str] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(20))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    raw_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    is_valid: Mapped[bool] = mapped_column(Boolean, default=False)
    validation_errors: Mapped[list] = mapped_column(JSON, default=list)
    grounding_issues: Mapped[list] = mapped_column(JSON, default=list)
    input_chars: Mapped[int] = mapped_column(Integer, default=0)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    change: Mapped[RegulatoryChange] = relationship(back_populates="analyses")


class EmailReport(Base):
    __tablename__ = "email_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    report_type: Mapped[str] = mapped_column(String(20), default="gunluk")  # gunluk | kritik
    report_date: Mapped[date] = mapped_column(Date, index=True)
    scan_run_id: Mapped[str | None] = mapped_column(ForeignKey("scan_runs.id"), nullable=True)
    period_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    subject: Mapped[str] = mapped_column(String(300))
    html_body: Mapped[str] = mapped_column(Text)
    text_body: Mapped[str] = mapped_column(Text)
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    critical_count: Mapped[int] = mapped_column(Integer, default=0)
    high_count: Mapped[int] = mapped_column(Integer, default=0)
    is_complete: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(20), default="olusturuldu")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    items: Mapped[list["EmailReportItem"]] = relationship(back_populates="report")
    deliveries: Mapped[list["EmailDeliveryLog"]] = relationship(back_populates="report")


class EmailReportItem(Base):
    __tablename__ = "email_report_items"
    __table_args__ = (UniqueConstraint("report_id", "change_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_id: Mapped[str] = mapped_column(ForeignKey("email_reports.id"), index=True)
    change_id: Mapped[str] = mapped_column(ForeignKey("regulatory_changes.id"), index=True)

    report: Mapped[EmailReport] = relationship(back_populates="items")
    change: Mapped[RegulatoryChange] = relationship()


class EmailDeliveryLog(Base):
    __tablename__ = "email_delivery_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_id: Mapped[str | None] = mapped_column(ForeignKey("email_reports.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(20), default="rapor")  # rapor | yonetici
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    recipients: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20))  # basarili | hata
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    report: Mapped[EmailReport | None] = relationship(back_populates="deliveries")


class ScanRun(Base):
    """Bir tarama çalıştırmasının (günlük/kritik/manuel) özeti."""

    __tablename__ = "scan_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_type: Mapped[str] = mapped_column(String(20), default="gunluk")
    trigger: Mapped[str] = mapped_column(String(20), default="zamanlanmis")
    status: Mapped[str] = mapped_column(String(20), default="calisiyor", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    docs_seen: Mapped[int] = mapped_column(Integer, default=0)
    docs_new: Mapped[int] = mapped_column(Integer, default=0)
    changes_new: Mapped[int] = mapped_column(Integer, default=0)
    coverage: Mapped[dict] = mapped_column(JSON, default=dict)
    errors: Mapped[list] = mapped_column(JSON, default=list)


class MonitoringLog(Base):
    __tablename__ = "monitoring_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scan_run_id: Mapped[str | None] = mapped_column(ForeignKey("scan_runs.id"), nullable=True, index=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("regulatory_sources.id"), nullable=True)
    level: Mapped[str] = mapped_column(String(10), default="INFO")
    event: Mapped[str] = mapped_column(String(60))
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
