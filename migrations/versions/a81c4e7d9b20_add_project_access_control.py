"""add project access control

Revision ID: a81c4e7d9b20
Revises: f42d8a1b6c53
Create Date: 2026-07-24
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from hashlib import sha256
from typing import Any

import sqlalchemy as sa
from alembic import op
from pydantic import ValidationError
from sqlalchemy.engine import RowMapping

from tenderguard.config import get_settings
from tenderguard.domain.audit import AUDIT_SIGNATURE_V1, AuditEvent, verify_chain
from tenderguard.domain.common import ensure_utc
from tenderguard.domain.enums import ActorRole

revision: str = "a81c4e7d9b20"
down_revision: str | None = "f42d8a1b6c53"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_list(value: Any, field: str) -> list[Any]:
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, list):
        raise ValueError(f"Legacy audit {field} must be a JSON list")
    return decoded


def _json_dict(value: Any, field: str) -> dict[str, Any]:
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, dict):
        raise ValueError(f"Legacy audit {field} must be a JSON object")
    return decoded


def _legacy_datetime(value: Any) -> datetime:
    parsed = (
        datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    )
    if not isinstance(parsed, datetime):
        raise ValueError("Legacy audit occurred_at must be a timestamp")
    normalized = ensure_utc(parsed)
    if normalized is None:
        raise ValueError("Legacy audit occurred_at is missing")
    return normalized


def _verified_creator_events() -> list[dict[str, Any]]:
    connection = op.get_bind()
    projects = connection.execute(sa.text("SELECT id FROM projects ORDER BY id")).mappings().all()
    audit_rows = (
        connection.execute(
            sa.text(
                "SELECT id, aggregate_id, sequence, event_type, actor_id, actor_roles, "
                "request_id, reason, payload, previous_hash, event_hash, signature, occurred_at "
                "FROM audit_events "
                "WHERE aggregate_type = 'project' "
                "ORDER BY aggregate_id, sequence"
            )
        )
        .mappings()
        .all()
    )
    by_project: dict[str, list[RowMapping]] = {}
    for event in audit_rows:
        by_project.setdefault(str(event["aggregate_id"]), []).append(event)
    settings = get_settings()
    signing_key_id = settings.audit_signing_key_id
    signing_key = settings.audit_signing_key.get_secret_value().encode("utf-8")
    verified: list[dict[str, Any]] = []
    for project in projects:
        project_id = str(project["id"])
        rows = by_project.get(project_id, [])
        try:
            events = [
                AuditEvent(
                    sequence=int(row["sequence"]),
                    event_id=str(row["id"]),
                    aggregate_type="project",
                    aggregate_id=project_id,
                    event_type=str(row["event_type"]),
                    actor_id=str(row["actor_id"]),
                    actor_roles=tuple(_json_list(row["actor_roles"], "actor_roles")),
                    request_id=str(row["request_id"]),
                    reason=str(row["reason"]),
                    occurred_at=_legacy_datetime(row["occurred_at"]),
                    payload=_json_dict(row["payload"], "payload"),
                    previous_hash=str(row["previous_hash"]),
                    signing_key_id=signing_key_id,
                    signature_version=AUDIT_SIGNATURE_V1,
                    event_hash=str(row["event_hash"]),
                    signature=str(row["signature"]),
                )
                for row in rows
            ]
        except (TypeError, ValueError, ValidationError) as error:
            raise RuntimeError(
                "Cannot infer project ACL safely: project audit data is invalid"
            ) from error
        if not events or not verify_chain(events, {signing_key_id: signing_key}):
            raise RuntimeError(
                "Cannot infer project ACL safely: the complete project audit chain "
                "does not verify with the configured audit key"
            )
        creators = [event for event in events if event.event_type == "project_created"]
        if len(creators) != 1:
            raise RuntimeError(
                "Cannot infer project ACL safely: every existing project must have "
                "exactly one verified project_created audit event"
            )
        event = creators[0]
        raw_roles = event.actor_roles
        valid_human_roles = {role.value for role in ActorRole if role is not ActorRole.SYSTEM}
        roles = sorted(
            {
                role.strip().upper()
                for role in raw_roles
                if isinstance(role, str) and role.strip().upper() in valid_human_roles
            }
        )
        invalid_roles = sorted(
            {
                str(role)
                for role in raw_roles
                if not isinstance(role, str) or role.strip().upper() not in valid_human_roles
            }
        )
        if invalid_roles:
            raise RuntimeError(
                "Cannot infer project ACL: creator audit contains invalid or "
                f"non-human roles: {invalid_roles}"
            )
        if not roles:
            raise RuntimeError("Cannot infer project ACL: creator has no human role evidence")
        event_id = event.event_id
        digest = sha256(f"project-acl:{project_id}:{event_id}".encode()).hexdigest()[:24]
        verified.append(
            {
                "id": f"membership-{digest}",
                "project_id": project_id,
                "principal_id": event.actor_id,
                "roles": roles,
                "access_level": "OWNER",
                "status": "ACTIVE",
                "version": 1,
                "supersedes_membership_id": None,
                "changed_by": event.actor_id,
                "reason": f"Backfilled from project_created audit event {event_id}",
                "created_at": event.occurred_at,
            }
        )
    return verified


def _require_no_membership_history() -> None:
    count = op.get_bind().execute(sa.text("SELECT COUNT(*) FROM project_memberships")).scalar_one()
    if count:
        raise RuntimeError(
            "Cannot downgrade after project ACLs exist; downgrade would restore "
            "organisation-wide project access"
        )


def upgrade() -> None:
    bootstrap_memberships = _verified_creator_events()
    op.create_table(
        "project_memberships",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=64),
            sa.ForeignKey("projects.id"),
            nullable=False,
        ),
        sa.Column("principal_id", sa.String(length=128), nullable=False),
        sa.Column("roles", sa.JSON(), nullable=False),
        sa.Column("access_level", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "supersedes_membership_id",
            sa.String(length=64),
            sa.ForeignKey("project_memberships.id"),
        ),
        sa.Column("changed_by", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_project_membership_version_positive",
        ),
        sa.CheckConstraint(
            "access_level IN ('MEMBER', 'OWNER')",
            name="ck_project_membership_access_level",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'REVOKED')",
            name="ck_project_membership_status",
        ),
        sa.UniqueConstraint(
            "project_id",
            "principal_id",
            "version",
            name="uq_project_membership_version",
        ),
        sa.UniqueConstraint(
            "supersedes_membership_id",
            name="uq_project_membership_supersedes",
        ),
    )
    op.create_index(
        "ix_project_memberships_project_id",
        "project_memberships",
        ["project_id"],
    )
    op.create_index(
        "ix_project_memberships_principal_id",
        "project_memberships",
        ["principal_id"],
    )
    op.create_index(
        "ix_project_membership_current_lookup",
        "project_memberships",
        ["project_id", "principal_id", "version"],
    )
    membership_table = sa.table(
        "project_memberships",
        sa.column("id", sa.String()),
        sa.column("project_id", sa.String()),
        sa.column("principal_id", sa.String()),
        sa.column("roles", sa.JSON()),
        sa.column("access_level", sa.String()),
        sa.column("status", sa.String()),
        sa.column("version", sa.Integer()),
        sa.column("supersedes_membership_id", sa.String()),
        sa.column("changed_by", sa.String()),
        sa.column("reason", sa.Text()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    if bootstrap_memberships:
        op.bulk_insert(membership_table, bootstrap_memberships)
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE FUNCTION tenderguard_protect_project_membership_history()
            RETURNS trigger AS $$
            DECLARE
                previous_id text;
                previous_version integer;
            BEGIN
                IF TG_OP = 'DELETE' OR TG_OP = 'UPDATE' THEN
                    RAISE EXCEPTION 'project membership history is immutable';
                END IF;
                IF jsonb_array_length(NEW.roles::jsonb) = 0
                   OR NEW.roles::jsonb ? 'SYSTEM' THEN
                    RAISE EXCEPTION 'project membership roles are invalid';
                END IF;
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


def downgrade() -> None:
    _require_no_membership_history()
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_project_membership_history_immutable ON project_memberships"
        )
        op.execute("DROP FUNCTION IF EXISTS tenderguard_protect_project_membership_history()")
    op.drop_index(
        "ix_project_membership_current_lookup",
        table_name="project_memberships",
    )
    op.drop_index(
        "ix_project_memberships_principal_id",
        table_name="project_memberships",
    )
    op.drop_index(
        "ix_project_memberships_project_id",
        table_name="project_memberships",
    )
    op.drop_table("project_memberships")
