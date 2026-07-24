from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from tenderguard.domain.enums import ApprovalState


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProjectRow(Base, TimestampMixin):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    state: Mapped[str] = mapped_column(
        String(64), nullable=False, default=ApprovalState.DRAFT.value
    )
    blocked_resume_state: Mapped[str | None] = mapped_column(String(64))
    current_document_set_revision_id: Mapped[str | None] = mapped_column(String(64))
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    documents: Mapped[list[DocumentRow]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("organization_id", "code"),)


class QuarantinedUploadRow(Base, TimestampMixin):
    __tablename__ = "quarantined_uploads"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    logical_key: Mapped[str] = mapped_column(String(300), nullable=False)
    title: Mapped[str] = mapped_column(String(1000), nullable=False)
    document_type: Mapped[str] = mapped_column(String(100), nullable=False)
    critical: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    revision_label: Mapped[str] = mapped_column(String(100), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(1000), nullable=False)
    declared_media_type: Mapped[str] = mapped_column(String(200), nullable=False)
    make_candidate_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    object_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    object_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    uploaded_by: Mapped[str] = mapped_column(String(128), nullable=False)
    invalidated_document_set_revision_id: Mapped[str | None] = mapped_column(String(64))
    processed_document_id: Mapped[str | None] = mapped_column(ForeignKey("documents.id"))
    processed_document_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("document_revisions.id")
    )
    candidate_document_set_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("document_set_revisions.id")
    )
    result_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    failure_code: Mapped[str | None] = mapped_column(String(100))
    failure_detail: Mapped[str | None] = mapped_column(Text)
    processing_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processing_worker_id: Mapped[str | None] = mapped_column(String(128))
    processing_lease_token: Mapped[str | None] = mapped_column(String(64))
    processing_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_dead_lettered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "("
            "status = 'PROCESSING' AND processing_worker_id IS NOT NULL "
            "AND processing_lease_token IS NOT NULL "
            "AND processing_lease_expires_at IS NOT NULL "
            "AND processing_deadline_at IS NOT NULL"
            ") OR ("
            "status <> 'PROCESSING' AND processing_worker_id IS NULL "
            "AND processing_lease_token IS NULL "
            "AND processing_lease_expires_at IS NULL "
            "AND processing_deadline_at IS NULL"
            ")",
            name="ck_quarantined_upload_processing_lease",
        ),
        Index(
            "uq_active_quarantined_upload_per_logical",
            "project_id",
            "logical_key",
            unique=True,
            sqlite_where=text(
                "status IN ('QUARANTINED', 'CLEAN', 'SCAN_FAILED', "
                "'PROCESSING', 'PROCESSING_FAILED', 'PROCESSING_DEAD_LETTERED')"
            ),
            postgresql_where=text(
                "status IN ('QUARANTINED', 'CLEAN', 'SCAN_FAILED', "
                "'PROCESSING', 'PROCESSING_FAILED', 'PROCESSING_DEAD_LETTERED')"
            ),
        ),
    )


class MalwareScanResultRow(Base):
    __tablename__ = "malware_scan_results"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    quarantined_upload_id: Mapped[str] = mapped_column(
        ForeignKey("quarantined_uploads.id"), nullable=False, index=True
    )
    adapter_qualification_id: Mapped[str] = mapped_column(
        ForeignKey("adapter_qualifications.id"), nullable=False
    )
    scanner_run_id: Mapped[str] = mapped_column(String(200), nullable=False)
    scanned_object_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    verdict: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    definitions_version: Mapped[str] = mapped_column(String(200), nullable=False)
    report_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "adapter_qualification_id",
            "scanner_run_id",
            name="uq_malware_scan_adapter_run",
        ),
    )


class DocumentRow(Base, TimestampMixin):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    logical_key: Mapped[str] = mapped_column(String(300), nullable=False)
    title: Mapped[str] = mapped_column(String(1000), nullable=False)
    document_type: Mapped[str] = mapped_column(String(100), nullable=False)
    critical: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cancelled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    project: Mapped[ProjectRow] = relationship(back_populates="documents")
    revisions: Mapped[list[DocumentRevisionRow]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("project_id", "logical_key"),)


