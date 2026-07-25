"""add governed business qualification campaigns

Revision ID: c7e9a2d4f681
Revises: b4d8e1f6c205
Create Date: 2026-07-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7e9a2d4f681"
down_revision: str | None = "b4d8e1f6c205"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_IMMUTABLE_TABLES = (
    "business_qualification_cases",
    "business_qualification_references",
    "business_qualification_evaluations",
    "business_qualification_discrepancies",
    "business_qualification_discrepancy_reviews",
    "business_qualification_approvals",
)


def _install_postgresql_guards() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table_name in _IMMUTABLE_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_immutable
            BEFORE UPDATE OR DELETE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION tenderguard_reject_immutable_mutation();
            """
        )
    op.execute(
        """
        CREATE FUNCTION tenderguard_protect_business_qualification_campaign()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'business qualification campaign cannot be deleted';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
                OR NEW.organization_id IS DISTINCT FROM OLD.organization_id
                OR NEW.profile_version_id IS DISTINCT FROM OLD.profile_version_id
                OR NEW.dataset_version_id IS DISTINCT FROM OLD.dataset_version_id
                OR NEW.profile_hash IS DISTINCT FROM OLD.profile_hash
                OR NEW.dataset_hash IS DISTINCT FROM OLD.dataset_hash
                OR NEW.application_build_reference
                    IS DISTINCT FROM OLD.application_build_reference
                OR NEW.input_hash IS DISTINCT FROM OLD.input_hash
                OR NEW.payload::jsonb IS DISTINCT FROM OLD.payload::jsonb
                OR NEW.created_by IS DISTINCT FROM OLD.created_by
                OR NEW.locked_at IS DISTINCT FROM OLD.locked_at
            THEN
                RAISE EXCEPTION 'business qualification campaign basis is immutable';
            END IF;
            IF OLD.status = 'INPUTS_LOCKED'
                AND NEW.status IN ('EXPERT_REVIEW', 'FAILED')
                AND OLD.evaluated_by IS NULL
                AND OLD.evaluated_at IS NULL
                AND OLD.finalized_by IS NULL
                AND OLD.finalized_at IS NULL
                AND OLD.result_hash IS NULL
                AND NEW.evaluated_by IS NOT NULL
                AND NEW.evaluated_at IS NOT NULL
                AND NEW.finalized_by IS NULL
                AND NEW.finalized_at IS NULL
                AND NEW.result_hash IS NOT NULL
            THEN
                RETURN NEW;
            END IF;
            IF OLD.status = 'EXPERT_REVIEW'
                AND NEW.status = 'PASSED'
                AND NEW.evaluated_by IS NOT DISTINCT FROM OLD.evaluated_by
                AND NEW.evaluated_at IS NOT DISTINCT FROM OLD.evaluated_at
                AND NEW.result_hash IS NOT DISTINCT FROM OLD.result_hash
                AND OLD.finalized_by IS NULL
                AND OLD.finalized_at IS NULL
                AND NEW.finalized_by IS NOT NULL
                AND NEW.finalized_at IS NOT NULL
            THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'invalid business qualification campaign transition';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_business_qualification_campaign_lifecycle
        BEFORE UPDATE OR DELETE ON business_qualification_campaigns
        FOR EACH ROW
        EXECUTE FUNCTION tenderguard_protect_business_qualification_campaign();
        """
    )


