"""version contract terms

Revision ID: f15a3d9b7e64
Revises: e94f2c8a6d53
Create Date: 2026-07-23 19:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f15a3d9b7e64"
down_revision: str | None = "e94f2c8a6d53"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    naming_convention = {
        "uq": "uq_%(table_name)s_%(column_0_name)s_%(column_1_name)s",
    }
    unique_constraints = sa.inspect(op.get_bind()).get_unique_constraints("contract_terms")
    kind_constraint = next(
        item for item in unique_constraints if item["column_names"] == ["project_id", "kind"]
    )
    constraint_name = kind_constraint["name"] or "uq_contract_terms_project_id_kind"
    with op.batch_alter_table(
        "contract_terms",
        naming_convention=naming_convention,
    ) as batch_op:
        batch_op.drop_constraint(constraint_name, type_="unique")
        batch_op.add_column(
            sa.Column("supersedes_term_id", sa.String(length=64), nullable=True),
        )
        batch_op.add_column(
            sa.Column(
                "is_current",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
    op.execute(
        """
        UPDATE contract_terms
        SET is_current = TRUE
        WHERE id IN (
            SELECT id
            FROM (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY project_id, kind
                        ORDER BY updated_at DESC, created_at DESC, id DESC
                    ) AS revision_rank
                FROM contract_terms
            ) ranked
            WHERE revision_rank = 1
        )
        """
    )
    op.create_index(
        op.f("ix_contract_terms_is_current"),
        "contract_terms",
        ["is_current"],
        unique=False,
    )
    if op.get_bind().dialect.name == "postgresql":
        op.create_index(
            "uq_contract_terms_current_per_kind",
            "contract_terms",
            ["project_id", "kind"],
            unique=True,
            postgresql_where=sa.text("is_current"),
        )
    else:
        op.create_index(
            "uq_contract_terms_current_per_kind",
            "contract_terms",
            ["project_id", "kind"],
            unique=True,
            sqlite_where=sa.text("is_current = 1"),
        )


def downgrade() -> None:
    op.drop_index(
        "uq_contract_terms_current_per_kind",
        table_name="contract_terms",
    )
    op.drop_index(
        op.f("ix_contract_terms_is_current"),
        table_name="contract_terms",
    )
    with op.batch_alter_table(
        "contract_terms",
        naming_convention={
            "uq": "uq_%(table_name)s_%(column_0_name)s_%(column_1_name)s",
        },
    ) as batch_op:
        batch_op.drop_column("is_current")
        batch_op.drop_column("supersedes_term_id")
        batch_op.create_unique_constraint(
            "uq_contract_terms_project_id_kind",
            ["project_id", "kind"],
        )