class DocumentRevisionRow(Base, TimestampMixin):
    __tablename__ = "document_revisions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), nullable=False, index=True)
    revision_label: Mapped[str] = mapped_column(String(100), nullable=False)
    issue_date: Mapped[date | None] = mapped_column(Date)
    object_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    object_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(1000), nullable=False)
    media_type: Mapped[str] = mapped_column(String(200), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes_revision_id: Mapped[str | None] = mapped_column(ForeignKey("document_revisions.id"))
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    corrupt: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    protected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    inspection_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    document: Mapped[DocumentRow] = relationship(back_populates="revisions")

    __table_args__ = (UniqueConstraint("document_id", "revision_label"),)


class DocumentSetRevisionRow(Base):
    __tablename__ = "document_set_revisions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    revision_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_by: Mapped[str | None] = mapped_column(String(128))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("project_id", "manifest_hash"),)


class FileManifestRow(Base):
    __tablename__ = "file_manifest_entries"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_revision_id: Mapped[str] = mapped_column(
        ForeignKey("document_revisions.id"), nullable=False, index=True
    )
    archive_path: Mapped[str] = mapped_column(String(2000), nullable=False)
    object_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    media_type: Mapped[str] = mapped_column(String(200), nullable=False)
    corrupt: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    protected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    nested_archive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    inspection_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    __table_args__ = (UniqueConstraint("document_revision_id", "archive_path"),)


class ControlledVersionRow(Base):
    __tablename__ = "controlled_versions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    version_label: Mapped[str] = mapped_column(String(200), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(128))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("kind", "version_label"),)


class ProjectControlledVersionRow(Base):
    __tablename__ = "project_controlled_versions"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    controlled_version_id: Mapped[str] = mapped_column(
        ForeignKey("controlled_versions.id"), primary_key=True
    )
    purpose: Mapped[str] = mapped_column(String(100), nullable=False)
    bound_by: Mapped[str] = mapped_column(String(128), nullable=False)
    bound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (UniqueConstraint("project_id", "purpose"),)


class ObservationRow(Base):
    __tablename__ = "evidence_observations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    document_revision_id: Mapped[str] = mapped_column(
        ForeignKey("document_revisions.id"), nullable=False, index=True
    )
    field_name: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    method: Mapped[str] = mapped_column(String(100), nullable=False)
    method_version: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ConflictRow(Base, TimestampMixin):
    __tablename__ = "conflicts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    field_name: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class BoqLineRow(Base, TimestampMixin):
    __tablename__ = "boq_lines"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    line_key: Mapped[str] = mapped_column(String(128), nullable=False)
    wbs_node_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    work_code: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    unit: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    supersedes_line_id: Mapped[str | None] = mapped_column(String(64))
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    __table_args__ = (
        Index(
            "uq_boq_lines_current_per_key",
            "project_id",
            "line_key",
            unique=True,
            sqlite_where=text("is_current = 1"),
            postgresql_where=text("is_current"),
        ),
    )


class QuantityRow(Base, TimestampMixin):
    __tablename__ = "quantities"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    boq_line_id: Mapped[str] = mapped_column(ForeignKey("boq_lines.id"), nullable=False, index=True)
    value: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    unit: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    supersedes_quantity_id: Mapped[str | None] = mapped_column(String(64))
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    __table_args__ = (
        Index(
            "uq_quantities_current_per_boq_line",
            "boq_line_id",
            unique=True,
            sqlite_where=text("is_current = 1"),
            postgresql_where=text("is_current"),
        ),
    )


class NomenclatureMatchRow(Base, TimestampMixin):
    __tablename__ = "nomenclature_matches"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    source_item_id: Mapped[str] = mapped_column(String(128), nullable=False)
    canonical_item_id: Mapped[str | None] = mapped_column(String(128))
    match_class: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    catalog_version_id: Mapped[str] = mapped_column(
        ForeignKey("controlled_versions.id"), nullable=False
    )
    supersedes_match_id: Mapped[str | None] = mapped_column(String(64))
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    __table_args__ = (
        Index(
            "uq_nomenclature_matches_current_per_source",
            "project_id",
            "source_item_id",
            unique=True,
            sqlite_where=text("is_current = 1"),
            postgresql_where=text("is_current"),
        ),
    )


