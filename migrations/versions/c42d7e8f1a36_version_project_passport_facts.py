"""version project passport facts

Revision ID: c42d7e8f1a36
Revises: a14c8f9d3b20
Create Date: 2026-07-23 16:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c42d7e8f1a36"
down_revision: str | None = "a14c8f9d3b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    naming_convention = {
        "uq": "uq_%(table_name)s_%(column_0_name)s_%(column_1_name)s",
    }
    unique_constraints = sa.inspect(op.get_bind()).get_unique_constraints("project_passport_facts")
    field_constraint = next(
        item for item in unique_constraints if item["column_names"] == ["project_id", "field_name"]
    )
    constraint_name = field_constraint["name"] or "uq_project_passport_facts_project_id_field_name"
    with op.batch_alter_table(
        "project_passport_facts",
        naming_convention=naming_convention,
    ) as batch_op:
        batch_op.drop_constraint(
            constraint_name,
            type_="unique",
        )
        batch_op.add_column(
            sa.Column("supersedes_fact_id", sa.String(length=64), nullable=True),
        )
        batch_op.add_column(
            sa.Column(
                "is_current",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
        )
    op.create_index(
        op.f("ix_project_passport_facts_is_current"),
        "project_passport_facts",
        ["is_current"],
        unique=False,
    )
    if op.get_bind().dialect.name == "postgresql":
        op.create_index(
            "uq_passport_facts_current_per_field",
            "project_passport_facts",
            ["project_id", "field_name"],
            unique=True,
            postgresql_where=sa.text("is_current"),
        )
    else:
        op.create_index(
            "uq_passport_facts_current_per_field",
            "project_passport_facts",
            ["project_id", "field_name"],
            unique=True,
            sqlite_where=sa.text("is_current = 1"),
        )


def downgrade() -> None:
    op.drop_index(
        "uq_passport_facts_current_per_field",
        table_name="project_passport_facts",
    )
    op.drop_index(
        op.f("ix_project_passport_facts_is_current"),
        table_name="project_passport_facts",
    )
    with op.batch_alter_table(
        "project_passport_facts",
        naming_convention={
            "uq": "uq_%(table_name)s_%(column_0_name)s_%(column_1_name)s",
        },
    ) as batch_op:
        batch_op.drop_column("is_current")
        batch_op.drop_column("supersedes_fact_id")
        batch_op.create_unique_constraint(
            "uq_project_passport_facts_project_id_field_name",
            ["project_id", "field_name"],
        )
