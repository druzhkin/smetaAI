from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tenderguard.application.outbox import (
    OutboxDeliveryPolicy,
    OutboxDeliveryService,
)
from tenderguard.application.projects import ProjectService, SystemProjectAccess
from tenderguard.config import Settings
from tenderguard.domain.common import canonical_data, content_hash, ensure_utc, utc_now
from tenderguard.domain.enums import ActorRole, ApprovalState, Severity
from tenderguard.domain.jobs import OutboxClaim, OutboxSettlement
from tenderguard.domain.models import DomainModel
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.object_store import ObjectStore
from tenderguard.infrastructure.orm import (
    AdapterQualificationRow,
    AutomationReworkDispatchRow,
    CalculationSnapshotRow,
    ExpertReworkRequestRow,
    OutboxEventRow,
    VerificationFindingRow,
)

AUTOMATION_REWORK_TOPIC = "project.final-review.rework-requested"
AUTOMATION_REWORK_CAPABILITY = "FINAL_REVIEW_REWORK"
_STAGE_TOPICS: dict[ApprovalState, str] = {
    ApprovalState.EXTRACTION_IN_PROGRESS: "project.automation.extraction.requested",
    ApprovalState.BOQ_IN_PROGRESS: "project.automation.boq.requested",
    ApprovalState.PRICING_IN_PROGRESS: "project.automation.pricing.requested",
    ApprovalState.CALCULATION_IN_PROGRESS: "project.automation.calculation.requested",
}


class AutomationDispatchDisposition(StrEnum):
    STAGE_COMMAND_QUEUED = "STAGE_COMMAND_QUEUED"
    BLOCKED = "BLOCKED"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    DEAD_LETTERED = "DEAD_LETTERED"
    IDLE = "IDLE"


class AutomationReworkDispatchView(DomainModel):
    dispatch_id: str
    rework_request_id: str
    project_id: str
    source_outbox_event_id: str
    command_outbox_event_id: str | None = None
    target_stage: ApprovalState
    command_topic: str | None = None
    status: Literal["STAGE_COMMAND_QUEUED", "BLOCKED"]
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    dispatch_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    worker_qualification_id: str
    worker_actor_id: str
    worker_qualified_stages: tuple[ApprovalState, ...]
    issue_references: tuple[dict[str, str], ...]
    dispatched_at: datetime


class AutomationDispatchResult(DomainModel):
    disposition: AutomationDispatchDisposition
    outbox_event_id: str | None = None
    dispatch: AutomationReworkDispatchView | None = None
    error_code: str | None = None


class AutomationReworkStatusView(DomainModel):
    rework_request_id: str
    project_id: str
    snapshot_id: str
    target_stage: ApprovalState
    requested_by: str
    requested_at: datetime
    status: Literal["PENDING_DISPATCH", "STAGE_COMMAND_QUEUED", "BLOCKED"]
    dispatch_id: str | None = None
    dispatch_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    command_topic: str | None = None
    command_delivery_status: str | None = None
    integrity_error_code: str | None = None
    issue_references: tuple[dict[str, str], ...]


class AutomationReworkStatusPage(DomainModel):
    project_id: str
    items: tuple[AutomationReworkStatusView, ...]


class AutomationReworkIntegrityError(RuntimeError):
    pass


