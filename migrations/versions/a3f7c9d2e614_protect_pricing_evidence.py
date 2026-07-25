"""protect pricing evidence

Revision ID: a3f7c9d2e614
Revises: b8d2e7f4a961
Create Date: 2026-07-25
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "a3f7c9d2e614"
down_revision: str | None = "b8d2e7f4a961"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _install_postgresql_guards() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE OR REPLACE FUNCTION tenderguard_protect_price_quote()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'price quote evidence cannot be deleted';
            END IF;
            IF OLD.status = 'UNNORMALIZED'
               AND NEW.status = 'NORMALIZED'
               AND OLD.id IS NOT DISTINCT FROM NEW.id
               AND OLD.project_id IS NOT DISTINCT FROM NEW.project_id
               AND OLD.item_id IS NOT DISTINCT FROM NEW.item_id
               AND OLD.quote_date IS NOT DISTINCT FROM NEW.quote_date
               AND OLD.valid_until IS NOT DISTINCT FROM NEW.valid_until
               AND OLD.amount IS NOT DISTINCT FROM NEW.amount
               AND OLD.currency IS NOT DISTINCT FROM NEW.currency
               AND OLD.source_observation_id IS NOT DISTINCT FROM NEW.source_observation_id
               AND OLD.payload::jsonb IS NOT DISTINCT FROM NEW.payload::jsonb
               AND OLD.created_at IS NOT DISTINCT FROM NEW.created_at THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'price quote inputs are immutable';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION tenderguard_protect_price_decision()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'price decision evidence cannot be deleted';
            END IF;
            IF OLD.is_current
               AND NOT NEW.is_current
               AND OLD.id IS NOT DISTINCT FROM NEW.id
               AND OLD.project_id IS NOT DISTINCT FROM NEW.project_id
               AND OLD.item_id IS NOT DISTINCT FROM NEW.item_id
               AND OLD.status IS NOT DISTINCT FROM NEW.status
               AND OLD.amount_per_unit IS NOT DISTINCT FROM NEW.amount_per_unit
               AND OLD.currency IS NOT DISTINCT FROM NEW.currency
               AND OLD.unit IS NOT DISTINCT FROM NEW.unit
               AND OLD.policy_version_id IS NOT DISTINCT FROM NEW.policy_version_id
               AND OLD.derived_observation_id IS NOT DISTINCT FROM NEW.derived_observation_id
               AND OLD.supersedes_decision_id IS NOT DISTINCT FROM NEW.supersedes_decision_id
               AND OLD.payload::jsonb IS NOT DISTINCT FROM NEW.payload::jsonb
               AND OLD.created_at IS NOT DISTINCT FROM NEW.created_at THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'price decision inputs and outputs are immutable';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_price_quotes_protected
        BEFORE UPDATE OR DELETE ON price_quotes
        FOR EACH ROW EXECUTE FUNCTION tenderguard_protect_price_quote();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_normalized_prices_immutable
        BEFORE UPDATE OR DELETE ON normalized_prices
        FOR EACH ROW EXECUTE FUNCTION tenderguard_reject_immutable_mutation();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_price_decisions_protected
        BEFORE UPDATE OR DELETE ON price_decisions
        FOR EACH ROW EXECUTE FUNCTION tenderguard_protect_price_decision();
        """
    )


def upgrade() -> None:
    _install_postgresql_guards()


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP TRIGGER IF EXISTS trg_price_decisions_protected ON price_decisions")
    op.execute("DROP TRIGGER IF EXISTS trg_normalized_prices_immutable ON normalized_prices")
    op.execute("DROP TRIGGER IF EXISTS trg_price_quotes_protected ON price_quotes")
    op.execute("DROP FUNCTION IF EXISTS tenderguard_protect_price_decision()")
    op.execute("DROP FUNCTION IF EXISTS tenderguard_protect_price_quote()")
