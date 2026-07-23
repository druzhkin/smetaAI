"""add governed pricing decisions

Revision ID: e94f2c8a6d53
Revises: d83e1b7c5f42
Create Date: 2026-07-23 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e94f2c8a6d53"
down_revision: str | None = "d83e1b7c5f42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "nomenclature_matches",
        sa.Column(
            "status",
            sa.String(length=50),
            nullable=False,
            server_default="UNVERIFIED",
        ),
    )
    op.add_column(
        "nomenclature_matches",
        sa.Column("supersedes_match_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "nomenclature_matches",
        sa.Column(
            "is_current",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.execute(
        """
        UPDATE nomenclature_matches
        SET is_current = TRUE
        WHERE id IN (
            SELECT id
            FROM (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY project_id, source_item_id
                        ORDER BY updated_at DESC, created_at DESC, id DESC
                    ) AS revision_rank
                FROM nomenclature_matches
            ) ranked
            WHERE revision_rank = 1
        )
        """
    )
    op.create_index(
        op.f("ix_nomenclature_matches_status"),
        "nomenclature_matches",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_nomenclature_matches_is_current"),
        "nomenclature_matches",
        ["is_current"],
        unique=False,
    )
    if op.get_bind().dialect.name == "postgresql":
        op.create_index(
            "uq_nomenclature_matches_current_per_source",
            "nomenclature_matches",
            ["project_id", "source_item_id"],
            unique=True,
            postgresql_where=sa.text("is_current"),
        )
    else:
        op.create_index(
            "uq_nomenclature_matches_current_per_source",
            "nomenclature_matches",
            ["project_id", "source_item_id"],
            unique=True,
            sqlite_where=sa.text("is_current = 1"),
        )
    with op.batch_alter_table("price_quotes") as batch_op:
        batch_op.add_column(
            sa.Column("source_observation_id", sa.String(length=64), nullable=True),
        )
        batch_op.create_foreign_key(
            "fk_price_quotes_source_observation_id",
            "evidence_observations",
            ["source_observation_id"],
            ["id"],
        )
        batch_op.create_index(
            op.f("ix_price_quotes_source_observation_id"),
            ["source_observation_id"],
            unique=False,
        )
    op.create_table(
        "price_decisions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("item_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=80), nullable=False),
        sa.Column("amount_per_unit", sa.Numeric(precision=38, scale=12), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("unit", sa.String(length=64), nullable=True),
        sa.Column("policy_version_id", sa.String(length=64), nullable=False),
        sa.Column("derived_observation_id", sa.String(length=64), nullable=True),
        sa.Column("supersedes_decision_id", sa.String(length=64), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["derived_observation_id"],
            ["evidence_observations.id"],
        ),
        sa.ForeignKeyConstraint(
            ["policy_version_id"],
            ["controlled_versions.id"],
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_price_decisions_project_id"),
        "price_decisions",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_price_decisions_status"),
        "price_decisions",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_price_decisions_is_current"),
        "price_decisions",
        ["is_current"],
        unique=False,
    )
    if op.get_bind().dialect.name == "postgresql":
        op.create_index(
            "uq_price_decisions_current_per_item",
            "price_decisions",
            ["project_id", "item_id"],
            unique=True,
            postgresql_where=sa.text("is_current"),
        )
    else:
        op.create_index(
            "uq_price_decisions_current_per_item",
            "price_decisions",
            ["project_id", "item_id"],
            unique=True,
            sqlite_where=sa.text("is_current = 1"),
        )
    op.create_table(
        "rfq_requests",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("item_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("price_decision_id", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["price_decision_id"],
            ["price_decisions.id"],
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_rfq_requests_project_id"),
        "rfq_requests",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_rfq_requests_item_id"),
        "rfq_requests",
        ["item_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_rfq_requests_status"),
        "rfq_requests",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_rfq_requests_status"), table_name="rfq_requests")
    op.drop_index(op.f("ix_rfq_requests_item_id"), table_name="rfq_requests")
    op.drop_index(op.f("ix_rfq_requests_project_id"), table_name="rfq_requests")
    op.drop_table("rfq_requests")
    op.drop_index(
        "uq_price_decisions_current_per_item",
        table_name="price_decisions",
    )
    op.drop_index(
        op.f("ix_price_decisions_is_current"),
        table_name="price_decisions",
    )
    op.drop_index(op.f("ix_price_decisions_status"), table_name="price_decisions")
    op.drop_index(op.f("ix_price_decisions_project_id"), table_name="price_decisions")
    op.drop_table("price_decisions")
    with op.batch_alter_table("price_quotes") as batch_op:
        batch_op.drop_index(op.f("ix_price_quotes_source_observation_id"))
        batch_op.drop_constraint(
            "fk_price_quotes_source_observation_id",
            type_="foreignkey",
        )
        batch_op.drop_column("source_observation_id")
    op.drop_index(
        "uq_nomenclature_matches_current_per_source",
        table_name="nomenclature_matches",
    )
    op.drop_index(
        op.f("ix_nomenclature_matches_is_current"),
        table_name="nomenclature_matches",
    )
    op.drop_index(
        op.f("ix_nomenclature_matches_status"),
        table_name="nomenclature_matches",
    )
    op.drop_column("nomenclature_matches", "is_current")
    op.drop_column("nomenclature_matches", "supersedes_match_id")
    op.drop_column("nomenclature_matches", "status")
