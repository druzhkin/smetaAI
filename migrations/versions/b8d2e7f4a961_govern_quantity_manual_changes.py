"""govern quantity manual changes

Revision ID: b8d2e7f4a961
Revises: f6a9c3d2e841
Create Date: 2026-07-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8d2e7f4a961"
down_revision: str | None = "f6a9c3d2e841"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _install_postgresql_guards() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE OR REPLACE FUNCTION tenderguard_protect_manual_change_evidence()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'UPDATE' OR TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'manual change evidence is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_manual_changes_immutable
        BEFORE UPDATE OR DELETE ON manual_changes
        FOR EACH ROW EXECUTE FUNCTION tenderguard_protect_manual_change_evidence();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_quantity_manual_change_applications_immutable
        BEFORE UPDATE OR DELETE ON quantity_manual_change_applications
        FOR EACH ROW EXECUTE FUNCTION tenderguard_protect_manual_change_evidence();
        """
    )


def upgrade() -> None:
    op.create_table(
        "quantity_manual_change_applications",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("manual_change_id", sa.String(length=64), nullable=False),
        sa.Column("quantity_id", sa.String(length=64), nullable=False),
        sa.Column("applied_by", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["manual_change_id"], ["manual_changes.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["quantity_id"], ["quantities.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "manual_change_id",
            name="uq_quantity_manual_change_application_change",
        ),
        sa.UniqueConstraint(
            "quantity_id",
            name="uq_quantity_manual_change_application_quantity",
        ),
    )
    op.create_index(
        "ix_quantity_manual_change_applications_project_id",
        "quantity_manual_change_applications",
        ["project_id"],
    )
    op.create_index(
        "ix_quantity_manual_change_applications_manual_change_id",
        "quantity_manual_change_applications",
        ["manual_change_id"],
    )
    op.create_index(
        "ix_quantity_manual_change_applications_quantity_id",
        "quantity_manual_change_applications",
        ["quantity_id"],
    )
    _install_postgresql_guards()


def downgrade() -> None:
    connection = op.get_bind()
    application_count = connection.execute(
        sa.text("SELECT COUNT(*) FROM quantity_manual_change_applications")
    ).scalar_one()
    if application_count:
        raise RuntimeError("cannot remove governed quantity manual-change application evidence")
    if connection.dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS "
            "trg_quantity_manual_change_applications_immutable "
            "ON quantity_manual_change_applications"
        )
        op.execute("DROP TRIGGER IF EXISTS trg_manual_changes_immutable ON manual_changes")
        op.execute("DROP FUNCTION IF EXISTS tenderguard_protect_manual_change_evidence()")
    op.drop_index(
        "ix_quantity_manual_change_applications_quantity_id",
        table_name="quantity_manual_change_applications",
    )
    op.drop_index(
        "ix_quantity_manual_change_applications_manual_change_id",
        table_name="quantity_manual_change_applications",
    )
    op.drop_index(
        "ix_quantity_manual_change_applications_project_id",
        table_name="quantity_manual_change_applications",
    )
    op.drop_table("quantity_manual_change_applications")
