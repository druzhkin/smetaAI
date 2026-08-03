from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import Field, field_validator, model_validator
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from tenderguard.application.controlled_version_integrity import (
    require_bound_controlled_version,
)
from tenderguard.application.evidence_independence import (
    require_distinct_qualified_independence,
    resolve_observation_leaves,
)
from tenderguard.application.projects import OptimisticLockError, ProjectService
from tenderguard.application.snapshot_integrity import read_verified_snapshot
from tenderguard.config import Settings
from tenderguard.domain.actuals import (
    ActualEvidenceValue,
    ActualFact,
    ActualMetricDefinition,
    ActualsPolicyDefinition,
    CalibrationExample,
    ForecastBasis,
    ForecastFact,
    VarianceRecord,
    build_calibration_example,
    compare_forecast_to_actual,
)
from tenderguard.domain.calculation import (
    AtomicCostInput,
    CalculationPolicy,
    calculate_primary,
)
from tenderguard.domain.common import content_hash, ensure_utc, utc_now
from tenderguard.domain.enums import (
    ActorRole,
    ApprovalDecision,
    ApprovalState,
    VarianceReason,
    VerificationStatus,
)
from tenderguard.domain.models import (
    CalculationResult,
    DomainModel,
    IndependentValidationResult,
    Observation,
)
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.object_store import ObjectStore
from tenderguard.infrastructure.orm import (
    ActualRecordRow,
    ApprovalRecordRow,
    ApprovalTaskRow,
    AuditEventRow,
    CalculationRunRow,
    CalculationSnapshotRow,
    CalibrationExampleRow,
    ConflictRow,
    ControlledVersionRow,
    CostInputRow,
    DocumentRevisionRow,
    DocumentRow,
    ObservationRow,
    ProjectRow,
    ReleaseDecisionRow,
    VarianceRecordRow,
)

_POST_BID_STATES = frozenset(
    {
        ApprovalState.APPROVED_FOR_INTERNAL_USE,
        ApprovalState.APPROVED_FOR_BID,
        ApprovalState.SUPERSEDED,
        ApprovalState.ARCHIVED,
    }
)
_ACCESS_ROLES = (
    ActorRole.ESTIMATOR,
    ActorRole.PROCUREMENT,
    ActorRole.TECHNICAL_EXPERT,
    ActorRole.REVIEWER,
    ActorRole.APPROVER,
    ActorRole.METHODOLOGY_OWNER,
    ActorRole.AUDITOR,
)


