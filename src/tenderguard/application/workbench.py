from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import Field
from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import Session

from tenderguard.application.projects import (
    ProjectService,
    ProjectView,
)
from tenderguard.config import Settings
from tenderguard.domain.access import (
    project_role_bit,
    project_role_mask,
    validate_project_role_evidence,
)
from tenderguard.domain.approvals import DEDICATED_APPROVAL_TASK_TYPES
from tenderguard.domain.common import ensure_utc, utc_now
from tenderguard.domain.enums import (
    ActorRole,
    ApprovalState,
    ProjectAccessLevel,
    ProjectMembershipStatus,
    VerificationStatus,
)
from tenderguard.domain.models import DomainModel, GateDecision
from tenderguard.domain.release import evaluate_bid_release
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.object_store import ObjectStore
from tenderguard.infrastructure.orm import (
    ActualRecordRow,
    ApprovalRecordRow,
    ApprovalTaskRow,
    AuditEventRow,
    BoqLineRow,
    CalculationRunRow,
    CalculationSnapshotRow,
    CalibrationExampleRow,
    CommercialCostModelRow,
    ConflictRow,
    ContractTermRow,
    ControlledVersionRow,
    CostInputRow,
    DocumentRevisionRow,
    DocumentRow,
    DocumentSetRevisionRow,
    ExportArtifactRow,
    ManualChangeRow,
    NomenclatureMatchRow,
    NormalizedPriceRow,
    ObservationRow,
    PriceDecisionRow,
    PriceQuoteRow,
    ProjectControlledVersionRow,
    ProjectMembershipRow,
    ProjectPassportFactRow,
    ProjectRow,
    QuantityRow,
    QuarantinedUploadRow,
    ReleaseDecisionRow,
    RfqRequestRow,
    RiskCalculationRow,
    RiskItemRow,
    ScenarioRunRow,
    ScopeEvaluationRow,
    ScopeFindingRow,
    VarianceRecordRow,
    VerificationFindingRow,
    WorkflowTransitionRow,
)


class ProjectRecordSection(StrEnum):
    DOCUMENTS = "DOCUMENTS"
    EVIDENCE = "EVIDENCE"
    BOQ_SCOPE = "BOQ_SCOPE"
    PRICING = "PRICING"
    CONTRACT_RISK = "CONTRACT_RISK"
    CALCULATION = "CALCULATION"
    APPROVALS = "APPROVALS"
    ACTUALS = "ACTUALS"
    GOVERNANCE = "GOVERNANCE"
    AUDIT = "AUDIT"


FINANCIAL_READ_ROLES = (
    ActorRole.ESTIMATOR,
    ActorRole.REVIEWER,
    ActorRole.APPROVER,
    ActorRole.AUDITOR,
)

SECTION_READ_ROLES: dict[ProjectRecordSection, tuple[ActorRole, ...]] = {
    ProjectRecordSection.DOCUMENTS: (
        ActorRole.ESTIMATOR,
        ActorRole.TECHNICAL_EXPERT,
        ActorRole.REVIEWER,
        ActorRole.APPROVER,
        ActorRole.AUDITOR,
    ),
    ProjectRecordSection.EVIDENCE: (
        ActorRole.ESTIMATOR,
        ActorRole.TECHNICAL_EXPERT,
        ActorRole.REVIEWER,
        ActorRole.APPROVER,
        ActorRole.AUDITOR,
    ),
    ProjectRecordSection.BOQ_SCOPE: (
        ActorRole.ESTIMATOR,
        ActorRole.TECHNICAL_EXPERT,
        ActorRole.REVIEWER,
        ActorRole.APPROVER,
        ActorRole.AUDITOR,
    ),
    ProjectRecordSection.PRICING: (
        ActorRole.ESTIMATOR,
        ActorRole.PROCUREMENT,
        ActorRole.REVIEWER,
        ActorRole.APPROVER,
        ActorRole.AUDITOR,
    ),
    ProjectRecordSection.CONTRACT_RISK: FINANCIAL_READ_ROLES,
    ProjectRecordSection.CALCULATION: FINANCIAL_READ_ROLES,
    ProjectRecordSection.APPROVALS: (
        ActorRole.ESTIMATOR,
        ActorRole.PROCUREMENT,
        ActorRole.TECHNICAL_EXPERT,
        ActorRole.REVIEWER,
        ActorRole.APPROVER,
        ActorRole.METHODOLOGY_OWNER,
        ActorRole.CATALOG_OWNER,
        ActorRole.AUDITOR,
    ),
    ProjectRecordSection.ACTUALS: (
        ActorRole.ESTIMATOR,
        ActorRole.REVIEWER,
        ActorRole.APPROVER,
        ActorRole.METHODOLOGY_OWNER,
        ActorRole.AUDITOR,
    ),
    ProjectRecordSection.GOVERNANCE: (
        ActorRole.ESTIMATOR,
        ActorRole.REVIEWER,
        ActorRole.APPROVER,
        ActorRole.METHODOLOGY_OWNER,
        ActorRole.CATALOG_OWNER,
        ActorRole.AUDITOR,
    ),
    ProjectRecordSection.AUDIT: (
        ActorRole.ESTIMATOR,
        ActorRole.REVIEWER,
        ActorRole.APPROVER,
        ActorRole.AUDITOR,
    ),
}


class ProjectAccessView(DomainModel):
    access_level: ProjectAccessLevel
    roles: tuple[ActorRole, ...]


class ProjectPortfolioItem(DomainModel):
    project: ProjectView
    access: ProjectAccessView
    open_approval_count: int = Field(ge=0)
    unresolved_blocker_count: int = Field(ge=0)
    latest_total: Decimal | None = None
    latest_currency: str | None = None
    updated_at: datetime


class ProjectPortfolioPage(DomainModel):
    items: tuple[ProjectPortfolioItem, ...]
    next_cursor: str | None


class WorkItemView(DomainModel):
    task_id: str
    project_id: str
    project_code: str
    project_name: str
    task_type: str
    entity_type: str
    entity_id: str
    assigned_role: ActorRole
    status: str
    required: bool
    created_by: str | None
    created_at: datetime
    updated_at: datetime


class WorkItemPage(DomainModel):
    items: tuple[WorkItemView, ...]
    next_cursor: str | None


class WorkItemDecisionView(DomainModel):
    approval_id: str
    decision: str
    decided_by: str
    reason: str
    evidence_ids: tuple[str, ...]
    related_change_ids: tuple[str, ...]
    decided_at: datetime


class WorkItemDetail(DomainModel):
    item: WorkItemView
    project: ProjectView
    policy_version_id: str | None = None
    task_key: str | None = None
    candidate_evidence_ids: tuple[str, ...] = ()
    decisions: tuple[WorkItemDecisionView, ...] = ()
    decision_allowed: bool
    decision_blockers: tuple[str, ...] = ()


class ProjectRecordLink(DomainModel):
    relation: str
    entity_type: str
    entity_id: str


class ProjectRecord(DomainModel):
    id: str
    section: ProjectRecordSection
    kind: str
    title: str
    subtitle: str | None = None
    status: str | None = None
    severity: str | None = None
    current: bool | None = None
    amount: Decimal | None = None
    currency: str | None = None
    unit: str | None = None
    occurred_at: datetime
    attributes: dict[str, Any] = Field(default_factory=dict)
    links: tuple[ProjectRecordLink, ...] = ()


class ProjectRecordPage(DomainModel):
    section: ProjectRecordSection
    items: tuple[ProjectRecord, ...]
    next_cursor: str | None


class WorkbenchMetric(DomainModel):
    code: str
    label: str
    value: int
    blocking: int = Field(ge=0)


class ProjectWorkbench(DomainModel):
    project: ProjectView
    access: ProjectAccessView
    release_decision: GateDecision
    metrics: tuple[WorkbenchMetric, ...]
    attention: tuple[ProjectRecord, ...]
    recent_activity: tuple[ProjectRecord, ...]
    latest_total: Decimal | None = None
    latest_currency: str | None = None
    generated_at: datetime


class _Cursor(DomainModel):
    scope: str
    occurred_at: datetime
    record_id: str


