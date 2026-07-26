"""add distributed rate limit buckets

Revision ID: fa2c5d7e9014
Revises: e9a1b4c6d803
Create Date: 2026-07-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "fa2c5d7e9014"
down_revision: str | None = "e9a1b4c6d803"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rate_limit_buckets",
        sa.Column("scope", sa.String(length=50), nullable=False),
        sa.Column("identity_hash", sa.String(length=64), nullable=False),
        sa.Column("policy_hash", sa.String(length=64), nullable=False),
        sa.Column("window_number", sa.BigInteger(), nullable=False),
        sa.Column("request_count", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "scope IN ("
            "'ACTOR_READ', 'ORGANIZATION_READ', "
            "'ACTOR_MUTATION', 'ORGANIZATION_MUTATION', "
            "'ACTOR_UPLOAD', 'ORGANIZATION_UPLOAD'"
            ")",
            name="ck_rate_limit_bucket_scope",
        ),
        sa.CheckConstraint(
            "request_count >= 1",
            name="ck_rate_limit_bucket_count",
        ),
        sa.PrimaryKeyConstraint("scope", "identity_hash"),
    )
    op.create_index(
        "ix_rate_limit_buckets_updated_at",
        "rate_limit_buckets",
        ["updated_at"],
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE FUNCTION tenderguard_protect_rate_limit_bucket()
            RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'rate-limit bucket cannot be deleted';
                END IF;
                IF NEW.scope IS DISTINCT FROM OLD.scope
                    OR NEW.identity_hash IS DISTINCT FROM OLD.identity_hash
                THEN
                    RAISE EXCEPTION 'rate-limit bucket identity is immutable';
                END IF;
                IF NEW.window_number = OLD.window_number
                    AND NEW.policy_hash IS NOT DISTINCT FROM OLD.policy_hash
                    AND NEW.request_count = OLD.request_count + 1
                    AND NEW.updated_at >= OLD.updated_at
                THEN
                    RETURN NEW;
                END IF;
                IF NEW.window_number > OLD.window_number
                    AND NEW.request_count = 1
                    AND NEW.updated_at >= OLD.updated_at
                THEN
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION 'invalid rate-limit bucket transition';
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_rate_limit_bucket_protected
            BEFORE UPDATE OR DELETE ON rate_limit_buckets
            FOR EACH ROW
            EXECUTE FUNCTION tenderguard_protect_rate_limit_bucket();
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_rate_limit_bucket_protected ON rate_limit_buckets")
        op.execute("DROP FUNCTION IF EXISTS tenderguard_protect_rate_limit_bucket()")
    op.drop_index(
        "ix_rate_limit_buckets_updated_at",
        table_name="rate_limit_buckets",
    )
    op.drop_table("rate_limit_buckets")
