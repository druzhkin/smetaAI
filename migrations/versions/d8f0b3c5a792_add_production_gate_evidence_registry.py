"""add production gate evidence registry

Revision ID: d8f0b3c5a792
Revises: c7e9a2d4f681
Create Date: 2026-07-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d8f0b3c5a792"
down_revision: str | None = "c7e9a2d4f681"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_IMMUTABLE_TABLES = (
    "production_gate_evidence_packages",
    "production_gate_evidence_approvals",
    "production_gate_evidence_revocations",
)


def upgrade() -> None:
    op.create_table(
        "production_gate_evidence_packages",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("gate_name", sa.String(length=100), nullable=False),
        sa.Column("profile_version_id", sa.String(length=64), nullable=False),
        sa.Column("profile_content_hash", sa.String(length=64), nullable=False),
        sa.Column("application_build_reference", sa.String(length=200), nullable=False),
        sa.Column("environment", sa.String(length=100), nullable=False),
        sa.Column("evidence_mode", sa.String(length=50), nullable=False),
        sa.Column("package_hash", sa.String(length=64), nullable=False),
        sa.Column("statement_payload", sa.JSON(), nullable=False),
        sa.Column("technical_result_payload", sa.JSON()),
        sa.Column("attester_id", sa.String(length=200)),
        sa.Column("attester_key_id", sa.String(length=200)),
        sa.Column("attestation_signature_b64", sa.String(length=200)),
        sa.Column("submitted_by", sa.String(length=128), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "gate_name IN ("
            "'rules_and_catalog_calibration', "
            "'damaged_conflicting_document_resilience', "
            "'load_test', 'security_review', 'backup_restore', "
            "'methodology_approval'"
            ")",
            name="ck_production_gate_evidence_package_gate",
        ),
        sa.CheckConstraint(
            "evidence_mode IN ('INTERNAL_QUALIFICATION_RESULT', 'EXTERNAL_ATTESTED_PACKAGE')",
            name="ck_production_gate_evidence_package_mode",
        ),
        sa.ForeignKeyConstraint(["profile_version_id"], ["controlled_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("package_hash"),
    )
    op.create_index(
        "ix_production_gate_evidence_packages_organization_id",
        "production_gate_evidence_packages",
        ["organization_id"],
    )
    op.create_index(
        "ix_production_gate_evidence_packages_gate_name",
        "production_gate_evidence_packages",
        ["gate_name"],
    )
    op.create_index(
        "ix_production_gate_evidence_org_gate",
        "production_gate_evidence_packages",
        ["organization_id", "gate_name"],
    )
    op.create_table(
        "production_gate_evidence_approvals",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("package_id", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("approval_hash", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("reviewed_by", sa.String(length=128), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision IN ('APPROVED', 'REJECTED')",
            name="ck_production_gate_evidence_approval_decision",
        ),
        sa.ForeignKeyConstraint(
            ["package_id"],
            ["production_gate_evidence_packages.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("approval_hash"),
        sa.UniqueConstraint("package_id"),
    )
    op.create_table(
        "production_gate_evidence_revocations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("package_id", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("revoked_by", sa.String(length=128), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["package_id"],
            ["production_gate_evidence_packages.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("package_id"),
    )
    if op.get_bind().dialect.name == "postgresql":
        for table_name in _IMMUTABLE_TABLES:
            op.execute(
                f"""
                CREATE TRIGGER trg_{table_name}_immutable
                BEFORE UPDATE OR DELETE ON {table_name}
                FOR EACH ROW EXECUTE FUNCTION tenderguard_reject_immutable_mutation();
                """
            )


def downgrade() -> None:
    connection = op.get_bind()
    count = connection.execute(
        sa.text("SELECT COUNT(*) FROM production_gate_evidence_packages")
    ).scalar_one()
    if count:
        raise RuntimeError("cannot remove governed production gate evidence")
    if connection.dialect.name == "postgresql":
        for table_name in reversed(_IMMUTABLE_TABLES):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_immutable ON {table_name}")
    op.drop_table("production_gate_evidence_revocations")
    op.drop_table("production_gate_evidence_approvals")
    op.drop_index(
        "ix_production_gate_evidence_org_gate",
        table_name="production_gate_evidence_packages",
    )
    op.drop_index(
        "ix_production_gate_evidence_packages_gate_name",
        table_name="production_gate_evidence_packages",
    )
    op.drop_index(
        "ix_production_gate_evidence_packages_organization_id",
        table_name="production_gate_evidence_packages",
    )
    op.drop_table("production_gate_evidence_packages")