class ActualRecordDraft(DomainModel):
    metric: str = Field(min_length=1, max_length=100)
    source_observation_id: str = Field(min_length=1, max_length=64)
    expected_observation_created_at: datetime

    @field_validator("metric", "source_observation_id")
    @classmethod
    def identifiers_are_normalized(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("Actual submission identifiers must be normalized")
        return value

    @field_validator("expected_observation_created_at")
    @classmethod
    def timestamp_is_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Expected observation timestamp must include a timezone")
        return value


class ActualRecordView(DomainModel):
    actual: ActualFact
    actual_key: str
    supersedes_actual_id: str | None
    is_current: bool
    created_by: str
    policy_version_id: str
    policy_content_hash: str
    source_leaf_ids: tuple[str, ...]
    project_outcome_evidence_ids: tuple[str, ...]
    approval_task_id: str
    task_status: str
    task_updated_at: datetime
    created_at: datetime


class ActualEvidenceCandidateView(DomainModel):
    observation: Observation
    observation_created_at: datetime
    evidence_value: ActualEvidenceValue | None
    eligible: bool
    blockers: tuple[str, ...] = ()


class ActualReviewView(DomainModel):
    record: ActualRecordView
    assigned_role: ActorRole
    has_classified_variance: bool
    decision_allowed: bool
    decision_blockers: tuple[str, ...]


class ActualDecisionCommand(DomainModel):
    decision: ApprovalDecision
    expected_actual_created_at: datetime
    expected_task_updated_at: datetime

    @field_validator("decision")
    @classmethod
    def decision_is_terminal(cls, value: ApprovalDecision) -> ApprovalDecision:
        if value not in {ApprovalDecision.APPROVED, ApprovalDecision.REJECTED}:
            raise ValueError(
                "Actual review decision must be APPROVED or REJECTED; "
                "correction requires a superseding fact"
            )
        return value

    @field_validator("expected_actual_created_at", "expected_task_updated_at")
    @classmethod
    def timestamps_are_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Expected timestamps must include a timezone")
        return value


class ActualDecisionResult(DomainModel):
    record: ActualRecordView
    approval_id: str
    decision: ApprovalDecision


class ForecastCandidateView(DomainModel):
    actual_id: str
    forecast: ForecastFact
    released_by_decision_id: str


class ForecastCandidatePage(DomainModel):
    items: tuple[ForecastCandidateView, ...]
    next_cursor: str | None


class CompareActualCommand(DomainModel):
    forecast_id: str = Field(min_length=1, max_length=80)
    released_by_decision_id: str = Field(min_length=1, max_length=64)
    reason: VarianceReason
    reason_detail: str = Field(min_length=1, max_length=4000)
    expected_actual_created_at: datetime
    actuals_policy_version_id: str = Field(min_length=1, max_length=64)

    @field_validator("expected_actual_created_at")
    @classmethod
    def timestamp_is_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Expected actual timestamp must include a timezone")
        return value

    @field_validator("reason_detail")
    @classmethod
    def reason_detail_is_normalized(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Variance reason detail is required")
        return normalized


class VarianceView(DomainModel):
    variance_record_id: str
    variance: VarianceRecord
    forecast: ForecastFact
    policy_version_id: str
    policy_content_hash: str
    approval_task_id: str
    task_status: str
    task_updated_at: datetime
    created_at: datetime
    assigned_role: ActorRole
    decision_allowed: bool
    decision_blockers: tuple[str, ...]


class ActualComparisonResult(DomainModel):
    forecast: ForecastFact
    actual: ActualFact
    variance: VarianceRecord
    variance_record_id: str
    calibration_example: CalibrationExample | None = None
    calibration_approved: bool = False


class VarianceDecisionCommand(DomainModel):
    decision: ApprovalDecision
    expected_variance_created_at: datetime
    expected_task_updated_at: datetime

    @field_validator("decision")
    @classmethod
    def decision_is_terminal(cls, value: ApprovalDecision) -> ApprovalDecision:
        if value not in {ApprovalDecision.APPROVED, ApprovalDecision.REJECTED}:
            raise ValueError(
                "Variance review decision must be APPROVED or REJECTED; "
                "reclassification requires a superseding actual fact"
            )
        return value

    @field_validator("expected_variance_created_at", "expected_task_updated_at")
    @classmethod
    def timestamps_are_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Expected timestamps must include a timezone")
        return value


class VarianceDecisionResult(DomainModel):
    variance: VarianceView
    approval_id: str
    decision: ApprovalDecision
    calibration_example: CalibrationExample | None = None


class CalibrationExampleView(DomainModel):
    example: CalibrationExample
    approved: bool
    approval_task_id: str
    task_status: str
    task_updated_at: datetime
    policy_version_id: str
    policy_content_hash: str
    created_at: datetime
    approved_by: str | None = None
    assigned_role: ActorRole
    decision_allowed: bool
    decision_blockers: tuple[str, ...]


class CalibrationDecisionCommand(DomainModel):
    decision: ApprovalDecision
    expected_example_created_at: datetime
    expected_task_updated_at: datetime

    @field_validator("decision")
    @classmethod
    def decision_is_terminal(cls, value: ApprovalDecision) -> ApprovalDecision:
        if value not in {ApprovalDecision.APPROVED, ApprovalDecision.REJECTED}:
            raise ValueError("Calibration decision must be APPROVED or REJECTED")
        return value

    @field_validator("expected_example_created_at", "expected_task_updated_at")
    @classmethod
    def timestamps_are_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Expected timestamps must include a timezone")
        return value


class CalibrationDecisionResult(DomainModel):
    example: CalibrationExampleView
    approval_id: str
    decision: ApprovalDecision


class ActualsContextView(DomainModel):
    project_id: str
    project_state: ApprovalState
    policy_version_id: str
    policy_content_hash: str
    record_roles: tuple[ActorRole, ...]
    actual_review_role: ActorRole
    variance_classifier_roles: tuple[ActorRole, ...]
    variance_review_role: ActorRole
    calibration_approval_role: ActorRole
    metric_definitions: tuple[ActualMetricDefinition, ...]
    required_metric_keys: tuple[str, ...]
    selected_metric: str
    project_outcome_evidence_ids: tuple[str, ...]
    records: tuple[ActualReviewView, ...]
    evidence_candidates: tuple[ActualEvidenceCandidateView, ...]
    candidates_truncated: bool
    variances: tuple[VarianceView, ...]
    calibration_examples: tuple[CalibrationExampleView, ...]
    next_cursor: str | None


class _ActualRecordCursor(DomainModel):
    actual_key: str
    record_id: str


class _ActualTimelineCursor(DomainModel):
    occurred_at: datetime
    record_id: str

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_is_timezone_aware(cls, value: datetime) -> datetime:
        normalized = ensure_utc(value)
        if normalized is None:
            raise ValueError("Actuals cursor timestamp must include a timezone")
        return normalized


class _ActualsCursor(DomainModel):
    scope: str
    records: _ActualRecordCursor | None = None
    records_done: bool = False
    variances: _ActualTimelineCursor | None = None
    variances_done: bool = False
    calibrations: _ActualTimelineCursor | None = None
    calibrations_done: bool = False

    @model_validator(mode="after")
    def positions_match_completion_state(self) -> _ActualsCursor:
        for position, done in (
            (self.records, self.records_done),
            (self.variances, self.variances_done),
            (self.calibrations, self.calibrations_done),
        ):
            if done and position is not None:
                raise ValueError("Completed actuals cursor section cannot retain a position")
            if not done and position is None:
                raise ValueError("Incomplete actuals cursor section requires a position")
        return self


class ActualsService:
    def __init__(
        self,
        *,
        session: Session,
        settings: Settings,
        object_store: ObjectStore,
    ) -> None:
        self.session = session
        self.settings = settings
        self.object_store = object_store

    def context(
        self,
        *,
        actor: Actor,
        project_id: str,
        selected_metric: str | None,
        limit: int,
        cursor: str | None = None,
    ) -> ActualsContextView:
        if limit < 1 or limit > 100:
            raise ValueError("Actuals context limit must be between 1 and 100")
        project = self._require_post_bid_project(
            actor,
            project_id,
            required_roles=_ACCESS_ROLES,
            lock=False,
        )
        policy, version = self._policy(project, actor.organization_id)
        metric = selected_metric or policy.metric_definitions[0].metric
        definition = policy.metric(metric)
        cursor_scope = self._cursor_scope(
            kind="context",
            project_id=project.id,
            metric=metric,
            policy_version_id=version.id,
        )
        decoded_cursor = self._decode_actuals_cursor(cursor, cursor_scope)
        outcome_ids = self._project_outcome_evidence(project, policy)
        rows, records_cursor, records_done = self._actual_record_page(
            project_id=project.id,
            limit=limit,
            cursor=decoded_cursor,
        )
        classified_actual_ids = (
            set(
                self.session.scalars(
                    select(VarianceRecordRow.actual_record_id).where(
                        VarianceRecordRow.project_id == project.id,
                        VarianceRecordRow.actual_record_id.in_(tuple(row.id for row in rows)),
                    )
                )
            )
            if rows
            else set()
        )
        reviews = tuple(
            self._actual_review_view(
                actor=actor,
                project=project,
                row=row,
                policy=policy,
                version=version,
                outcome_ids=outcome_ids,
                has_classified_variance=row.id in classified_actual_ids,
            )
            for row in rows
        )
        candidate_rows = (
            list(
                self.session.scalars(
                    select(ObservationRow)
                    .where(
                        ObservationRow.project_id == project.id,
                        ObservationRow.field_name == definition.evidence_field_name,
                    )
                    .order_by(ObservationRow.created_at.desc(), ObservationRow.id.desc())
                    .limit(limit + 1)
                )
            )
            if decoded_cursor is None
            else []
        )
        selected_source_ids = {
            actual_row.source_observation_id for actual_row in rows if actual_row.metric == metric
        }
        visible = {row.id: row for row in candidate_rows[:limit]}
        if selected_source_ids.difference(visible):
            for missing_observation in self.session.scalars(
                select(ObservationRow).where(
                    ObservationRow.project_id == project.id,
                    ObservationRow.id.in_(selected_source_ids.difference(visible)),
                )
            ):
                visible[missing_observation.id] = missing_observation
        candidates = tuple(
            self._candidate_view(
                project=project,
                row=candidate_row,
                definition=definition,
                policy=policy,
            )
            for candidate_row in sorted(
                visible.values(),
                key=lambda item: (item.created_at, item.id),
                reverse=True,
            )
        )
        variance_rows, variances_cursor, variances_done = self._variance_record_page(
            project_id=project.id,
            limit=limit,
            cursor=(decoded_cursor.variances if decoded_cursor is not None else None),
            done=(decoded_cursor.variances_done if decoded_cursor is not None else False),
        )
        variances = tuple(
            self._variance_view(
                actor=actor,
                project=project,
                row=row,
                policy=policy,
                version=version,
            )
            for row in variance_rows
        )
        calibration_rows, calibrations_cursor, calibrations_done = self._calibration_record_page(
            project_id=project.id,
            limit=limit,
            cursor=(decoded_cursor.calibrations if decoded_cursor is not None else None),
            done=(decoded_cursor.calibrations_done if decoded_cursor is not None else False),
        )
        next_cursor = (
            None
            if records_done and variances_done and calibrations_done
            else self._encode_actuals_cursor(
                _ActualsCursor(
                    scope=cursor_scope,
                    records=records_cursor,
                    records_done=records_done,
                    variances=variances_cursor,
                    variances_done=variances_done,
                    calibrations=calibrations_cursor,
                    calibrations_done=calibrations_done,
                )
            )
        )
        return ActualsContextView(
            project_id=project.id,
            project_state=ApprovalState(project.state),
            policy_version_id=version.id,
            policy_content_hash=version.content_hash,
            record_roles=policy.record_roles,
            actual_review_role=policy.actual_review_role,
            variance_classifier_roles=policy.variance_classifier_roles,
            variance_review_role=policy.variance_review_role,
            calibration_approval_role=policy.calibration_approval_role,
            metric_definitions=policy.metric_definitions,
            required_metric_keys=policy.required_metric_keys,
            selected_metric=metric,
            project_outcome_evidence_ids=outcome_ids,
            records=reviews,
            evidence_candidates=candidates,
            candidates_truncated=len(candidate_rows) > limit,
            variances=variances,
            calibration_examples=tuple(
                self._calibration_view(
                    actor=actor,
                    project=project,
                    row=row,
                    policy=policy,
                    version=version,
                )
                for row in calibration_rows
            ),
            next_cursor=next_cursor,
        )

    def forecast_candidates(
        self,
        *,
        actor: Actor,
        project_id: str,
        actual_id: str,
        limit: int,
        cursor: str | None,
    ) -> ForecastCandidatePage:
        if limit < 1 or limit > 50:
            raise ValueError("Forecast candidate page limit must be between 1 and 50")
        project = self._require_post_bid_project(
            actor,
            project_id,
            required_roles=_ACCESS_ROLES,
            lock=False,
        )
        policy, version = self._policy(project, actor.organization_id)
        row = self._current_actual(project.id, actual_id)
        outcome_ids = self._project_outcome_evidence(project, policy)
        self._require_verified_actual_integrity(
            row=row,
            project=project,
            policy=policy,
            version=version,
            outcome_ids=outcome_ids,
        )
        cursor_scope = self._cursor_scope(
            kind="forecasts",
            project_id=project.id,
            metric=row.metric,
            policy_version_id=version.id,
            actual_id=row.id,
        )
        decoded = self._decode_timeline_cursor(cursor, cursor_scope)
        statement = select(ReleaseDecisionRow).where(
            ReleaseDecisionRow.project_id == project.id,
            ReleaseDecisionRow.allowed.is_(True),
            ReleaseDecisionRow.requested_state == ApprovalState.APPROVED_FOR_BID.value,
            ReleaseDecisionRow.resulting_state == ApprovalState.APPROVED_FOR_BID.value,
            ReleaseDecisionRow.snapshot_id.is_not(None),
        )
        if decoded is not None:
            statement = statement.where(
                or_(
                    ReleaseDecisionRow.decided_at < decoded.occurred_at,
                    and_(
                        ReleaseDecisionRow.decided_at == decoded.occurred_at,
                        ReleaseDecisionRow.id < decoded.record_id,
                    ),
                )
            )
        release_rows = tuple(
            self.session.scalars(
                statement.order_by(
                    ReleaseDecisionRow.decided_at.desc(),
                    ReleaseDecisionRow.id.desc(),
                ).limit(limit + 1)
            )
        )
        selected = release_rows[:limit]
        snapshot_payload_cache: dict[str, dict[str, Any]] = {}
        candidates: list[ForecastCandidateView] = []
        seen: set[str] = set()
        for release in selected:
            assert release.snapshot_id is not None
            try:
                candidate = self._forecast_from_snapshot(
                    project,
                    row,
                    policy,
                    release.snapshot_id,
                    release.id,
                    snapshot_payload_cache=snapshot_payload_cache,
                )
            except (LookupError, RuntimeError, TypeError, ValueError):
                continue
            if candidate.forecast.forecast_id not in seen:
                candidates.append(candidate)
                seen.add(candidate.forecast.forecast_id)
        return ForecastCandidatePage(
            items=tuple(candidates),
            next_cursor=(
                self._encode_timeline_cursor(
                    scope=cursor_scope,
                    occurred_at=selected[-1].decided_at,
                    record_id=selected[-1].id,
                )
                if len(release_rows) > limit and selected
                else None
            ),
        )

    def require_verified_actual_integrity(
        self,
        *,
        project_id: str,
        actual_id: str,
    ) -> ActualFact:
        """Reproduce a verified actual for another trusted application service."""

        project = self.session.get(ProjectRow, project_id)
        if project is None:
            raise LookupError(project_id)
        row = self._current_actual(project_id, actual_id)
        policy_id = self._required_string(
            row.payload,
            "actuals_policy_version_id",
        )
        policy, version = self._policy(
            project,
            project.organization_id,
            expected_version_id=policy_id,
        )
        outcome_ids = self._project_outcome_evidence(project, policy)
        return self._require_verified_actual_integrity(
            row=row,
            project=project,
            policy=policy,
            version=version,
            outcome_ids=outcome_ids,
        )

    def record_actual(
        self,
        *,
        actor: Actor,
        project_id: str,
        draft: ActualRecordDraft,
        actuals_policy_version_id: str,
        request_id: str,
        reason: str,
    ) -> ActualRecordView:
        reason = self._reason(reason)
        project = self._require_post_bid_project(
            actor,
            project_id,
            required_roles=_ACCESS_ROLES,
            lock=True,
        )
        policy, version = self._policy(
            project,
            actor.organization_id,
            expected_version_id=actuals_policy_version_id,
        )
        self._project_service().get_project(
            actor=actor,
            project_id=project.id,
            required_roles=policy.record_roles,
        )
        definition = policy.metric(draft.metric)
        outcome_ids = self._project_outcome_evidence(project, policy)
        observation = self._observation_row(
            project.id,
            draft.source_observation_id,
            lock=True,
        )
        expected_observation_at = self._timestamp(draft.expected_observation_created_at)
        if self._timestamp(observation.created_at) != expected_observation_at:
            raise OptimisticLockError("Actual evidence changed after context was loaded")
        value, leaf_ids = self._require_actual_source_integrity(
            project=project,
            row=observation,
            definition=definition,
            policy=policy,
        )
        if value.metric != draft.metric:
            raise ValueError("Actual evidence metric differs from the selected policy metric")
        actual_id = f"actual-{uuid4()}"
        previous = self.session.scalar(
            select(ActualRecordRow)
            .where(
                ActualRecordRow.project_id == project.id,
                ActualRecordRow.actual_key == value.actual_key,
                ActualRecordRow.is_current.is_(True),
            )
            .with_for_update()
        )
        now = utc_now()
        superseded_task_id: str | None = None
        if previous is not None:
            previous.is_current = False
            previous_task = self._actual_task(previous, lock=True)
            if previous_task is not None and previous_task.status != "SUPERSEDED":
                superseded_task_id = previous_task.id
                previous_task.status = "SUPERSEDED"
                previous_task.payload = {
                    **previous_task.payload,
                    "superseded_at": now.isoformat(),
                    "superseded_by_entity_id": actual_id,
                    "supersession_reason": "ACTUAL_FACT_REPLACED",
                }
                previous_task.updated_at = now
        row = ActualRecordRow(
            id=actual_id,
            project_id=project.id,
            actual_key=value.actual_key,
            entity_type=value.entity_type,
            entity_id=value.entity_id,
            metric=value.metric,
            value=value.value,
            unit=value.unit,
            verified=False,
            source_observation_id=observation.id,
            occurred_on=value.occurred_on,
            payload={
                "evidence_value": value.model_dump(mode="json"),
                "source_leaf_ids": list(leaf_ids),
                "project_outcome_evidence_ids": list(outcome_ids),
                "created_by": actor.actor_id,
                "actuals_policy_version_id": version.id,
                "actuals_policy_content_hash": version.content_hash,
                "review_role": policy.actual_review_role.value,
                "review_status": VerificationStatus.IN_REVIEW.value,
            },
            supersedes_actual_id=previous.id if previous is not None else None,
            is_current=True,
            created_at=now,
        )
        task = self._new_task(
            project_id=project.id,
            task_type="ACTUAL_FACT_REVIEW",
            entity_type="actual_record",
            entity_id=row.id,
            assigned_role=policy.actual_review_role,
            payload=self._actual_task_payload(
                row=row,
                policy=policy,
                version=version,
                outcome_ids=outcome_ids,
            ),
            created_by=actor.actor_id,
            now=now,
        )
        row.payload = {**row.payload, "approval_task_id": task.id}
        self.session.add(row)
        self._audit(
            project.id,
            actor,
            request_id,
            reason,
            "actual_fact_recorded",
            {
                "actual_id": row.id,
                "actual_key": row.actual_key,
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "metric": row.metric,
                "value": row.value,
                "unit": row.unit,
                "source_class": value.source_class.value,
                "source_observation_id": row.source_observation_id,
                "source_leaf_ids": list(leaf_ids),
                "project_outcome_evidence_ids": list(outcome_ids),
                "actuals_policy_version_id": version.id,
                "actuals_policy_content_hash": version.content_hash,
                "actual_submission_hash": task.payload["actual_submission_hash"],
                "approval_task_id": task.id,
                "supersedes_actual_id": row.supersedes_actual_id,
                "superseded_approval_task_id": superseded_task_id,
            },
        )
        return self._actual_view(row)

    def decide_actual(
        self,
        *,
        actor: Actor,
        project_id: str,
        actual_id: str,
        command: ActualDecisionCommand,
        request_id: str,
        reason: str,
    ) -> ActualDecisionResult:
        reason = self._reason(reason)
        project = self._require_post_bid_project(
            actor,
            project_id,
            required_roles=_ACCESS_ROLES,
            lock=True,
        )
        row = self._current_actual(project.id, actual_id, lock=True)
        if self._timestamp(row.created_at) != self._timestamp(command.expected_actual_created_at):
            raise OptimisticLockError(
                "Actual fact changed after it was loaded; reload before deciding"
            )
        policy_id = self._required_string(
            row.payload,
            "actuals_policy_version_id",
        )
        policy, version = self._policy(
            project,
            actor.organization_id,
            expected_version_id=policy_id,
        )
        self._project_service().get_project(
            actor=actor,
            project_id=project.id,
            required_roles=(policy.actual_review_role,),
        )
        outcome_ids = self._project_outcome_evidence(project, policy)
        task = self._actual_task(row, lock=True)
        if task is None:
            raise ValueError("Actual review task is missing")
        if self._timestamp(task.updated_at) != self._timestamp(command.expected_task_updated_at):
            raise OptimisticLockError(
                "Actual review task changed after it was loaded; reload before deciding"
            )
        blockers = self._actual_review_blockers(
            actor=actor,
            project=project,
            row=row,
            task=task,
            policy=policy,
            version=version,
            outcome_ids=outcome_ids,
        )
        if blockers:
            raise ValueError("Actual review is blocked: " + ", ".join(blockers))
        now = utc_now()
        approval_id = f"approval-{uuid4()}"
        row.verified = command.decision is ApprovalDecision.APPROVED
        row.payload = {
            **row.payload,
            "review_status": (
                VerificationStatus.VERIFIED.value
                if command.decision is ApprovalDecision.APPROVED
                else VerificationStatus.REJECTED.value
            ),
            "review_decision": command.decision.value,
            "reviewed_by": actor.actor_id,
            "reviewed_at": now.isoformat(),
            **(
                {
                    "verified_by": actor.actor_id,
                    "verified_at": now.isoformat(),
                }
                if command.decision is ApprovalDecision.APPROVED
                else {}
            ),
        }
        task.status = command.decision.value
        task.updated_at = now
        approval_payload = {
            "project_id": project.id,
            "actual_id": row.id,
            "actual_key": row.actual_key,
            "source_observation_id": row.source_observation_id,
            "source_leaf_ids": list(row.payload.get("source_leaf_ids", [])),
            "project_outcome_evidence_ids": list(outcome_ids),
            "actuals_policy_version_id": version.id,
            "actuals_policy_content_hash": version.content_hash,
            "actual_submission_hash": task.payload.get("actual_submission_hash"),
            "expected_actual_created_at": self._timestamp(row.created_at).isoformat(),
            "expected_task_updated_at": self._timestamp(
                command.expected_task_updated_at
            ).isoformat(),
        }
        self.session.add(
            ApprovalRecordRow(
                id=approval_id,
                task_id=task.id,
                decision=command.decision.value,
                decided_by=actor.actor_id,
                reason=reason,
                payload=approval_payload,
                decided_at=now,
            )
        )
        self._audit(
            project.id,
            actor,
            request_id,
            reason,
            "actual_fact_verified",
            {
                "approval_id": approval_id,
                "approval_task_id": task.id,
                "decision": command.decision.value,
                **approval_payload,
            },
        )
        return ActualDecisionResult(
            record=self._actual_view(row),
            approval_id=approval_id,
            decision=command.decision,
        )

    def verify_actual(
        self,
        *,
        actor: Actor,
        project_id: str,
        actual_id: str,
        expected_actual_created_at: datetime,
        expected_task_updated_at: datetime,
        request_id: str,
        reason: str,
    ) -> ActualRecordView:
        return self.decide_actual(
            actor=actor,
            project_id=project_id,
            actual_id=actual_id,
            command=ActualDecisionCommand(
                decision=ApprovalDecision.APPROVED,
                expected_actual_created_at=expected_actual_created_at,
                expected_task_updated_at=expected_task_updated_at,
            ),
            request_id=request_id,
            reason=reason,
        ).record

    def compare_to_forecast(
        self,
        *,
        actor: Actor,
        project_id: str,
        actual_id: str,
        command: CompareActualCommand,
        request_id: str,
        reason: str,
    ) -> ActualComparisonResult:
        reason = self._reason(reason)
        project = self._require_post_bid_project(
            actor,
            project_id,
            required_roles=_ACCESS_ROLES,
            lock=True,
        )
        policy, version = self._policy(
            project,
            actor.organization_id,
            expected_version_id=command.actuals_policy_version_id,
        )
        self._project_service().get_project(
            actor=actor,
            project_id=project.id,
            required_roles=policy.variance_classifier_roles,
        )
        row = self._current_actual(project.id, actual_id, lock=True)
        if self._timestamp(row.created_at) != self._timestamp(command.expected_actual_created_at):
            raise OptimisticLockError("Actual fact changed after forecast context was loaded")
        outcome_ids = self._project_outcome_evidence(project, policy)
        actual = self._require_verified_actual_integrity(
            row=row,
            project=project,
            policy=policy,
            version=version,
            outcome_ids=outcome_ids,
        )
        release = self.session.scalar(
            select(ReleaseDecisionRow).where(
                ReleaseDecisionRow.id == command.released_by_decision_id,
                ReleaseDecisionRow.project_id == project.id,
                ReleaseDecisionRow.allowed.is_(True),
                ReleaseDecisionRow.requested_state == ApprovalState.APPROVED_FOR_BID.value,
                ReleaseDecisionRow.resulting_state == ApprovalState.APPROVED_FOR_BID.value,
                ReleaseDecisionRow.snapshot_id.is_not(None),
            )
        )
        try:
            if release is None or release.snapshot_id is None:
                raise ValueError("Released forecast decision is unavailable")
            candidate = self._forecast_from_snapshot(
                project,
                row,
                policy,
                release.snapshot_id,
                release.id,
            )
            if candidate.forecast.forecast_id != command.forecast_id:
                raise ValueError("Released forecast identifier changed")
        except (LookupError, RuntimeError, TypeError, ValueError) as error:
            raise OptimisticLockError(
                "Released forecast candidate changed or no longer verifies"
            ) from error
        if candidate.released_by_decision_id != command.released_by_decision_id:
            raise OptimisticLockError("Released forecast candidate changed or no longer verifies")
        if (
            self.session.scalar(
                select(VarianceRecordRow).where(VarianceRecordRow.actual_record_id == row.id)
            )
            is not None
        ):
            raise ValueError(
                "Current actual fact already has a variance classification; "
                "submit a superseding actual to reclassify"
            )
        variance = compare_forecast_to_actual(
            candidate.forecast,
            actual,
            reason=command.reason,
            reason_detail=command.reason_detail,
            classified_by=actor.actor_id,
            relative_scale=policy.relative_variance_scale,
            relative_rounding=policy.relative_variance_rounding_mode,
        )
        now = utc_now()
        variance_id = f"variance-{uuid4()}"
        variance_row = VarianceRecordRow(
            id=variance_id,
            project_id=project.id,
            actual_record_id=row.id,
            snapshot_id=candidate.forecast.snapshot_id,
            metric=row.metric,
            reason=variance.reason.value,
            absolute_variance=variance.absolute_variance,
            relative_variance=variance.relative_variance,
            payload={
                "variance": variance.model_dump(mode="json"),
                "forecast": candidate.forecast.model_dump(mode="json"),
                "released_by_decision_id": candidate.released_by_decision_id,
                "actual_content_hash": self._actual_content_hash(row),
                "classified_by": actor.actor_id,
                "actuals_policy_version_id": version.id,
                "actuals_policy_content_hash": version.content_hash,
                "review_role": policy.variance_review_role.value,
            },
            classified_by=actor.actor_id,
            created_at=now,
        )
        task = self._new_task(
            project_id=project.id,
            task_type="VARIANCE_CLASSIFICATION_REVIEW",
            entity_type="variance_record",
            entity_id=variance_id,
            assigned_role=policy.variance_review_role,
            payload=self._variance_task_payload(
                variance_row=variance_row,
                policy=policy,
                version=version,
            ),
            created_by=actor.actor_id,
            now=now,
        )
        variance_row.payload = {
            **variance_row.payload,
            "approval_task_id": task.id,
        }
        self.session.add(variance_row)
        self._audit(
            project.id,
            actor,
            request_id,
            reason,
            "forecast_actual_variance_classified",
            {
                "actual_id": row.id,
                "forecast_id": candidate.forecast.forecast_id,
                "snapshot_id": candidate.forecast.snapshot_id,
                "cost_input_row_id": candidate.forecast.cost_input_row_id,
                "released_by_decision_id": candidate.released_by_decision_id,
                "variance_record_id": variance_id,
                "variance_reason": variance.reason.value,
                "variance_submission_hash": task.payload["variance_submission_hash"],
                "actuals_policy_version_id": version.id,
                "actuals_policy_content_hash": version.content_hash,
                "approval_task_id": task.id,
            },
        )
        return ActualComparisonResult(
            forecast=candidate.forecast,
            actual=actual,
            variance=variance,
            variance_record_id=variance_id,
        )

    def decide_variance(
        self,
        *,
        actor: Actor,
        project_id: str,
        variance_id: str,
        command: VarianceDecisionCommand,
        request_id: str,
        reason: str,
    ) -> VarianceDecisionResult:
        reason = self._reason(reason)
        project = self._require_post_bid_project(
            actor,
            project_id,
            required_roles=_ACCESS_ROLES,
            lock=True,
        )
        row = self.session.scalar(
            select(VarianceRecordRow)
            .where(
                VarianceRecordRow.id == variance_id,
                VarianceRecordRow.project_id == project.id,
            )
            .with_for_update()
        )
        if row is None:
            raise LookupError(variance_id)
        if self._timestamp(row.created_at) != self._timestamp(command.expected_variance_created_at):
            raise OptimisticLockError(
                "Variance changed after it was loaded; reload before deciding"
            )
        policy_id = self._required_string(
            row.payload,
            "actuals_policy_version_id",
        )
        policy, version = self._policy(
            project,
            actor.organization_id,
            expected_version_id=policy_id,
        )
        self._project_service().get_project(
            actor=actor,
            project_id=project.id,
            required_roles=(policy.variance_review_role,),
        )
        task = self._variance_task(row, lock=True)
        if task is None:
            raise ValueError("Variance review task is missing")
        if self._timestamp(task.updated_at) != self._timestamp(command.expected_task_updated_at):
            raise OptimisticLockError(
                "Variance task changed after it was loaded; reload before deciding"
            )
        blockers = self._variance_review_blockers(
            actor=actor,
            project=project,
            row=row,
            task=task,
            policy=policy,
            version=version,
        )
        if blockers:
            raise ValueError("Variance review is blocked: " + ", ".join(blockers))
        now = utc_now()
        stored_variance = VarianceRecord.model_validate(row.payload["variance"])
        reviewed_variance = stored_variance.model_copy(
            update={
                "status": (
                    VerificationStatus.VERIFIED
                    if command.decision is ApprovalDecision.APPROVED
                    else VerificationStatus.REJECTED
                ),
                "reviewed_by": actor.actor_id,
            }
        )
        row.payload = {
            **row.payload,
            "variance": reviewed_variance.model_dump(mode="json"),
            "review_decision": command.decision.value,
            "reviewed_by": actor.actor_id,
            "reviewed_at": now.isoformat(),
        }
        task.status = command.decision.value
        task.updated_at = now
        approval_id = f"approval-{uuid4()}"
        approval_payload = {
            "project_id": project.id,
            "variance_record_id": row.id,
            "actual_record_id": row.actual_record_id,
            "forecast_id": stored_variance.forecast_id,
            "released_by_decision_id": self._required_string(
                row.payload,
                "released_by_decision_id",
            ),
            "variance_submission_hash": task.payload.get("variance_submission_hash"),
            "actuals_policy_version_id": version.id,
            "actuals_policy_content_hash": version.content_hash,
        }
        self.session.add(
            ApprovalRecordRow(
                id=approval_id,
                task_id=task.id,
                decision=command.decision.value,
                decided_by=actor.actor_id,
                reason=reason,
                payload=approval_payload,
                decided_at=now,
            )
        )
        calibration: CalibrationExample | None = None
        calibration_task: ApprovalTaskRow | None = None
        if command.decision is ApprovalDecision.APPROVED:
            actual_row = self._current_actual(
                project.id,
                row.actual_record_id,
                lock=True,
            )
            outcome_ids = self._project_outcome_evidence(project, policy)
            actual = self._require_verified_actual_integrity(
                row=actual_row,
                project=project,
                policy=policy,
                version=version,
                outcome_ids=outcome_ids,
            )
            forecast = self._replay_forecast(
                project,
                actual_row,
                policy,
                ForecastFact.model_validate(row.payload["forecast"]),
                expected_release_decision_id=self._required_string(
                    row.payload,
                    "released_by_decision_id",
                ),
            )
            replayed = compare_forecast_to_actual(
                forecast.forecast,
                actual,
                reason=reviewed_variance.reason,
                reason_detail=reviewed_variance.reason_detail,
                classified_by=reviewed_variance.classified_by,
                relative_scale=policy.relative_variance_scale,
                relative_rounding=policy.relative_variance_rounding_mode,
            )
            if (
                replayed.absolute_variance != reviewed_variance.absolute_variance
                or replayed.relative_variance != reviewed_variance.relative_variance
            ):
                raise ValueError("Variance arithmetic no longer reproduces")
            calibration = build_calibration_example(
                forecast.forecast,
                actual,
                reviewed_variance,
            ).model_copy(
                update={
                    "example_id": (
                        "calibration-"
                        + content_hash(
                            {
                                "actual_id": actual.actual_id,
                                "variance_id": row.id,
                                "forecast_id": forecast.forecast.forecast_id,
                            }
                        )[:24]
                    )
                }
            )
            calibration_row = CalibrationExampleRow(
                id=calibration.example_id,
                project_id=project.id,
                actual_record_id=actual_row.id,
                variance_record_id=row.id,
                features_snapshot_id=forecast.forecast.snapshot_id,
                metric=calibration.metric,
                target_value=calibration.target_value,
                unit=calibration.unit,
                approved=False,
                payload={
                    "calibration_example": calibration.model_dump(mode="json"),
                    "created_by": actor.actor_id,
                    "actual_content_hash": self._actual_content_hash(actual_row),
                    "variance_content_hash": self._variance_content_hash(row),
                    "forecast": forecast.forecast.model_dump(mode="json"),
                    "released_by_decision_id": self._required_string(
                        row.payload,
                        "released_by_decision_id",
                    ),
                    "actuals_policy_version_id": version.id,
                    "actuals_policy_content_hash": version.content_hash,
                    "approval_role": policy.calibration_approval_role.value,
                    "review_status": VerificationStatus.IN_REVIEW.value,
                },
                created_at=now,
            )
            calibration_task = self._new_task(
                project_id=project.id,
                task_type="CALIBRATION_EXAMPLE_REVIEW",
                entity_type="calibration_example",
                entity_id=calibration_row.id,
                assigned_role=policy.calibration_approval_role,
                payload=self._calibration_task_payload(
                    row=calibration_row,
                    policy=policy,
                    version=version,
                ),
                created_by=actor.actor_id,
                now=now,
            )
            calibration_row.payload = {
                **calibration_row.payload,
                "approval_task_id": calibration_task.id,
            }
            self.session.add(calibration_row)
        self._audit(
            project.id,
            actor,
            request_id,
            reason,
            "variance_review_decided",
            {
                "approval_id": approval_id,
                "approval_task_id": task.id,
                "decision": command.decision.value,
                **approval_payload,
                "calibration_example_id": (
                    calibration.example_id if calibration is not None else None
                ),
            },
        )
        if calibration is not None and calibration_task is not None:
            self._audit(
                project.id,
                actor,
                request_id,
                reason,
                "calibration_example_created",
                {
                    "calibration_example_id": calibration.example_id,
                    "actual_record_id": row.actual_record_id,
                    "variance_record_id": row.id,
                    "released_by_decision_id": self._required_string(
                        row.payload,
                        "released_by_decision_id",
                    ),
                    "actuals_policy_version_id": version.id,
                    "actuals_policy_content_hash": version.content_hash,
                    "calibration_submission_hash": calibration_task.payload[
                        "calibration_submission_hash"
                    ],
                    "approval_task_id": calibration_task.id,
                },
            )
        return VarianceDecisionResult(
            variance=self._variance_view(
                actor=actor,
                project=project,
                row=row,
                policy=policy,
                version=version,
            ),
            approval_id=approval_id,
            decision=command.decision,
            calibration_example=calibration,
        )

    def decide_calibration_example(
        self,
        *,
        actor: Actor,
        project_id: str,
        example_id: str,
        command: CalibrationDecisionCommand,
        request_id: str,
        reason: str,
    ) -> CalibrationDecisionResult:
        reason = self._reason(reason)
        project = self._require_post_bid_project(
            actor,
            project_id,
            required_roles=_ACCESS_ROLES,
            lock=True,
        )
        row = self.session.scalar(
            select(CalibrationExampleRow)
            .where(
                CalibrationExampleRow.id == example_id,
                CalibrationExampleRow.project_id == project.id,
            )
            .with_for_update()
        )
        if row is None:
            raise LookupError(example_id)
        if self._timestamp(row.created_at) != self._timestamp(command.expected_example_created_at):
            raise OptimisticLockError("Calibration example changed after it was loaded")
        policy_id = self._required_string(
            row.payload,
            "actuals_policy_version_id",
        )
        policy, version = self._policy(
            project,
            actor.organization_id,
            expected_version_id=policy_id,
        )
        self._project_service().get_project(
            actor=actor,
            project_id=project.id,
            required_roles=(policy.calibration_approval_role,),
        )
        task = self._calibration_task(row, lock=True)
        if task is None:
            raise ValueError("Calibration review task is missing")
        if self._timestamp(task.updated_at) != self._timestamp(command.expected_task_updated_at):
            raise OptimisticLockError("Calibration review task changed after it was loaded")
        blockers = self._calibration_review_blockers(
            actor=actor,
            project=project,
            row=row,
            task=task,
            policy=policy,
            version=version,
        )
        if blockers:
            raise ValueError("Calibration review is blocked: " + ", ".join(blockers))
        now = utc_now()
        row.approved = command.decision is ApprovalDecision.APPROVED
        row.payload = {
            **row.payload,
            "review_status": (
                VerificationStatus.VERIFIED.value
                if command.decision is ApprovalDecision.APPROVED
                else VerificationStatus.REJECTED.value
            ),
            "review_decision": command.decision.value,
            "reviewed_by": actor.actor_id,
            "reviewed_at": now.isoformat(),
            **(
                {
                    "approved_by": actor.actor_id,
                    "approved_at": now.isoformat(),
                    "approval_reason": reason,
                }
                if command.decision is ApprovalDecision.APPROVED
                else {}
            ),
        }
        task.status = command.decision.value
        task.updated_at = now
        approval_id = f"approval-{uuid4()}"
        approval_payload = {
            "project_id": project.id,
            "calibration_example_id": row.id,
            "actual_record_id": row.actual_record_id,
            "variance_record_id": row.variance_record_id,
            "released_by_decision_id": self._required_string(
                row.payload,
                "released_by_decision_id",
            ),
            "calibration_submission_hash": task.payload.get("calibration_submission_hash"),
            "actuals_policy_version_id": version.id,
            "actuals_policy_content_hash": version.content_hash,
        }
        self.session.add(
            ApprovalRecordRow(
                id=approval_id,
                task_id=task.id,
                decision=command.decision.value,
                decided_by=actor.actor_id,
                reason=reason,
                payload=approval_payload,
                decided_at=now,
            )
        )
        self._audit(
            project.id,
            actor,
            request_id,
            reason,
            "calibration_example_review_decided",
            {
                "approval_id": approval_id,
                "approval_task_id": task.id,
                "decision": command.decision.value,
                **approval_payload,
            },
        )
        return CalibrationDecisionResult(
            example=self._calibration_view(
                actor=actor,
                project=project,
                row=row,
                policy=policy,
                version=version,
            ),
            approval_id=approval_id,
            decision=command.decision,
        )

    def approve_calibration_example(
        self,
        *,
        actor: Actor,
        project_id: str,
        example_id: str,
        expected_example_created_at: datetime,
        expected_task_updated_at: datetime,
        request_id: str,
        reason: str,
    ) -> CalibrationExample:
        return self.decide_calibration_example(
            actor=actor,
            project_id=project_id,
            example_id=example_id,
            command=CalibrationDecisionCommand(
                decision=ApprovalDecision.APPROVED,
                expected_example_created_at=expected_example_created_at,
                expected_task_updated_at=expected_task_updated_at,
            ),
            request_id=request_id,
            reason=reason,
        ).example.example

    def _actual_record_page(
        self,
        *,
        project_id: str,
        limit: int,
        cursor: _ActualsCursor | None,
    ) -> tuple[tuple[ActualRecordRow, ...], _ActualRecordCursor | None, bool]:
        if cursor is not None and cursor.records_done:
            return (), None, True
        position = cursor.records if cursor is not None else None
        statement = select(ActualRecordRow).where(
            ActualRecordRow.project_id == project_id,
            ActualRecordRow.is_current.is_(True),
        )
        if position is not None:
            statement = statement.where(
                or_(
                    ActualRecordRow.actual_key > position.actual_key,
                    and_(
                        ActualRecordRow.actual_key == position.actual_key,
                        ActualRecordRow.id > position.record_id,
                    ),
                )
            )
        page = tuple(
            self.session.scalars(
                statement.order_by(ActualRecordRow.actual_key, ActualRecordRow.id).limit(limit + 1)
            )
        )
        selected = page[:limit]
        has_more = len(page) > limit
        return (
            selected,
            (
                _ActualRecordCursor(
                    actual_key=selected[-1].actual_key,
                    record_id=selected[-1].id,
                )
                if has_more and selected
                else None
            ),
            not has_more,
        )

    def _variance_record_page(
        self,
        *,
        project_id: str,
        limit: int,
        cursor: _ActualTimelineCursor | None,
        done: bool,
    ) -> tuple[tuple[VarianceRecordRow, ...], _ActualTimelineCursor | None, bool]:
        rows, next_cursor, completed = self._timeline_record_page(
            model=VarianceRecordRow,
            project_id=project_id,
            limit=limit,
            cursor=cursor,
            done=done,
        )
        return tuple(rows), next_cursor, completed

    def _calibration_record_page(
        self,
        *,
        project_id: str,
        limit: int,
        cursor: _ActualTimelineCursor | None,
        done: bool,
    ) -> tuple[tuple[CalibrationExampleRow, ...], _ActualTimelineCursor | None, bool]:
        rows, next_cursor, completed = self._timeline_record_page(
            model=CalibrationExampleRow,
            project_id=project_id,
            limit=limit,
            cursor=cursor,
            done=done,
        )
        return tuple(rows), next_cursor, completed

    def _timeline_record_page(
        self,
        *,
        model: Any,
        project_id: str,
        limit: int,
        cursor: _ActualTimelineCursor | None,
        done: bool,
    ) -> tuple[tuple[Any, ...], _ActualTimelineCursor | None, bool]:
        if done:
            return (), None, True
        statement = select(model).where(model.project_id == project_id)
        if cursor is not None:
            statement = statement.where(
                or_(
                    model.created_at < cursor.occurred_at,
                    and_(
                        model.created_at == cursor.occurred_at,
                        model.id < cursor.record_id,
                    ),
                )
            )
        page = tuple(
            self.session.scalars(
                statement.order_by(model.created_at.desc(), model.id.desc()).limit(limit + 1)
            )
        )
        selected = page[:limit]
        has_more = len(page) > limit
        return (
            selected,
            (
                _ActualTimelineCursor(
                    occurred_at=self._timestamp(selected[-1].created_at),
                    record_id=selected[-1].id,
                )
                if has_more and selected
                else None
            ),
            not has_more,
        )

    @staticmethod
    def _cursor_scope(
        *,
        kind: str,
        project_id: str,
        metric: str,
        policy_version_id: str,
        actual_id: str | None = None,
    ) -> str:
        scope_hash = content_hash(
            {
                "project_id": project_id,
                "metric": metric,
                "policy_version_id": policy_version_id,
                "actual_id": actual_id,
            }
        )
        return f"{kind}:{scope_hash}"

    @staticmethod
    def _encode_actuals_cursor(cursor: _ActualsCursor) -> str:
        raw = json.dumps(
            cursor.model_dump(mode="json"),
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    @staticmethod
    def _decode_actuals_cursor(cursor: str | None, scope: str) -> _ActualsCursor | None:
        if cursor is None:
            return None
        try:
            decoded = _ActualsCursor.model_validate(ActualsService._decode_cursor_json(cursor))
        except ValueError as error:
            raise ValueError("Pagination cursor is invalid") from error
        if decoded.scope != scope:
            raise ValueError("Actuals pagination cursor belongs to another query")
        return decoded

    @staticmethod
    def _encode_timeline_cursor(
        *,
        scope: str,
        occurred_at: datetime,
        record_id: str,
    ) -> str:
        normalized = ensure_utc(occurred_at)
        if normalized is None:
            raise ValueError("Cursor timestamp is missing")
        raw = json.dumps(
            {
                "scope": scope,
                "occurred_at": normalized.isoformat(),
                "record_id": record_id,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    @staticmethod
    def _decode_timeline_cursor(
        cursor: str | None,
        scope: str,
    ) -> _ActualTimelineCursor | None:
        if cursor is None:
            return None
        payload = ActualsService._decode_cursor_json(cursor)
        if payload.get("scope") != scope:
            raise ValueError("Forecast pagination cursor belongs to another query")
        try:
            return _ActualTimelineCursor.model_validate(
                {
                    "occurred_at": payload.get("occurred_at"),
                    "record_id": payload.get("record_id"),
                }
            )
        except ValueError as error:
            raise ValueError("Pagination cursor is invalid") from error

    @staticmethod
    def _decode_cursor_json(cursor: str) -> dict[str, Any]:
        if not cursor or len(cursor) > 2000:
            raise ValueError("Pagination cursor is invalid")
        try:
            padding = "=" * (-len(cursor) % 4)
            raw = base64.b64decode(cursor + padding, altchars=b"-_", validate=True)
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Pagination cursor is invalid") from error
        if not isinstance(payload, dict):
            raise ValueError("Pagination cursor is invalid")
        return payload

    def _policy(
        self,
        project: ProjectRow,
        organization_id: str,
        *,
        expected_version_id: str | None = None,
    ) -> tuple[ActualsPolicyDefinition, ControlledVersionRow]:
        row = require_bound_controlled_version(
            session=self.session,
            settings=self.settings,
            project_id=project.id,
            organization_id=organization_id,
            purpose="actuals_policy",
            kind="actuals_policy",
            expected_version_id=expected_version_id,
        )
        return (
            ActualsPolicyDefinition.model_validate(
                {key: value for key, value in row.payload.items() if key != "_governance"}
            ),
            row,
        )

    def _project_outcome_evidence(
        self,
        project: ProjectRow,
        policy: ActualsPolicyDefinition,
    ) -> tuple[str, ...]:
        if self._unresolved_conflicts(project.id, policy.project_outcome_field_name):
            raise ValueError("Project outcome evidence has an unresolved conflict")
        eligible: list[ObservationRow] = []
        values: set[str] = set()
        for row in self.session.scalars(
            select(ObservationRow)
            .where(
                ObservationRow.project_id == project.id,
                ObservationRow.field_name == policy.project_outcome_field_name,
                ObservationRow.status == VerificationStatus.VERIFIED.value,
            )
            .order_by(ObservationRow.created_at, ObservationRow.id)
        ):
            try:
                observation = self._observation(row)
                self._require_current_healthy_document(project.id, row)
            except (LookupError, TypeError, ValueError):
                continue
            if (
                isinstance(observation.value, str)
                and observation.value in policy.eligible_project_outcomes
            ):
                eligible.append(row)
                values.add(observation.value)
        if not eligible:
            raise ValueError(
                "Actuals require verified evidence that the project is awarded or completed"
            )
        if len(values) != 1:
            raise ValueError("Project outcome evidence is ambiguous")
        return tuple(row.id for row in eligible)

    def _candidate_view(
        self,
        *,
        project: ProjectRow,
        row: ObservationRow,
        definition: ActualMetricDefinition,
        policy: ActualsPolicyDefinition,
    ) -> ActualEvidenceCandidateView:
        blockers: list[str] = []
        try:
            observation = self._observation(row)
        except (TypeError, ValueError):
            observation = Observation.model_validate(row.payload.get("observation"))
            blockers.append("EVIDENCE_INTEGRITY_FAILED")
        value: ActualEvidenceValue | None = None
        try:
            value = ActualEvidenceValue.model_validate(observation.value)
            self._validate_actual_value(
                project,
                value,
                definition,
                expected_field_name=observation.field_name,
            )
        except (TypeError, ValueError):
            blockers.append("ACTUAL_VALUE_INVALID")
        if observation.status is not VerificationStatus.VERIFIED:
            blockers.append("VERIFIED_EVIDENCE_REQUIRED")
        try:
            self._require_current_healthy_document(project.id, row)
        except (LookupError, ValueError):
            blockers.append("SOURCE_DOCUMENT_NOT_CURRENT_OR_HEALTHY")
        if self._unresolved_conflicts(project.id, definition.evidence_field_name):
            blockers.append("UNRESOLVED_EVIDENCE_CONFLICT")
        try:
            leaves = resolve_observation_leaves(
                self.session,
                project_id=project.id,
                observations=(row,),
            )
            for leaf in leaves:
                self._observation(leaf)
                self._require_current_healthy_document(project.id, leaf)
            if definition.metric in policy.independently_verified_metric_keys:
                require_distinct_qualified_independence(
                    self.session,
                    project_id=project.id,
                    observations=(row,),
                )
        except (LookupError, TypeError, ValueError):
            blockers.append("SOURCE_LINEAGE_NOT_QUALIFIED")
        return ActualEvidenceCandidateView(
            observation=observation,
            observation_created_at=self._timestamp(row.created_at),
            evidence_value=value,
            eligible=not blockers,
            blockers=tuple(dict.fromkeys(blockers)),
        )

    def _require_actual_source_integrity(
        self,
        *,
        project: ProjectRow,
        row: ObservationRow,
        definition: ActualMetricDefinition,
        policy: ActualsPolicyDefinition,
    ) -> tuple[ActualEvidenceValue, tuple[str, ...]]:
        observation = self._observation(row)
        self._require_current_healthy_document(project.id, row)
        if observation.status is not VerificationStatus.VERIFIED:
            raise ValueError("Actual source observation must be verified")
        if observation.field_name != definition.evidence_field_name:
            raise ValueError("Actual evidence field differs from the approved policy")
        if self._unresolved_conflicts(project.id, definition.evidence_field_name):
            raise ValueError("Actual evidence has an unresolved conflict")
        value = ActualEvidenceValue.model_validate(observation.value)
        self._validate_actual_value(
            project,
            value,
            definition,
            expected_field_name=observation.field_name,
        )
        leaves = resolve_observation_leaves(
            self.session,
            project_id=project.id,
            observations=(row,),
        )
        for leaf in leaves:
            self._observation(leaf)
            self._require_current_healthy_document(project.id, leaf)
        leaf_ids = tuple(item.id for item in leaves)
        if definition.metric in policy.independently_verified_metric_keys:
            qualified = require_distinct_qualified_independence(
                self.session,
                project_id=project.id,
                observations=(row,),
            )
            if qualified != leaf_ids:
                raise ValueError("Actual evidence independence graph changed")
        return value, leaf_ids

    @staticmethod
    def _validate_actual_value(
        project: ProjectRow,
        value: ActualEvidenceValue,
        definition: ActualMetricDefinition,
        *,
        expected_field_name: str,
    ) -> None:
        if (
            value.metric != definition.metric
            or value.entity_type != definition.entity_type
            or expected_field_name != definition.evidence_field_name
            or value.unit not in definition.allowed_units
            or value.source_class not in definition.allowed_source_classes
        ):
            raise ValueError("Actual value is outside the approved metric definition")
        if value.entity_type == "PROJECT" and value.entity_id != project.id:
            raise ValueError("Project actual evidence identifies another project")

    def _actual_review_view(
        self,
        *,
        actor: Actor,
        project: ProjectRow,
        row: ActualRecordRow,
        policy: ActualsPolicyDefinition,
        version: ControlledVersionRow,
        outcome_ids: tuple[str, ...],
        has_classified_variance: bool,
    ) -> ActualReviewView:
        task = self._actual_task(row)
        if task is None:
            record = self._actual_view(row, missing_task_status="MISSING")
            return ActualReviewView(
                record=record,
                assigned_role=policy.actual_review_role,
                has_classified_variance=has_classified_variance,
                decision_allowed=False,
                decision_blockers=("TASK_MISSING",),
            )
        blockers = self._actual_review_blockers(
            actor=actor,
            project=project,
            row=row,
            task=task,
            policy=policy,
            version=version,
            outcome_ids=outcome_ids,
        )
        return ActualReviewView(
            record=self._actual_view(row),
            assigned_role=policy.actual_review_role,
            has_classified_variance=has_classified_variance,
            decision_allowed=not blockers,
            decision_blockers=tuple(blockers),
        )

    def _actual_review_blockers(
        self,
        *,
        actor: Actor,
        project: ProjectRow,
        row: ActualRecordRow,
        task: ApprovalTaskRow,
        policy: ActualsPolicyDefinition,
        version: ControlledVersionRow,
        outcome_ids: tuple[str, ...],
    ) -> list[str]:
        blockers: list[str] = []
        if not row.is_current:
            blockers.append("ACTUAL_SUPERSEDED")
        if row.verified or row.payload.get("review_status") != "IN_REVIEW":
            blockers.append("ACTUAL_NOT_IN_REVIEW")
        if row.payload.get("created_by") == actor.actor_id:
            blockers.append("FOUR_EYES_ACTUAL_AUTHOR")
        if policy.actual_review_role not in actor.roles:
            blockers.append("ACTUAL_REVIEW_ROLE_REQUIRED")
        if task.payload.get("created_by") == actor.actor_id:
            blockers.append("FOUR_EYES_TASK_CREATOR")
        if task.status != "PENDING":
            blockers.append("TASK_NOT_PENDING")
        try:
            definition = policy.metric(row.metric)
            value, leaf_ids = self._require_actual_source_integrity(
                project=project,
                row=self._observation_row(project.id, row.source_observation_id),
                definition=definition,
                policy=policy,
            )
            if (
                self._row_value(row) != value
                or tuple(row.payload.get("source_leaf_ids", [])) != leaf_ids
                or tuple(row.payload.get("project_outcome_evidence_ids", [])) != outcome_ids
                or row.payload.get("actuals_policy_version_id") != version.id
                or row.payload.get("actuals_policy_content_hash") != version.content_hash
                or task.payload
                != self._actual_task_payload(
                    row=row,
                    policy=policy,
                    version=version,
                    outcome_ids=outcome_ids,
                )
                or task.task_type != "ACTUAL_FACT_REVIEW"
                or task.entity_type != "actual_record"
                or task.entity_id != row.id
                or task.project_id != row.project_id
                or task.assigned_role != policy.actual_review_role.value
                or not task.required
            ):
                blockers.append("ACTUAL_INTEGRITY_FAILED")
        except (KeyError, LookupError, TypeError, ValueError):
            blockers.append("ACTUAL_INTEGRITY_FAILED")
        return list(dict.fromkeys(blockers))

    def _require_verified_actual_integrity(
        self,
        *,
        row: ActualRecordRow,
        project: ProjectRow,
        policy: ActualsPolicyDefinition,
        version: ControlledVersionRow,
        outcome_ids: tuple[str, ...],
    ) -> ActualFact:
        created_by = row.payload.get("created_by")
        verified_by = row.payload.get("verified_by")
        if (
            not row.is_current
            or not row.verified
            or row.payload.get("review_status") != VerificationStatus.VERIFIED.value
            or row.payload.get("review_decision") != ApprovalDecision.APPROVED.value
            or not isinstance(created_by, str)
            or not isinstance(verified_by, str)
            or created_by == verified_by
            or row.payload.get("reviewed_by") != verified_by
            or row.payload.get("actuals_policy_version_id") != version.id
            or row.payload.get("actuals_policy_content_hash") != version.content_hash
            or tuple(row.payload.get("project_outcome_evidence_ids", [])) != outcome_ids
        ):
            raise ValueError("Verified actual provenance is invalid")
        definition = policy.metric(row.metric)
        value, leaf_ids = self._require_actual_source_integrity(
            project=project,
            row=self._observation_row(project.id, row.source_observation_id),
            definition=definition,
            policy=policy,
        )
        if (
            self._row_value(row) != value
            or tuple(row.payload.get("source_leaf_ids", [])) != leaf_ids
        ):
            raise ValueError("Verified actual no longer reproduces its evidence")
        task = self._actual_task(row)
        expected_task_payload = self._actual_task_payload(
            row=row,
            policy=policy,
            version=version,
            outcome_ids=outcome_ids,
        )
        if (
            task is None
            or task.status != "APPROVED"
            or task.payload != expected_task_payload
            or task.assigned_role != policy.actual_review_role.value
        ):
            raise ValueError("Actual approval task integrity failed")
        approval = self.session.scalar(
            select(ApprovalRecordRow).where(ApprovalRecordRow.task_id == task.id)
        )
        if (
            approval is None
            or approval.decision != "APPROVED"
            or approval.decided_by != verified_by
            or approval.payload.get("actual_id") != row.id
            or approval.payload.get("actual_key") != row.actual_key
            or approval.payload.get("source_observation_id") != row.source_observation_id
            or approval.payload.get("source_leaf_ids") != list(leaf_ids)
            or approval.payload.get("project_outcome_evidence_ids") != list(outcome_ids)
            or approval.payload.get("actuals_policy_version_id") != version.id
            or approval.payload.get("actuals_policy_content_hash") != version.content_hash
            or approval.payload.get("actual_submission_hash")
            != expected_task_payload["actual_submission_hash"]
        ):
            raise ValueError("Actual approval record integrity failed")
        event = self._decision_event(
            project.id,
            "actual_fact_verified",
            "actual_id",
            row.id,
        )
        if (
            event is None
            or event.actor_id != verified_by
            or event.payload.get("approval_id") != approval.id
            or event.payload.get("approval_task_id") != task.id
            or event.payload.get("decision") != "APPROVED"
            or event.payload.get("actual_submission_hash")
            != expected_task_payload["actual_submission_hash"]
        ):
            raise ValueError("Actual approval audit event integrity failed")
        return self._actual_fact(row)

    def _replay_forecast(
        self,
        project: ProjectRow,
        actual_row: ActualRecordRow,
        policy: ActualsPolicyDefinition,
        expected: ForecastFact,
        *,
        expected_release_decision_id: str,
    ) -> ForecastCandidateView:
        release = self.session.scalar(
            select(ReleaseDecisionRow).where(
                ReleaseDecisionRow.id == expected_release_decision_id,
                ReleaseDecisionRow.project_id == project.id,
                ReleaseDecisionRow.snapshot_id == expected.snapshot_id,
                ReleaseDecisionRow.allowed.is_(True),
                ReleaseDecisionRow.requested_state == ApprovalState.APPROVED_FOR_BID.value,
                ReleaseDecisionRow.resulting_state == ApprovalState.APPROVED_FOR_BID.value,
            )
        )
        if release is None:
            raise ValueError("Forecast snapshot has no current allowed bid release")
        replayed = self._forecast_from_snapshot(
            project,
            actual_row,
            policy,
            expected.snapshot_id,
            release.id,
        )
        if replayed.forecast != expected:
            raise ValueError("Forecast no longer reproduces the classified basis")
        return replayed

    def _forecast_from_snapshot(
        self,
        project: ProjectRow,
        actual_row: ActualRecordRow,
        policy: ActualsPolicyDefinition,
        snapshot_id: str,
        release_id: str,
        *,
        snapshot_payload_cache: dict[str, dict[str, Any]] | None = None,
    ) -> ForecastCandidateView:
        snapshot = self.session.scalar(
            select(CalculationSnapshotRow).where(
                CalculationSnapshotRow.id == snapshot_id,
                CalculationSnapshotRow.project_id == project.id,
                CalculationSnapshotRow.fixed.is_(True),
            )
        )
        if snapshot is None:
            raise LookupError(snapshot_id)
        payload = (
            snapshot_payload_cache.get(snapshot.id) if snapshot_payload_cache is not None else None
        )
        if payload is None:
            payload = read_verified_snapshot(
                object_store=self.object_store,
                snapshot=snapshot,
            )
            if snapshot_payload_cache is not None:
                snapshot_payload_cache[snapshot.id] = payload
        inputs = tuple(AtomicCostInput.model_validate(item) for item in payload["inputs"])
        calculation_policy = CalculationPolicy.model_validate(payload["policy"])
        primary = CalculationResult.model_validate(payload["primary"])
        independent = IndependentValidationResult.model_validate(payload["independent"])
        run = self.session.get(CalculationRunRow, snapshot.calculation_run_id)
        if (
            run is None
            or run.project_id != project.id
            or run.status != "VALIDATED"
            or run.grand_total != primary.grand_total
            or run.currency != primary.currency
            or not independent.passed
            or independent.primary_total != primary.grand_total
            or independent.independently_calculated_total != primary.grand_total
        ):
            raise ValueError("Released forecast calculation integrity failed")
        definition = policy.metric(actual_row.metric)
        cost_row: CostInputRow | None = None
        basis = definition.forecast_basis
        if basis is ForecastBasis.PROJECT_COST_TOTAL:
            value = primary.grand_total
            unit = primary.currency
            if actual_row.entity_id != project.id:
                raise ValueError("Project-total actual identifies another project")
        else:
            cost_row = self.session.scalar(
                select(CostInputRow).where(
                    CostInputRow.id == actual_row.entity_id,
                    CostInputRow.project_id == project.id,
                    CostInputRow.calculation_run_id == snapshot.calculation_run_id,
                )
            )
            if cost_row is None:
                raise ValueError("Actual cost input is absent from released snapshot")
            stored_input = AtomicCostInput.model_validate(cost_row.payload)
            matching = tuple(
                item for item in inputs if item.cost_input_id == stored_input.cost_input_id
            )
            if len(matching) != 1 or matching[0] != stored_input:
                raise ValueError("Released atomic input does not reproduce its row")
            atomic = matching[0]
            if basis is ForecastBasis.ATOMIC_QUANTITY:
                value = atomic.quantity
                unit = atomic.unit
            elif basis is ForecastBasis.ATOMIC_UNIT_RATE:
                value = atomic.unit_rate
                unit = f"{atomic.currency}/{atomic.unit}"
            else:
                single = calculate_primary(
                    (atomic,),
                    calculation_policy,
                    engine_version=run.engine_version,
                    calculated_at=primary.calculated_at,
                )
                value = single.grand_total
                unit = atomic.currency
        if unit != actual_row.unit:
            raise ValueError("Released forecast and actual units differ")
        identity = {
            "snapshot_id": snapshot.id,
            "snapshot_hash": snapshot.snapshot_hash,
            "actual_id": actual_row.id,
            "entity_id": actual_row.entity_id,
            "metric": actual_row.metric,
            "forecast_basis": basis.value,
            "value": value,
            "unit": unit,
        }
        forecast = ForecastFact(
            forecast_id=f"forecast-{content_hash(identity)[:24]}",
            project_id=project.id,
            entity_id=actual_row.entity_id,
            metric=actual_row.metric,
            value=value,
            unit=unit,
            snapshot_id=snapshot.id,
            snapshot_hash=snapshot.snapshot_hash,
            cost_input_row_id=cost_row.id if cost_row is not None else None,
            forecast_basis=basis,
        )
        return ForecastCandidateView(
            actual_id=actual_row.id,
            forecast=forecast,
            released_by_decision_id=release_id,
        )

    def _variance_review_blockers(
        self,
        *,
        actor: Actor,
        project: ProjectRow,
        row: VarianceRecordRow,
        task: ApprovalTaskRow,
        policy: ActualsPolicyDefinition,
        version: ControlledVersionRow,
    ) -> list[str]:
        blockers: list[str] = []
        variance = VarianceRecord.model_validate(row.payload.get("variance"))
        if variance.status is not VerificationStatus.IN_REVIEW:
            blockers.append("VARIANCE_NOT_IN_REVIEW")
        if row.classified_by == actor.actor_id:
            blockers.append("FOUR_EYES_VARIANCE_CLASSIFIER")
        if task.payload.get("created_by") == actor.actor_id:
            blockers.append("FOUR_EYES_TASK_CREATOR")
        if policy.variance_review_role not in actor.roles:
            blockers.append("VARIANCE_REVIEW_ROLE_REQUIRED")
        if task.status != "PENDING":
            blockers.append("TASK_NOT_PENDING")
        try:
            actual = self._current_actual(project.id, row.actual_record_id)
            outcome_ids = self._project_outcome_evidence(project, policy)
            actual_fact = self._require_verified_actual_integrity(
                row=actual,
                project=project,
                policy=policy,
                version=version,
                outcome_ids=outcome_ids,
            )
            forecast = self._replay_forecast(
                project,
                actual,
                policy,
                ForecastFact.model_validate(row.payload.get("forecast")),
                expected_release_decision_id=self._required_string(
                    row.payload,
                    "released_by_decision_id",
                ),
            )
            replayed = compare_forecast_to_actual(
                forecast.forecast,
                actual_fact,
                reason=variance.reason,
                reason_detail=variance.reason_detail,
                classified_by=variance.classified_by,
                relative_scale=policy.relative_variance_scale,
                relative_rounding=policy.relative_variance_rounding_mode,
            )
            review_state_valid = (
                variance.status is VerificationStatus.IN_REVIEW
                and variance.reviewed_by is None
                and row.payload.get("reviewed_by") is None
                and row.payload.get("review_decision") is None
            ) or (
                variance.status
                in {
                    VerificationStatus.VERIFIED,
                    VerificationStatus.REJECTED,
                }
                and isinstance(variance.reviewed_by, str)
                and variance.reviewed_by
                and variance.reviewed_by == row.payload.get("reviewed_by")
                and row.payload.get("review_decision")
                == (
                    ApprovalDecision.APPROVED.value
                    if variance.status is VerificationStatus.VERIFIED
                    else ApprovalDecision.REJECTED.value
                )
            )
            replayed_with_review_state = replayed.model_copy(
                update={
                    "status": variance.status,
                    "reviewed_by": variance.reviewed_by,
                }
            )
            if (
                not review_state_valid
                or replayed_with_review_state != variance
                or row.payload.get("actual_content_hash") != self._actual_content_hash(actual)
                or row.payload.get("actuals_policy_version_id") != version.id
                or row.payload.get("actuals_policy_content_hash") != version.content_hash
                or task.payload
                != self._variance_task_payload(
                    variance_row=row,
                    policy=policy,
                    version=version,
                )
            ):
                blockers.append("VARIANCE_INTEGRITY_FAILED")
        except (KeyError, LookupError, RuntimeError, TypeError, ValueError):
            blockers.append("VARIANCE_INTEGRITY_FAILED")
        if (
            task.task_type != "VARIANCE_CLASSIFICATION_REVIEW"
            or task.entity_type != "variance_record"
            or task.entity_id != row.id
            or task.project_id != row.project_id
            or task.assigned_role != policy.variance_review_role.value
            or not task.required
        ):
            blockers.append("TASK_INTEGRITY_FAILED")
        return list(dict.fromkeys(blockers))

    def _calibration_review_blockers(
        self,
        *,
        actor: Actor,
        project: ProjectRow,
        row: CalibrationExampleRow,
        task: ApprovalTaskRow,
        policy: ActualsPolicyDefinition,
        version: ControlledVersionRow,
    ) -> list[str]:
        blockers: list[str] = []
        if row.approved or row.payload.get("review_status") != "IN_REVIEW":
            blockers.append("CALIBRATION_NOT_IN_REVIEW")
        excluded_actors = {
            row.payload.get("created_by"),
        }
        actual = self.session.get(ActualRecordRow, row.actual_record_id)
        variance_row = self.session.get(VarianceRecordRow, row.variance_record_id)
        if actual is not None:
            excluded_actors.update(
                {
                    actual.payload.get("created_by"),
                    actual.payload.get("verified_by"),
                }
            )
        if variance_row is not None:
            excluded_actors.update(
                {
                    variance_row.classified_by,
                    variance_row.payload.get("reviewed_by"),
                }
            )
        if actor.actor_id in excluded_actors:
            blockers.append("CALIBRATION_FOUR_EYES_REQUIRED")
        if task.payload.get("created_by") == actor.actor_id:
            blockers.append("FOUR_EYES_TASK_CREATOR")
        if policy.calibration_approval_role not in actor.roles:
            blockers.append("METHODOLOGY_OWNER_REQUIRED")
        if task.status != "PENDING":
            blockers.append("TASK_NOT_PENDING")
        try:
            if actual is None or variance_row is None:
                raise ValueError("Calibration source is missing")
            outcome_ids = self._project_outcome_evidence(project, policy)
            actual_fact = self._require_verified_actual_integrity(
                row=actual,
                project=project,
                policy=policy,
                version=version,
                outcome_ids=outcome_ids,
            )
            variance = self._require_verified_variance_integrity(
                project=project,
                row=variance_row,
                actual_row=actual,
                actual=actual_fact,
                policy=policy,
                version=version,
            )
            forecast = self._replay_forecast(
                project,
                actual,
                policy,
                ForecastFact.model_validate(row.payload.get("forecast")),
                expected_release_decision_id=self._required_string(
                    row.payload,
                    "released_by_decision_id",
                ),
            )
            expected = build_calibration_example(
                forecast.forecast,
                actual_fact,
                variance,
            ).model_copy(update={"example_id": row.id})
            if (
                CalibrationExample.model_validate(row.payload.get("calibration_example"))
                != expected
                or row.actual_record_id != actual.id
                or row.variance_record_id != variance_row.id
                or row.features_snapshot_id != expected.features_snapshot_id
                or row.metric != expected.metric
                or row.target_value != expected.target_value
                or row.unit != expected.unit
                or row.payload.get("actual_content_hash") != self._actual_content_hash(actual)
                or row.payload.get("variance_content_hash")
                != self._variance_content_hash(variance_row)
                or row.payload.get("released_by_decision_id")
                != variance_row.payload.get("released_by_decision_id")
                or row.payload.get("actuals_policy_version_id") != version.id
                or row.payload.get("actuals_policy_content_hash") != version.content_hash
                or task.payload
                != self._calibration_task_payload(
                    row=row,
                    policy=policy,
                    version=version,
                )
            ):
                raise ValueError("Calibration example does not reproduce")
        except (KeyError, LookupError, RuntimeError, TypeError, ValueError):
            blockers.append("CALIBRATION_INTEGRITY_FAILED")
        if (
            task.task_type != "CALIBRATION_EXAMPLE_REVIEW"
            or task.entity_type != "calibration_example"
            or task.entity_id != row.id
            or task.project_id != row.project_id
            or task.assigned_role != policy.calibration_approval_role.value
            or not task.required
        ):
            blockers.append("TASK_INTEGRITY_FAILED")
        return list(dict.fromkeys(blockers))

    def _require_verified_variance_integrity(
        self,
        *,
        project: ProjectRow,
        row: VarianceRecordRow,
        actual_row: ActualRecordRow,
        actual: ActualFact,
        policy: ActualsPolicyDefinition,
        version: ControlledVersionRow,
    ) -> VarianceRecord:
        variance = VarianceRecord.model_validate(row.payload.get("variance"))
        reviewer = row.payload.get("reviewed_by")
        classifier = row.classified_by
        if (
            variance.status is not VerificationStatus.VERIFIED
            or not isinstance(reviewer, str)
            or not reviewer
            or reviewer == classifier
            or variance.reviewed_by != reviewer
            or row.payload.get("review_decision") != "APPROVED"
            or row.payload.get("actuals_policy_version_id") != version.id
            or row.payload.get("actuals_policy_content_hash") != version.content_hash
            or row.payload.get("actual_content_hash") != self._actual_content_hash(actual_row)
        ):
            raise ValueError("Verified variance provenance is invalid")
        forecast = self._replay_forecast(
            project,
            actual_row,
            policy,
            ForecastFact.model_validate(row.payload.get("forecast")),
            expected_release_decision_id=self._required_string(
                row.payload,
                "released_by_decision_id",
            ),
        )
        replayed = compare_forecast_to_actual(
            forecast.forecast,
            actual,
            reason=variance.reason,
            reason_detail=variance.reason_detail,
            classified_by=variance.classified_by,
            relative_scale=policy.relative_variance_scale,
            relative_rounding=policy.relative_variance_rounding_mode,
        )
        if (
            replayed.absolute_variance != variance.absolute_variance
            or replayed.relative_variance != variance.relative_variance
        ):
            raise ValueError("Verified variance arithmetic differs")
        task = self._variance_task(row)
        expected_task = self._variance_task_payload(
            variance_row=row,
            policy=policy,
            version=version,
        )
        if task is None or task.status != "APPROVED" or task.payload != expected_task:
            raise ValueError("Variance approval task integrity failed")
        approval = self.session.scalar(
            select(ApprovalRecordRow).where(ApprovalRecordRow.task_id == task.id)
        )
        if (
            approval is None
            or approval.decision != "APPROVED"
            or approval.decided_by != reviewer
            or approval.payload.get("variance_record_id") != row.id
            or approval.payload.get("released_by_decision_id")
            != self._required_string(row.payload, "released_by_decision_id")
            or approval.payload.get("variance_submission_hash")
            != expected_task["variance_submission_hash"]
            or approval.payload.get("actuals_policy_version_id") != version.id
            or approval.payload.get("actuals_policy_content_hash") != version.content_hash
        ):
            raise ValueError("Variance approval record integrity failed")
        event = self._decision_event(
            project.id,
            "variance_review_decided",
            "variance_record_id",
            row.id,
        )
        if (
            event is None
            or event.actor_id != reviewer
            or event.payload.get("approval_id") != approval.id
            or event.payload.get("decision") != "APPROVED"
        ):
            raise ValueError("Variance approval audit event integrity failed")
        return variance

    def _actual_task_payload(
        self,
        *,
        row: ActualRecordRow,
        policy: ActualsPolicyDefinition,
        version: ControlledVersionRow,
        outcome_ids: tuple[str, ...],
    ) -> dict[str, Any]:
        submission = {
            "actual_id": row.id,
            "actual_key": row.actual_key,
            "evidence_value": row.payload.get("evidence_value"),
            "source_observation_id": row.source_observation_id,
            "source_leaf_ids": row.payload.get("source_leaf_ids", []),
            "project_outcome_evidence_ids": list(outcome_ids),
            "created_by": row.payload.get("created_by"),
            "actuals_policy_version_id": version.id,
            "actuals_policy_content_hash": version.content_hash,
            "review_role": policy.actual_review_role.value,
        }
        return {
            "created_by": row.payload.get("created_by"),
            "actual_id": row.id,
            "actual_key": row.actual_key,
            "actual_submission_hash": content_hash(submission),
            "source_observation_id": row.source_observation_id,
            "source_leaf_ids": list(row.payload.get("source_leaf_ids", [])),
            "project_outcome_evidence_ids": list(outcome_ids),
            "actuals_policy_version_id": version.id,
            "actuals_policy_content_hash": version.content_hash,
            "review_role": policy.actual_review_role.value,
        }

    @staticmethod
    def _variance_task_payload(
        *,
        variance_row: VarianceRecordRow,
        policy: ActualsPolicyDefinition,
        version: ControlledVersionRow,
    ) -> dict[str, Any]:
        variance = VarianceRecord.model_validate(variance_row.payload.get("variance")).model_copy(
            update={
                "status": VerificationStatus.IN_REVIEW,
                "reviewed_by": None,
            }
        )
        submission = {
            "variance_record_id": variance_row.id,
            "actual_record_id": variance_row.actual_record_id,
            "snapshot_id": variance_row.snapshot_id,
            "variance": variance.model_dump(mode="json"),
            "forecast": variance_row.payload.get("forecast"),
            "released_by_decision_id": variance_row.payload.get("released_by_decision_id"),
            "actual_content_hash": variance_row.payload.get("actual_content_hash"),
            "classified_by": variance_row.classified_by,
            "actuals_policy_version_id": version.id,
            "actuals_policy_content_hash": version.content_hash,
            "review_role": policy.variance_review_role.value,
        }
        return {
            "created_by": variance_row.classified_by,
            "variance_record_id": variance_row.id,
            "actual_record_id": variance_row.actual_record_id,
            "released_by_decision_id": variance_row.payload.get("released_by_decision_id"),
            "variance_submission_hash": content_hash(submission),
            "actuals_policy_version_id": version.id,
            "actuals_policy_content_hash": version.content_hash,
            "review_role": policy.variance_review_role.value,
        }

    @staticmethod
    def _calibration_task_payload(
        *,
        row: CalibrationExampleRow,
        policy: ActualsPolicyDefinition,
        version: ControlledVersionRow,
    ) -> dict[str, Any]:
        submission = {
            "calibration_example_id": row.id,
            "actual_record_id": row.actual_record_id,
            "variance_record_id": row.variance_record_id,
            "features_snapshot_id": row.features_snapshot_id,
            "calibration_example": row.payload.get("calibration_example"),
            "actual_content_hash": row.payload.get("actual_content_hash"),
            "variance_content_hash": row.payload.get("variance_content_hash"),
            "forecast": row.payload.get("forecast"),
            "released_by_decision_id": row.payload.get("released_by_decision_id"),
            "created_by": row.payload.get("created_by"),
            "actuals_policy_version_id": version.id,
            "actuals_policy_content_hash": version.content_hash,
            "approval_role": policy.calibration_approval_role.value,
        }
        return {
            "created_by": row.payload.get("created_by"),
            "calibration_example_id": row.id,
            "actual_record_id": row.actual_record_id,
            "variance_record_id": row.variance_record_id,
            "released_by_decision_id": row.payload.get("released_by_decision_id"),
            "calibration_submission_hash": content_hash(submission),
            "actuals_policy_version_id": version.id,
            "actuals_policy_content_hash": version.content_hash,
            "approval_role": policy.calibration_approval_role.value,
        }

    def _new_task(
        self,
        *,
        project_id: str,
        task_type: str,
        entity_type: str,
        entity_id: str,
        assigned_role: ActorRole,
        payload: dict[str, Any],
        created_by: str,
        now: datetime,
    ) -> ApprovalTaskRow:
        task_id = (
            "approval-task-actuals-"
            + content_hash(
                {
                    "task_type": task_type,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                }
            )[:24]
        )
        if self.session.get(ApprovalTaskRow, task_id) is not None:
            raise RuntimeError("Actuals approval task identifier collision")
        task = ApprovalTaskRow(
            id=task_id,
            project_id=project_id,
            task_type=task_type,
            entity_type=entity_type,
            entity_id=entity_id,
            assigned_role=assigned_role.value,
            status="PENDING",
            required=True,
            payload={**payload, "created_by": created_by},
            created_at=now,
            updated_at=now,
        )
        self.session.add(task)
        return task

    def _actual_task(
        self,
        row: ActualRecordRow,
        *,
        lock: bool = False,
    ) -> ApprovalTaskRow | None:
        return self._task(
            project_id=row.project_id,
            entity_type="actual_record",
            entity_id=row.id,
            task_id=row.payload.get("approval_task_id"),
            lock=lock,
        )

    def _variance_task(
        self,
        row: VarianceRecordRow,
        *,
        lock: bool = False,
    ) -> ApprovalTaskRow | None:
        return self._task(
            project_id=row.project_id,
            entity_type="variance_record",
            entity_id=row.id,
            task_id=row.payload.get("approval_task_id"),
            lock=lock,
        )

    def _calibration_task(
        self,
        row: CalibrationExampleRow,
        *,
        lock: bool = False,
    ) -> ApprovalTaskRow | None:
        return self._task(
            project_id=row.project_id,
            entity_type="calibration_example",
            entity_id=row.id,
            task_id=row.payload.get("approval_task_id"),
            lock=lock,
        )

    def _task(
        self,
        *,
        project_id: str,
        entity_type: str,
        entity_id: str,
        task_id: object,
        lock: bool,
    ) -> ApprovalTaskRow | None:
        if not isinstance(task_id, str) or not task_id:
            return None
        statement = select(ApprovalTaskRow).where(
            ApprovalTaskRow.id == task_id,
            ApprovalTaskRow.project_id == project_id,
            ApprovalTaskRow.entity_type == entity_type,
            ApprovalTaskRow.entity_id == entity_id,
        )
        if lock:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def _variance_view(
        self,
        *,
        actor: Actor,
        project: ProjectRow,
        row: VarianceRecordRow,
        policy: ActualsPolicyDefinition,
        version: ControlledVersionRow,
    ) -> VarianceView:
        task = self._variance_task(row)
        policy_id = self._required_string(
            row.payload,
            "actuals_policy_version_id",
        )
        policy_hash = self._required_string(
            row.payload,
            "actuals_policy_content_hash",
        )
        blockers = (
            ["TASK_MISSING"]
            if task is None
            else self._variance_review_blockers(
                actor=actor,
                project=project,
                row=row,
                task=task,
                policy=policy,
                version=version,
            )
        )
        return VarianceView(
            variance_record_id=row.id,
            variance=VarianceRecord.model_validate(row.payload.get("variance")),
            forecast=ForecastFact.model_validate(row.payload.get("forecast")),
            policy_version_id=policy_id,
            policy_content_hash=policy_hash,
            approval_task_id=(
                task.id
                if task is not None
                else self._required_string(row.payload, "approval_task_id")
            ),
            task_status=task.status if task is not None else "MISSING",
            task_updated_at=self._timestamp(
                task.updated_at if task is not None else row.created_at
            ),
            created_at=self._timestamp(row.created_at),
            assigned_role=policy.variance_review_role,
            decision_allowed=not blockers,
            decision_blockers=tuple(blockers),
        )

    def _calibration_view(
        self,
        *,
        actor: Actor,
        project: ProjectRow,
        row: CalibrationExampleRow,
        policy: ActualsPolicyDefinition,
        version: ControlledVersionRow,
    ) -> CalibrationExampleView:
        task = self._calibration_task(row)
        blockers = (
            ["TASK_MISSING"]
            if task is None
            else self._calibration_review_blockers(
                actor=actor,
                project=project,
                row=row,
                task=task,
                policy=policy,
                version=version,
            )
        )
        return CalibrationExampleView(
            example=CalibrationExample.model_validate(row.payload.get("calibration_example")),
            approved=row.approved,
            approval_task_id=(
                task.id
                if task is not None
                else self._required_string(row.payload, "approval_task_id")
            ),
            task_status=task.status if task is not None else "MISSING",
            task_updated_at=self._timestamp(
                task.updated_at if task is not None else row.created_at
            ),
            policy_version_id=self._required_string(
                row.payload,
                "actuals_policy_version_id",
            ),
            policy_content_hash=self._required_string(
                row.payload,
                "actuals_policy_content_hash",
            ),
            created_at=self._timestamp(row.created_at),
            approved_by=row.payload.get("approved_by"),
            assigned_role=policy.calibration_approval_role,
            decision_allowed=not blockers,
            decision_blockers=tuple(blockers),
        )

    def _actual_view(
        self,
        row: ActualRecordRow,
        *,
        missing_task_status: str | None = None,
    ) -> ActualRecordView:
        task = self._actual_task(row)
        if task is None and missing_task_status is None:
            raise ValueError("Actual approval task is missing")
        return ActualRecordView(
            actual=self._actual_fact(row),
            actual_key=row.actual_key,
            supersedes_actual_id=row.supersedes_actual_id,
            is_current=row.is_current,
            created_by=self._required_string(row.payload, "created_by"),
            policy_version_id=self._required_string(
                row.payload,
                "actuals_policy_version_id",
            ),
            policy_content_hash=self._required_string(
                row.payload,
                "actuals_policy_content_hash",
            ),
            source_leaf_ids=tuple(row.payload.get("source_leaf_ids", [])),
            project_outcome_evidence_ids=tuple(row.payload.get("project_outcome_evidence_ids", [])),
            approval_task_id=(
                task.id
                if task is not None
                else self._required_string(row.payload, "approval_task_id")
            ),
            task_status=task.status if task is not None else str(missing_task_status),
            task_updated_at=(
                self._timestamp(task.updated_at)
                if task is not None
                else self._timestamp(row.created_at)
            ),
            created_at=self._timestamp(row.created_at),
        )

    @staticmethod
    def _actual_fact(row: ActualRecordRow) -> ActualFact:
        value = ActualEvidenceValue.model_validate(row.payload.get("evidence_value"))
        status = VerificationStatus(
            row.payload.get("review_status", VerificationStatus.IN_REVIEW.value)
        )
        return ActualFact(
            actual_id=row.id,
            project_id=row.project_id,
            entity_id=row.entity_id,
            metric=row.metric,
            value=row.value,
            unit=row.unit,
            occurred_on=row.occurred_on,
            source_observation_id=row.source_observation_id,
            verified=row.verified,
            verified_by=row.payload.get("verified_by"),
            actual_key=row.actual_key,
            source_class=value.source_class,
            status=status,
        )

    @staticmethod
    def _row_value(row: ActualRecordRow) -> ActualEvidenceValue:
        value = ActualEvidenceValue.model_validate(row.payload.get("evidence_value"))
        if (
            value.actual_key != row.actual_key
            or value.entity_type != row.entity_type
            or value.entity_id != row.entity_id
            or value.metric != row.metric
            or value.value != row.value
            or value.unit != row.unit
            or value.occurred_on != row.occurred_on
        ):
            raise ValueError("Actual row does not reproduce its evidence value")
        return value

    @staticmethod
    def _actual_content_hash(row: ActualRecordRow) -> str:
        return content_hash(
            {
                "id": row.id,
                "project_id": row.project_id,
                "actual_key": row.actual_key,
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "metric": row.metric,
                "value": row.value,
                "unit": row.unit,
                "source_observation_id": row.source_observation_id,
                "occurred_on": row.occurred_on,
                "payload": row.payload,
                "supersedes_actual_id": row.supersedes_actual_id,
                "is_current": row.is_current,
                "created_at": ActualsService._timestamp(row.created_at),
            }
        )

    @staticmethod
    def _variance_content_hash(row: VarianceRecordRow) -> str:
        return content_hash(
            {
                "id": row.id,
                "project_id": row.project_id,
                "actual_record_id": row.actual_record_id,
                "snapshot_id": row.snapshot_id,
                "metric": row.metric,
                "reason": row.reason,
                "absolute_variance": row.absolute_variance,
                "relative_variance": row.relative_variance,
                "payload": row.payload,
                "classified_by": row.classified_by,
                "created_at": ActualsService._timestamp(row.created_at),
            }
        )

    def _observation_row(
        self,
        project_id: str,
        observation_id: str,
        *,
        lock: bool = False,
    ) -> ObservationRow:
        statement = select(ObservationRow).where(
            ObservationRow.id == observation_id,
            ObservationRow.project_id == project_id,
        )
        if lock:
            statement = statement.with_for_update()
        row = self.session.scalar(statement)
        if row is None:
            raise LookupError(observation_id)
        return row

    @staticmethod
    def _observation(row: ObservationRow) -> Observation:
        observation = Observation.model_validate(row.payload.get("observation"))
        if (
            row.id != observation.observation_id
            or row.document_revision_id != observation.location.document_revision_id
            or row.field_name != observation.field_name
            or row.method != observation.method.value
            or row.method_version != observation.method_version
            or row.status != observation.status.value
        ):
            raise ValueError("Actual evidence row does not reproduce its payload")
        return observation

    def _require_current_healthy_document(
        self,
        project_id: str,
        observation: ObservationRow,
    ) -> None:
        row = self.session.scalar(
            select(DocumentRevisionRow)
            .join(DocumentRow, DocumentRow.id == DocumentRevisionRow.document_id)
            .where(
                DocumentRevisionRow.id == observation.document_revision_id,
                DocumentRow.project_id == project_id,
                DocumentRow.cancelled.is_(False),
                DocumentRevisionRow.is_current.is_(True),
                DocumentRevisionRow.corrupt.is_(False),
                DocumentRevisionRow.protected.is_(False),
            )
        )
        if row is None:
            raise ValueError("Actual evidence document is not current and healthy")
        parsed = self._observation(observation)
        if parsed.location.original_object_hash != row.object_hash:
            raise ValueError("Actual evidence object hash differs from its document")

    def _current_actual(
        self,
        project_id: str,
        actual_id: str,
        *,
        lock: bool = False,
    ) -> ActualRecordRow:
        statement = select(ActualRecordRow).where(
            ActualRecordRow.id == actual_id,
            ActualRecordRow.project_id == project_id,
            ActualRecordRow.is_current.is_(True),
        )
        if lock:
            statement = statement.with_for_update()
        row = self.session.scalar(statement)
        if row is None:
            raise LookupError(actual_id)
        return row

    def _unresolved_conflicts(
        self,
        project_id: str,
        field_name: str,
    ) -> tuple[ConflictRow, ...]:
        return tuple(
            self.session.scalars(
                select(ConflictRow).where(
                    ConflictRow.project_id == project_id,
                    ConflictRow.field_name == field_name,
                    ConflictRow.status != VerificationStatus.VERIFIED.value,
                )
            )
        )

    def _decision_event(
        self,
        project_id: str,
        event_type: str,
        entity_key: str,
        entity_id: str,
    ) -> AuditEventRow | None:
        return next(
            (
                event
                for event in self.session.scalars(
                    select(AuditEventRow)
                    .where(
                        AuditEventRow.aggregate_type == "project",
                        AuditEventRow.aggregate_id == project_id,
                        AuditEventRow.event_type == event_type,
                    )
                    .order_by(AuditEventRow.sequence.desc())
                )
                if event.payload.get(entity_key) == entity_id
            ),
            None,
        )

    def _require_post_bid_project(
        self,
        actor: Actor,
        project_id: str,
        *,
        required_roles: tuple[ActorRole, ...],
        lock: bool,
    ) -> ProjectRow:
        project = self._project_service().get_project(
            actor=actor,
            project_id=project_id,
            lock=lock,
            required_roles=required_roles,
        )
        if ApprovalState(project.state) not in _POST_BID_STATES:
            raise ValueError("Actual facts require a released or archived project")
        return project

    def _audit(
        self,
        project_id: str,
        actor: Actor,
        request_id: str,
        reason: str,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        self._project_service().record_event(
            aggregate_type="project",
            aggregate_id=project_id,
            event_type=event_type,
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload=payload,
        )

    @staticmethod
    def _required_string(payload: dict[str, Any], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"Actuals provenance field is missing: {key}")
        return value

    @staticmethod
    def _reason(reason: str) -> str:
        normalized = reason.strip()
        if not normalized or len(normalized) > 2000:
            raise ValueError("Actuals workflow reason must contain 1 to 2000 characters")
        return normalized

    @staticmethod
    def _timestamp(value: datetime) -> datetime:
        normalized = ensure_utc(value)
        if normalized is None:
            raise ValueError("Actuals workflow timestamp is missing")
        return normalized

    def _project_service(self) -> ProjectService:
        return ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )
