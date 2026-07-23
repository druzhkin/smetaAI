"""add signed export packages

Revision ID: d59e7b3f1c08
Revises: c48d6a2e0b97
Create Date: 2026-07-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d59e7b3f1c08"
down_revision: str | None = "c48d6a2e0b97"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_empty_export_table(direction: str) -> None:
    count = op.get_bind().execute(sa.text("SELECT COUNT(*) FROM export_artifacts")).scalar_one()
    if count:
        raise RuntimeError(
            f"Cannot {direction} signed-export schema while export_artifacts contains rows; "
            "preserve and migrate those immutable artifacts explicitly"
        )


def upgrade() -> None:
    _require_empty_export_table("upgrade")
    with op.batch_alter_table("export_artifacts") as batch_op:
        batch_op.alter_column(
            "adapter_qualification_id",
            existing_type=sa.String(length=128),
            nullable=True,
        )
        batch_op.add_column(
            sa.Column(
                "release_decision_id",
                sa.String(length=64),
                sa.ForeignKey(
                    "release_decisions.id",
                    name="fk_export_artifacts_release_decision_id",
                ),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "template_version_id",
                sa.String(length=64),
                sa.ForeignKey(
                    "controlled_versions.id",
                    name="fk_export_artifacts_template_version_id",
                ),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column("package_schema_version", sa.String(length=100), nullable=False)
        )
        batch_op.add_column(sa.Column("media_type", sa.String(length=200), nullable=False))
        batch_op.add_column(sa.Column("filename", sa.String(length=500), nullable=False))
        batch_op.add_column(sa.Column("size_bytes", sa.Integer(), nullable=False))
        batch_op.add_column(sa.Column("manifest_hash", sa.String(length=64), nullable=False))
        batch_op.add_column(sa.Column("signature_algorithm", sa.String(length=50), nullable=False))
        batch_op.add_column(sa.Column("signature", sa.String(length=200), nullable=False))
        batch_op.add_column(sa.Column("signing_key_id", sa.String(length=200), nullable=False))
        batch_op.add_column(
            sa.Column("signing_public_key_b64", sa.String(length=100), nullable=False)
        )
        batch_op.add_column(
            sa.Column("public_key_fingerprint", sa.String(length=64), nullable=False)
        )
        batch_op.add_column(sa.Column("created_by", sa.String(length=128), nullable=False))
    op.create_index(
        "uq_signed_export_artifact_basis",
        "export_artifacts",
        ["snapshot_id", "release_decision_id", "template_version_id", "format"],
        unique=True,
        sqlite_where=sa.text("signature_algorithm = 'Ed25519'"),
        postgresql_where=sa.text("signature_algorithm = 'Ed25519'"),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE TRIGGER trg_export_artifacts_immutable
            BEFORE UPDATE OR DELETE ON export_artifacts
            FOR EACH ROW EXECUTE FUNCTION tenderguard_reject_immutable_mutation();
            """
        )


def downgrade() -> None:
    _require_empty_export_table("downgrade")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_export_artifacts_immutable ON export_artifacts")
    op.drop_index("uq_signed_export_artifact_basis", table_name="export_artifacts")
    with op.batch_alter_table("export_artifacts") as batch_op:
        batch_op.drop_column("created_by")
        batch_op.drop_column("public_key_fingerprint")
        batch_op.drop_column("signing_public_key_b64")
        batch_op.drop_column("signing_key_id")
        batch_op.drop_column("signature")
        batch_op.drop_column("signature_algorithm")
        batch_op.drop_column("manifest_hash")
        batch_op.drop_column("size_bytes")
        batch_op.drop_column("filename")
        batch_op.drop_column("media_type")
        batch_op.drop_column("package_schema_version")
        batch_op.drop_column("template_version_id")
        batch_op.drop_column("release_decision_id")
        batch_op.alter_column(
            "adapter_qualification_id",
            existing_type=sa.String(length=128),
            nullable=False,
        )
