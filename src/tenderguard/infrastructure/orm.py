from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
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


class ProjectMembershipRow(Base):
    __tablename__ = "project_memberships"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    principal_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    roles: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    role_mask: Mapped[int] = mapped_column(Integer, nullable=False)
    access_level: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes_membership_id: Mapped[str | None] = mapped_column(
        ForeignKey("project_memberships.id")
    )
    changed_by: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_project_membership_version_positive"),
        CheckConstraint(
            "access_level IN ('MEMBER', 'OWNER')",
            name="ck_project_membership_access_level",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'REVOKED')",
            name="ck_project_membership_status",
        ),
        CheckConstraint(
            "role_mask > 0 AND role_mask <= 511",
            name="ck_project_membership_role_mask",
        ),
        UniqueConstraint(
            "project_id",
            "principal_id",
            "version",
            name="uq_project_membership_version",
        ),
        UniqueConstraint(
            "supersedes_membership_id",
            name="uq_project_membership_supersedes",
        ),
        Index(
            "ix_project_membership_current_lookup",
            "project_id",
            "principal_id",
            "version",
        ),
        Index(
            "ix_project_membership_principal_current",
            "principal_id",
            "project_id",
            "version",
        ),
    )


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


class QuantityManualChangeApplicationRow(Base):
    __tablename__ = "quantity_manual_change_applications"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    manual_change_id: Mapped[str] = mapped_column(ForeignKey("manual_changes.id"), nullable=False)
    quantity_id: Mapped[str] = mapped_column(ForeignKey("quantities.id"), nullable=False)
    applied_by: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "manual_change_id",
            name="uq_quantity_manual_change_application_change",
        ),
        UniqueConstraint(
            "quantity_id",
            name="uq_quantity_manual_change_application_quantity",
        ),
        Index(
            "ix_quantity_manual_change_applications_manual_change_id",
            "manual_change_id",
        ),
        Index(
            "ix_quantity_manual_change_applications_quantity_id",
            "quantity_id",
        ),
    )


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

    __table_args__ = (UniqueConstraint("task_id", name="uq_approval_records_task_id"),)


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
    signing_key_id: Mapped[str] = mapped_column(String(200), nullable=False)
    signature_version: Mapped[str] = mapped_column(String(32), nullable=False)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    signature: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("aggregate_type", "aggregate_id", "sequence"),
        Index("ix_audit_aggregate_sequence", "aggregate_type", "aggregate_id", "sequence"),
    )


class AuditCheckpointRow(Base):
    __tablename__ = "audit_checkpoints"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(100), nullable=False)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False)
    terminal_count: Mapped[int] = mapped_column(Integer, nullable=False)
    checkpoint_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    object_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("event_count >= 1", name="ck_audit_checkpoint_event_count"),
        CheckConstraint("terminal_count >= 1", name="ck_audit_checkpoint_terminal_count"),
        Index("ix_audit_checkpoints_created_at", "created_at"),
    )


class AuditAnchorReceiptRow(Base):
    __tablename__ = "audit_anchor_receipts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    checkpoint_id: Mapped[str] = mapped_column(
        ForeignKey("audit_checkpoints.id"),
        nullable=False,
        unique=True,
    )
    provider_id: Mapped[str] = mapped_column(String(200), nullable=False)
    provider_key_id: Mapped[str] = mapped_column(String(200), nullable=False)
    external_reference: Mapped[str] = mapped_column(String(500), nullable=False)
    anchored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    signature_b64: Mapped[str] = mapped_column(String(200), nullable=False)
    receipt_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    registered_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_audit_anchor_receipts_anchored_at", "anchored_at"),)


class IdempotencyRecordRow(Base):
    __tablename__ = "idempotency_records"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_method: Mapped[str] = mapped_column(String(16), nullable=False)
    request_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    initial_request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_media_type: Mapped[str | None] = mapped_column(String(200))
    response_payload: Mapped[Any | None] = mapped_column(JSON(none_as_null=True))
    response_has_body: Mapped[bool | None] = mapped_column(Boolean)
    response_headers: Mapped[dict[str, str] | None] = mapped_column(JSON(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "actor_id",
            "idempotency_key",
            name="uq_idempotency_actor_key",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'COMPLETED')",
            name="ck_idempotency_status",
        ),
        CheckConstraint(
            "("
            "status = 'PENDING' AND response_status IS NULL "
            "AND response_has_body IS NULL AND response_headers IS NULL "
            "AND completed_at IS NULL"
            ") OR ("
            "status = 'COMPLETED' AND response_status IS NOT NULL "
            "AND response_has_body IS NOT NULL AND response_headers IS NOT NULL "
            "AND completed_at IS NOT NULL"
            ")",
            name="ck_idempotency_completion",
        ),
        Index("ix_idempotency_created_at", "created_at"),
    )


