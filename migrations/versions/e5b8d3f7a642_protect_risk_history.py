"""protect risk item and calculation history

Revision ID: e5b8d3f7a642
Revises: d4a7c2e9f531
Create Date: 2026-07-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "e5b8d3f7a642"
down_revision: str | None = "d4a7c2e9f531"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE OR REPLACE FUNCTION tenderguard_protect_risk_item()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'risk item history cannot be deleted';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
                OR NEW.project_id IS DISTINCT FROM OLD.project_id
                OR NEW.risk_key IS DISTINCT FROM OLD.risk_key
                OR NEW.currency IS DISTINCT FROM OLD.currency
                OR NEW.expected_impact IS DISTINCT FROM OLD.expected_impact
                OR NEW.supersedes_risk_id IS DISTINCT FROM OLD.supersedes_risk_id
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
                OR NEW.updated_at <= OLD.updated_at
            THEN
                RAISE EXCEPTION 'risk item identity or timestamp is invalid';
            END IF;
            IF NOT OLD.is_current THEN
                RAISE EXCEPTION 'superseded risk item is immutable';
            END IF;
            IF OLD.is_current AND NOT NEW.is_current
                AND NEW.status IS NOT DISTINCT FROM OLD.status
                AND NEW.payload::jsonb = OLD.payload::jsonb
            THEN
                RETURN NEW;
            END IF;
            IF OLD.is_current AND NEW.is_current
                AND OLD.status = 'IN_REVIEW'
                AND NEW.status IN ('VERIFIED', 'REJECTED')
                AND NEW.payload::jsonb -> 'risk'
                    = jsonb_set(
                        OLD.payload::jsonb -> 'risk',
                        '{status}',
                        to_jsonb(NEW.status),
                        false
                    )
                AND (
                    NEW.payload::jsonb
                    - ARRAY[
                        'risk',
                        'verified_by',
                        'verified_at',
                        'reviewed_by',
                        'reviewed_at',
                        'review_decision'
                    ]
                ) = (OLD.payload::jsonb - 'risk')
                AND (
                    (
                        NEW.status = 'VERIFIED'
                        AND NEW.payload::jsonb ->> 'review_decision' = 'APPROVED'
                    )
                    OR (
                        NEW.status = 'REJECTED'
                        AND NEW.payload::jsonb ->> 'review_decision' = 'REJECTED'
                    )
                )
                AND jsonb_typeof(NEW.payload::jsonb -> 'reviewed_by') = 'string'
                AND jsonb_typeof(NEW.payload::jsonb -> 'reviewed_at') = 'string'
                AND (
                    NEW.status = 'REJECTED'
                    OR (
                        jsonb_typeof(NEW.payload::jsonb -> 'verified_by') = 'string'
                        AND jsonb_typeof(NEW.payload::jsonb -> 'verified_at') = 'string'
                    )
                )
            THEN
                RETURN NEW;
            END IF;
            IF OLD.is_current AND NEW.is_current
                AND NEW.status = 'IN_REVIEW'
                AND NEW.payload::jsonb -> 'risk'
                    = jsonb_set(
                        OLD.payload::jsonb -> 'risk',
                        '{status}',
                        '"IN_REVIEW"'::jsonb,
                        false
                    )
                AND (
                    NEW.payload::jsonb
                    - ARRAY[
                        'risk',
                        'invalidated_by_document_revision_id',
                        'invalidated_document_set_revision_id',
                        'invalidated_at'
                    ]
                ) = (OLD.payload::jsonb - 'risk')
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
            RAISE EXCEPTION 'invalid risk item transition';
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_risk_items_protect
        BEFORE UPDATE OR DELETE ON risk_items
        FOR EACH ROW EXECUTE FUNCTION tenderguard_protect_risk_item();

        CREATE OR REPLACE FUNCTION tenderguard_protect_risk_calculation()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'risk calculation history cannot be deleted';
            END IF;
            IF NOT OLD.is_current THEN
                RAISE EXCEPTION 'superseded risk calculation is immutable';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
                OR NEW.project_id IS DISTINCT FROM OLD.project_id
                OR NEW.policy_version_id IS DISTINCT FROM OLD.policy_version_id
                OR NEW.status IS DISTINCT FROM OLD.status
                OR NEW.expected_reserve IS DISTINCT FROM OLD.expected_reserve
                OR NEW.currency IS DISTINCT FROM OLD.currency
                OR NEW.unit IS DISTINCT FROM OLD.unit
                OR NEW.supersedes_calculation_id
                    IS DISTINCT FROM OLD.supersedes_calculation_id
                OR NEW.payload::jsonb <> OLD.payload::jsonb
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
                OR NEW.is_current
            THEN
                RAISE EXCEPTION 'invalid risk calculation transition';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_risk_calculations_protect
        BEFORE UPDATE OR DELETE ON risk_calculations
        FOR EACH ROW EXECUTE FUNCTION tenderguard_protect_risk_calculation();
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP TRIGGER IF EXISTS trg_risk_calculations_protect ON risk_calculations")
    op.execute("DROP FUNCTION IF EXISTS tenderguard_protect_risk_calculation()")
    op.execute("DROP TRIGGER IF EXISTS trg_risk_items_protect ON risk_items")
    op.execute("DROP FUNCTION IF EXISTS tenderguard_protect_risk_item()")
