"""protect contract term history

Revision ID: d4a7c2e9f531
Revises: c8d3f6a1b247
Create Date: 2026-07-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d4a7c2e9f531"
down_revision: str | None = "c8d3f6a1b247"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE OR REPLACE FUNCTION tenderguard_protect_contract_term()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'contract term history cannot be deleted';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
                OR NEW.project_id IS DISTINCT FROM OLD.project_id
                OR NEW.kind IS DISTINCT FROM OLD.kind
                OR NEW.supersedes_term_id IS DISTINCT FROM OLD.supersedes_term_id
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
                OR NEW.updated_at <= OLD.updated_at
            THEN
                RAISE EXCEPTION 'contract term identity or timestamp is invalid';
            END IF;
            IF NOT OLD.is_current THEN
                RAISE EXCEPTION 'superseded contract term is immutable';
            END IF;
            IF NOT OLD.is_current AND NEW.is_current THEN
                RAISE EXCEPTION 'contract term cannot become current again';
            END IF;
            IF OLD.is_current AND NOT NEW.is_current
                AND NEW.verified IS NOT DISTINCT FROM OLD.verified
                AND NEW.cost_impact_resolved
                    IS NOT DISTINCT FROM OLD.cost_impact_resolved
                AND NEW.payload::jsonb = OLD.payload::jsonb
            THEN
                RETURN NEW;
            END IF;
            IF OLD.is_current AND NEW.is_current
                AND NOT OLD.verified
                AND NEW.cost_impact_resolved
                    IS NOT DISTINCT FROM OLD.cost_impact_resolved
                AND NEW.payload::jsonb @> OLD.payload::jsonb
                AND jsonb_typeof(
                    NEW.payload::jsonb -> 'review_decision'
                ) = 'string'
                AND jsonb_typeof(
                    NEW.payload::jsonb -> 'reviewed_by'
                ) = 'string'
                AND jsonb_typeof(
                    NEW.payload::jsonb -> 'reviewed_at'
                ) = 'string'
            THEN
                RETURN NEW;
            END IF;
            IF OLD.is_current AND NEW.is_current
                AND OLD.verified
                AND NEW.verified
                AND NOT OLD.cost_impact_resolved
                AND NEW.cost_impact_resolved
                AND NEW.payload::jsonb @> OLD.payload::jsonb
                AND jsonb_typeof(
                    NEW.payload::jsonb -> 'cost_impact'
                ) = 'object'
                AND jsonb_typeof(
                    NEW.payload::jsonb -> 'cost_impact_approval_id'
                ) = 'string'
                AND jsonb_typeof(
                    NEW.payload::jsonb -> 'cost_impact_finalized_at'
                ) = 'string'
            THEN
                RETURN NEW;
            END IF;
            IF OLD.is_current AND NEW.is_current
                AND NOT NEW.verified
                AND NOT NEW.cost_impact_resolved
                AND NEW.payload::jsonb @> OLD.payload::jsonb
                AND jsonb_typeof(
                    NEW.payload::jsonb
                        -> 'invalidated_by_document_revision_id'
                ) = 'string'
                AND jsonb_typeof(
                    NEW.payload::jsonb
                        -> 'invalidated_document_set_revision_id'
                ) = 'string'
                AND jsonb_typeof(
                    NEW.payload::jsonb -> 'invalidated_at'
                ) = 'string'
            THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'invalid contract term transition';
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_contract_terms_protect
        BEFORE UPDATE OR DELETE ON contract_terms
        FOR EACH ROW EXECUTE FUNCTION tenderguard_protect_contract_term();
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP TRIGGER IF EXISTS trg_contract_terms_protect ON contract_terms")
    op.execute("DROP FUNCTION IF EXISTS tenderguard_protect_contract_term()")