class RateLimitBucketRow(Base):
    __tablename__ = "rate_limit_buckets"

    scope: Mapped[str] = mapped_column(String(50), primary_key=True)
    identity_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    window_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    request_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "scope IN ("
            "'ACTOR_READ', 'ORGANIZATION_READ', "
            "'ACTOR_MUTATION', 'ORGANIZATION_MUTATION', "
            "'ACTOR_UPLOAD', 'ORGANIZATION_UPLOAD'"
            ")",
            name="ck_rate_limit_bucket_scope",
        ),
        CheckConstraint(
            "request_count >= 1",
            name="ck_rate_limit_bucket_count",
        ),
        Index("ix_rate_limit_buckets_updated_at", "updated_at"),
    )


class OutboxEventRow(Base):
    __tablename__ = "outbox_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    deduplication_key: Mapped[str] = mapped_column(String(200), nullable=False)
    delivery_deduplication_key: Mapped[str] = mapped_column(String(200), nullable=False)
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
        UniqueConstraint(
            "deduplication_key",
            name="uq_outbox_deduplication_key",
        ),
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


class ConnectorDeliveryAttemptRow(Base):
    __tablename__ = "connector_delivery_attempts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    outbox_event_id: Mapped[str] = mapped_column(
        ForeignKey("outbox_events.id"),
        nullable=False,
        index=True,
    )
    connector_qualification_id: Mapped[str] = mapped_column(
        ForeignKey("adapter_qualifications.id"),
        nullable=False,
        index=True,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    envelope_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    receipt_hash: Mapped[str | None] = mapped_column(String(64))
    external_message_id: Mapped[str | None] = mapped_column(String(200))
    error_code: Mapped[str | None] = mapped_column(String(200))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "outbox_event_id",
            "attempt_number",
            name="uq_connector_delivery_attempt",
        ),
        CheckConstraint(
            "status IN ('ACCEPTED', 'DUPLICATE', 'RETRYABLE_FAILURE', 'PERMANENT_FAILURE')",
            name="ck_connector_delivery_attempt_status",
        ),
        CheckConstraint(
            "("
            "status IN ('ACCEPTED', 'DUPLICATE') AND receipt_hash IS NOT NULL "
            "AND external_message_id IS NOT NULL AND error_code IS NULL"
            ") OR ("
            "status IN ('RETRYABLE_FAILURE', 'PERMANENT_FAILURE') "
            "AND error_code IS NOT NULL AND receipt_hash IS NULL "
            "AND external_message_id IS NULL"
            ")",
            name="ck_connector_delivery_attempt_result",
        ),
        CheckConstraint(
            "attempt_number >= 1 AND completed_at >= started_at",
            name="ck_connector_delivery_attempt_timing",
        ),
        Index(
            "ix_connector_delivery_attempt_completed",
            "connector_qualification_id",
            "completed_at",
        ),
    )


class OutboxReplayRow(Base):
    __tablename__ = "outbox_replays"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_outbox_event_id: Mapped[str] = mapped_column(
        ForeignKey("outbox_events.id"),
        nullable=False,
        unique=True,
    )
    replay_outbox_event_id: Mapped[str] = mapped_column(
        ForeignKey("outbox_events.id"),
        nullable=False,
        unique=True,
    )
    delivery_deduplication_key: Mapped[str] = mapped_column(String(200), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    replayed_by: Mapped[str] = mapped_column(String(128), nullable=False)
    replayed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IntegrationInboxMessageRow(Base):
    __tablename__ = "integration_inbox_messages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_qualification_id: Mapped[str] = mapped_column(
        ForeignKey("adapter_qualifications.id"),
        nullable=False,
        index=True,
    )
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    delivery_deduplication_key: Mapped[str] = mapped_column(String(200), nullable=False)
    topic: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    aggregate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(100), nullable=False)
    core_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    envelope: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    receipt: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    qualification_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "source_qualification_id",
            "source_message_id",
            name="uq_inbox_source_message",
        ),
        UniqueConstraint(
            "source_qualification_id",
            "delivery_deduplication_key",
            name="uq_inbox_source_deduplication",
        ),
        Index(
            "ix_inbox_organization_received",
            "organization_id",
            "received_at",
        ),
    )


