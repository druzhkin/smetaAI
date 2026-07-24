from datetime import UTC, datetime, timedelta

from tenderguard.domain.audit import append_event, verify_chain

KEY = b"test-only-audit-signing-key"
KEY_ID = "audit-key-2026-01"


def test_audit_chain_detects_payload_tampering() -> None:
    first = append_event(
        previous=None,
        event_id="event-1",
        aggregate_type="project",
        aggregate_id="project-1",
        event_type="created",
        actor_id="user-1",
        actor_roles=("ESTIMATOR",),
        request_id="request-1",
        reason="New tender",
        occurred_at=datetime(2026, 7, 23, tzinfo=UTC),
        payload={"name": "Water network"},
        signing_key_id=KEY_ID,
        signing_key=KEY,
    )
    second = append_event(
        previous=first,
        event_id="event-2",
        aggregate_type="project",
        aggregate_id="project-1",
        event_type="state_changed",
        actor_id="user-1",
        actor_roles=("ESTIMATOR",),
        request_id="request-2",
        reason="Documents uploaded",
        occurred_at=datetime(2026, 7, 23, tzinfo=UTC) + timedelta(minutes=1),
        payload={"from": "DRAFT", "to": "EXTRACTION_IN_PROGRESS"},
        signing_key_id=KEY_ID,
        signing_key=KEY,
    )
    assert verify_chain([first, second], {KEY_ID: KEY})

    tampered = second.model_copy(update={"payload": {"from": "DRAFT", "to": "APPROVED_FOR_BID"}})
    assert not verify_chain([first, tampered], {KEY_ID: KEY})


def test_audit_chain_supports_key_rotation_without_resigning_history() -> None:
    old_key_id = "audit-key-2025-01"
    new_key_id = "audit-key-2026-01"
    old_key = b"old-test-only-audit-signing-key"
    new_key = b"new-test-only-audit-signing-key"
    first = append_event(
        previous=None,
        event_id="event-old",
        aggregate_type="project",
        aggregate_id="project-rotation",
        event_type="created",
        actor_id="user-1",
        actor_roles=("ESTIMATOR",),
        request_id="request-old",
        reason="New tender",
        occurred_at=datetime(2025, 7, 23, tzinfo=UTC),
        payload={"name": "Water network"},
        signing_key_id=old_key_id,
        signing_key=old_key,
    )
    second = append_event(
        previous=first,
        event_id="event-new",
        aggregate_type="project",
        aggregate_id="project-rotation",
        event_type="state_changed",
        actor_id="user-2",
        actor_roles=("ADMIN",),
        request_id="request-new",
        reason="Key rotation test",
        occurred_at=datetime(2026, 7, 23, tzinfo=UTC),
        payload={"from": "DRAFT", "to": "DOCUMENTS_INCOMPLETE"},
        signing_key_id=new_key_id,
        signing_key=new_key,
    )

    assert verify_chain(
        [first, second],
        {
            old_key_id: old_key,
            new_key_id: new_key,
        },
    )
    assert not verify_chain([first, second], {new_key_id: new_key})


def test_audit_chain_detects_signing_key_identifier_tampering() -> None:
    event = append_event(
        previous=None,
        event_id="event-key-id",
        aggregate_type="project",
        aggregate_id="project-key-id",
        event_type="created",
        actor_id="user-1",
        actor_roles=("ESTIMATOR",),
        request_id="request-key-id",
        reason="New tender",
        occurred_at=datetime(2026, 7, 23, tzinfo=UTC),
        payload={"name": "Water network"},
        signing_key_id=KEY_ID,
        signing_key=KEY,
    )

    tampered = event.model_copy(update={"signing_key_id": "audit-key-attacker"})
    assert not verify_chain(
        [tampered],
        {
            KEY_ID: KEY,
            "audit-key-attacker": KEY,
        },
    )
