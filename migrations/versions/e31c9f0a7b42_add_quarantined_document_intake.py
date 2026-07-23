"""add quarantined document intake

Revision ID: e31c9f0a7b42
Revises: d59e7b3f1c08
Create Date: 2026-07-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e31c9f0a7b42"
down_revision: str | None = "d59e7b3f1c08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_empty_tables(direction: str) -> None:
    connection = op.get_bind()
    for table in ("malware_scan_results", "quarantined_uploads"):
        count = connection.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        if count:
            raise RuntimeError(
                f"Cannot {direction} quarantine schema while {table} contains rows; "
                "preserve and migrate the immutable security evidence explicitly"
            )


def upgrade() -> None:
    op.create_table(
        "quarantined_uploads",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("logical_key", sa.String(length=300), nullable=False),
        sa.Column("title", sa.String(length=1000), nullable=False),
        sa.Column("document_type", sa.String(length=100), nullable=False),
        sa.Column("critical", sa.Boolean(), nullable=False),
        sa.Column("revision_label", sa.String(length=100), nullable=False),
        sa.Column("original_filename", sa.String(length=1000), nullable=False),
        sa.Column("declared_media_type", sa.String(length=200), nullable=False),
        sa.Column("make_candidate_current", sa.Boolean(), nullable=False),
        sa.Column("object_hash", sa.String(length=64), nullable=False),
        sa.Column("object_key", sa.String(length=1000), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("uploaded_by", sa.String(length=128), nullable=False),
        sa.Column("invalidated_document_set_revision_id", sa.String(length=64)),
        sa.Column("processed_document_id", sa.String(length=64)),
        sa.Column("processed_document_revision_id", sa.String(length=64)),
        sa.Column("candidate_document_set_revision_id", sa.String(length=64)),
        sa.Column("result_payload", sa.JSON()),
        sa.Column("failure_code", sa.String(length=100)),
        sa.Column("failure_detail", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["processed_document_id"], ["documents.id"]),
        sa.ForeignKeyConstraint(
            ["processed_document_revision_id"],
            ["document_revisions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["candidate_document_set_revision_id"],
            ["document_set_revisions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_quarantined_uploads_project_id",
        "quarantined_uploads",
        ["project_id"],
    )
    op.create_index(
        "ix_quarantined_uploads_organization_id",
        "quarantined_uploads",
        ["organization_id"],
    )
    op.create_index(
        "ix_quarantined_uploads_object_hash",
        "quarantined_uploads",
        ["object_hash"],
    )
    op.create_index(
        "ix_quarantined_uploads_status",
        "quarantined_uploads",
        ["status"],
    )
    op.create_index(
        "uq_active_quarantined_upload_per_logical",
        "quarantined_uploads",
        ["project_id", "logical_key"],
        unique=True,
        sqlite_where=sa.text(
            "status IN ('QUARANTINED', 'CLEAN', 'SCAN_FAILED', 'PROCESSING', 'PROCESSING_FAILED')"
        ),
        postgresql_where=sa.text(
            "status IN ('QUARANTINED', 'CLEAN', 'SCAN_FAILED', 'PROCESSING', 'PROCESSING_FAILED')"
        ),
    )
    op.create_table(
        "malware_scan_results",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("quarantined_upload_id", sa.String(length=64), nullable=False),
        sa.Column("adapter_qualification_id", sa.String(length=128), nullable=False),
        sa.Column("scanner_run_id", sa.String(length=200), nullable=False),
        sa.Column("scanned_object_hash", sa.String(length=64), nullable=False),
        sa.Column("verdict", sa.String(length=50), nullable=False),
        sa.Column("definitions_version", sa.String(length=200), nullable=False),
        sa.Column("report_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["quarantined_upload_id"],
            ["quarantined_uploads.id"],
        ),
        sa.ForeignKeyConstraint(
            ["adapter_qualification_id"],
            ["adapter_qualifications.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "adapter_qualification_id",
            "scanner_run_id",
            name="uq_malware_scan_adapter_run",
        ),
    )
    op.create_index(
        "ix_malware_scan_results_quarantined_upload_id",
        "malware_scan_results",
        ["quarantined_upload_id"],
    )
    op.create_index(
        "ix_malware_scan_results_verdict",
        "malware_scan_results",
        ["verdict"],
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE FUNCTION tenderguard_protect_quarantine_identity()
            RETURNS trigger AS $$
            BEGIN
                IF NEW.project_id IS DISTINCT FROM OLD.project_id
                   OR NEW.organization_id IS DISTINCT FROM OLD.organization_id
                   OR NEW.logical_key IS DISTINCT FROM OLD.logical_key
                   OR NEW.title IS DISTINCT FROM OLD.title
                   OR NEW.document_type IS DISTINCT FROM OLD.document_type
                   OR NEW.critical IS DISTINCT FROM OLD.critical
                   OR NEW.revision_label IS DISTINCT FROM OLD.revision_label
                   OR NEW.original_filename IS DISTINCT FROM OLD.original_filename
                   OR NEW.declared_media_type IS DISTINCT FROM OLD.declared_media_type
                   OR NEW.make_candidate_current IS DISTINCT FROM OLD.make_candidate_current
                   OR NEW.object_hash IS DISTINCT FROM OLD.object_hash
                   OR NEW.object_key IS DISTINCT FROM OLD.object_key
                   OR NEW.size_bytes IS DISTINCT FROM OLD.size_bytes
                   OR NEW.uploaded_by IS DISTINCT FROM OLD.uploaded_by
                   OR NEW.invalidated_document_set_revision_id
                      IS DISTINCT FROM OLD.invalidated_document_set_revision_id
                   OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                    RAISE EXCEPTION 'quarantine upload identity is immutable';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_quarantined_upload_identity
            BEFORE UPDATE ON quarantined_uploads
            FOR EACH ROW EXECUTE FUNCTION tenderguard_protect_quarantine_identity();
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_malware_scan_results_immutable
            BEFORE UPDATE OR DELETE ON malware_scan_results
            FOR EACH ROW EXECUTE FUNCTION tenderguard_reject_immutable_mutation();
            """
        )


def downgrade() -> None:
    _require_empty_tables("downgrade")
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_malware_scan_results_immutable ON malware_scan_results"
        )
        op.execute("DROP TRIGGER IF EXISTS trg_quarantined_upload_identity ON quarantined_uploads")
        op.execute("DROP FUNCTION IF EXISTS tenderguard_protect_quarantine_identity()")
    op.drop_index(
        "ix_malware_scan_results_verdict",
        table_name="malware_scan_results",
    )
    op.drop_index(
        "ix_malware_scan_results_quarantined_upload_id",
        table_name="malware_scan_results",
    )
    op.drop_table("malware_scan_results")
    op.drop_index("ix_quarantined_uploads_status", table_name="quarantined_uploads")
    op.drop_index(
        "uq_active_quarantined_upload_per_logical",
        table_name="quarantined_uploads",
    )
    op.drop_index(
        "ix_quarantined_uploads_object_hash",
        table_name="quarantined_uploads",
    )
    op.drop_index(
        "ix_quarantined_uploads_organization_id",
        table_name="quarantined_uploads",
    )
    op.drop_index(
        "ix_quarantined_uploads_project_id",
        table_name="quarantined_uploads",
    )
    op.drop_table("quarantined_uploads")