class IntegrationInboxProcessingRow(Base):
    __tablename__ = "integration_inbox_processings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    message_id: Mapped[str] = mapped_column(
        ForeignKey("integration_inbox_messages.id"),
        nullable=False,
        index=True,
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    locked_by: Mapped[str | None] = mapped_column(String(128))
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(200))
    handler_qualification_id: Mapped[str | None] = mapped_column(
        ForeignKey("adapter_qualifications.id")
    )
    result_reference: Mapped[str | None] = mapped_column(String(500))
    result_hash: Mapped[str | None] = mapped_column(String(64))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dead_lettered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "message_id",
            "generation",
            name="uq_inbox_processing_generation",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'CONSUMED', 'DEAD_LETTERED')",
            name="ck_inbox_processing_status",
        ),
        CheckConstraint(
            "generation >= 1 AND ("
            "(attempts = 0 AND last_attempt_at IS NULL "
            "AND handler_qualification_id IS NULL) OR "
            "(attempts >= 1 AND last_attempt_at IS NOT NULL "
            "AND handler_qualification_id IS NOT NULL)"
            ")",
            name="ck_inbox_processing_counters",
        ),
        CheckConstraint(
            "("
            "locked_by IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL"
            ") OR ("
            "locked_by IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL"
            ")",
            name="ck_inbox_processing_lease_complete",
        ),
        CheckConstraint(
            "("
            "status = 'PENDING' AND consumed_at IS NULL AND dead_lettered_at IS NULL "
            "AND result_reference IS NULL AND result_hash IS NULL"
            ") OR ("
            "status = 'CONSUMED' AND consumed_at IS NOT NULL "
            "AND dead_lettered_at IS NULL AND result_reference IS NOT NULL "
            "AND result_hash IS NOT NULL AND handler_qualification_id IS NOT NULL "
            "AND locked_by IS NULL"
            ") OR ("
            "status = 'DEAD_LETTERED' AND dead_lettered_at IS NOT NULL "
            "AND consumed_at IS NULL AND result_reference IS NULL "
            "AND result_hash IS NULL AND locked_by IS NULL"
            ")",
            name="ck_inbox_processing_terminal_state",
        ),
        Index(
            "uq_inbox_processing_pending_message",
            "message_id",
            unique=True,
            sqlite_where=text("status = 'PENDING'"),
            postgresql_where=text("status = 'PENDING'"),
        ),
        Index(
            "ix_inbox_processing_ready",
            "status",
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


class CommercialCostModelRow(Base):
    __tablename__ = "commercial_cost_models"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    model_kind: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    target_line_id: Mapped[str] = mapped_column(
        ForeignKey("boq_lines.id"),
        nullable=False,
    )
    target_semantic_key: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    policy_version_id: Mapped[str] = mapped_column(
        ForeignKey("controlled_versions.id"),
        nullable=False,
    )
    document_set_revision_id: Mapped[str] = mapped_column(
        ForeignKey("document_set_revisions.id"),
        nullable=False,
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    total: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    independent_total: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    output_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    approval_task_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    approval_record_ids: Mapped[list[str] | None] = mapped_column(JSON(none_as_null=True))
    supersedes_model_id: Mapped[str | None] = mapped_column(ForeignKey("commercial_cost_models.id"))
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    finalized_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "model_kind IN ('LOGISTICS', 'MOBILISATION', 'CONTRACT_FINANCE')",
            name="ck_commercial_cost_model_kind",
        ),
        CheckConstraint(
            "status IN ('BLOCKED', 'REVIEW_REQUIRED', 'VALIDATED')",
            name="ck_commercial_cost_model_status",
        ),
        CheckConstraint(
            "total >= 0 AND independent_total >= 0",
            name="ck_commercial_cost_model_totals",
        ),
        CheckConstraint(
            "("
            "status = 'VALIDATED' AND finalized_by IS NOT NULL "
            "AND finalized_at IS NOT NULL AND approval_record_ids IS NOT NULL"
            ") OR ("
            "status <> 'VALIDATED' AND finalized_by IS NULL "
            "AND finalized_at IS NULL AND approval_record_ids IS NULL "
            "AND is_current = false"
            ")",
            name="ck_commercial_cost_model_finalization",
        ),
        Index(
            "uq_commercial_cost_model_current_target",
            "project_id",
            "target_line_id",
            "target_semantic_key",
            unique=True,
            sqlite_where=text("is_current = 1"),
            postgresql_where=text("is_current"),
        ),
        Index(
            "ix_commercial_cost_model_project_kind",
            "project_id",
            "model_kind",
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


class BusinessQualificationCampaignRow(Base):
    __tablename__ = "business_qualification_campaigns"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    profile_version_id: Mapped[str] = mapped_column(
        ForeignKey("controlled_versions.id"), nullable=False
    )
    dataset_version_id: Mapped[str] = mapped_column(
        ForeignKey("controlled_versions.id"), nullable=False
    )
    profile_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    application_build_reference: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    locked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evaluated_by: Mapped[str | None] = mapped_column(String(128))
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finalized_by: Mapped[str | None] = mapped_column(String(128))
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_hash: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        CheckConstraint(
            "status IN ('INPUTS_LOCKED', 'EXPERT_REVIEW', 'FAILED', 'PASSED')",
            name="ck_business_qualification_campaign_status",
        ),
        CheckConstraint(
            "("
            "status = 'INPUTS_LOCKED' AND evaluated_by IS NULL "
            "AND evaluated_at IS NULL AND finalized_by IS NULL "
            "AND finalized_at IS NULL AND result_hash IS NULL"
            ") OR ("
            "status IN ('EXPERT_REVIEW', 'FAILED') AND evaluated_by IS NOT NULL "
            "AND evaluated_at IS NOT NULL AND finalized_by IS NULL "
            "AND finalized_at IS NULL AND result_hash IS NOT NULL"
            ") OR ("
            "status = 'PASSED' AND evaluated_by IS NOT NULL "
            "AND evaluated_at IS NOT NULL AND finalized_by IS NOT NULL "
            "AND finalized_at IS NOT NULL AND result_hash IS NOT NULL"
            ")",
            name="ck_business_qualification_campaign_lifecycle",
        ),
        UniqueConstraint(
            "organization_id",
            "input_hash",
            name="uq_business_qualification_campaign_input",
        ),
        Index(
            "ix_business_qualification_campaign_org_status",
            "organization_id",
            "status",
        ),
    )


class BusinessQualificationCaseRow(Base):
    __tablename__ = "business_qualification_cases"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("business_qualification_campaigns.id"), nullable=False, index=True
    )
    case_key: Mapped[str] = mapped_column(String(128), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("calculation_snapshots.id"), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    prediction_total: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    prediction_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    stratum: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "mode IN ('HISTORICAL', 'BLIND', 'PARALLEL')",
            name="ck_business_qualification_case_mode",
        ),
        CheckConstraint(
            "prediction_total > 0",
            name="ck_business_qualification_case_prediction_positive",
        ),
        UniqueConstraint(
            "campaign_id",
            "case_key",
            name="uq_business_qualification_case_key",
        ),
        UniqueConstraint(
            "campaign_id",
            "snapshot_id",
            name="uq_business_qualification_case_snapshot",
        ),
    )