class PriceQuoteRow(Base, TimestampMixin):
    __tablename__ = "price_quotes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    item_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    quote_date: Mapped[date] = mapped_column(Date, nullable=False)
    valid_until: Mapped[date | None] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    source_observation_id: Mapped[str | None] = mapped_column(
        ForeignKey("evidence_observations.id"), index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class NormalizedPriceRow(Base):
    __tablename__ = "normalized_prices"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    quote_id: Mapped[str] = mapped_column(ForeignKey("price_quotes.id"), nullable=False, index=True)
    amount_per_unit: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    formula_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PriceDecisionRow(Base):
    __tablename__ = "price_decisions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    item_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    amount_per_unit: Mapped[Decimal | None] = mapped_column(Numeric(38, 12))
    currency: Mapped[str | None] = mapped_column(String(3))
    unit: Mapped[str | None] = mapped_column(String(64))
    policy_version_id: Mapped[str] = mapped_column(
        ForeignKey("controlled_versions.id"), nullable=False
    )
    derived_observation_id: Mapped[str | None] = mapped_column(
        ForeignKey("evidence_observations.id")
    )
    supersedes_decision_id: Mapped[str | None] = mapped_column(String(64))
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index(
            "uq_price_decisions_current_per_item",
            "project_id",
            "item_id",
            unique=True,
            sqlite_where=text("is_current = 1"),
            postgresql_where=text("is_current"),
        ),
    )


class RfqRequestRow(Base, TimestampMixin):
    __tablename__ = "rfq_requests"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    item_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    price_decision_id: Mapped[str] = mapped_column(ForeignKey("price_decisions.id"), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class ScopeFindingRow(Base, TimestampMixin):
    __tablename__ = "scope_findings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    rule_id: Mapped[str] = mapped_column(String(128), nullable=False)
    severity: Mapped[str] = mapped_column(String(30), nullable=False)
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class ScopeEvaluationRow(Base):
    __tablename__ = "scope_evaluations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    wbs_node_id: Mapped[str] = mapped_column(String(128), nullable=False)
    rule_pack_version_id: Mapped[str] = mapped_column(
        ForeignKey("controlled_versions.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    input_signature: Mapped[str] = mapped_column(String(64), nullable=False)
    supersedes_evaluation_id: Mapped[str | None] = mapped_column(String(64))
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index(
            "uq_scope_evaluations_current_per_wbs",
            "project_id",
            "wbs_node_id",
            unique=True,
            sqlite_where=text("is_current = 1"),
            postgresql_where=text("is_current"),
        ),
    )


class VerificationFindingRow(Base, TimestampMixin):
    __tablename__ = "verification_findings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    contour: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(150), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(30), nullable=False)
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class ManualChangeRow(Base):
    __tablename__ = "manual_changes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    field_name: Mapped[str] = mapped_column(String(200), nullable=False)
    critical: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    changed_by: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CostInputRow(Base):
    __tablename__ = "atomic_cost_inputs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    calculation_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("calculation_runs.id"), index=True
    )
    semantic_key: Mapped[str] = mapped_column(String(300), nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    amount_basis_id: Mapped[str | None] = mapped_column(String(128))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CalculationRunRow(Base):
    __tablename__ = "calculation_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    engine_version: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    grand_total: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CalculationSnapshotRow(Base):
    __tablename__ = "calculation_snapshots"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    calculation_run_id: Mapped[str] = mapped_column(
        ForeignKey("calculation_runs.id"), nullable=False, unique=True
    )
    document_set_revision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    output_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    fixed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    object_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ApprovalTaskRow(Base, TimestampMixin):
    __tablename__ = "approval_tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    task_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    assigned_role: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class ApprovalRecordRow(Base):
    __tablename__ = "approval_records"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("approval_tasks.id"), nullable=False, index=True
    )
    decision: Mapped[str] = mapped_column(String(50), nullable=False)
    decided_by: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkflowTransitionRow(Base):
    __tablename__ = "workflow_transitions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    from_state: Mapped[str] = mapped_column(String(64), nullable=False)
    to_state: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReleaseDecisionRow(Base):
    __tablename__ = "release_decisions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("calculation_snapshots.id"))
    requested_state: Mapped[str] = mapped_column(String(64), nullable=False)
    resulting_state: Mapped[str] = mapped_column(String(64), nullable=False)
    allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    decided_by: Mapped[str] = mapped_column(String(128), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuditEventRow(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    aggregate_type: Mapped[str] = mapped_column(String(100), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_roles: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    previous_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    signature: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("aggregate_type", "aggregate_id", "sequence"),
        Index("ix_audit_aggregate_sequence", "aggregate_type", "aggregate_id", "sequence"),
    )


class OutboxEventRow(Base):
    __tablename__ = "outbox_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    topic: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    aggregate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    locked_by: Mapped[str | None] = mapped_column(String(128))
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dead_lettered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "("
            "locked_by IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL"
            ") OR ("
            "locked_by IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL"
            ")",
            name="ck_outbox_lease_complete",
        ),
        CheckConstraint(
            "published_at IS NULL OR dead_lettered_at IS NULL",
            name="ck_outbox_single_terminal_state",
        ),
        Index(
            "ix_outbox_delivery_ready",
            "topic",
            "published_at",
            "dead_lettered_at",
            "available_at",
            "lease_expires_at",
        ),
    )


class ActualRecordRow(Base):
    __tablename__ = "actual_records"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    actual_key: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    metric: Mapped[str] = mapped_column(String(100), nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    unit: Mapped[str] = mapped_column(String(64), nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source_observation_id: Mapped[str] = mapped_column(
        ForeignKey("evidence_observations.id"), nullable=False
    )
    occurred_on: Mapped[date] = mapped_column(Date, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    supersedes_actual_id: Mapped[str | None] = mapped_column(String(64))
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index(
            "uq_actual_records_current_per_key",
            "project_id",
            "actual_key",
            unique=True,
            sqlite_where=text("is_current = 1"),
            postgresql_where=text("is_current"),
        ),
    )


class ProjectPassportFactRow(Base, TimestampMixin):
    __tablename__ = "project_passport_facts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    field_name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    supersedes_fact_id: Mapped[str | None] = mapped_column(String(64))
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    __table_args__ = (
        Index(
            "uq_passport_facts_current_per_field",
            "project_id",
            "field_name",
            unique=True,
            sqlite_where=text("is_current = 1"),
            postgresql_where=text("is_current"),
        ),
    )


class ContractTermRow(Base, TimestampMixin):
    __tablename__ = "contract_terms"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cost_impact_resolved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )
    supersedes_term_id: Mapped[str | None] = mapped_column(String(64))
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    __table_args__ = (
        Index(
            "uq_contract_terms_current_per_kind",
            "project_id",
            "kind",
            unique=True,
            sqlite_where=text("is_current = 1"),
            postgresql_where=text("is_current"),
        ),
    )


class RiskItemRow(Base, TimestampMixin):
    __tablename__ = "risk_items"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    risk_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    expected_impact: Mapped[Decimal | None] = mapped_column(Numeric(38, 12))
    supersedes_risk_id: Mapped[str | None] = mapped_column(String(64))
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    __table_args__ = (
        Index(
            "uq_risk_items_current_per_key",
            "project_id",
            "risk_key",
            unique=True,
            sqlite_where=text("is_current = 1"),
            postgresql_where=text("is_current"),
        ),
    )


class RiskCalculationRow(Base):
    __tablename__ = "risk_calculations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    policy_version_id: Mapped[str] = mapped_column(
        ForeignKey("controlled_versions.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    expected_reserve: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    unit: Mapped[str] = mapped_column(String(64), nullable=False)
    supersedes_calculation_id: Mapped[str | None] = mapped_column(String(64))
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index(
            "uq_risk_calculations_current_per_project",
            "project_id",
            unique=True,
            sqlite_where=text("is_current = 1"),
            postgresql_where=text("is_current"),
        ),
    )


class ScenarioRunRow(Base):
    __tablename__ = "scenario_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    base_calculation_run_id: Mapped[str] = mapped_column(
        ForeignKey("calculation_runs.id"), nullable=False
    )
    scenario_version: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    grand_total: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class VarianceRecordRow(Base):
    __tablename__ = "variance_records"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    actual_record_id: Mapped[str] = mapped_column(
        ForeignKey("actual_records.id"), nullable=False, unique=True
    )
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("calculation_snapshots.id"), nullable=False)
    metric: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    absolute_variance: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    relative_variance: Mapped[Decimal | None] = mapped_column(Numeric(38, 12))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    classified_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CalibrationExampleRow(Base):
    __tablename__ = "calibration_examples"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    actual_record_id: Mapped[str] = mapped_column(
        ForeignKey("actual_records.id"), nullable=False, unique=True
    )
    variance_record_id: Mapped[str] = mapped_column(
        ForeignKey("variance_records.id"), nullable=False, unique=True
    )
    features_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("calculation_snapshots.id"), nullable=False
    )
    metric: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    target_value: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    unit: Mapped[str] = mapped_column(String(64), nullable=False)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AdapterQualificationRow(Base):
    __tablename__ = "adapter_qualifications"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    adapter_name: Mapped[str] = mapped_column(String(200), nullable=False)
    adapter_version: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    valid_until: Mapped[date | None] = mapped_column(Date)
    test_evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    approved_by: Mapped[str] = mapped_column(String(128), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (UniqueConstraint("adapter_name", "adapter_version"),)


class ExtractionRunRow(Base):
    __tablename__ = "extraction_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    document_revision_id: Mapped[str] = mapped_column(
        ForeignKey("document_revisions.id"), nullable=False, index=True
    )
    adapter_qualification_id: Mapped[str] = mapped_column(
        ForeignKey("adapter_qualifications.id"), nullable=False
    )
    method: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NormativeCalculationRow(Base):
    __tablename__ = "normative_calculations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    adapter_qualification_id: Mapped[str] = mapped_column(
        ForeignKey("adapter_qualifications.id"), nullable=False
    )
    normative_basis_version: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    total: Mapped[Decimal | None] = mapped_column(Numeric(38, 12))
    currency: Mapped[str | None] = mapped_column(String(3))
    artifact_hash: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExportArtifactRow(Base):
    __tablename__ = "export_artifacts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("calculation_snapshots.id"), nullable=False)
    adapter_qualification_id: Mapped[str | None] = mapped_column(
        ForeignKey("adapter_qualifications.id")
    )
    release_decision_id: Mapped[str] = mapped_column(
        ForeignKey("release_decisions.id"), nullable=False
    )
    template_version_id: Mapped[str] = mapped_column(
        ForeignKey("controlled_versions.id"), nullable=False
    )
    package_schema_version: Mapped[str] = mapped_column(String(100), nullable=False)
    format: Mapped[str] = mapped_column(String(80), nullable=False)
    media_type: Mapped[str] = mapped_column(String(200), nullable=False)
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    object_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    signature_algorithm: Mapped[str] = mapped_column(String(50), nullable=False)
    signature: Mapped[str] = mapped_column(String(200), nullable=False)
    signing_key_id: Mapped[str] = mapped_column(String(200), nullable=False)
    signing_public_key_b64: Mapped[str] = mapped_column(String(100), nullable=False)
    public_key_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index(
            "uq_signed_export_artifact_basis",
            "snapshot_id",
            "release_decision_id",
            "template_version_id",
            "format",
            unique=True,
            sqlite_where=text("signature_algorithm = 'Ed25519'"),
            postgresql_where=text("signature_algorithm = 'Ed25519'"),
        ),
    )
