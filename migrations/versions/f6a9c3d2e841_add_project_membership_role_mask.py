"""add project membership role mask

Revision ID: f6a9c3d2e841
Revises: e82f5d0b3c74
Create Date: 2026-07-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6a9c3d2e841"
down_revision: str | None = "e82f5d0b3c74"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE_BITS = {
    "ESTIMATOR": 1 << 0,
    "PROCUREMENT": 1 << 1,
    "TECHNICAL_EXPERT": 1 << 2,
    "REVIEWER": 1 << 3,
    "APPROVER": 1 << 4,
    "METHODOLOGY_OWNER": 1 << 5,
    "CATALOG_OWNER": 1 << 6,
    "AUDITOR": 1 << 7,
    "ADMIN": 1 << 8,
}


def _validated_backfill() -> list[dict[str, object]]:
    memberships = sa.table(
        "project_memberships",
        sa.column("id", sa.String()),
        sa.column("roles", sa.JSON()),
    )
    rows = op.get_bind().execute(sa.select(memberships.c.id, memberships.c.roles))
    backfill: list[dict[str, object]] = []
    for membership_id, raw_roles in rows:
        if (
            not isinstance(raw_roles, list)
            or not raw_roles
            or any(not isinstance(role, str) for role in raw_roles)
            or len(set(raw_roles)) != len(raw_roles)
            or any(role not in ROLE_BITS for role in raw_roles)
        ):
            raise RuntimeError(f"project membership {membership_id} has invalid role evidence")
        mask = 0
        for role in raw_roles:
            mask |= ROLE_BITS[role]
        backfill.append({"membership_id": membership_id, "role_mask": mask})
    return backfill


def _drop_postgresql_guard() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_project_membership_history_immutable ON project_memberships"
        )


def _install_postgresql_guard(*, with_role_mask: bool) -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    role_mask_declaration = "expected_role_mask integer;" if with_role_mask else ""
    role_validation = ""
    if with_role_mask:
        role_validation = """
                IF EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements_text(NEW.roles::jsonb)
                         AS roles(role_name)
                    WHERE role_name NOT IN (
                        'ESTIMATOR', 'PROCUREMENT', 'TECHNICAL_EXPERT', 'REVIEWER',
                        'APPROVER', 'METHODOLOGY_OWNER', 'CATALOG_OWNER', 'AUDITOR',
                        'ADMIN'
                    )
                ) OR jsonb_array_length(NEW.roles::jsonb) <> (
                    SELECT COUNT(DISTINCT role_name)
                    FROM jsonb_array_elements_text(NEW.roles::jsonb)
                         AS roles(role_name)
                ) THEN
                    RAISE EXCEPTION 'project membership roles are invalid';
                END IF;
                expected_role_mask :=
                    CASE WHEN NEW.roles::jsonb ? 'ESTIMATOR' THEN 1 ELSE 0 END
                  + CASE WHEN NEW.roles::jsonb ? 'PROCUREMENT' THEN 2 ELSE 0 END
                  + CASE WHEN NEW.roles::jsonb ? 'TECHNICAL_EXPERT' THEN 4 ELSE 0 END
                  + CASE WHEN NEW.roles::jsonb ? 'REVIEWER' THEN 8 ELSE 0 END
                  + CASE WHEN NEW.roles::jsonb ? 'APPROVER' THEN 16 ELSE 0 END
                  + CASE WHEN NEW.roles::jsonb ? 'METHODOLOGY_OWNER' THEN 32 ELSE 0 END
                  + CASE WHEN NEW.roles::jsonb ? 'CATALOG_OWNER' THEN 64 ELSE 0 END
                  + CASE WHEN NEW.roles::jsonb ? 'AUDITOR' THEN 128 ELSE 0 END
                  + CASE WHEN NEW.roles::jsonb ? 'ADMIN' THEN 256 ELSE 0 END;
                IF NEW.role_mask <> expected_role_mask THEN
                    RAISE EXCEPTION 'project membership role mask is invalid';
                END IF;
        """
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION tenderguard_protect_project_membership_history()
        RETURNS trigger AS $$
        DECLARE
            previous_id text;
            previous_version integer;
            {role_mask_declaration}
        BEGIN
            IF TG_OP = 'DELETE' OR TG_OP = 'UPDATE' THEN
                RAISE EXCEPTION 'project membership history is immutable';
            END IF;
            IF jsonb_array_length(NEW.roles::jsonb) = 0
               OR NEW.roles::jsonb ? 'SYSTEM' THEN
                RAISE EXCEPTION 'project membership roles are invalid';
            END IF;
            {role_validation}
            SELECT id, version
            INTO previous_id, previous_version
            FROM project_memberships
            WHERE project_id = NEW.project_id
              AND principal_id = NEW.principal_id
            ORDER BY version DESC
            LIMIT 1;
            IF previous_id IS NULL THEN
                IF NEW.version <> 1 OR NEW.supersedes_membership_id IS NOT NULL THEN
                    RAISE EXCEPTION 'initial project membership revision is invalid';
                END IF;
            ELSIF NEW.version <> previous_version + 1
                  OR NEW.supersedes_membership_id <> previous_id THEN
                RAISE EXCEPTION 'project membership revision chain is invalid';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_project_membership_history_immutable
        BEFORE INSERT OR UPDATE OR DELETE ON project_memberships
        FOR EACH ROW EXECUTE FUNCTION tenderguard_protect_project_membership_history();
        """
    )


def upgrade() -> None:
    backfill = _validated_backfill()
    _drop_postgresql_guard()
    with op.batch_alter_table("project_memberships") as batch_op:
        batch_op.add_column(sa.Column("role_mask", sa.Integer(), nullable=True))
    if backfill:
        op.get_bind().execute(
            sa.text(
                "UPDATE project_memberships SET role_mask = :role_mask WHERE id = :membership_id"
            ),
            backfill,
        )
    with op.batch_alter_table("project_memberships") as batch_op:
        batch_op.alter_column(
            "role_mask",
            existing_type=sa.Integer(),
            nullable=False,
        )
        batch_op.create_check_constraint(
            "ck_project_membership_role_mask",
            "role_mask > 0 AND role_mask <= 511",
        )
    op.create_index(
        "ix_project_membership_principal_current",
        "project_memberships",
        ["principal_id", "project_id", "version"],
    )
    _install_postgresql_guard(with_role_mask=True)


def downgrade() -> None:
    _drop_postgresql_guard()
    op.drop_index(
        "ix_project_membership_principal_current",
        table_name="project_memberships",
    )
    with op.batch_alter_table("project_memberships") as batch_op:
        batch_op.drop_constraint(
            "ck_project_membership_role_mask",
            type_="check",
        )
        batch_op.drop_column("role_mask")
    _install_postgresql_guard(with_role_mask=False)