class BusinessQualificationReferenceRow(Base):
    __tablename__ = "business_qualification_references"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("business_qualification_campaigns.id"), nullable=False, index=True
    )
    case_id: Mapped[str] = mapped_column(
        ForeignKey("business_qualification_cases.id"), nullable=False, unique=True
    )
    reference_kind: Mapped[str] = mapped_column(String(50), nullable=False)
    source_entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source_entity_id: Mapped[str] = mapped_column(String(64), nullable=False)
    reference_total: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    independence_domain: Mapped[str] = mapped_column(String(200), nullable=False)
    professional_estimator_id: Mapped[str | None] = mapped_column(String(200))
    performed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    registered_by: Mapped[str] = mapped_column(String(128), nullable=False)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "reference_kind IN ('VERIFIED_ACTUAL', 'PROFESSIONAL_ESTIMATE', 'PARALLEL_ESTIMATE')",
            name="ck_business_qualification_reference_kind",
        ),
        CheckConstraint(
            "source_entity_type IN ('ACTUAL_RECORD', 'OBSERVATION')",
            name="ck_business_qualification_reference_source_type",
        ),
        CheckConstraint(
            "reference_total > 0",
            name="ck_business_qualification_reference_total_positive",
        ),
        UniqueConstraint(
            "campaign_id",
            "source_entity_type",
            "source_entity_id",
            name="uq_business_qualification_reference_source",
        ),
    )


