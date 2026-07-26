"""protect controlled version lifecycle

Revision ID: e9a1b4c6d803
Revises: d8f0b3c5a792
Create Date: 2026-07-24
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "e9a1b4c6d803"
down_revision: str | None = "d8f0b3c5a792"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE FUNCTION tenderguard_protect_controlled_version()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.status = 'DRAFT'
                    AND NEW.approved_by IS NULL
                    AND NEW.approved_at IS NULL
                THEN
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION
                    'controlled version must be inserted as an unapproved draft';
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'controlled version cannot be deleted';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
                OR NEW.kind IS DISTINCT FROM OLD.kind
                OR NEW.version_label IS DISTINCT FROM OLD.version_label
                OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
                OR NEW.payload::jsonb IS DISTINCT FROM OLD.payload::jsonb
            THEN
                RAISE EXCEPTION 'controlled version basis is immutable';
            END IF;
            IF OLD.status = 'DRAFT'
                AND NEW.status = 'APPROVED'
                AND OLD.approved_by IS NULL
                AND OLD.approved_at IS NULL
                AND NEW.approved_by IS NOT NULL
                AND length(btrim(NEW.approved_by)) > 0
                AND NEW.approved_by = btrim(NEW.approved_by)
                AND NEW.approved_at IS NOT NULL
            THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'invalid controlled version lifecycle transition';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_controlled_version_lifecycle
        BEFORE INSERT OR UPDATE OR DELETE ON controlled_versions
        FOR EACH ROW
        EXECUTE FUNCTION tenderguard_protect_controlled_version();
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_controlled_version_lifecycle ON controlled_versions")
        op.execute("DROP FUNCTION IF EXISTS tenderguard_protect_controlled_version()")
