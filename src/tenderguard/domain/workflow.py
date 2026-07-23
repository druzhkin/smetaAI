from __future__ import annotations

from tenderguard.domain.enums import ApprovalState
from tenderguard.domain.models import WorkflowTransition

_NORMAL_TRANSITIONS: dict[ApprovalState, frozenset[ApprovalState]] = {
    ApprovalState.DRAFT: frozenset(
        {
            ApprovalState.DOCUMENTS_INCOMPLETE,
            ApprovalState.EXTRACTION_IN_PROGRESS,
            ApprovalState.BLOCKED,
            ApprovalState.ARCHIVED,
        }
    ),
    ApprovalState.DOCUMENTS_INCOMPLETE: frozenset(
        {
            ApprovalState.EXTRACTION_IN_PROGRESS,
            ApprovalState.BLOCKED,
            ApprovalState.ARCHIVED,
        }
    ),
    ApprovalState.EXTRACTION_IN_PROGRESS: frozenset(
        {
            ApprovalState.EXTRACTION_REVIEW,
            ApprovalState.DOCUMENTS_INCOMPLETE,
            ApprovalState.BLOCKED,
        }
    ),
    ApprovalState.EXTRACTION_REVIEW: frozenset(
        {
            ApprovalState.EXTRACTION_IN_PROGRESS,
            ApprovalState.BOQ_IN_PROGRESS,
            ApprovalState.BLOCKED,
        }
    ),
    ApprovalState.BOQ_IN_PROGRESS: frozenset({ApprovalState.BOQ_REVIEW, ApprovalState.BLOCKED}),
    ApprovalState.BOQ_REVIEW: frozenset(
        {
            ApprovalState.BOQ_IN_PROGRESS,
            ApprovalState.PRICING_IN_PROGRESS,
            ApprovalState.RFQ_REQUIRED,
            ApprovalState.BLOCKED,
        }
    ),
    ApprovalState.PRICING_IN_PROGRESS: frozenset(
        {
            ApprovalState.RFQ_REQUIRED,
            ApprovalState.EXPERT_REVIEW,
            ApprovalState.CALCULATION_IN_PROGRESS,
            ApprovalState.BLOCKED,
        }
    ),
    ApprovalState.RFQ_REQUIRED: frozenset(
        {
            ApprovalState.PRICING_IN_PROGRESS,
            ApprovalState.EXPERT_REVIEW,
            ApprovalState.BLOCKED,
        }
    ),
    ApprovalState.CALCULATION_IN_PROGRESS: frozenset(
        {ApprovalState.INDEPENDENT_VALIDATION, ApprovalState.BLOCKED}
    ),
    ApprovalState.INDEPENDENT_VALIDATION: frozenset(
        {
            ApprovalState.CALCULATION_IN_PROGRESS,
            ApprovalState.EXPERT_REVIEW,
            ApprovalState.BLOCKED,
        }
    ),
    ApprovalState.EXPERT_REVIEW: frozenset(
        {
            ApprovalState.EXTRACTION_REVIEW,
            ApprovalState.BOQ_REVIEW,
            ApprovalState.PRICING_IN_PROGRESS,
            ApprovalState.CALCULATION_IN_PROGRESS,
            ApprovalState.APPROVED_FOR_INTERNAL_USE,
            ApprovalState.APPROVED_FOR_BID,
            ApprovalState.BLOCKED,
        }
    ),
    ApprovalState.APPROVED_FOR_INTERNAL_USE: frozenset(
        {
            ApprovalState.EXPERT_REVIEW,
            ApprovalState.APPROVED_FOR_BID,
            ApprovalState.BLOCKED,
            ApprovalState.SUPERSEDED,
            ApprovalState.ARCHIVED,
        }
    ),
    ApprovalState.APPROVED_FOR_BID: frozenset({ApprovalState.SUPERSEDED, ApprovalState.ARCHIVED}),
    ApprovalState.SUPERSEDED: frozenset({ApprovalState.ARCHIVED}),
    ApprovalState.ARCHIVED: frozenset(),
}

_BLOCKED_RESUME_STATES = frozenset(
    {
        ApprovalState.DOCUMENTS_INCOMPLETE,
        ApprovalState.EXTRACTION_IN_PROGRESS,
        ApprovalState.EXTRACTION_REVIEW,
        ApprovalState.BOQ_IN_PROGRESS,
        ApprovalState.BOQ_REVIEW,
        ApprovalState.PRICING_IN_PROGRESS,
        ApprovalState.RFQ_REQUIRED,
        ApprovalState.CALCULATION_IN_PROGRESS,
        ApprovalState.INDEPENDENT_VALIDATION,
        ApprovalState.EXPERT_REVIEW,
        ApprovalState.ARCHIVED,
    }
)


def can_transition(from_state: ApprovalState, to_state: ApprovalState) -> bool:
    if from_state is ApprovalState.BLOCKED:
        return to_state in _BLOCKED_RESUME_STATES
    return to_state in _NORMAL_TRANSITIONS[from_state]


def validate_transition(transition: WorkflowTransition) -> None:
    if transition.from_state == transition.to_state:
        raise ValueError("Workflow self-transitions are not allowed")
    if not can_transition(transition.from_state, transition.to_state):
        raise ValueError(
            f"Illegal workflow transition: {transition.from_state} -> {transition.to_state}"
        )