class BusinessQualificationEvaluationRow(Base):
    __tablename__ = "business_qualification_evaluations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("business_qualification_campaigns.id"), nullable=False, unique=True
    )
    metrics_passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    result_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    evaluated_by: Mapped[str] = mapped_column(String(128), nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BusinessQualificationDiscrepancyRow(Base):
    __tablename__ = "business_qualification_discrepancies"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("business_qualification_campaigns.id"), nullable=False, index=True
    )
    evaluation_id: Mapped[str] = mapped_column(
        ForeignKey("business_qualification_evaluations.id"), nullable=False
    )
    case_id: Mapped[str] = mapped_column(
        ForeignKey("business_qualification_cases.id"), nullable=False, unique=True
    )
    absolute_error: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    exact_ratio_numerator: Mapped[str] = mapped_column(Text, nullable=False)
    exact_ratio_denominator: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "absolute_error >= 0",
            name="ck_business_qualification_discrepancy_absolute_error",
        ),
    )


class BusinessQualificationDiscrepancyReviewRow(Base):
    __tablename__ = "business_qualification_discrepancy_reviews"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    discrepancy_id: Mapped[str] = mapped_column(
        ForeignKey("business_qualification_discrepancies.id"),
        nullable=False,
        unique=True,
    )
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(100), nullable=False)
    root_cause: Mapped[str] = mapped_column(Text, nullable=False)
    corrective_action: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_observation_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    reviewed_by: Mapped[str] = mapped_column(String(128), nullable=False)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "decision IN ('ACCEPTED', 'REJECTED')",
            name="ck_business_qualification_discrepancy_review_decision",
        ),
    )


class BusinessQualificationApprovalRow(Base):
    __tablename__ = "business_qualification_approvals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("business_qualification_campaigns.id"), nullable=False, unique=True
    )
    evaluation_id: Mapped[str] = mapped_column(
        ForeignKey("business_qualification_evaluations.id"), nullable=False, unique=True
    )
    package_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    approved_by: Mapped[str] = mapped_column(String(128), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProductionGateEvidencePackageRow(Base):
    __tablename__ = "production_gate_evidence_packages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    gate_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    profile_version_id: Mapped[str] = mapped_column(
        ForeignKey("controlled_versions.id"), nullable=False
    )
    profile_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    application_build_reference: Mapped[str] = mapped_column(String(200), nullable=False)
    environment: Mapped[str] = mapped_column(String(100), nullable=False)
    evidence_mode: Mapped[str] = mapped_column(String(50), nullable=False)
    package_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    statement_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    technical_result_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    attester_id: Mapped[str | None] = mapped_column(String(200))
    attester_key_id: Mapped[str | None] = mapped_column(String(200))
    attestation_signature_b64: Mapped[str | None] = mapped_column(String(200))
    submitted_by: Mapped[str] = mapped_column(String(128), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "gate_name IN ("
            "'rules_and_catalog_calibration', "
            "'damaged_conflicting_document_resilience', "
            "'load_test', 'security_review', 'backup_restore', "
            "'methodology_approval'"
            ")",
            name="ck_production_gate_evidence_package_gate",
        ),
        CheckConstraint(
            "evidence_mode IN ('INTERNAL_QUALIFICATION_RESULT', 'EXTERNAL_ATTESTED_PACKAGE')",
            name="ck_production_gate_evidence_package_mode",
        ),
        Index(
            "ix_production_gate_evidence_org_gate",
            "organization_id",
            "gate_name",
        ),
    )


class ProductionGateEvidenceApprovalRow(Base):
    __tablename__ = "production_gate_evidence_approvals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    package_id: Mapped[str] = mapped_column(
        ForeignKey("production_gate_evidence_packages.id"),
        nullable=False,
        unique=True,
    )
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    approval_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    reviewed_by: Mapped[str] = mapped_column(String(128), nullable=False)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "decision IN ('APPROVED', 'REJECTED')",
            name="ck_production_gate_evidence_approval_decision",
        ),
    )


class ProductionGateEvidenceRevocationRow(Base):
    __tablename__ = "production_gate_evidence_revocations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    package_id: Mapped[str] = mapped_column(
        ForeignKey("production_gate_evidence_packages.id"),
        nullable=False,
        unique=True,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    revoked_by: Mapped[str] = mapped_column(String(128), nullable=False)
    revoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
