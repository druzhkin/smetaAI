"""allow explicit approval task supersession

Revision ID: c8d3f6a1b247
Revises: fb7d2a9c4e16
Create Date: 2026-07-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c8d3f6a1b247"
down_revision: str | None = "fb7d2a9c4e16"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _replace_protection_function(*, allow_entity_supersession: bool) -> None:
    entity_supersession = (
        """
                OR (
                    jsonb_typeof(NEW.payload::jsonb -> 'superseded_at') = 'string'
                    AND jsonb_typeof(
                        NEW.payload::jsonb -> 'superseded_by_entity_id'
                    ) = 'string'
                    AND jsonb_typeof(
                        NEW.payload::jsonb -> 'supersession_reason'
                    ) = 'string'
                )
    """
        if allow_entity_supersession
        else ""
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION tenderguard_protect_approval_task()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'approval task cannot be deleted';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
                OR NEW.project_id IS DISTINCT FROM OLD.project_id
                OR NEW.task_type IS DISTINCT FROM OLD.task_type
                OR NEW.entity_type IS DISTINCT FROM OLD.entity_type
                OR NEW.entity_id IS DISTINCT FROM OLD.entity_id
                OR NEW.assigned_role IS DISTINCT FROM OLD.assigned_role
                OR NEW.required IS DISTINCT FROM OLD.required
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
                OR NEW.updated_at <= OLD.updated_at
            THEN
                RAISE EXCEPTION 'approval task identity or timestamp is invalid';
            END IF;
            IF OLD.status = 'PENDING'
                AND NEW.status IN ('APPROVED', 'REJECTED', 'CHANGES_REQUESTED')
                AND NEW.payload::jsonb = OLD.payload::jsonb
            THEN
                RETURN NEW;
            END IF;
            IF OLD.status <> 'SUPERSEDED'
                AND NEW.status = 'SUPERSEDED'
                AND NEW.payload::jsonb @> OLD.payload::jsonb
                AND (
                    (
                        jsonb_typeof(
                            NEW.payload::jsonb -> 'invalidated_at'
                        ) = 'string'
                        AND jsonb_typeof(
                            NEW.payload::jsonb
                                -> 'invalidated_by_document_revision_id'
                        ) = 'string'
                        AND jsonb_typeof(
                            NEW.payload::jsonb
                                -> 'invalidated_document_set_revision_id'
                        ) = 'string'
                    )
                    {entity_supersession}
                )
            THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'invalid approval task transition';
        END;
        $$ LANGUAGE plpgsql;
        """
    )


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        _replace_protection_function(allow_entity_supersession=True)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        _replace_protection_function(allow_entity_supersession=False)
