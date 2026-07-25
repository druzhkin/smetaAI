"""protect calculation and release evidence

Revision ID: b4d8e1f6c205
Revises: a3f7c9d2e614
Create Date: 2026-07-25
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b4d8e1f6c205"
down_revision: str | None = "a3f7c9d2e614"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_PROTECTED_TABLES = (
    ("calculation_runs", "trg_calculation_runs_immutable"),
    ("atomic_cost_inputs", "trg_atomic_cost_inputs_immutable"),
    ("release_decisions", "trg_release_decisions_immutable"),
    ("scenario_runs", "trg_scenario_runs_immutable"),
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table_name, trigger_name in _PROTECTED_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER {trigger_name}
            BEFORE UPDATE OR DELETE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION tenderguard_reject_immutable_mutation();
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table_name, trigger_name in reversed(_PROTECTED_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table_name}")