def upgrade() -> None:
    op.create_table(
        "business_qualification_campaigns",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("profile_version_id", sa.String(length=64), nullable=False),
        sa.Column("dataset_version_id", sa.String(length=64), nullable=False),
        sa.Column("profile_hash", sa.String(length=64), nullable=False),
        sa.Column("dataset_hash", sa.String(length=64), nullable=False),
        sa.Column("application_build_reference", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evaluated_by", sa.String(length=128)),
        sa.Column("evaluated_at", sa.DateTime(timezone=True)),
        sa.Column("finalized_by", sa.String(length=128)),
        sa.Column("finalized_at", sa.DateTime(timezone=True)),
        sa.Column("result_hash", sa.String(length=64)),
        sa.CheckConstraint(
            "status IN ('INPUTS_LOCKED', 'EXPERT_REVIEW', 'FAILED', 'PASSED')",
            name="ck_business_qualification_campaign_status",
        ),
        sa.CheckConstraint(
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
        sa.ForeignKeyConstraint(["dataset_version_id"], ["controlled_versions.id"]),
        sa.ForeignKeyConstraint(["profile_version_id"], ["controlled_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "input_hash",
            name="uq_business_qualification_campaign_input",
        ),
    )
    op.create_index(
        "ix_business_qualification_campaigns_organization_id",
        "business_qualification_campaigns",
        ["organization_id"],
    )
    op.create_index(
        "ix_business_qualification_campaigns_status",
        "business_qualification_campaigns",
        ["status"],
    )
    op.create_index(
        "ix_business_qualification_campaign_org_status",
        "business_qualification_campaigns",
        ["organization_id", "status"],
    )

    op.create_table(
        "business_qualification_cases",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("case_key", sa.String(length=128), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("prediction_total", sa.Numeric(precision=38, scale=12), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("prediction_hash", sa.String(length=64), nullable=False),
        sa.Column("stratum", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "mode IN ('HISTORICAL', 'BLIND', 'PARALLEL')",
            name="ck_business_qualification_case_mode",
        ),
        sa.CheckConstraint(
            "prediction_total > 0",
            name="ck_business_qualification_case_prediction_positive",
        ),
        sa.ForeignKeyConstraint(["campaign_id"], ["business_qualification_campaigns.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["snapshot_id"], ["calculation_snapshots.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "campaign_id",
            "case_key",
            name="uq_business_qualification_case_key",
        ),
        sa.UniqueConstraint(
            "campaign_id",
            "snapshot_id",
            name="uq_business_qualification_case_snapshot",
        ),
    )
    op.create_index(
        "ix_business_qualification_cases_campaign_id",
        "business_qualification_cases",
        ["campaign_id"],
    )
    op.create_index(
        "ix_business_qualification_cases_project_id",
        "business_qualification_cases",
        ["project_id"],
    )

    op.create_table(
        "business_qualification_references",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("case_id", sa.String(length=64), nullable=False),
        sa.Column("reference_kind", sa.String(length=50), nullable=False),
        sa.Column("source_entity_type", sa.String(length=50), nullable=False),
        sa.Column("source_entity_id", sa.String(length=64), nullable=False),
        sa.Column("reference_total", sa.Numeric(precision=38, scale=12), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("independence_domain", sa.String(length=200), nullable=False),
        sa.Column("professional_estimator_id", sa.String(length=200)),
        sa.Column("performed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("registered_by", sa.String(length=128), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "reference_kind IN ('VERIFIED_ACTUAL', 'PROFESSIONAL_ESTIMATE', 'PARALLEL_ESTIMATE')",
            name="ck_business_qualification_reference_kind",
        ),
        sa.CheckConstraint(
            "source_entity_type IN ('ACTUAL_RECORD', 'OBSERVATION')",
            name="ck_business_qualification_reference_source_type",
        ),
        sa.CheckConstraint(
            "reference_total > 0",
            name="ck_business_qualification_reference_total_positive",
        ),
        sa.ForeignKeyConstraint(["campaign_id"], ["business_qualification_campaigns.id"]),
        sa.ForeignKeyConstraint(["case_id"], ["business_qualification_cases.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("case_id"),
        sa.UniqueConstraint(
            "campaign_id",
            "source_entity_type",
            "source_entity_id",
            name="uq_business_qualification_reference_source",
        ),
    )
    op.create_index(
        "ix_business_qualification_references_campaign_id",
        "business_qualification_references",
        ["campaign_id"],
    )

    op.create_table(
        "business_qualification_evaluations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("metrics_passed", sa.Boolean(), nullable=False),
        sa.Column("result_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("evaluated_by", sa.String(length=128), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["business_qualification_campaigns.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id"),
        sa.UniqueConstraint("result_hash"),
    )

    op.create_table(
        "business_qualification_discrepancies",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("evaluation_id", sa.String(length=64), nullable=False),
        sa.Column("case_id", sa.String(length=64), nullable=False),
        sa.Column("absolute_error", sa.Numeric(precision=38, scale=12), nullable=False),
        sa.Column("exact_ratio_numerator", sa.Text(), nullable=False),
        sa.Column("exact_ratio_denominator", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "absolute_error >= 0",
            name="ck_business_qualification_discrepancy_absolute_error",
        ),
        sa.ForeignKeyConstraint(["campaign_id"], ["business_qualification_campaigns.id"]),
        sa.ForeignKeyConstraint(["case_id"], ["business_qualification_cases.id"]),
        sa.ForeignKeyConstraint(["evaluation_id"], ["business_qualification_evaluations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("case_id"),
    )
    op.create_index(
        "ix_business_qualification_discrepancies_campaign_id",
        "business_qualification_discrepancies",
        ["campaign_id"],
    )

    op.create_table(
        "business_qualification_discrepancy_reviews",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("discrepancy_id", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("reason_code", sa.String(length=100), nullable=False),
        sa.Column("root_cause", sa.Text(), nullable=False),
        sa.Column("corrective_action", sa.Text(), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("evidence_observation_ids", sa.JSON(), nullable=False),
        sa.Column("reviewed_by", sa.String(length=128), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision IN ('ACCEPTED', 'REJECTED')",
            name="ck_business_qualification_discrepancy_review_decision",
        ),
        sa.ForeignKeyConstraint(["discrepancy_id"], ["business_qualification_discrepancies.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("discrepancy_id"),
    )

    op.create_table(
        "business_qualification_approvals",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("evaluation_id", sa.String(length=64), nullable=False),
        sa.Column("package_hash", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("approved_by", sa.String(length=128), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["business_qualification_campaigns.id"]),
        sa.ForeignKeyConstraint(["evaluation_id"], ["business_qualification_evaluations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id"),
        sa.UniqueConstraint("evaluation_id"),
        sa.UniqueConstraint("package_hash"),
    )
    _install_postgresql_guards()


def downgrade() -> None:
    connection = op.get_bind()
    count = connection.execute(
        sa.text("SELECT COUNT(*) FROM business_qualification_campaigns")
    ).scalar_one()
    if count:
        raise RuntimeError("cannot remove governed business qualification campaign evidence")
    if connection.dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS "
            "trg_business_qualification_campaign_lifecycle "
            "ON business_qualification_campaigns"
        )
        op.execute("DROP FUNCTION IF EXISTS tenderguard_protect_business_qualification_campaign()")
        for table_name in reversed(_IMMUTABLE_TABLES):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_immutable ON {table_name}")
    op.drop_table("business_qualification_approvals")
    op.drop_table("business_qualification_discrepancy_reviews")
    op.drop_index(
        "ix_business_qualification_discrepancies_campaign_id",
        table_name="business_qualification_discrepancies",
    )
    op.drop_table("business_qualification_discrepancies")
    op.drop_table("business_qualification_evaluations")
    op.drop_index(
        "ix_business_qualification_references_campaign_id",
        table_name="business_qualification_references",
    )
    op.drop_table("business_qualification_references")
    op.drop_index(
        "ix_business_qualification_cases_project_id",
        table_name="business_qualification_cases",
    )
    op.drop_index(
        "ix_business_qualification_cases_campaign_id",
        table_name="business_qualification_cases",
    )
    op.drop_table("business_qualification_cases")
    op.drop_index(
        "ix_business_qualification_campaign_org_status",
        table_name="business_qualification_campaigns",
    )
    op.drop_index(
        "ix_business_qualification_campaigns_status",
        table_name="business_qualification_campaigns",
    )
    op.drop_index(
        "ix_business_qualification_campaigns_organization_id",
        table_name="business_qualification_campaigns",
    )
    op.drop_table("business_qualification_campaigns")
