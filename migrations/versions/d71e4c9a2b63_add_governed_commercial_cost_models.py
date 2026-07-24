"""add governed commercial cost models

Revision ID: d71e4c9a2b63
Revises: ca3e6a9d1f42
Create Date: 2026-07-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d71e4c9a2b63"
down_revision: str | None = "ca3e6a9d1f42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _install_postgresql_guard() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE FUNCTION tenderguard_protect_commercial_cost_model()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'commercial cost models cannot be deleted';
            END IF;
            IF OLD.id IS DISTINCT FROM NEW.id
               OR OLD.project_id IS DISTINCT FROM NEW.project_id
               OR OLD.model_kind IS DISTINCT FROM NEW.model_kind
               OR OLD.target_line_id IS DISTINCT FROM NEW.target_line_id
               OR OLD.target_semantic_key IS DISTINCT FROM NEW.target_semantic_key
               OR OLD.category IS DISTINCT FROM NEW.category
               OR OLD.policy_version_id IS DISTINCT FROM NEW.policy_version_id
               OR OLD.document_set_revision_id IS DISTINCT FROM NEW.document_set_revision_id
               OR OLD.currency IS DISTINCT FROM NEW.currency
               OR OLD.total IS DISTINCT FROM NEW.total
               OR OLD.independent_total IS DISTINCT FROM NEW.independent_total
               OR OLD.input_hash IS DISTINCT FROM NEW.input_hash
               OR OLD.output_hash IS DISTINCT FROM NEW.output_hash
               OR OLD.payload::text IS DISTINCT FROM NEW.payload::text
               OR OLD.approval_task_ids::text IS DISTINCT FROM NEW.approval_task_ids::text
               OR OLD.supersedes_model_id IS DISTINCT FROM NEW.supersedes_model_id
               OR OLD.created_by IS DISTINCT FROM NEW.created_by
               OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
                RAISE EXCEPTION 'commercial cost model inputs and outputs are immutable';
            END IF;
            IF OLD.status = 'REVIEW_REQUIRED'
               AND NEW.status = 'VALIDATED'
               AND OLD.is_current = false
               AND NEW.is_current = true
               AND OLD.approval_record_ids IS NULL
               AND NEW.approval_record_ids IS NOT NULL
               AND OLD.finalized_by IS NULL
               AND NEW.finalized_by IS NOT NULL
               AND OLD.finalized_at IS NULL
               AND NEW.finalized_at IS NOT NULL THEN
                RETURN NEW;
            END IF;
            IF OLD.status = 'REVIEW_REQUIRED'
               AND NEW.status = 'BLOCKED'
               AND OLD.is_current = false
               AND NEW.is_current = false
               AND OLD.approval_record_ids IS NULL
               AND NEW.approval_record_ids IS NULL
               AND OLD.finalized_by IS NULL
               AND NEW.finalized_by IS NULL
               AND OLD.finalized_at IS NULL
               AND NEW.finalized_at IS NULL THEN
                RETURN NEW;
            END IF;
            IF OLD.status = 'VALIDATED'
               AND NEW.status = 'VALIDATED'
               AND OLD.is_current = true
               AND NEW.is_current = false
               AND OLD.approval_record_ids::text
                   IS NOT DISTINCT FROM NEW.approval_record_ids::text
               AND OLD.finalized_by IS NOT DISTINCT FROM NEW.finalized_by
               AND OLD.finalized_at IS NOT DISTINCT FROM NEW.finalized_at THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'invalid commercial cost model transition';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_commercial_cost_model_protected
        BEFORE UPDATE OR DELETE ON commercial_cost_models
        FOR EACH ROW EXECUTE FUNCTION tenderguard_protect_commercial_cost_model();
        """
    )


def upgrade() -> None:
    op.create_table(
        "commercial_cost_models",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=64),
            sa.ForeignKey("projects.id"),
            nullable=False,
        ),
        sa.Column("model_kind", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column(
            "target_line_id",
            sa.String(length=64),
            sa.ForeignKey("boq_lines.id"),
            nullable=False,
        ),
        sa.Column("target_semantic_key", sa.String(length=200), nullable=False),
        sa.Column("category", sa.String(length=80), nullable=False),
        sa.Column(
            "policy_version_id",
            sa.String(length=64),
            sa.ForeignKey("controlled_versions.id"),
            nullable=False,
        ),
        sa.Column(
            "document_set_revision_id",
            sa.String(length=64),
            sa.ForeignKey("document_set_revisions.id"),
            nullable=False,
        ),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("total", sa.Numeric(38, 12), nullable=False),
        sa.Column("independent_total", sa.Numeric(38, 12), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("output_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("approval_task_ids", sa.JSON(), nullable=False),
        sa.Column("approval_record_ids", sa.JSON(none_as_null=True)),
        sa.Column(
            "supersedes_model_id",
            sa.String(length=64),
            sa.ForeignKey("commercial_cost_models.id"),
        ),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("finalized_by", sa.String(length=128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "model_kind IN ('LOGISTICS', 'MOBILISATION', 'CONTRACT_FINANCE')",
            name="ck_commercial_cost_model_kind",
        ),
        sa.CheckConstraint(
            "status IN ('BLOCKED', 'REVIEW_REQUIRED', 'VALIDATED')",
            name="ck_commercial_cost_model_status",
        ),
        sa.CheckConstraint(
            "total >= 0 AND independent_total >= 0",
            name="ck_commercial_cost_model_totals",
        ),
        sa.CheckConstraint(
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
    )
    op.create_index(
        "ix_commercial_cost_models_project_id",
        "commercial_cost_models",
        ["project_id"],
    )
    op.create_index(
        "ix_commercial_cost_models_model_kind",
        "commercial_cost_models",
        ["model_kind"],
    )
    op.create_index(
        "ix_commercial_cost_models_status",
        "commercial_cost_models",
        ["status"],
    )
    op.create_index(
        "ix_commercial_cost_models_is_current",
        "commercial_cost_models",
        ["is_current"],
    )
    op.create_index(
        "ix_commercial_cost_model_project_kind",
        "commercial_cost_models",
        ["project_id", "model_kind"],
    )
    op.create_index(
        "uq_commercial_cost_model_current_target",
        "commercial_cost_models",
        ["project_id", "target_line_id", "target_semantic_key"],
        unique=True,
        sqlite_where=sa.text("is_current = 1"),
        postgresql_where=sa.text("is_current"),
    )
    _install_postgresql_guard()


def downgrade() -> None:
    count = (
        op.get_bind().execute(sa.text("SELECT COUNT(*) FROM commercial_cost_models")).scalar_one()
    )
    if count:
        raise RuntimeError("Cannot downgrade after commercial cost model evidence exists")
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_commercial_cost_model_protected ON commercial_cost_models"
        )
        op.execute("DROP FUNCTION IF EXISTS tenderguard_protect_commercial_cost_model()")
    op.drop_index(
        "uq_commercial_cost_model_current_target",
        table_name="commercial_cost_models",
    )
    op.drop_index(
        "ix_commercial_cost_model_project_kind",
        table_name="commercial_cost_models",
    )
    op.drop_index(
        "ix_commercial_cost_models_is_current",
        table_name="commercial_cost_models",
    )
    op.drop_index(
        "ix_commercial_cost_models_status",
        table_name="commercial_cost_models",
    )
    op.drop_index(
        "ix_commercial_cost_models_model_kind",
        table_name="commercial_cost_models",
    )
    op.drop_index(
        "ix_commercial_cost_models_project_id",
        table_name="commercial_cost_models",
    )
    op.drop_table("commercial_cost_models")
