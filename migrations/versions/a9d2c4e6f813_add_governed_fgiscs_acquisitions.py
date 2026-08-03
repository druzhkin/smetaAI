"""add governed FGIS CS acquisition evidence

Revision ID: a9d2c4e6f813
Revises: f7c1a9d4e620
Create Date: 2026-07-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a9d2c4e6f813"
down_revision: str | None = "f7c1a9d4e620"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fgiscs_acquisitions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("item_id", sa.String(length=128), nullable=False),
        sa.Column("nomenclature_match_id", sa.String(length=64), nullable=False),
        sa.Column("document_set_revision_id", sa.String(length=64), nullable=False),
        sa.Column("policy_version_id", sa.String(length=64), nullable=False),
        sa.Column("adapter_qualification_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("artifact_object_hash", sa.String(length=64), nullable=False),
        sa.Column("artifact_object_key", sa.String(length=1000), nullable=False),
        sa.Column("artifact_size_bytes", sa.Integer(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(
            ["nomenclature_match_id"],
            ["nomenclature_matches.id"],
        ),
        sa.ForeignKeyConstraint(
            ["document_set_revision_id"],
            ["document_set_revisions.id"],
        ),
        sa.ForeignKeyConstraint(["policy_version_id"], ["controlled_versions.id"]),
        sa.ForeignKeyConstraint(
            ["adapter_qualification_id"],
            ["adapter_qualifications.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id",
            "item_id",
            "artifact_object_hash",
            name="uq_fgiscs_acquisition_artifact_per_item",
        ),
    )
    for column_name in (
        "project_id",
        "item_id",
        "nomenclature_match_id",
        "document_set_revision_id",
        "policy_version_id",
        "adapter_qualification_id",
        "status",
        "artifact_object_hash",
    ):
        op.create_index(
            f"ix_fgiscs_acquisitions_{column_name}",
            "fgiscs_acquisitions",
            [column_name],
        )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE TRIGGER trg_fgiscs_acquisitions_immutable
            BEFORE UPDATE OR DELETE ON fgiscs_acquisitions
            FOR EACH ROW EXECUTE FUNCTION tenderguard_reject_immutable_mutation();
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_fgiscs_acquisitions_immutable ON fgiscs_acquisitions"
        )
    op.drop_table("fgiscs_acquisitions")