class ProjectReadService:
    """Authorization-preserving read models for dense operator workflows."""

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
        self.projects = ProjectService(
            session=session,
            settings=settings,
            object_store=object_store,
        )

    def list_projects(
        self,
        *,
        actor: Actor,
        query: str | None,
        states: frozenset[ApprovalState],
        limit: int,
        cursor: str | None,
    ) -> ProjectPortfolioPage:
        self._require_human(actor)
        limit = self._limit(limit)
        normalized_query = self._query(query)
        decoded = self._decode_cursor(cursor, "projects")
        actor_role_mask = self._actor_project_role_mask(actor)
        if actor_role_mask == 0:
            return ProjectPortfolioPage(items=(), next_cursor=None)
        latest_memberships = self._latest_actor_memberships(actor).subquery()
        statement = (
            select(ProjectRow, ProjectMembershipRow)
            .join(
                ProjectMembershipRow,
                ProjectMembershipRow.project_id == ProjectRow.id,
            )
            .join(
                latest_memberships,
                and_(
                    latest_memberships.c.project_id == ProjectMembershipRow.project_id,
                    latest_memberships.c.version == ProjectMembershipRow.version,
                ),
            )
            .where(
                ProjectMembershipRow.principal_id == actor.actor_id,
                ProjectMembershipRow.status == ProjectMembershipStatus.ACTIVE.value,
                ProjectMembershipRow.role_mask.bitwise_and(actor_role_mask) != 0,
                ProjectRow.organization_id == actor.organization_id,
            )
        )
        if states:
            statement = statement.where(
                ProjectRow.state.in_(tuple(state.value for state in states))
            )
        if normalized_query:
            pattern = f"%{self._escape_like(normalized_query)}%"
            statement = statement.where(
                or_(
                    ProjectRow.code.ilike(pattern, escape="\\"),
                    ProjectRow.name.ilike(pattern, escape="\\"),
                )
            )
        statement = self._before(
            statement,
            ProjectRow.updated_at,
            ProjectRow.id,
            decoded,
        ).order_by(ProjectRow.updated_at.desc(), ProjectRow.id.desc())
        rows = tuple(self.session.execute(statement.limit(limit + 1)).all())
        selected = rows[:limit]
        for _, membership in selected:
            self._membership_roles(membership)
        project_ids = tuple(project.id for project, _ in selected)
        open_tasks = self._counts(
            ApprovalTaskRow.project_id,
            project_ids,
            ApprovalTaskRow.status == "PENDING",
        )
        blocking_findings = self._counts(
            VerificationFindingRow.project_id,
            project_ids,
            VerificationFindingRow.resolved.is_(False),
            VerificationFindingRow.severity == "BLOCKER",
        )
        financial_project_ids = tuple(
            project.id
            for project, membership in selected
            if actor.roles.intersection(self._membership_roles(membership), FINANCIAL_READ_ROLES)
        )
        latest_runs = self._latest_calculations(financial_project_ids)
        items = tuple(
            ProjectPortfolioItem(
                project=self._project_view(project),
                access=self._access_view(membership),
                open_approval_count=open_tasks.get(project.id, 0),
                unresolved_blocker_count=blocking_findings.get(project.id, 0),
                latest_total=(
                    latest_runs[project.id].grand_total if project.id in latest_runs else None
                ),
                latest_currency=(
                    latest_runs[project.id].currency if project.id in latest_runs else None
                ),
                updated_at=project.updated_at,
            )
            for project, membership in selected
        )
        return ProjectPortfolioPage(
            items=items,
            next_cursor=(
                self._encode_cursor(
                    scope="projects",
                    occurred_at=selected[-1][0].updated_at,
                    record_id=selected[-1][0].id,
                )
                if len(rows) > limit and selected
                else None
            ),
        )

    def list_work_items(
        self,
        *,
        actor: Actor,
        statuses: frozenset[str],
        limit: int,
        cursor: str | None,
    ) -> WorkItemPage:
        self._require_human(actor)
        limit = self._limit(limit)
        decoded = self._decode_cursor(cursor, "work-items")
        actor_project_roles = tuple(
            sorted(
                (role for role in actor.roles if role is not ActorRole.SYSTEM),
                key=lambda role: role.value,
            )
        )
        if not actor_project_roles:
            return WorkItemPage(items=(), next_cursor=None)
        latest_memberships = self._latest_actor_memberships(actor).subquery()
        role_conditions = tuple(
            and_(
                ApprovalTaskRow.assigned_role == role.value,
                ProjectMembershipRow.role_mask.bitwise_and(project_role_bit(role)) != 0,
            )
            for role in actor_project_roles
        )
        statement = (
            select(ApprovalTaskRow, ProjectRow, ProjectMembershipRow)
            .join(ProjectRow, ProjectRow.id == ApprovalTaskRow.project_id)
            .join(
                ProjectMembershipRow,
                ProjectMembershipRow.project_id == ProjectRow.id,
            )
            .join(
                latest_memberships,
                and_(
                    latest_memberships.c.project_id == ProjectMembershipRow.project_id,
                    latest_memberships.c.version == ProjectMembershipRow.version,
                ),
            )
            .where(
                ProjectMembershipRow.principal_id == actor.actor_id,
                ProjectMembershipRow.status == ProjectMembershipStatus.ACTIVE.value,
                ProjectRow.organization_id == actor.organization_id,
                or_(*role_conditions),
            )
        )
        if statuses:
            statement = statement.where(ApprovalTaskRow.status.in_(tuple(statuses)))
        statement = self._before(
            statement,
            ApprovalTaskRow.updated_at,
            ApprovalTaskRow.id,
            decoded,
        ).order_by(ApprovalTaskRow.updated_at.desc(), ApprovalTaskRow.id.desc())
        rows = self.session.execute(statement.limit(limit + 1)).all()
        selected = rows[:limit]
        for _, _, membership in selected:
            self._membership_roles(membership)
        items = tuple(self._work_item_view(task, project) for task, project, _ in selected)
        return WorkItemPage(
            items=items,
            next_cursor=(
                self._encode_cursor(
                    scope="work-items",
                    occurred_at=selected[-1][0].updated_at,
                    record_id=selected[-1][0].id,
                )
                if len(rows) > limit and selected
                else None
            ),
        )

    def get_work_item(self, *, actor: Actor, task_id: str) -> WorkItemDetail:
        self._require_human(actor)
        task = self.session.get(ApprovalTaskRow, task_id)
        if task is None:
            raise LookupError(task_id)
        assigned_role = ActorRole(task.assigned_role)
        project = self.projects.get_project(
            actor=actor,
            project_id=task.project_id,
            required_roles=(assigned_role,),
        )
        candidate_ids = tuple(
            value for value in task.payload.get("observation_ids", []) if isinstance(value, str)
        )
        if candidate_ids:
            existing = frozenset(
                self.session.scalars(
                    select(ObservationRow.id).where(
                        ObservationRow.project_id == task.project_id,
                        ObservationRow.id.in_(candidate_ids),
                    )
                )
            )
            candidate_ids = tuple(value for value in candidate_ids if value in existing)
        decision_rows = tuple(
            self.session.scalars(
                select(ApprovalRecordRow)
                .where(ApprovalRecordRow.task_id == task.id)
                .order_by(
                    ApprovalRecordRow.decided_at.desc(),
                    ApprovalRecordRow.id.desc(),
                )
            )
        )
        blockers: list[str] = []
        if task.status != "PENDING":
            blockers.append("TASK_NOT_PENDING")
        if task.task_type in DEDICATED_APPROVAL_TASK_TYPES:
            blockers.append("DEDICATED_WORKFLOW_REQUIRED")
        if task.required and task.payload.get("created_by") == actor.actor_id:
            blockers.append("FOUR_EYES_TASK_CREATOR")
        if task.entity_type == "manual_change":
            change = self.session.scalar(
                select(ManualChangeRow).where(
                    ManualChangeRow.project_id == task.project_id,
                    ManualChangeRow.id == task.entity_id,
                )
            )
            if change is None:
                blockers.append("MANUAL_CHANGE_MISSING")
            elif change.changed_by == actor.actor_id:
                blockers.append("FOUR_EYES_CHANGE_AUTHOR")
        return WorkItemDetail(
            item=self._work_item_view(task, project),
            project=self._project_view(project),
            policy_version_id=self._string(task.payload.get("policy_version_id")),
            task_key=self._string(task.payload.get("task_key")),
            candidate_evidence_ids=candidate_ids,
            decisions=tuple(
                WorkItemDecisionView(
                    approval_id=row.id,
                    decision=row.decision,
                    decided_by=row.decided_by,
                    reason=row.reason,
                    evidence_ids=tuple(
                        value
                        for value in row.payload.get("evidence_ids", [])
                        if isinstance(value, str)
                    ),
                    related_change_ids=tuple(
                        value
                        for value in row.payload.get("related_change_ids", [])
                        if isinstance(value, str)
                    ),
                    decided_at=self._required_utc(row.decided_at),
                )
                for row in decision_rows
            ),
            decision_allowed=not blockers,
            decision_blockers=tuple(blockers),
        )

    def workbench(self, *, actor: Actor, project_id: str) -> ProjectWorkbench:
        project = self.projects.get_project(
            actor=actor,
            project_id=project_id,
            required_roles=FINANCIAL_READ_ROLES,
        )
        membership = self._current_membership(actor, project_id)
        release = evaluate_bid_release(
            self.projects.evaluate_release(actor=actor, project_id=project_id)
        )
        current_documents = self._count(
            DocumentRevisionRow,
            DocumentRevisionRow.document_id.in_(
                select(DocumentRow.id).where(DocumentRow.project_id == project_id)
            ),
            DocumentRevisionRow.is_current.is_(True),
        )
        current_boq = self._count(
            BoqLineRow,
            BoqLineRow.project_id == project_id,
            BoqLineRow.is_current.is_(True),
        )
        pending_approvals = self._count(
            ApprovalTaskRow,
            ApprovalTaskRow.project_id == project_id,
            ApprovalTaskRow.status == "PENDING",
        )
        unresolved_conflicts = self._count(
            ConflictRow,
            ConflictRow.project_id == project_id,
            ConflictRow.status != VerificationStatus.VERIFIED.value,
        )
        blocking_findings = self._count(
            VerificationFindingRow,
            VerificationFindingRow.project_id == project_id,
            VerificationFindingRow.resolved.is_(False),
            VerificationFindingRow.severity == "BLOCKER",
        )
        unresolved_scope = self._count(
            ScopeFindingRow,
            ScopeFindingRow.project_id == project_id,
            ScopeFindingRow.resolved.is_(False),
        )
        metrics = (
            WorkbenchMetric(
                code="DOCUMENTS",
                label="Current documents",
                value=current_documents,
                blocking=sum(
                    1
                    for finding in release.findings
                    if (
                        finding.code.value
                        in {
                            "CURRENT_DOCUMENT_SET_NOT_CONFIRMED",
                            "CRITICAL_DOCUMENT_MISSING",
                        }
                    )
                ),
            ),
            WorkbenchMetric(
                code="BOQ",
                label="Current BoQ lines",
                value=current_boq,
                blocking=unresolved_scope,
            ),
            WorkbenchMetric(
                code="CONFLICTS",
                label="Unresolved conflicts",
                value=unresolved_conflicts,
                blocking=unresolved_conflicts,
            ),
            WorkbenchMetric(
                code="APPROVALS",
                label="Pending approvals",
                value=pending_approvals,
                blocking=pending_approvals,
            ),
            WorkbenchMetric(
                code="FINDINGS",
                label="Blocking findings",
                value=blocking_findings,
                blocking=blocking_findings,
            ),
        )
        attention = self._attention(project_id, limit=12)
        recent_activity = self.records(
            actor=actor,
            project_id=project_id,
            section=ProjectRecordSection.AUDIT,
            limit=12,
            cursor=None,
            current_only=False,
            query=None,
            statuses=frozenset(),
        ).items
        latest = self.session.scalar(
            select(CalculationRunRow)
            .where(CalculationRunRow.project_id == project_id)
            .order_by(CalculationRunRow.created_at.desc(), CalculationRunRow.id.desc())
            .limit(1)
        )
        return ProjectWorkbench(
            project=self._project_view(project),
            access=self._access_view(membership),
            release_decision=release,
            metrics=metrics,
            attention=attention,
            recent_activity=recent_activity,
            latest_total=latest.grand_total if latest else None,
            latest_currency=latest.currency if latest else None,
            generated_at=utc_now(),
        )

    def records(
        self,
        *,
        actor: Actor,
        project_id: str,
        section: ProjectRecordSection,
        limit: int,
        cursor: str | None,
        current_only: bool,
        query: str | None,
        statuses: frozenset[str],
    ) -> ProjectRecordPage:
        self.projects.get_project(
            actor=actor,
            project_id=project_id,
            required_roles=SECTION_READ_ROLES[section],
        )
        limit = self._limit(limit)
        scope = f"records:{project_id}:{section.value}"
        decoded = self._decode_cursor(cursor, scope)
        records = self._section_records(
            project_id=project_id,
            section=section,
            per_type_limit=limit + 1,
            cursor=decoded,
            current_only=current_only,
            query=self._query(query),
            statuses=statuses,
        )
        records.sort(key=lambda item: (item.occurred_at, item.id), reverse=True)
        selected = tuple(records[:limit])
        return ProjectRecordPage(
            section=section,
            items=selected,
            next_cursor=(
                self._encode_cursor(
                    scope=scope,
                    occurred_at=selected[-1].occurred_at,
                    record_id=selected[-1].id,
                )
                if len(records) > limit and selected
                else None
            ),
        )

    def _section_records(
        self,
        *,
        project_id: str,
        section: ProjectRecordSection,
        per_type_limit: int,
        cursor: _Cursor | None,
        current_only: bool,
        query: str | None,
        statuses: frozenset[str],
    ) -> list[ProjectRecord]:
        handlers = {
            ProjectRecordSection.DOCUMENTS: self._document_records,
            ProjectRecordSection.EVIDENCE: self._evidence_records,
            ProjectRecordSection.BOQ_SCOPE: self._boq_records,
            ProjectRecordSection.PRICING: self._pricing_records,
            ProjectRecordSection.CONTRACT_RISK: self._contract_risk_records,
            ProjectRecordSection.CALCULATION: self._calculation_records,
            ProjectRecordSection.APPROVALS: self._approval_records,
            ProjectRecordSection.ACTUALS: self._actual_records,
            ProjectRecordSection.GOVERNANCE: self._governance_records,
            ProjectRecordSection.AUDIT: self._audit_records,
        }
        return handlers[section](
            project_id,
            per_type_limit,
            cursor,
            current_only,
            query,
            statuses,
        )

    def _document_records(
        self,
        project_id: str,
        limit: int,
        cursor: _Cursor | None,
        current_only: bool,
        query: str | None,
        statuses: frozenset[str],
    ) -> list[ProjectRecord]:
        del statuses
        revision_statement = (
            select(DocumentRevisionRow, DocumentRow)
            .join(DocumentRow, DocumentRow.id == DocumentRevisionRow.document_id)
            .where(DocumentRow.project_id == project_id)
        )
        if current_only:
            revision_statement = revision_statement.where(DocumentRevisionRow.is_current.is_(True))
        if query:
            pattern = f"%{self._escape_like(query)}%"
            revision_statement = revision_statement.where(
                or_(
                    DocumentRow.title.ilike(pattern, escape="\\"),
                    DocumentRow.logical_key.ilike(pattern, escape="\\"),
                    DocumentRevisionRow.original_filename.ilike(pattern, escape="\\"),
                )
            )
        revision_statement = self._before(
            revision_statement,
            DocumentRevisionRow.created_at,
            DocumentRevisionRow.id,
            cursor,
        ).order_by(DocumentRevisionRow.created_at.desc(), DocumentRevisionRow.id.desc())
        records = [
            ProjectRecord(
                id=revision.id,
                section=ProjectRecordSection.DOCUMENTS,
                kind="DOCUMENT_REVISION",
                title=document.title,
                subtitle=revision.original_filename,
                status=(
                    "CANCELLED"
                    if document.cancelled
                    else "CURRENT"
                    if revision.is_current
                    else "SUPERSEDED"
                ),
                current=revision.is_current,
                occurred_at=revision.created_at,
                attributes={
                    "logical_key": document.logical_key,
                    "document_type": document.document_type,
                    "revision_label": revision.revision_label,
                    "issue_date": revision.issue_date,
                    "critical": document.critical,
                    "corrupt": revision.corrupt,
                    "protected": revision.protected,
                    "object_hash": revision.object_hash,
                    "size_bytes": revision.size_bytes,
                },
                links=(
                    ProjectRecordLink(
                        relation="document",
                        entity_type="document",
                        entity_id=document.id,
                    ),
                ),
            )
            for revision, document in self.session.execute(revision_statement.limit(limit)).all()
        ]
        document_sets = select(DocumentSetRevisionRow).where(
            DocumentSetRevisionRow.project_id == project_id
        )
        if current_only:
            document_sets = document_sets.where(DocumentSetRevisionRow.status == "CONFIRMED")
        if query:
            pattern = f"%{self._escape_like(query)}%"
            document_sets = document_sets.where(
                or_(
                    DocumentSetRevisionRow.id.ilike(pattern, escape="\\"),
                    DocumentSetRevisionRow.manifest_hash.ilike(pattern, escape="\\"),
                    DocumentSetRevisionRow.created_by.ilike(pattern, escape="\\"),
                )
            )
        document_sets = self._before(
            document_sets,
            DocumentSetRevisionRow.created_at,
            DocumentSetRevisionRow.id,
            cursor,
        ).order_by(DocumentSetRevisionRow.created_at.desc(), DocumentSetRevisionRow.id.desc())
        records.extend(
            ProjectRecord(
                id=row.id,
                section=ProjectRecordSection.DOCUMENTS,
                kind="DOCUMENT_SET_REVISION",
                title="Document set candidate",
                subtitle=f"{len(row.revision_ids)} revision(s)",
                status=row.status,
                current=row.status == "CONFIRMED",
                occurred_at=row.created_at,
                attributes={
                    "manifest_hash": row.manifest_hash,
                    "revision_ids": row.revision_ids,
                    "created_by": row.created_by,
                    "confirmed_by": row.confirmed_by,
                    "confirmed_at": row.confirmed_at,
                },
            )
            for row in self.session.scalars(document_sets.limit(limit))
        )
        upload_statement = select(QuarantinedUploadRow).where(
            QuarantinedUploadRow.project_id == project_id
        )
        if query:
            pattern = f"%{self._escape_like(query)}%"
            upload_statement = upload_statement.where(
                or_(
                    QuarantinedUploadRow.title.ilike(pattern, escape="\\"),
                    QuarantinedUploadRow.original_filename.ilike(pattern, escape="\\"),
                )
            )
        upload_statement = self._before(
            upload_statement,
            QuarantinedUploadRow.created_at,
            QuarantinedUploadRow.id,
            cursor,
        ).order_by(QuarantinedUploadRow.created_at.desc(), QuarantinedUploadRow.id.desc())
        records.extend(
            ProjectRecord(
                id=row.id,
                section=ProjectRecordSection.DOCUMENTS,
                kind="QUARANTINED_UPLOAD",
                title=row.title,
                subtitle=row.original_filename,
                status=row.status,
                occurred_at=row.created_at,
                attributes={
                    "logical_key": row.logical_key,
                    "document_type": row.document_type,
                    "critical": row.critical,
                    "revision_label": row.revision_label,
                    "object_hash": row.object_hash,
                    "size_bytes": row.size_bytes,
                    "failure_code": row.failure_code,
                    "processing_attempts": row.processing_attempts,
                },
            )
            for row in self.session.scalars(upload_statement.limit(limit))
        )
        return records

    def _evidence_records(
        self,
        project_id: str,
        limit: int,
        cursor: _Cursor | None,
        current_only: bool,
        query: str | None,
        statuses: frozenset[str],
    ) -> list[ProjectRecord]:
        observations = select(ObservationRow).where(ObservationRow.project_id == project_id)
        if statuses:
            observations = observations.where(ObservationRow.status.in_(tuple(statuses)))
        if query:
            observations = observations.where(
                ObservationRow.field_name.ilike(
                    f"%{self._escape_like(query)}%",
                    escape="\\",
                )
            )
        observations = self._before(
            observations,
            ObservationRow.created_at,
            ObservationRow.id,
            cursor,
        ).order_by(ObservationRow.created_at.desc(), ObservationRow.id.desc())
        records = [
            ProjectRecord(
                id=row.id,
                section=ProjectRecordSection.EVIDENCE,
                kind="OBSERVATION",
                title=row.field_name,
                subtitle=row.method,
                status=row.status,
                occurred_at=row.created_at,
                attributes={
                    "method_version": row.method_version,
                    "value": row.payload.get("value"),
                    "unit": row.payload.get("unit"),
                    "locator": row.payload.get("locator"),
                    "adapter_qualification_id": row.payload.get("adapter_qualification_id"),
                },
                links=(
                    ProjectRecordLink(
                        relation="source",
                        entity_type="document_revision",
                        entity_id=row.document_revision_id,
                    ),
                ),
            )
            for row in self.session.scalars(observations.limit(limit))
        ]
        conflicts = select(ConflictRow).where(ConflictRow.project_id == project_id)
        if statuses:
            conflicts = conflicts.where(ConflictRow.status.in_(tuple(statuses)))
        if query:
            conflicts = conflicts.where(
                ConflictRow.field_name.ilike(
                    f"%{self._escape_like(query)}%",
                    escape="\\",
                )
            )
        conflicts = self._before(
            conflicts,
            ConflictRow.created_at,
            ConflictRow.id,
            cursor,
        ).order_by(ConflictRow.created_at.desc(), ConflictRow.id.desc())
        records.extend(
            ProjectRecord(
                id=row.id,
                section=ProjectRecordSection.EVIDENCE,
                kind="CONFLICT",
                title=row.field_name,
                subtitle="Conflicting source observations",
                status=row.status,
                severity=("BLOCKER" if row.status != VerificationStatus.VERIFIED.value else None),
                occurred_at=row.created_at,
                attributes={
                    "observation_ids": row.payload.get("observation_ids", []),
                    "resolution_observation_id": row.payload.get("resolution_observation_id"),
                },
            )
            for row in self.session.scalars(conflicts.limit(limit))
        )
        facts = select(ProjectPassportFactRow).where(
            ProjectPassportFactRow.project_id == project_id
        )
        if current_only:
            facts = facts.where(ProjectPassportFactRow.is_current.is_(True))
        if statuses:
            facts = facts.where(ProjectPassportFactRow.status.in_(tuple(statuses)))
        if query:
            facts = facts.where(
                ProjectPassportFactRow.field_name.ilike(
                    f"%{self._escape_like(query)}%",
                    escape="\\",
                )
            )
        facts = self._before(
            facts,
            ProjectPassportFactRow.updated_at,
            ProjectPassportFactRow.id,
            cursor,
        ).order_by(
            ProjectPassportFactRow.updated_at.desc(),
            ProjectPassportFactRow.id.desc(),
        )
        records.extend(
            ProjectRecord(
                id=row.id,
                section=ProjectRecordSection.EVIDENCE,
                kind="PASSPORT_FACT",
                title=row.field_name,
                subtitle="Project passport",
                status=row.status,
                current=row.is_current,
                occurred_at=row.updated_at,
                attributes={
                    "value": row.payload.get("value"),
                    "unit": row.payload.get("unit"),
                    "observation_ids": row.payload.get("observation_ids", []),
                    "requirements_version_id": row.payload.get("requirements_version_id"),
                    "document_set_revision_id": row.payload.get("document_set_revision_id"),
                    "created_by": row.payload.get("created_by"),
                },
            )
            for row in self.session.scalars(facts.limit(limit))
        )
        return records

    def _boq_records(
        self,
        project_id: str,
        limit: int,
        cursor: _Cursor | None,
        current_only: bool,
        query: str | None,
        statuses: frozenset[str],
    ) -> list[ProjectRecord]:
        lines = select(BoqLineRow).where(BoqLineRow.project_id == project_id)
        if current_only:
            lines = lines.where(BoqLineRow.is_current.is_(True))
        if statuses:
            lines = lines.where(BoqLineRow.status.in_(tuple(statuses)))
        if query:
            pattern = f"%{self._escape_like(query)}%"
            lines = lines.where(
                or_(
                    BoqLineRow.description.ilike(pattern, escape="\\"),
                    BoqLineRow.work_code.ilike(pattern, escape="\\"),
                    BoqLineRow.line_key.ilike(pattern, escape="\\"),
                )
            )
        lines = self._before(
            lines,
            BoqLineRow.updated_at,
            BoqLineRow.id,
            cursor,
        ).order_by(BoqLineRow.updated_at.desc(), BoqLineRow.id.desc())
        line_rows = tuple(self.session.scalars(lines.limit(limit)))
        quantity_by_line = {
            row.boq_line_id: row
            for row in self.session.scalars(
                select(QuantityRow).where(
                    QuantityRow.boq_line_id.in_(tuple(item.id for item in line_rows)),
                    QuantityRow.is_current.is_(True),
                )
            )
        }
        records = [
            ProjectRecord(
                id=row.id,
                section=ProjectRecordSection.BOQ_SCOPE,
                kind="BOQ_LINE",
                title=row.description,
                subtitle=f"{row.wbs_node_id} · {row.work_code}",
                status=row.status,
                current=row.is_current,
                amount=(quantity_by_line[row.id].value if row.id in quantity_by_line else None),
                unit=(quantity_by_line[row.id].unit if row.id in quantity_by_line else row.unit),
                occurred_at=row.updated_at,
                attributes={
                    "line_key": row.line_key,
                    "planned_components": row.payload.get(
                        "expected_cost_components",
                        [],
                    ),
                    "quantity_status": (
                        quantity_by_line[row.id].status if row.id in quantity_by_line else "MISSING"
                    ),
                },
                links=(
                    (
                        ProjectRecordLink(
                            relation="quantity",
                            entity_type="quantity",
                            entity_id=quantity_by_line[row.id].id,
                        ),
                    )
                    if row.id in quantity_by_line
                    else ()
                ),
            )
            for row in line_rows
        ]
        scope = select(ScopeFindingRow).where(ScopeFindingRow.project_id == project_id)
        if current_only:
            scope = scope.where(ScopeFindingRow.resolved.is_(False))
        if statuses:
            scope = scope.where(ScopeFindingRow.severity.in_(tuple(statuses)))
        scope = self._before(
            scope,
            ScopeFindingRow.updated_at,
            ScopeFindingRow.id,
            cursor,
        ).order_by(ScopeFindingRow.updated_at.desc(), ScopeFindingRow.id.desc())
        records.extend(
            ProjectRecord(
                id=row.id,
                section=ProjectRecordSection.BOQ_SCOPE,
                kind="SCOPE_FINDING",
                title=str(row.payload.get("message") or row.rule_id),
                subtitle=row.rule_id,
                status="RESOLVED" if row.resolved else "OPEN",
                severity=row.severity,
                current=not row.resolved,
                occurred_at=row.updated_at,
                attributes={
                    "missing_work_codes": row.payload.get("missing_work_codes", []),
                    "triggering_line_ids": row.payload.get("triggering_line_ids", []),
                },
            )
            for row in self.session.scalars(scope.limit(limit))
        )
        matches = select(NomenclatureMatchRow).where(NomenclatureMatchRow.project_id == project_id)
        if current_only:
            matches = matches.where(NomenclatureMatchRow.is_current.is_(True))
        if statuses:
            matches = matches.where(NomenclatureMatchRow.status.in_(tuple(statuses)))
        matches = self._before(
            matches,
            NomenclatureMatchRow.updated_at,
            NomenclatureMatchRow.id,
            cursor,
        ).order_by(NomenclatureMatchRow.updated_at.desc(), NomenclatureMatchRow.id.desc())
        records.extend(
            ProjectRecord(
                id=row.id,
                section=ProjectRecordSection.BOQ_SCOPE,
                kind="NOMENCLATURE_MATCH",
                title=row.source_item_id,
                subtitle=row.canonical_item_id,
                status=row.status,
                current=row.is_current,
                occurred_at=row.updated_at,
                attributes={
                    "match_class": row.match_class,
                    "catalog_version_id": row.catalog_version_id,
                    "critical_attribute_results": row.payload.get(
                        "critical_attribute_results",
                        [],
                    ),
                },
            )
            for row in self.session.scalars(matches.limit(limit))
        )
        evaluations = select(ScopeEvaluationRow).where(ScopeEvaluationRow.project_id == project_id)
        if current_only:
            evaluations = evaluations.where(ScopeEvaluationRow.is_current.is_(True))
        if statuses:
            evaluations = evaluations.where(ScopeEvaluationRow.status.in_(tuple(statuses)))
        if query:
            evaluations = evaluations.where(
                ScopeEvaluationRow.wbs_node_id.ilike(
                    f"%{self._escape_like(query)}%",
                    escape="\\",
                )
            )
        evaluations = self._before(
            evaluations,
            ScopeEvaluationRow.created_at,
            ScopeEvaluationRow.id,
            cursor,
        ).order_by(
            ScopeEvaluationRow.created_at.desc(),
            ScopeEvaluationRow.id.desc(),
        )
        records.extend(
            ProjectRecord(
                id=row.id,
                section=ProjectRecordSection.BOQ_SCOPE,
                kind="SCOPE_EVALUATION",
                title=row.wbs_node_id,
                subtitle="Scope completeness evaluation",
                status=row.status,
                severity="BLOCKER" if row.status == "BLOCKED" else None,
                current=row.is_current,
                occurred_at=row.created_at,
                attributes={
                    "rule_pack_version_id": row.rule_pack_version_id,
                    "input_signature": row.input_signature,
                    "evaluated_work_codes": row.payload.get(
                        "evaluated_work_codes",
                        [],
                    ),
                    "finding_ids": row.payload.get("finding_ids", []),
                },
            )
            for row in self.session.scalars(evaluations.limit(limit))
        )
        return records

    def _pricing_records(
        self,
        project_id: str,
        limit: int,
        cursor: _Cursor | None,
        current_only: bool,
        query: str | None,
        statuses: frozenset[str],
    ) -> list[ProjectRecord]:
        decisions = select(PriceDecisionRow).where(PriceDecisionRow.project_id == project_id)
        if current_only:
            decisions = decisions.where(PriceDecisionRow.is_current.is_(True))
        if statuses:
            decisions = decisions.where(PriceDecisionRow.status.in_(tuple(statuses)))
        if query:
            decisions = decisions.where(
                PriceDecisionRow.item_id.ilike(
                    f"%{self._escape_like(query)}%",
                    escape="\\",
                )
            )
        decisions = self._before(
            decisions,
            PriceDecisionRow.created_at,
            PriceDecisionRow.id,
            cursor,
        ).order_by(PriceDecisionRow.created_at.desc(), PriceDecisionRow.id.desc())
        records = [
            ProjectRecord(
                id=row.id,
                section=ProjectRecordSection.PRICING,
                kind="PRICE_DECISION",
                title=row.item_id,
                subtitle="Governed normalized unit price",
                status=row.status,
                current=row.is_current,
                amount=row.amount_per_unit,
                currency=row.currency,
                unit=row.unit,
                occurred_at=row.created_at,
                attributes={
                    "policy_version_id": row.policy_version_id,
                    "source_quote_ids": row.payload.get("source_quote_ids", []),
                    "commercial_basis_hash": row.payload.get("commercial_basis_hash"),
                },
            )
            for row in self.session.scalars(decisions.limit(limit))
        ]
        quotes = select(PriceQuoteRow).where(PriceQuoteRow.project_id == project_id)
        if statuses:
            quotes = quotes.where(PriceQuoteRow.status.in_(tuple(statuses)))
        if query:
            quotes = quotes.where(
                PriceQuoteRow.item_id.ilike(
                    f"%{self._escape_like(query)}%",
                    escape="\\",
                )
            )
        quotes = self._before(
            quotes,
            PriceQuoteRow.created_at,
            PriceQuoteRow.id,
            cursor,
        ).order_by(PriceQuoteRow.created_at.desc(), PriceQuoteRow.id.desc())
        records.extend(
            ProjectRecord(
                id=row.id,
                section=ProjectRecordSection.PRICING,
                kind="PRICE_QUOTE",
                title=row.item_id,
                subtitle=f"Quote dated {row.quote_date.isoformat()}",
                status=row.status,
                amount=row.amount,
                currency=row.currency,
                occurred_at=row.created_at,
                attributes={
                    "valid_until": row.valid_until,
                    "source_observation_id": row.source_observation_id,
                    "region": row.payload.get("region"),
                    "vat_included": row.payload.get("vat_included"),
                    "delivery_included": row.payload.get("delivery_included"),
                },
            )
            for row in self.session.scalars(quotes.limit(limit))
        )
        if not statuses or "NORMALIZED" in statuses:
            normalized = (
                select(NormalizedPriceRow, PriceQuoteRow)
                .join(PriceQuoteRow, PriceQuoteRow.id == NormalizedPriceRow.quote_id)
                .where(PriceQuoteRow.project_id == project_id)
            )
            if query:
                normalized = normalized.where(
                    PriceQuoteRow.item_id.ilike(
                        f"%{self._escape_like(query)}%",
                        escape="\\",
                    )
                )
            normalized = self._before(
                normalized,
                NormalizedPriceRow.created_at,
                NormalizedPriceRow.id,
                cursor,
            ).order_by(
                NormalizedPriceRow.created_at.desc(),
                NormalizedPriceRow.id.desc(),
            )
            records.extend(
                ProjectRecord(
                    id=row.id,
                    section=ProjectRecordSection.PRICING,
                    kind="NORMALIZED_PRICE",
                    title=quote.item_id,
                    subtitle="Commercially comparable unit price",
                    status="NORMALIZED",
                    amount=row.amount_per_unit,
                    currency=row.currency,
                    unit=self._string(
                        row.payload.get("normalized_price", {})
                        .get(
                            "target_basis",
                            {},
                        )
                        .get("unit")
                    ),
                    occurred_at=row.created_at,
                    attributes={
                        "quote_id": row.quote_id,
                        "formula_hash": row.formula_hash,
                        "policy_version_id": row.payload.get("policy_version_id"),
                        "target_basis": row.payload.get(
                            "normalized_price",
                            {},
                        ).get("target_basis"),
                        "delivery_component": row.payload.get(
                            "normalized_price",
                            {},
                        ).get("delivery_component"),
                        "unloading_component": row.payload.get(
                            "normalized_price",
                            {},
                        ).get("unloading_component"),
                    },
                    links=(
                        ProjectRecordLink(
                            relation="source",
                            entity_type="price_quote",
                            entity_id=row.quote_id,
                        ),
                    ),
                )
                for row, quote in self.session.execute(normalized.limit(limit)).all()
            )
        rfqs = select(RfqRequestRow).where(RfqRequestRow.project_id == project_id)
        if statuses:
            rfqs = rfqs.where(RfqRequestRow.status.in_(tuple(statuses)))
        rfqs = self._before(
            rfqs,
            RfqRequestRow.updated_at,
            RfqRequestRow.id,
            cursor,
        ).order_by(RfqRequestRow.updated_at.desc(), RfqRequestRow.id.desc())
        records.extend(
            ProjectRecord(
                id=row.id,
                section=ProjectRecordSection.PRICING,
                kind="RFQ",
                title=row.item_id,
                subtitle="Request for quotation",
                status=row.status,
                occurred_at=row.updated_at,
                attributes={"price_decision_id": row.price_decision_id},
            )
            for row in self.session.scalars(rfqs.limit(limit))
        )
        return records

    def _contract_risk_records(
        self,
        project_id: str,
        limit: int,
        cursor: _Cursor | None,
        current_only: bool,
        query: str | None,
        statuses: frozenset[str],
    ) -> list[ProjectRecord]:
        terms = select(ContractTermRow).where(ContractTermRow.project_id == project_id)
        if current_only:
            terms = terms.where(ContractTermRow.is_current.is_(True))
        if query:
            terms = terms.where(
                ContractTermRow.kind.ilike(
                    f"%{self._escape_like(query)}%",
                    escape="\\",
                )
            )
        terms = self._before(
            terms,
            ContractTermRow.updated_at,
            ContractTermRow.id,
            cursor,
        ).order_by(ContractTermRow.updated_at.desc(), ContractTermRow.id.desc())
        records = [
            ProjectRecord(
                id=row.id,
                section=ProjectRecordSection.CONTRACT_RISK,
                kind="CONTRACT_TERM",
                title=row.kind,
                status="VERIFIED" if row.verified else "UNVERIFIED",
                severity=("BLOCKER" if not row.verified or not row.cost_impact_resolved else None),
                current=row.is_current,
                occurred_at=row.updated_at,
                attributes={
                    "cost_impact_resolved": row.cost_impact_resolved,
                    "value": row.payload.get("value"),
                    "unit": row.payload.get("unit"),
                    "observation_ids": row.payload.get("observation_ids", []),
                },
            )
            for row in self.session.scalars(terms.limit(limit))
            if not statuses or ("VERIFIED" if row.verified else "UNVERIFIED") in statuses
        ]
        risks = select(RiskItemRow).where(RiskItemRow.project_id == project_id)
        if current_only:
            risks = risks.where(RiskItemRow.is_current.is_(True))
        if statuses:
            risks = risks.where(RiskItemRow.status.in_(tuple(statuses)))
        if query:
            risks = risks.where(
                RiskItemRow.risk_key.ilike(
                    f"%{self._escape_like(query)}%",
                    escape="\\",
                )
            )
        risks = self._before(
            risks,
            RiskItemRow.updated_at,
            RiskItemRow.id,
            cursor,
        ).order_by(RiskItemRow.updated_at.desc(), RiskItemRow.id.desc())
        records.extend(
            ProjectRecord(
                id=row.id,
                section=ProjectRecordSection.CONTRACT_RISK,
                kind="RISK_ITEM",
                title=str(row.payload.get("title") or row.risk_key),
                subtitle=row.risk_key,
                status=row.status,
                current=row.is_current,
                amount=row.expected_impact,
                currency=row.currency,
                occurred_at=row.updated_at,
                attributes={
                    "probability": row.payload.get("probability"),
                    "impact": row.payload.get("impact"),
                    "mitigation": row.payload.get("mitigation"),
                },
            )
            for row in self.session.scalars(risks.limit(limit))
        )
        models = select(CommercialCostModelRow).where(
            CommercialCostModelRow.project_id == project_id
        )
        if current_only:
            models = models.where(CommercialCostModelRow.is_current.is_(True))
        if statuses:
            models = models.where(CommercialCostModelRow.status.in_(tuple(statuses)))
        models = self._before(
            models,
            CommercialCostModelRow.created_at,
            CommercialCostModelRow.id,
            cursor,
        ).order_by(CommercialCostModelRow.created_at.desc(), CommercialCostModelRow.id.desc())
        records.extend(
            ProjectRecord(
                id=row.id,
                section=ProjectRecordSection.CONTRACT_RISK,
                kind="COMMERCIAL_COST_MODEL",
                title=row.model_kind,
                subtitle=row.target_semantic_key,
                status=row.status,
                current=row.is_current,
                amount=row.total,
                currency=row.currency,
                occurred_at=row.created_at,
                attributes={
                    "independent_total": row.independent_total,
                    "policy_version_id": row.policy_version_id,
                    "document_set_revision_id": row.document_set_revision_id,
                    "target_line_id": row.target_line_id,
                },
            )
            for row in self.session.scalars(models.limit(limit))
        )
        risk_calculations = select(RiskCalculationRow).where(
            RiskCalculationRow.project_id == project_id
        )
        if current_only:
            risk_calculations = risk_calculations.where(RiskCalculationRow.is_current.is_(True))
        if statuses:
            risk_calculations = risk_calculations.where(
                RiskCalculationRow.status.in_(tuple(statuses))
            )
        risk_calculations = self._before(
            risk_calculations,
            RiskCalculationRow.created_at,
            RiskCalculationRow.id,
            cursor,
        ).order_by(
            RiskCalculationRow.created_at.desc(),
            RiskCalculationRow.id.desc(),
        )
        records.extend(
            ProjectRecord(
                id=row.id,
                section=ProjectRecordSection.CONTRACT_RISK,
                kind="RISK_CALCULATION",
                title="Expected risk reserve",
                subtitle=row.policy_version_id,
                status=row.status,
                severity="BLOCKER" if row.status == "BLOCKED" else None,
                current=row.is_current,
                amount=row.expected_reserve,
                currency=row.currency,
                unit=row.unit,
                occurred_at=row.created_at,
                attributes={
                    "input_signature": row.payload.get("input_signature"),
                    "reserve_cost_component": row.payload.get("reserve_cost_component"),
                    "calculated_by": row.payload.get("calculated_by"),
                    "finding_count": len(row.payload.get("calculation", {}).get("findings", [])),
                },
            )
            for row in self.session.scalars(risk_calculations.limit(limit))
        )
        return records

    def _calculation_records(
        self,
        project_id: str,
        limit: int,
        cursor: _Cursor | None,
        current_only: bool,
        query: str | None,
        statuses: frozenset[str],
    ) -> list[ProjectRecord]:
        del current_only
        runs = select(CalculationRunRow).where(CalculationRunRow.project_id == project_id)
        if statuses:
            runs = runs.where(CalculationRunRow.status.in_(tuple(statuses)))
        if query:
            pattern = f"%{self._escape_like(query)}%"
            runs = runs.where(
                or_(
                    CalculationRunRow.id.ilike(pattern, escape="\\"),
                    CalculationRunRow.engine_version.ilike(pattern, escape="\\"),
                )
            )
        runs = self._before(
            runs,
            CalculationRunRow.created_at,
            CalculationRunRow.id,
            cursor,
        ).order_by(CalculationRunRow.created_at.desc(), CalculationRunRow.id.desc())
        records = [
            ProjectRecord(
                id=row.id,
                section=ProjectRecordSection.CALCULATION,
                kind="CALCULATION_RUN",
                title=f"Calculation {row.id}",
                subtitle=row.engine_version,
                status=row.status,
                amount=row.grand_total,
                currency=row.currency,
                occurred_at=row.created_at,
                attributes={
                    "input_hash": row.payload.get("input_hash"),
                    "independent_validation": row.payload.get("independent_validation"),
                },
            )
            for row in self.session.scalars(runs.limit(limit))
        ]
        cost_inputs = select(CostInputRow).where(CostInputRow.project_id == project_id)
        if query:
            pattern = f"%{self._escape_like(query)}%"
            cost_inputs = cost_inputs.where(
                or_(
                    CostInputRow.semantic_key.ilike(pattern, escape="\\"),
                    CostInputRow.category.ilike(pattern, escape="\\"),
                )
            )
        cost_inputs = self._before(
            cost_inputs,
            CostInputRow.created_at,
            CostInputRow.id,
            cursor,
        ).order_by(CostInputRow.created_at.desc(), CostInputRow.id.desc())
        records.extend(
            ProjectRecord(
                id=row.id,
                section=ProjectRecordSection.CALCULATION,
                kind="ATOMIC_COST_INPUT",
                title=row.semantic_key,
                subtitle=row.category,
                status="BOUND" if row.amount_basis_id else "UNSUPPORTED",
                severity=None if row.amount_basis_id else "BLOCKER",
                amount=self._decimal(row.payload.get("unit_rate")),
                currency=self._string(row.payload.get("currency")),
                unit=self._string(row.payload.get("unit")),
                occurred_at=row.created_at,
                attributes={
                    "calculation_run_id": row.calculation_run_id,
                    "line_id": row.payload.get("line_id"),
                    "wbs_node_id": row.payload.get("wbs_node_id"),
                    "quantity": row.payload.get("quantity"),
                    "unit_rate": row.payload.get("unit_rate"),
                    "sign": row.payload.get("sign"),
                    "factors": row.payload.get("factors", []),
                    "amount_basis_id": row.amount_basis_id,
                    "source_observation_id": row.payload.get("source_observation_id"),
                    "approved_assumption_id": row.payload.get("approved_assumption_id"),
                    "normative_rate_id": row.payload.get("normative_rate_id"),
                    "risk_reserve_id": row.payload.get("risk_reserve_id"),
                    "derived_cost_model_id": row.payload.get("derived_cost_model_id"),
                },
                links=(
                    (
                        ProjectRecordLink(
                            relation="calculation",
                            entity_type="calculation_run",
                            entity_id=row.calculation_run_id,
                        ),
                    )
                    if row.calculation_run_id
                    else ()
                ),
            )
            for row in self.session.scalars(cost_inputs.limit(limit))
        )
        snapshots = select(CalculationSnapshotRow).where(
            CalculationSnapshotRow.project_id == project_id
        )
        if query:
            pattern = f"%{self._escape_like(query)}%"
            snapshots = snapshots.where(
                or_(
                    CalculationSnapshotRow.id.ilike(pattern, escape="\\"),
                    CalculationSnapshotRow.calculation_run_id.ilike(
                        pattern,
                        escape="\\",
                    ),
                )
            )
        snapshots = self._before(
            snapshots,
            CalculationSnapshotRow.created_at,
            CalculationSnapshotRow.id,
            cursor,
        ).order_by(
            CalculationSnapshotRow.created_at.desc(),
            CalculationSnapshotRow.id.desc(),
        )
        records.extend(
            ProjectRecord(
                id=row.id,
                section=ProjectRecordSection.CALCULATION,
                kind="SNAPSHOT",
                title=f"Fixed snapshot {row.id}",
                status="FIXED" if row.fixed else "UNFIXED",
                occurred_at=row.created_at,
                attributes={
                    "calculation_run_id": row.calculation_run_id,
                    "document_set_revision_id": row.document_set_revision_id,
                    "input_hash": row.input_hash,
                    "output_hash": row.output_hash,
                    "snapshot_hash": row.snapshot_hash,
                    "created_by": row.created_by,
                },
                links=(
                    ProjectRecordLink(
                        relation="lineage",
                        entity_type="snapshot",
                        entity_id=row.id,
                    ),
                ),
            )
            for row in self.session.scalars(snapshots.limit(limit))
        )
        scenarios = select(ScenarioRunRow).where(ScenarioRunRow.project_id == project_id)
        if statuses:
            scenarios = scenarios.where(ScenarioRunRow.status.in_(tuple(statuses)))
        if query:
            scenarios = scenarios.where(
                ScenarioRunRow.scenario_version.ilike(
                    f"%{self._escape_like(query)}%",
                    escape="\\",
                )
            )
        scenarios = self._before(
            scenarios,
            ScenarioRunRow.created_at,
            ScenarioRunRow.id,
            cursor,
        ).order_by(ScenarioRunRow.created_at.desc(), ScenarioRunRow.id.desc())
        records.extend(
            ProjectRecord(
                id=row.id,
                section=ProjectRecordSection.CALCULATION,
                kind="SCENARIO",
                title=row.scenario_version,
                status=row.status,
                amount=row.grand_total,
                occurred_at=row.created_at,
                attributes={
                    "base_calculation_run_id": row.base_calculation_run_id,
                    "independent_validation": row.payload.get("independent_validation"),
                },
            )
            for row in self.session.scalars(scenarios.limit(limit))
        )
        releases = select(ReleaseDecisionRow).where(ReleaseDecisionRow.project_id == project_id)
        if query:
            pattern = f"%{self._escape_like(query)}%"
            releases = releases.where(
                or_(
                    ReleaseDecisionRow.requested_state.ilike(pattern, escape="\\"),
                    ReleaseDecisionRow.resulting_state.ilike(pattern, escape="\\"),
                )
            )
        releases = self._before(
            releases,
            ReleaseDecisionRow.decided_at,
            ReleaseDecisionRow.id,
            cursor,
        ).order_by(ReleaseDecisionRow.decided_at.desc(), ReleaseDecisionRow.id.desc())
        records.extend(
            ProjectRecord(
                id=row.id,
                section=ProjectRecordSection.CALCULATION,
                kind="RELEASE_DECISION",
                title=row.requested_state,
                subtitle=row.resulting_state,
                status="ALLOWED" if row.allowed else "BLOCKED",
                severity=None if row.allowed else "BLOCKER",
                occurred_at=row.decided_at,
                attributes={
                    "snapshot_id": row.snapshot_id,
                    "decided_by": row.decided_by,
                    "findings": row.payload.get("findings", []),
                },
            )
            for row in self.session.scalars(releases.limit(limit))
        )
        return records

    def _approval_records(
        self,
        project_id: str,
        limit: int,
        cursor: _Cursor | None,
        current_only: bool,
        query: str | None,
        statuses: frozenset[str],
    ) -> list[ProjectRecord]:
        tasks = select(ApprovalTaskRow).where(ApprovalTaskRow.project_id == project_id)
        if current_only:
            tasks = tasks.where(ApprovalTaskRow.status == "PENDING")
        if statuses:
            tasks = tasks.where(ApprovalTaskRow.status.in_(tuple(statuses)))
        if query:
            pattern = f"%{self._escape_like(query)}%"
            tasks = tasks.where(
                or_(
                    ApprovalTaskRow.task_type.ilike(pattern, escape="\\"),
                    ApprovalTaskRow.entity_type.ilike(pattern, escape="\\"),
                    ApprovalTaskRow.entity_id.ilike(pattern, escape="\\"),
                )
            )
        tasks = self._before(
            tasks,
            ApprovalTaskRow.updated_at,
            ApprovalTaskRow.id,
            cursor,
        ).order_by(ApprovalTaskRow.updated_at.desc(), ApprovalTaskRow.id.desc())
        task_rows = tuple(self.session.scalars(tasks.limit(limit)))
        records = [
            ProjectRecord(
                id=row.id,
                section=ProjectRecordSection.APPROVALS,
                kind="APPROVAL_TASK",
                title=row.task_type,
                subtitle=f"{row.entity_type} · {row.entity_id}",
                status=row.status,
                severity="BLOCKER" if row.required and row.status == "PENDING" else None,
                current=row.status == "PENDING",
                occurred_at=row.updated_at,
                attributes={
                    "assigned_role": row.assigned_role,
                    "required": row.required,
                    "created_by": row.payload.get("created_by"),
                    "policy_version_id": row.payload.get("policy_version_id"),
                },
            )
            for row in task_rows
        ]
        if not current_only:
            decisions = (
                select(ApprovalRecordRow)
                .join(ApprovalTaskRow, ApprovalTaskRow.id == ApprovalRecordRow.task_id)
                .where(ApprovalTaskRow.project_id == project_id)
            )
            if statuses:
                decisions = decisions.where(ApprovalRecordRow.decision.in_(tuple(statuses)))
            if query:
                pattern = f"%{self._escape_like(query)}%"
                decisions = decisions.where(
                    or_(
                        ApprovalRecordRow.reason.ilike(pattern, escape="\\"),
                        ApprovalTaskRow.task_type.ilike(pattern, escape="\\"),
                        ApprovalTaskRow.entity_type.ilike(pattern, escape="\\"),
                        ApprovalTaskRow.entity_id.ilike(pattern, escape="\\"),
                    )
                )
            decisions = self._before(
                decisions,
                ApprovalRecordRow.decided_at,
                ApprovalRecordRow.id,
                cursor,
            ).order_by(
                ApprovalRecordRow.decided_at.desc(),
                ApprovalRecordRow.id.desc(),
            )
            records.extend(
                ProjectRecord(
                    id=row.id,
                    section=ProjectRecordSection.APPROVALS,
                    kind="APPROVAL_DECISION",
                    title=row.decision,
                    subtitle=row.task_id,
                    status=row.decision,
                    occurred_at=row.decided_at,
                    attributes={
                        "decided_by": row.decided_by,
                        "reason": row.reason,
                        "evidence_ids": row.payload.get("evidence_ids", []),
                    },
                    links=(
                        ProjectRecordLink(
                            relation="task",
                            entity_type="approval_task",
                            entity_id=row.task_id,
                        ),
                    ),
                )
                for row in self.session.scalars(decisions.limit(limit))
            )
        return records

    def _actual_records(
        self,
        project_id: str,
        limit: int,
        cursor: _Cursor | None,
        current_only: bool,
        query: str | None,
        statuses: frozenset[str],
    ) -> list[ProjectRecord]:
        actuals = select(ActualRecordRow).where(ActualRecordRow.project_id == project_id)
        if current_only:
            actuals = actuals.where(ActualRecordRow.is_current.is_(True))
        if statuses:
            verified = {value == "VERIFIED" for value in statuses}
            if len(verified) == 1:
                actuals = actuals.where(ActualRecordRow.verified.is_(verified.pop()))
        if query:
            pattern = f"%{self._escape_like(query)}%"
            actuals = actuals.where(
                or_(
                    ActualRecordRow.metric.ilike(pattern, escape="\\"),
                    ActualRecordRow.actual_key.ilike(pattern, escape="\\"),
                )
            )
        actuals = self._before(
            actuals,
            ActualRecordRow.created_at,
            ActualRecordRow.id,
            cursor,
        ).order_by(ActualRecordRow.created_at.desc(), ActualRecordRow.id.desc())
        records = [
            ProjectRecord(
                id=row.id,
                section=ProjectRecordSection.ACTUALS,
                kind="ACTUAL",
                title=row.metric,
                subtitle=f"{row.entity_type} · {row.entity_id}",
                status="VERIFIED" if row.verified else "UNVERIFIED",
                current=row.is_current,
                amount=row.value,
                unit=row.unit,
                occurred_at=row.created_at,
                attributes={
                    "actual_key": row.actual_key,
                    "occurred_on": row.occurred_on,
                    "source_observation_id": row.source_observation_id,
                },
            )
            for row in self.session.scalars(actuals.limit(limit))
        ]
        variances = select(VarianceRecordRow).where(VarianceRecordRow.project_id == project_id)
        if query:
            variances = variances.where(
                VarianceRecordRow.metric.ilike(
                    f"%{self._escape_like(query)}%",
                    escape="\\",
                )
            )
        variances = self._before(
            variances,
            VarianceRecordRow.created_at,
            VarianceRecordRow.id,
            cursor,
        ).order_by(VarianceRecordRow.created_at.desc(), VarianceRecordRow.id.desc())
        records.extend(
            ProjectRecord(
                id=row.id,
                section=ProjectRecordSection.ACTUALS,
                kind="VARIANCE",
                title=row.metric,
                subtitle=row.reason,
                amount=row.absolute_variance,
                occurred_at=row.created_at,
                attributes={
                    "relative_variance": row.relative_variance,
                    "actual_record_id": row.actual_record_id,
                    "snapshot_id": row.snapshot_id,
                    "classified_by": row.classified_by,
                },
            )
            for row in self.session.scalars(variances.limit(limit))
        )
        calibrations = select(CalibrationExampleRow).where(
            CalibrationExampleRow.project_id == project_id
        )
        if current_only:
            calibrations = calibrations.where(CalibrationExampleRow.approved.is_(True))
        if statuses:
            approved_values = {value == "APPROVED" for value in statuses}
            if len(approved_values) == 1:
                calibrations = calibrations.where(
                    CalibrationExampleRow.approved.is_(approved_values.pop())
                )
        if query:
            calibrations = calibrations.where(
                CalibrationExampleRow.metric.ilike(
                    f"%{self._escape_like(query)}%",
                    escape="\\",
                )
            )
        calibrations = self._before(
            calibrations,
            CalibrationExampleRow.created_at,
            CalibrationExampleRow.id,
            cursor,
        ).order_by(
            CalibrationExampleRow.created_at.desc(),
            CalibrationExampleRow.id.desc(),
        )
        records.extend(
            ProjectRecord(
                id=row.id,
                section=ProjectRecordSection.ACTUALS,
                kind="CALIBRATION_EXAMPLE",
                title=row.metric,
                subtitle="Verified-fact calibration candidate",
                status="APPROVED" if row.approved else "DRAFT",
                current=row.approved,
                amount=row.target_value,
                unit=row.unit,
                occurred_at=row.created_at,
                attributes={
                    "actual_record_id": row.actual_record_id,
                    "variance_record_id": row.variance_record_id,
                    "features_snapshot_id": row.features_snapshot_id,
                    "variance_reason": row.payload.get(
                        "calibration_example",
                        {},
                    ).get("variance_reason"),
                    "created_by": row.payload.get("created_by"),
                },
            )
            for row in self.session.scalars(calibrations.limit(limit))
        )
        return records

    def _governance_records(
        self,
        project_id: str,
        limit: int,
        cursor: _Cursor | None,
        current_only: bool,
        query: str | None,
        statuses: frozenset[str],
    ) -> list[ProjectRecord]:
        del current_only
        versions = (
            select(ProjectControlledVersionRow, ControlledVersionRow)
            .join(
                ControlledVersionRow,
                ControlledVersionRow.id == ProjectControlledVersionRow.controlled_version_id,
            )
            .where(ProjectControlledVersionRow.project_id == project_id)
        )
        if statuses:
            versions = versions.where(ControlledVersionRow.status.in_(tuple(statuses)))
        if query:
            pattern = f"%{self._escape_like(query)}%"
            versions = versions.where(
                or_(
                    ControlledVersionRow.kind.ilike(pattern, escape="\\"),
                    ControlledVersionRow.version_label.ilike(pattern, escape="\\"),
                    ProjectControlledVersionRow.purpose.ilike(pattern, escape="\\"),
                )
            )
        versions = self._before(
            versions,
            ProjectControlledVersionRow.bound_at,
            ControlledVersionRow.id,
            cursor,
        ).order_by(
            ProjectControlledVersionRow.bound_at.desc(),
            ControlledVersionRow.id.desc(),
        )
        records = [
            ProjectRecord(
                id=version.id,
                section=ProjectRecordSection.GOVERNANCE,
                kind="CONTROLLED_VERSION",
                title=version.kind,
                subtitle=f"{binding.purpose} · {version.version_label}",
                status=version.status,
                occurred_at=binding.bound_at,
                attributes={
                    "content_hash": version.content_hash,
                    "approved_by": version.approved_by,
                    "approved_at": version.approved_at,
                    "bound_by": binding.bound_by,
                },
            )
            for binding, version in self.session.execute(versions.limit(limit)).all()
        ]
        exports = select(ExportArtifactRow).where(ExportArtifactRow.project_id == project_id)
        exports = self._before(
            exports,
            ExportArtifactRow.created_at,
            ExportArtifactRow.id,
            cursor,
        ).order_by(ExportArtifactRow.created_at.desc(), ExportArtifactRow.id.desc())
        records.extend(
            ProjectRecord(
                id=row.id,
                section=ProjectRecordSection.GOVERNANCE,
                kind="EXPORT_ARTIFACT",
                title=row.filename,
                subtitle=row.format,
                status="SIGNED",
                occurred_at=row.created_at,
                attributes={
                    "snapshot_id": row.snapshot_id,
                    "release_decision_id": row.release_decision_id,
                    "object_hash": row.object_hash,
                    "manifest_hash": row.manifest_hash,
                    "signing_key_id": row.signing_key_id,
                    "public_key_fingerprint": row.public_key_fingerprint,
                    "size_bytes": row.size_bytes,
                },
            )
            for row in self.session.scalars(exports.limit(limit))
        )
        return records

    def _audit_records(
        self,
        project_id: str,
        limit: int,
        cursor: _Cursor | None,
        current_only: bool,
        query: str | None,
        statuses: frozenset[str],
    ) -> list[ProjectRecord]:
        del current_only, statuses
        transitions = select(WorkflowTransitionRow).where(
            WorkflowTransitionRow.project_id == project_id
        )
        if query:
            pattern = f"%{self._escape_like(query)}%"
            transitions = transitions.where(
                or_(
                    WorkflowTransitionRow.from_state.ilike(pattern, escape="\\"),
                    WorkflowTransitionRow.to_state.ilike(pattern, escape="\\"),
                    WorkflowTransitionRow.reason.ilike(pattern, escape="\\"),
                )
            )
        transitions = self._before(
            transitions,
            WorkflowTransitionRow.occurred_at,
            WorkflowTransitionRow.id,
            cursor,
        ).order_by(
            WorkflowTransitionRow.occurred_at.desc(),
            WorkflowTransitionRow.id.desc(),
        )
        records = [
            ProjectRecord(
                id=row.id,
                section=ProjectRecordSection.AUDIT,
                kind="WORKFLOW_TRANSITION",
                title=f"{row.from_state} → {row.to_state}",
                subtitle=row.reason,
                status=row.to_state,
                occurred_at=row.occurred_at,
                attributes={"actor_id": row.actor_id},
            )
            for row in self.session.scalars(transitions.limit(limit))
        ]
        events = select(AuditEventRow).where(
            AuditEventRow.aggregate_type == "project",
            AuditEventRow.aggregate_id == project_id,
        )
        if query:
            pattern = f"%{self._escape_like(query)}%"
            events = events.where(
                or_(
                    AuditEventRow.event_type.ilike(pattern, escape="\\"),
                    AuditEventRow.reason.ilike(pattern, escape="\\"),
                    AuditEventRow.actor_id.ilike(pattern, escape="\\"),
                )
            )
        events = self._before(
            events,
            AuditEventRow.occurred_at,
            AuditEventRow.id,
            cursor,
        ).order_by(AuditEventRow.occurred_at.desc(), AuditEventRow.id.desc())
        records.extend(
            ProjectRecord(
                id=row.id,
                section=ProjectRecordSection.AUDIT,
                kind="AUDIT_EVENT",
                title=row.event_type,
                subtitle=row.reason,
                occurred_at=row.occurred_at,
                attributes={
                    "sequence": row.sequence,
                    "actor_id": row.actor_id,
                    "actor_roles": row.actor_roles,
                    "request_id": row.request_id,
                    "event_hash": row.event_hash,
                    "previous_hash": row.previous_hash,
                    "signing_key_id": row.signing_key_id,
                },
            )
            for row in self.session.scalars(events.limit(limit))
        )
        return records

    def _attention(self, project_id: str, *, limit: int) -> tuple[ProjectRecord, ...]:
        records: list[ProjectRecord] = []
        findings = self.session.scalars(
            select(VerificationFindingRow)
            .where(
                VerificationFindingRow.project_id == project_id,
                VerificationFindingRow.resolved.is_(False),
            )
            .order_by(
                VerificationFindingRow.updated_at.desc(),
                VerificationFindingRow.id.desc(),
            )
            .limit(limit)
        )
        records.extend(
            ProjectRecord(
                id=row.id,
                section=ProjectRecordSection.EVIDENCE,
                kind="VERIFICATION_FINDING",
                title=str(row.payload.get("message") or row.code),
                subtitle=row.contour,
                status="OPEN",
                severity=row.severity,
                current=True,
                occurred_at=row.updated_at,
                attributes={"code": row.code, "entity_ids": row.payload.get("entity_ids", [])},
            )
            for row in findings
        )
        conflicts = self.session.scalars(
            select(ConflictRow)
            .where(
                ConflictRow.project_id == project_id,
                ConflictRow.status != VerificationStatus.VERIFIED.value,
            )
            .order_by(ConflictRow.updated_at.desc(), ConflictRow.id.desc())
            .limit(limit)
        )
        records.extend(
            ProjectRecord(
                id=row.id,
                section=ProjectRecordSection.EVIDENCE,
                kind="CONFLICT",
                title=row.field_name,
                status=row.status,
                severity="BLOCKER",
                current=True,
                occurred_at=row.updated_at,
                attributes={
                    "observation_ids": row.payload.get("observation_ids", []),
                },
            )
            for row in conflicts
        )
        tasks = self.session.scalars(
            select(ApprovalTaskRow)
            .where(
                ApprovalTaskRow.project_id == project_id,
                ApprovalTaskRow.status == "PENDING",
            )
            .order_by(ApprovalTaskRow.updated_at.desc(), ApprovalTaskRow.id.desc())
            .limit(limit)
        )
        records.extend(
            ProjectRecord(
                id=row.id,
                section=ProjectRecordSection.APPROVALS,
                kind="APPROVAL_TASK",
                title=row.task_type,
                subtitle=f"{row.entity_type} · {row.entity_id}",
                status=row.status,
                severity="BLOCKER" if row.required else None,
                current=True,
                occurred_at=row.updated_at,
                attributes={
                    "assigned_role": row.assigned_role,
                    "required": row.required,
                },
            )
            for row in tasks
        )
        records.sort(key=lambda item: (item.occurred_at, item.id), reverse=True)
        return tuple(records[:limit])

    @staticmethod
    def _latest_actor_memberships(actor: Actor) -> Select[Any]:
        return (
            select(
                ProjectMembershipRow.project_id.label("project_id"),
                func.max(ProjectMembershipRow.version).label("version"),
            )
            .where(ProjectMembershipRow.principal_id == actor.actor_id)
            .group_by(ProjectMembershipRow.project_id)
        )

    def _current_membership(
        self,
        actor: Actor,
        project_id: str,
    ) -> ProjectMembershipRow:
        row = self.session.scalar(
            select(ProjectMembershipRow)
            .join(ProjectRow, ProjectRow.id == ProjectMembershipRow.project_id)
            .where(
                ProjectMembershipRow.project_id == project_id,
                ProjectMembershipRow.principal_id == actor.actor_id,
                ProjectRow.organization_id == actor.organization_id,
            )
            .order_by(ProjectMembershipRow.version.desc())
            .limit(1)
        )
        if row is None:
            raise LookupError(project_id)
        self._membership_roles(row)
        return row

    @staticmethod
    def _membership_roles(row: ProjectMembershipRow) -> frozenset[ActorRole]:
        try:
            return validate_project_role_evidence(row.roles, row.role_mask)
        except ValueError as error:
            raise RuntimeError("Project membership role evidence is invalid") from error

    @staticmethod
    def _actor_project_role_mask(actor: Actor) -> int:
        roles = tuple(role for role in actor.roles if role is not ActorRole.SYSTEM)
        return project_role_mask(roles) if roles else 0

    def _access_view(self, row: ProjectMembershipRow) -> ProjectAccessView:
        return ProjectAccessView(
            access_level=ProjectAccessLevel(row.access_level),
            roles=tuple(sorted(self._membership_roles(row), key=lambda role: role.value)),
        )

    def _latest_calculations(
        self,
        project_ids: Sequence[str],
    ) -> dict[str, CalculationRunRow]:
        if not project_ids:
            return {}
        ranked = (
            select(
                CalculationRunRow.id.label("id"),
                func.row_number()
                .over(
                    partition_by=CalculationRunRow.project_id,
                    order_by=(
                        CalculationRunRow.created_at.desc(),
                        CalculationRunRow.id.desc(),
                    ),
                )
                .label("position"),
            )
            .where(CalculationRunRow.project_id.in_(project_ids))
            .subquery()
        )
        rows = self.session.scalars(
            select(CalculationRunRow)
            .join(ranked, ranked.c.id == CalculationRunRow.id)
            .where(ranked.c.position == 1)
        )
        return {row.project_id: row for row in rows}

    def _counts(
        self,
        project_column: Any,
        project_ids: Sequence[str],
        *conditions: Any,
    ) -> dict[str, int]:
        if not project_ids:
            return {}
        rows = self.session.execute(
            select(project_column, func.count())
            .where(project_column.in_(project_ids), *conditions)
            .group_by(project_column)
        )
        return {str(project_id): int(count) for project_id, count in rows}

    def _count(self, model: Any, *conditions: Any) -> int:
        return int(
            self.session.scalar(select(func.count()).select_from(model).where(*conditions)) or 0
        )

    @staticmethod
    def _before(
        statement: Select[Any],
        timestamp_column: Any,
        id_column: Any,
        cursor: _Cursor | None,
    ) -> Select[Any]:
        if cursor is None:
            return statement
        occurred_at = ensure_utc(cursor.occurred_at)
        assert occurred_at is not None
        return statement.where(
            or_(
                timestamp_column < occurred_at,
                and_(
                    timestamp_column == occurred_at,
                    id_column < cursor.record_id,
                ),
            )
        )

    @staticmethod
    def _encode_cursor(
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
    def _decode_cursor(cursor: str | None, scope: str) -> _Cursor | None:
        if cursor is None:
            return None
        if not cursor or len(cursor) > 1000:
            raise ValueError("Pagination cursor is invalid")
        try:
            padding = "=" * (-len(cursor) % 4)
            raw = base64.b64decode(cursor + padding, altchars=b"-_", validate=True)
            payload = json.loads(raw.decode("utf-8"))
            decoded = _Cursor.model_validate(payload)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Pagination cursor is invalid") from error
        if decoded.scope != scope:
            raise ValueError("Pagination cursor belongs to another query")
        return decoded

    @staticmethod
    def _limit(value: int) -> int:
        if value < 1 or value > 100:
            raise ValueError("Page limit must be between 1 and 100")
        return value

    @staticmethod
    def _query(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        if len(normalized) > 200:
            raise ValueError("Search query exceeds 200 characters")
        return normalized

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @staticmethod
    def _require_human(actor: Actor) -> None:
        if ActorRole.SYSTEM in actor.roles:
            raise ValueError("SYSTEM identities cannot use operator read models")

    @staticmethod
    def _string(value: object) -> str | None:
        return value if isinstance(value, str) else None

    @staticmethod
    def _project_view(project: ProjectRow) -> ProjectView:
        return ProjectView(
            id=project.id,
            organization_id=project.organization_id,
            code=project.code,
            name=project.name,
            state=ApprovalState(project.state),
            row_version=project.row_version,
            current_document_set_revision_id=project.current_document_set_revision_id,
        )

    def _work_item_view(self, task: ApprovalTaskRow, project: ProjectRow) -> WorkItemView:
        return WorkItemView(
            task_id=task.id,
            project_id=project.id,
            project_code=project.code,
            project_name=project.name,
            task_type=task.task_type,
            entity_type=task.entity_type,
            entity_id=task.entity_id,
            assigned_role=ActorRole(task.assigned_role),
            status=task.status,
            required=task.required,
            created_by=self._string(task.payload.get("created_by")),
            created_at=self._required_utc(task.created_at),
            updated_at=self._required_utc(task.updated_at),
        )

    @staticmethod
    def _required_utc(value: datetime) -> datetime:
        normalized = ensure_utc(value)
        if normalized is None:
            raise RuntimeError("Required timestamp is missing")
        return normalized

    @staticmethod
    def _decimal(value: object) -> Decimal | None:
        if isinstance(value, Decimal):
            return value
        if isinstance(value, (str, int)):
            try:
                return Decimal(value)
            except ArithmeticError:
                return None
        return None
