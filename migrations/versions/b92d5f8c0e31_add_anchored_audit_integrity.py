"""add anchored audit integrity

Revision ID: b92d5f8c0e31
Revises: a81c4e7d9b20
Create Date: 2026-07-24
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from alembic import op
from pydantic import ValidationError
from sqlalchemy.engine import RowMapping

from tenderguard.config import get_settings
from tenderguard.domain.audit import AUDIT_SIGNATURE_V1, AuditEvent, verify_chain
from tenderguard.domain.common import ensure_utc

revision: str = "b92d5f8c0e31"
down_revision: str | None = "a81c4e7d9b20"
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


def _verified_legacy_audit_key_id() -> str:
    connection = op.get_bind()
    rows = (
        connection.execute(
            sa.text(
                "SELECT id, aggregate_type, aggregate_id, sequence, event_type, actor_id, "
                "actor_roles, request_id, reason, payload, previous_hash, event_hash, "
                "signature, occurred_at "
                "FROM audit_events "
                "ORDER BY aggregate_type, aggregate_id, sequence"
            )
        )
        .mappings()
        .all()
    )
    settings = get_settings()
    key_id = settings.audit_signing_key_id
    signing_key = settings.audit_signing_key.get_secret_value().encode("utf-8")
    chains: dict[tuple[str, str], list[RowMapping]] = {}
    for row in rows:
        chains.setdefault(
            (str(row["aggregate_type"]), str(row["aggregate_id"])),
            [],
        ).append(row)
    for (aggregate_type, aggregate_id), chain_rows in chains.items():
        try:
            events = [
                AuditEvent(
                    sequence=int(row["sequence"]),
                    event_id=str(row["id"]),
                    aggregate_type=aggregate_type,
                    aggregate_id=aggregate_id,
                    event_type=str(row["event_type"]),
                    actor_id=str(row["actor_id"]),
                    actor_roles=tuple(_json_list(row["actor_roles"], "actor_roles")),
                    request_id=str(row["request_id"]),
                    reason=str(row["reason"]),
                    occurred_at=_legacy_datetime(row["occurred_at"]),
                    payload=_json_dict(row["payload"], "payload"),
                    previous_hash=str(row["previous_hash"]),
                    signing_key_id=key_id,
                    signature_version=AUDIT_SIGNATURE_V1,
                    event_hash=str(row["event_hash"]),
                    signature=str(row["signature"]),
                )
                for row in chain_rows
            ]
        except (TypeError, ValueError, ValidationError) as error:
            raise RuntimeError(
                "Cannot version audit keys safely: legacy audit data is invalid"
            ) from error
        if not verify_chain(events, {key_id: signing_key}):
            raise RuntimeError(
                "Cannot version audit keys safely: every legacy audit chain must "
                "verify with the configured historical signing key"
            )
    return key_id


def _require_empty_audit_integrity_history() -> None:
    connection = op.get_bind()
    audit_count = connection.execute(sa.text("SELECT COUNT(*) FROM audit_events")).scalar_one()
    checkpoint_count = connection.execute(
        sa.text("SELECT COUNT(*) FROM audit_checkpoints")
    ).scalar_one()
    receipt_count = connection.execute(
        sa.text("SELECT COUNT(*) FROM audit_anchor_receipts")
    ).scalar_one()
    if audit_count or checkpoint_count or receipt_count:
        raise RuntimeError("Cannot downgrade anchored audit integrity after audit evidence exists")


def _drop_audit_immutability_trigger() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_audit_events_immutable ON audit_events")


def _create_audit_immutability_trigger() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE TRIGGER trg_audit_events_immutable
            BEFORE UPDATE OR DELETE ON audit_events
            FOR EACH ROW EXECUTE FUNCTION tenderguard_reject_immutable_mutation();
            """
        )


def upgrade() -> None:
    legacy_key_id = _verified_legacy_audit_key_id()
    _drop_audit_immutability_trigger()
    with op.batch_alter_table("audit_events") as batch_op:
        batch_op.add_column(sa.Column("signing_key_id", sa.String(length=200)))
        batch_op.add_column(sa.Column("signature_version", sa.String(length=32)))
    op.execute(
        sa.text(
            "UPDATE audit_events "
            "SET signing_key_id = :key_id, signature_version = :signature_version"
        ).bindparams(
            key_id=legacy_key_id,
            signature_version=AUDIT_SIGNATURE_V1,
        )
    )
    with op.batch_alter_table("audit_events") as batch_op:
        batch_op.alter_column(
            "signing_key_id",
            existing_type=sa.String(length=200),
            nullable=False,
        )
        batch_op.alter_column(
            "signature_version",
            existing_type=sa.String(length=32),
            nullable=False,
        )
    _create_audit_immutability_trigger()

    op.create_table(
        "audit_checkpoints",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("schema_version", sa.String(length=100), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("terminal_count", sa.Integer(), nullable=False),
        sa.Column("checkpoint_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("object_hash", sa.String(length=64), nullable=False),
        sa.Column("object_key", sa.String(length=1000), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "event_count >= 1",
            name="ck_audit_checkpoint_event_count",
        ),
        sa.CheckConstraint(
            "terminal_count >= 1",
            name="ck_audit_checkpoint_terminal_count",
        ),
    )
    op.create_index(
        "ix_audit_checkpoints_created_at",
        "audit_checkpoints",
        ["created_at"],
    )
    op.create_table(
        "audit_anchor_receipts",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "checkpoint_id",
            sa.String(length=64),
            sa.ForeignKey("audit_checkpoints.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("provider_id", sa.String(length=200), nullable=False),
        sa.Column("provider_key_id", sa.String(length=200), nullable=False),
        sa.Column("external_reference", sa.String(length=500), nullable=False),
        sa.Column("anchored_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("signature_b64", sa.String(length=200), nullable=False),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("registered_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_audit_anchor_receipts_anchored_at",
        "audit_anchor_receipts",
        ["anchored_at"],
    )
    if op.get_bind().dialect.name == "postgresql":
        for table_name in ("audit_checkpoints", "audit_anchor_receipts"):
            op.execute(
                f"""
                CREATE TRIGGER trg_{table_name}_immutable
                BEFORE UPDATE OR DELETE ON {table_name}
                FOR EACH ROW EXECUTE FUNCTION tenderguard_reject_immutable_mutation();
                """
            )


def downgrade() -> None:
    _require_empty_audit_integrity_history()
    if op.get_bind().dialect.name == "postgresql":
        for table_name in ("audit_anchor_receipts", "audit_checkpoints"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_immutable ON {table_name}")
    op.drop_index(
        "ix_audit_anchor_receipts_anchored_at",
        table_name="audit_anchor_receipts",
    )
    op.drop_table("audit_anchor_receipts")
    op.drop_index(
        "ix_audit_checkpoints_created_at",
        table_name="audit_checkpoints",
    )
    op.drop_table("audit_checkpoints")
    _drop_audit_immutability_trigger()
    with op.batch_alter_table("audit_events") as batch_op:
        batch_op.drop_column("signature_version")
        batch_op.drop_column("signing_key_id")
    _create_audit_immutability_trigger()
