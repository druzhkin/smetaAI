from datetime import UTC, datetime

import pytest

from tenderguard.domain.enums import ApprovalState
from tenderguard.domain.models import WorkflowTransition
from tenderguard.domain.workflow import can_transition, validate_transition


def test_direct_bid_approval_from_draft_is_forbidden() -> None:
    assert not can_transition(ApprovalState.DRAFT, ApprovalState.APPROVED_FOR_BID)


def test_blocked_project_cannot_resume_directly_to_approval() -> None:
    assert not can_transition(ApprovalState.BLOCKED, ApprovalState.APPROVED_FOR_BID)
    assert can_transition(ApprovalState.BLOCKED, ApprovalState.EXTRACTION_REVIEW)


def test_transition_validation_records_formal_path() -> None:
    transition = WorkflowTransition(
        project_id="project-1",
        from_state=ApprovalState.EXPERT_REVIEW,
        to_state=ApprovalState.APPROVED_FOR_BID,
        actor_id="approver-1",
        reason="All release gates passed",
        occurred_at=datetime(2026, 7, 23, tzinfo=UTC),
    )
    validate_transition(transition)

    with pytest.raises(ValueError, match="Illegal workflow transition"):
        validate_transition(
            transition.model_copy(
                update={
                    "from_state": ApprovalState.DRAFT,
                    "to_state": ApprovalState.APPROVED_FOR_BID,
                }
            )
        )


def test_final_expert_can_return_to_automatic_processing() -> None:
    for target in (
        ApprovalState.EXTRACTION_IN_PROGRESS,
        ApprovalState.BOQ_IN_PROGRESS,
        ApprovalState.PRICING_IN_PROGRESS,
        ApprovalState.CALCULATION_IN_PROGRESS,
    ):
        validate_transition(
            WorkflowTransition(
                project_id="project-1",
                from_state=ApprovalState.EXPERT_REVIEW,
                to_state=target,
                actor_id="expert-1",
                reason="Return selected findings to automatic rework",
                occurred_at=datetime(2026, 7, 31, tzinfo=UTC),
            )
        )