class AutomationReworkDispatcher:
    """Validate final-review rework and enqueue exactly one qualified stage command."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        settings: Settings,
        object_store: ObjectStore,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.object_store = object_store

    def dispatch_next(
        self,
        *,
        worker_id: str,
        rework_request_id: str | None = None,
    ) -> AutomationDispatchResult:
        actor, qualification_id, qualified_stages = self._qualified_worker()
        claim = self._claim(worker_id=worker_id, rework_request_id=rework_request_id)
        if claim is None:
            return AutomationDispatchResult(disposition=AutomationDispatchDisposition.IDLE)
        try:
            dispatch = self._process_claim(
                claim=claim,
                actor=actor,
                qualification_id=qualification_id,
                qualified_stages=qualified_stages,
            )
        except (AutomationReworkIntegrityError, LookupError, ValueError) as error:
            error_code = self._error_code(error)
            self._reject(claim, error_code=error_code, force_dead_letter=True)
            return AutomationDispatchResult(
                disposition=AutomationDispatchDisposition.DEAD_LETTERED,
                outbox_event_id=claim.event_id,
                error_code=error_code,
            )
        except Exception as error:
            error_code = self._error_code(error)
            settlement = self._reject(claim, error_code=error_code)
            return AutomationDispatchResult(
                disposition=(
                    AutomationDispatchDisposition.DEAD_LETTERED
                    if settlement.dead_lettered
                    else AutomationDispatchDisposition.RETRY_SCHEDULED
                ),
                outbox_event_id=claim.event_id,
                error_code=error_code,
            )
        self._acknowledge(claim)
        return AutomationDispatchResult(
            disposition=AutomationDispatchDisposition(dispatch.status),
            outbox_event_id=claim.event_id,
            dispatch=dispatch,
        )

    def _qualified_worker(self) -> tuple[Actor, str, frozenset[ApprovalState]]:
        if not self.settings.automation_rework_configured:
            raise ValueError("Automatic rework worker binding is not configured")
        qualification_id = str(self.settings.automation_rework_qualification_id)
        with self.session_factory() as session:
            row = session.get(AdapterQualificationRow, qualification_id)
            if row is None:
                raise ValueError("Automatic rework worker qualification is absent")
            organization_id = row.payload.get("organization_id")
            service_actor_id = row.payload.get("service_actor_id")
            supported_methods = row.payload.get("supported_methods")
            raw_stages = row.payload.get("supported_rework_stages")
            if (
                row.adapter_name != self.settings.automation_rework_adapter
                or row.status != "APPROVED"
                or (row.valid_until is not None and row.valid_until < utc_now().date())
                or not isinstance(organization_id, str)
                or not organization_id
                or service_actor_id != self.settings.automation_rework_worker_actor_id
                or not isinstance(supported_methods, list)
                or AUTOMATION_REWORK_CAPABILITY not in supported_methods
                or not isinstance(raw_stages, list)
                or not raw_stages
                or len(raw_stages) > len(_STAGE_TOPICS)
                or any(not isinstance(value, str) for value in raw_stages)
                or len(raw_stages) != len(set(raw_stages))
            ):
                raise ValueError("Automatic rework worker qualification is invalid")
            try:
                qualified_stages = frozenset(ApprovalState(value) for value in raw_stages)
            except (TypeError, ValueError) as error:
                raise ValueError(
                    "Automatic rework worker qualification has invalid stages"
                ) from error
            if not qualified_stages <= frozenset(_STAGE_TOPICS):
                raise ValueError("Automatic rework worker qualification has unsupported stages")
        return (
            Actor(
                actor_id=str(self.settings.automation_rework_worker_actor_id),
                organization_id=organization_id,
                roles=frozenset({ActorRole.SYSTEM}),
            ),
            qualification_id,
            qualified_stages,
        )

    def _claim(
        self,
        *,
        worker_id: str,
        rework_request_id: str | None,
    ) -> OutboxClaim | None:
        with self.session_factory.begin() as session:
            return OutboxDeliveryService(
                session=session,
                settings=self.settings,
                policy=OutboxDeliveryPolicy.automation(self.settings),
            ).claim_next(
                topics={AUTOMATION_REWORK_TOPIC},
                worker_id=worker_id,
                aggregate_id=rework_request_id,
            )

    def _process_claim(
        self,
        *,
        claim: OutboxClaim,
        actor: Actor,
        qualification_id: str,
        qualified_stages: frozenset[ApprovalState],
    ) -> AutomationReworkDispatchView:
        with self.session_factory.begin() as session:
            request = self._request_for_claim(session, claim)
            existing = session.scalar(
                select(AutomationReworkDispatchRow).where(
                    AutomationReworkDispatchRow.rework_request_id == request.id
                )
            )
            if existing is not None:
                self._require_existing_dispatch_integrity(session, existing, request, claim)
                return self._dispatch_view(existing)

            project = ProjectService(
                session=session,
                settings=self.settings,
                object_store=self.object_store,
            ).get_project(
                actor=actor,
                project_id=request.project_id,
                lock=True,
                system_access=SystemProjectAccess(
                    qualification_id=qualification_id,
                    capability=AUTOMATION_REWORK_CAPABILITY,
                ),
            )
            if project.state != request.target_stage:
                raise AutomationReworkIntegrityError(
                    "Project state differs from the immutable rework target"
                )
            source_project_version = request.payload.get("project_row_version")
            if (
                not isinstance(source_project_version, int)
                or isinstance(source_project_version, bool)
                or project.row_version != source_project_version + 1
                or project.current_document_set_revision_id
                != request.payload.get("document_set_revision_id")
            ):
                raise AutomationReworkIntegrityError(
                    "Project context differs from the immutable rework request"
                )
            snapshot = session.get(CalculationSnapshotRow, request.snapshot_id)
            if (
                snapshot is None
                or snapshot.project_id != project.id
                or not snapshot.fixed
                or snapshot.document_set_revision_id
                != request.payload.get("document_set_revision_id")
            ):
                raise AutomationReworkIntegrityError("Final rework snapshot provenance is invalid")

            issue_references = self._issue_references(request.payload.get("issues"))
            target_stage = ApprovalState(request.target_stage)
            dispatch_id = f"automation-dispatch-{content_hash(request.id)[:24]}"
            request_hash = str(request.payload["request_hash"])
            command_topic = (
                _STAGE_TOPICS.get(target_stage) if target_stage in qualified_stages else None
            )
            command_event_id = (
                "outbox-automation-"
                + content_hash({"request": request.id, "stage": target_stage})[:24]
                if command_topic is not None
                else None
            )
            status = (
                AutomationDispatchDisposition.STAGE_COMMAND_QUEUED.value
                if command_topic is not None
                else AutomationDispatchDisposition.BLOCKED.value
            )
            dispatched_at = utc_now()
            command_payload: dict[str, object] | None = None
            if command_topic is not None and command_event_id is not None:
                command_payload = {
                    "project_id": project.id,
                    "rework_request_id": request.id,
                    "dispatch_id": dispatch_id,
                    "snapshot_id": request.snapshot_id,
                    "document_set_revision_id": request.payload.get("document_set_revision_id"),
                    "target_stage": target_stage.value,
                    "request_hash": request_hash,
                    "issue_references": issue_references,
                }
                session.add(
                    OutboxEventRow(
                        id=command_event_id,
                        deduplication_key=f"automation-rework-stage:{request.id}",
                        delivery_deduplication_key=(f"automation-rework-stage:{request.id}"),
                        topic=command_topic,
                        aggregate_id=dispatch_id,
                        payload=canonical_data(command_payload),
                        attempts=0,
                        available_at=dispatched_at,
                        created_at=dispatched_at,
                    )
                )

            dispatch_basis: dict[str, object] = {
                "dispatch_id": dispatch_id,
                "rework_request_id": request.id,
                "project_id": project.id,
                "source_outbox_event_id": claim.event_id,
                "command_outbox_event_id": command_event_id,
                "target_stage": target_stage.value,
                "command_topic": command_topic,
                "status": status,
                "request_hash": request_hash,
                "worker_qualification_id": qualification_id,
                "worker_actor_id": actor.actor_id,
                "worker_qualified_stages": [
                    stage.value for stage in sorted(qualified_stages, key=lambda item: item.value)
                ],
                "issue_references": issue_references,
                "command_payload_hash": (
                    content_hash(command_payload) if command_payload is not None else None
                ),
                "dispatched_at": dispatched_at,
            }
            dispatch_hash = content_hash(dispatch_basis)
            payload = {**dispatch_basis, "dispatch_hash": dispatch_hash}
            row = AutomationReworkDispatchRow(
                id=dispatch_id,
                rework_request_id=request.id,
                project_id=project.id,
                source_outbox_event_id=claim.event_id,
                command_outbox_event_id=command_event_id,
                target_stage=target_stage.value,
                command_topic=command_topic,
                status=status,
                request_hash=request_hash,
                dispatch_hash=dispatch_hash,
                worker_qualification_id=qualification_id,
                worker_actor_id=actor.actor_id,
                payload=canonical_data(payload),
                dispatched_at=dispatched_at,
            )
            session.add(row)
            if status == AutomationDispatchDisposition.BLOCKED.value:
                finding_code = (
                    "AUTOMATION_REWORK_NOT_AUTOMATABLE"
                    if target_stage not in _STAGE_TOPICS
                    else "AUTOMATION_REWORK_STAGE_NOT_QUALIFIED"
                )
                finding_id = f"finding-automation-{content_hash(request.id)[:24]}"
                if session.get(VerificationFindingRow, finding_id) is None:
                    session.add(
                        VerificationFindingRow(
                            id=finding_id,
                            project_id=project.id,
                            contour="AUTOMATION_REWORK",
                            code=finding_code,
                            severity=Severity.BLOCKER.value,
                            resolved=False,
                            payload={
                                "rework_request_id": request.id,
                                "dispatch_id": dispatch_id,
                                "target_stage": target_stage.value,
                                "worker_qualified_stages": [
                                    stage.value
                                    for stage in sorted(
                                        qualified_stages,
                                        key=lambda item: item.value,
                                    )
                                ],
                                "issue_references": issue_references,
                            },
                            created_at=dispatched_at,
                            updated_at=dispatched_at,
                        )
                    )
            ProjectService(
                session=session,
                settings=self.settings,
                object_store=self.object_store,
            ).record_event(
                aggregate_type="project",
                aggregate_id=project.id,
                event_type=(
                    "automation_rework_stage_command_queued"
                    if command_topic is not None
                    else "automation_rework_blocked"
                ),
                actor=actor,
                request_id=f"automation-dispatch:{claim.event_id}",
                reason=(
                    "Qualified automatic rework dispatcher validated the immutable expert request"
                ),
                payload={
                    "dispatch_id": dispatch_id,
                    "rework_request_id": request.id,
                    "dispatch_hash": dispatch_hash,
                    "target_stage": target_stage.value,
                    "command_topic": command_topic,
                    "command_outbox_event_id": command_event_id,
                },
            )
            session.flush()
            return self._dispatch_view(row)

    @staticmethod
    def _request_for_claim(
        session: Session,
        claim: OutboxClaim,
    ) -> ExpertReworkRequestRow:
        if claim.topic != AUTOMATION_REWORK_TOPIC:
            raise AutomationReworkIntegrityError("Unexpected automatic rework topic")
        request_id = claim.payload.get("rework_request_id")
        if not isinstance(request_id, str) or request_id != claim.aggregate_id:
            raise AutomationReworkIntegrityError("Automatic rework event identity is invalid")
        request = session.get(ExpertReworkRequestRow, request_id)
        if request is None:
            raise LookupError(request_id)
        request_hash = AutomationReworkDispatcher._require_request_integrity(request)
        if (
            claim.payload.get("project_id") != request.project_id
            or claim.payload.get("snapshot_id") != request.snapshot_id
            or claim.payload.get("target_stage") != request.target_stage
            or claim.payload.get("request_hash") != request_hash
        ):
            raise AutomationReworkIntegrityError("Expert rework event provenance is invalid")
        return request

    @staticmethod
    def _require_request_integrity(request: ExpertReworkRequestRow) -> str:
        request_hash = request.payload.get("request_hash")
        if not isinstance(request_hash, str):
            raise AutomationReworkIntegrityError("Expert rework request hash is missing")
        base_payload = {
            key: value for key, value in request.payload.items() if key != "request_hash"
        }
        if (
            content_hash(base_payload) != request_hash
            or request.payload.get("project_id") != request.project_id
            or request.payload.get("snapshot_id") != request.snapshot_id
            or request.payload.get("gate_hash") != request.gate_hash
            or request.payload.get("target_stage") != request.target_stage
            or request.payload.get("requested_state") != request.requested_state
        ):
            raise AutomationReworkIntegrityError("Expert rework request provenance is invalid")
        return request_hash

    @staticmethod
    def _issue_references(raw_issues: object) -> list[dict[str, str]]:
        if not isinstance(raw_issues, list) or not raw_issues or len(raw_issues) > 200:
            raise AutomationReworkIntegrityError("Expert rework issue set is invalid")
        references: list[dict[str, str]] = []
        for raw in raw_issues:
            if not isinstance(raw, dict):
                raise AutomationReworkIntegrityError("Expert rework issue is invalid")
            values = {key: raw.get(key) for key in ("kind", "reference_id", "code")}
            if any(
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or len(value) > 300
                for value in values.values()
            ):
                raise AutomationReworkIntegrityError("Expert rework reference is invalid")
            references.append({key: str(value) for key, value in values.items()})
        keys = tuple((item["kind"], item["reference_id"], item["code"]) for item in references)
        if len(keys) != len(set(keys)):
            raise AutomationReworkIntegrityError("Expert rework references are duplicated")
        return references

    def _require_existing_dispatch_integrity(
        self,
        session: Session,
        row: AutomationReworkDispatchRow,
        request: ExpertReworkRequestRow,
        claim: OutboxClaim,
    ) -> None:
        self._require_dispatch_row_integrity(session, row)
        payload = row.payload
        if (
            row.rework_request_id != request.id
            or row.project_id != request.project_id
            or row.source_outbox_event_id != claim.event_id
            or row.request_hash != request.payload.get("request_hash")
            or row.target_stage != request.target_stage
        ):
            raise AutomationReworkIntegrityError("Stored automatic dispatch is invalid")
        if row.command_outbox_event_id is not None:
            command = session.get(OutboxEventRow, row.command_outbox_event_id)
            expected_payload = {
                "project_id": request.project_id,
                "rework_request_id": request.id,
                "dispatch_id": row.id,
                "snapshot_id": request.snapshot_id,
                "document_set_revision_id": request.payload.get("document_set_revision_id"),
                "target_stage": request.target_stage,
                "request_hash": row.request_hash,
                "issue_references": self._issue_references(request.payload.get("issues")),
            }
            if (
                command is None
                or command.topic != row.command_topic
                or command.aggregate_id != row.id
                or command.payload != canonical_data(expected_payload)
                or payload.get("command_payload_hash") != content_hash(expected_payload)
            ):
                raise AutomationReworkIntegrityError("Stored automatic stage command is invalid")

    @staticmethod
    def _require_dispatch_row_integrity(
        session: Session,
        row: AutomationReworkDispatchRow,
    ) -> None:
        payload = row.payload
        dispatch_hash = payload.get("dispatch_hash")
        basis = {key: value for key, value in payload.items() if key != "dispatch_hash"}
        dispatched_at = ensure_utc(row.dispatched_at)
        expected_columns = {
            "dispatch_id": row.id,
            "rework_request_id": row.rework_request_id,
            "project_id": row.project_id,
            "source_outbox_event_id": row.source_outbox_event_id,
            "command_outbox_event_id": row.command_outbox_event_id,
            "target_stage": row.target_stage,
            "command_topic": row.command_topic,
            "status": row.status,
            "request_hash": row.request_hash,
            "worker_qualification_id": row.worker_qualification_id,
            "worker_actor_id": row.worker_actor_id,
            "dispatched_at": canonical_data(dispatched_at),
        }
        if (
            dispatched_at is None
            or not isinstance(dispatch_hash, str)
            or content_hash(basis) != dispatch_hash
            or row.dispatch_hash != dispatch_hash
            or any(payload.get(key) != value for key, value in expected_columns.items())
        ):
            raise AutomationReworkIntegrityError("Stored automatic dispatch is invalid")
        references = AutomationReworkDispatcher._issue_references(payload.get("issue_references"))
        raw_qualified_stages = payload.get("worker_qualified_stages")
        if (
            not isinstance(raw_qualified_stages, list)
            or not raw_qualified_stages
            or len(raw_qualified_stages) > len(_STAGE_TOPICS)
            or any(not isinstance(value, str) for value in raw_qualified_stages)
            or len(raw_qualified_stages) != len(set(raw_qualified_stages))
        ):
            raise AutomationReworkIntegrityError(
                "Stored automatic worker stage qualification is invalid"
            )
        try:
            qualified_stages = frozenset(ApprovalState(value) for value in raw_qualified_stages)
        except (TypeError, ValueError) as error:
            raise AutomationReworkIntegrityError(
                "Stored automatic worker stage qualification is invalid"
            ) from error
        if not qualified_stages <= frozenset(_STAGE_TOPICS):
            raise AutomationReworkIntegrityError(
                "Stored automatic worker stage qualification is invalid"
            )
        target_stage = ApprovalState(row.target_stage)
        expected_topic = (
            _STAGE_TOPICS.get(target_stage) if target_stage in qualified_stages else None
        )
        if row.status == AutomationDispatchDisposition.STAGE_COMMAND_QUEUED.value:
            if (
                row.command_outbox_event_id is None
                or row.command_topic != expected_topic
                or payload.get("command_payload_hash") is None
            ):
                raise AutomationReworkIntegrityError(
                    "Stored automatic dispatch command binding is invalid"
                )
        elif row.status == AutomationDispatchDisposition.BLOCKED.value:
            if (
                row.command_outbox_event_id is not None
                or row.command_topic is not None
                or payload.get("command_payload_hash") is not None
                or expected_topic is not None
            ):
                raise AutomationReworkIntegrityError("Stored blocked automatic dispatch is invalid")
        else:
            raise AutomationReworkIntegrityError("Stored automatic dispatch status is invalid")
        if row.command_outbox_event_id is not None:
            command = session.get(OutboxEventRow, row.command_outbox_event_id)
            if (
                command is None
                or command.topic != row.command_topic
                or command.aggregate_id != row.id
                or payload.get("command_payload_hash") != content_hash(command.payload)
                or command.payload.get("issue_references") != references
            ):
                raise AutomationReworkIntegrityError("Stored automatic stage command is invalid")

    def _acknowledge(self, claim: OutboxClaim) -> OutboxSettlement:
        with self.session_factory.begin() as session:
            return OutboxDeliveryService(
                session=session,
                settings=self.settings,
                policy=OutboxDeliveryPolicy.automation(self.settings),
            ).acknowledge(claim)

    def _reject(
        self,
        claim: OutboxClaim,
        *,
        error_code: str,
        force_dead_letter: bool = False,
    ) -> OutboxSettlement:
        with self.session_factory.begin() as session:
            return OutboxDeliveryService(
                session=session,
                settings=self.settings,
                policy=OutboxDeliveryPolicy.automation(self.settings),
            ).reject(
                claim,
                error_code=error_code,
                force_dead_letter=force_dead_letter,
            )

    @staticmethod
    def _error_code(error: Exception) -> str:
        if isinstance(error, AutomationReworkIntegrityError):
            return "AUTOMATION_REWORK_INTEGRITY_FAILED"
        if isinstance(error, LookupError):
            return "AUTOMATION_REWORK_TARGET_NOT_FOUND"
        if isinstance(error, ValueError):
            return "AUTOMATION_REWORK_VALIDATION_FAILED"
        return "AUTOMATION_REWORK_HANDLER_FAILED"

    @staticmethod
    def _dispatch_view(row: AutomationReworkDispatchRow) -> AutomationReworkDispatchView:
        dispatched_at = ensure_utc(row.dispatched_at)
        if dispatched_at is None:
            raise AutomationReworkIntegrityError("Automatic dispatch timestamp is missing")
        raw_references = row.payload.get("issue_references")
        references = AutomationReworkDispatcher._issue_references(raw_references)
        raw_qualified_stages = row.payload.get("worker_qualified_stages")
        if not isinstance(raw_qualified_stages, list):
            raise AutomationReworkIntegrityError(
                "Automatic dispatch stage qualification is missing"
            )
        try:
            qualified_stages = tuple(
                sorted(
                    (ApprovalState(value) for value in raw_qualified_stages),
                    key=lambda item: item.value,
                )
            )
        except (TypeError, ValueError) as error:
            raise AutomationReworkIntegrityError(
                "Automatic dispatch stage qualification is invalid"
            ) from error
        return AutomationReworkDispatchView(
            dispatch_id=row.id,
            rework_request_id=row.rework_request_id,
            project_id=row.project_id,
            source_outbox_event_id=row.source_outbox_event_id,
            command_outbox_event_id=row.command_outbox_event_id,
            target_stage=ApprovalState(row.target_stage),
            command_topic=row.command_topic,
            status=row.status,
            request_hash=row.request_hash,
            dispatch_hash=row.dispatch_hash,
            worker_qualification_id=row.worker_qualification_id,
            worker_actor_id=row.worker_actor_id,
            worker_qualified_stages=qualified_stages,
            issue_references=tuple(references),
            dispatched_at=dispatched_at,
        )


class AutomationReworkStatusService:
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

    def list_status(
        self,
        *,
        actor: Actor,
        project_id: str,
        limit: int,
    ) -> AutomationReworkStatusPage:
        if limit < 1 or limit > 100:
            raise ValueError("Automatic rework status limit must be between 1 and 100")
        ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        ).get_project(
            actor=actor,
            project_id=project_id,
            required_roles=(
                ActorRole.ESTIMATOR,
                ActorRole.REVIEWER,
                ActorRole.APPROVER,
                ActorRole.AUDITOR,
            ),
        )
        requests = tuple(
            self.session.scalars(
                select(ExpertReworkRequestRow)
                .where(ExpertReworkRequestRow.project_id == project_id)
                .order_by(
                    ExpertReworkRequestRow.requested_at.desc(),
                    ExpertReworkRequestRow.id.desc(),
                )
                .limit(limit)
            )
        )
        dispatches = (
            {
                row.rework_request_id: row
                for row in self.session.scalars(
                    select(AutomationReworkDispatchRow).where(
                        AutomationReworkDispatchRow.rework_request_id.in_(
                            tuple(row.id for row in requests)
                        )
                    )
                )
            }
            if requests
            else {}
        )
        items: list[AutomationReworkStatusView] = []
        for request in requests:
            dispatch = dispatches.get(request.id)
            command_status = None
            integrity_error_code = None
            effective_status: Literal["PENDING_DISPATCH", "STAGE_COMMAND_QUEUED", "BLOCKED"] = (
                "PENDING_DISPATCH"
            )
            try:
                AutomationReworkDispatcher._require_request_integrity(request)
                references = AutomationReworkDispatcher._issue_references(
                    request.payload.get("issues")
                )
                if dispatch is not None:
                    AutomationReworkDispatcher._require_dispatch_row_integrity(
                        self.session,
                        dispatch,
                    )
                    effective_status = (
                        "STAGE_COMMAND_QUEUED"
                        if dispatch.status == "STAGE_COMMAND_QUEUED"
                        else "BLOCKED"
                    )
                    if dispatch.command_outbox_event_id is not None:
                        command = self.session.get(
                            OutboxEventRow,
                            dispatch.command_outbox_event_id,
                        )
                        assert command is not None
                        if command.dead_lettered_at is not None:
                            command_status = "DEAD_LETTERED"
                        elif command.published_at is not None:
                            command_status = "ACKNOWLEDGED"
                        elif command.locked_by is not None:
                            command_status = "PROCESSING"
                        else:
                            command_status = "PENDING"
            except (AutomationReworkIntegrityError, ValueError):
                references = []
                effective_status = "BLOCKED"
                command_status = "INTEGRITY_FAILED"
                integrity_error_code = "AUTOMATION_REWORK_INTEGRITY_FAILED"
            requested_at = ensure_utc(request.requested_at)
            if requested_at is None:
                raise AutomationReworkIntegrityError("Expert rework timestamp is missing")
            items.append(
                AutomationReworkStatusView(
                    rework_request_id=request.id,
                    project_id=request.project_id,
                    snapshot_id=request.snapshot_id,
                    target_stage=ApprovalState(request.target_stage),
                    requested_by=request.requested_by,
                    requested_at=requested_at,
                    status=effective_status,
                    dispatch_id=dispatch.id if dispatch is not None else None,
                    dispatch_hash=(dispatch.dispatch_hash if dispatch is not None else None),
                    command_topic=(dispatch.command_topic if dispatch is not None else None),
                    command_delivery_status=command_status,
                    integrity_error_code=integrity_error_code,
                    issue_references=tuple(references),
                )
            )
        return AutomationReworkStatusPage(project_id=project_id, items=tuple(items))
