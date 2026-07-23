from datetime import UTC, datetime, timedelta

from tenderguard.domain.audit import append_event, verify_chain

KEY = b"test-only-audit-signing-key"


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
        signing_key=KEY,
    )
    assert verify_chain([first, second], KEY)

    tampered = second.model_copy(update={"payload": {"from": "DRAFT", "to": "APPROVED_FOR_BID"}})
    assert not verify_chain([first, tampered], KEY)
