from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderguard.application.audit_integrity import AuditIntegrityService
from tenderguard.application.operational_qualification import load_approved_profile
from tenderguard.application.projects import ProjectService
from tenderguard.application.snapshot_integrity import read_verified_snapshot
from tenderguard.config import Settings
from tenderguard.domain.audit import verify_chain
from tenderguard.domain.business_qualification import (
    BusinessQualificationDataset,
    BusinessQualificationEvaluation,
    BusinessQualificationProfile,
    QualificationMeasurement,
    QualificationReferenceEvidenceDraft,
    QualificationReferencePayload,
    evaluate_business_qualification,
)
from tenderguard.domain.common import canonical_data, content_hash, ensure_utc, utc_now
from tenderguard.domain.enums import ActorRole, EvidenceMethod, VerificationStatus
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
    AuditEventRow,
    BusinessQualificationApprovalRow,
    BusinessQualificationCampaignRow,
    BusinessQualificationCaseRow,
    BusinessQualificationDiscrepancyReviewRow,
    BusinessQualificationDiscrepancyRow,
    BusinessQualificationEvaluationRow,
    BusinessQualificationReferenceRow,
    CalculationRunRow,
    CalculationSnapshotRow,
    DocumentRevisionRow,
    DocumentRow,
    ObservationRow,
)

BUSINESS_QUALIFICATION_PROFILE_KIND = "business_qualification_profile"
BUSINESS_QUALIFICATION_DATASET_KIND = "business_qualification_dataset"


class QualificationCampaignView(DomainModel):
    campaign_id: str
    organization_id: str
    profile_version_id: str
    dataset_version_id: str
    application_build_reference: str
    status: str
    input_hash: str
    case_count: int
    reference_count: int
    material_discrepancy_count: int
    reviewed_discrepancy_count: int
    created_by: str
    locked_at: datetime
    evaluated_by: str | None = None
    evaluated_at: datetime | None = None
    finalized_by: str | None = None
    finalized_at: datetime | None = None
    result_hash: str | None = None
    approval_package_hash: str | None = None


class QualificationReferenceEvidenceView(DomainModel):
    observation_id: str
    campaign_id: str
    case_id: str
    case_key: str
    mode: str
    status: VerificationStatus
    prepared_by: str
    created_at: datetime


class QualificationCaseView(DomainModel):
    case_id: str
    case_key: str
    mode: str
    project_id: str
    snapshot_id: str
    snapshot_hash: str
    prediction_total: Decimal
    currency: str
    stratum: str
    reference_registered: bool


class QualificationDiscrepancyView(DomainModel):
    discrepancy_id: str
    case_id: str
    absolute_error: Decimal
    exact_ratio_numerator: str
    exact_ratio_denominator: str
    review_id: str | None = None
    review_decision: str | None = None
    review_reason_code: str | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None


class QualificationCampaignDetail(DomainModel):
    campaign: QualificationCampaignView
    cases: tuple[QualificationCaseView, ...]
    discrepancies: tuple[QualificationDiscrepancyView, ...]
    evaluation: BusinessQualificationEvaluation | None = None


class DiscrepancyReviewCommand(DomainModel):
    decision: Literal["ACCEPTED", "REJECTED"]
    reason_code: str = Field(min_length=1, max_length=100)
    root_cause: str = Field(min_length=1, max_length=4000)
    corrective_action: str = Field(min_length=1, max_length=4000)
    evidence_observation_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator("reason_code", "root_cause", "corrective_action")
    @classmethod
    def no_surrounding_whitespace(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("Review fields must not contain surrounding whitespace")
        return value

    @field_validator("evidence_observation_ids")
    @classmethod
    def evidence_ids_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item for item in value):
            raise ValueError("Review evidence observation IDs must be unique and non-empty")
        return value


class BusinessQualificationService:
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

    def create_campaign(
        self,
        *,
        actor: Actor,
        profile_version_id: str,
        profile_content_hash: str,
        dataset_version_id: str,
        dataset_content_hash: str,
        request_id: str,
        reason: str,
    ) -> QualificationCampaignView:
        actor.require_any(ActorRole.AUDITOR, ActorRole.METHODOLOGY_OWNER)
        reason = self._required_text(reason, "reason", 2000)
        build_reference = self.settings.application_build_reference
        if build_reference is None:
            raise ValueError("An immutable application build reference is required")
        profile, profile_row = load_approved_profile(
            session=self.session,
            settings=self.settings,
            version_id=profile_version_id,
            expected_content_hash=profile_content_hash,
            expected_kind=BUSINESS_QUALIFICATION_PROFILE_KIND,
            profile_type=BusinessQualificationProfile,
        )
        dataset, dataset_row = load_approved_profile(
            session=self.session,
            settings=self.settings,
            version_id=dataset_version_id,
            expected_content_hash=dataset_content_hash,
            expected_kind=BUSINESS_QUALIFICATION_DATASET_KIND,
            profile_type=BusinessQualificationDataset,
        )
        self._require_governed_organization(profile_row.payload, actor.organization_id)
        self._require_governed_organization(dataset_row.payload, actor.organization_id)
        if profile.expected_application_build_reference != build_reference:
            raise ValueError("Qualification profile is bound to a different application build")
        now = utc_now()
        cutoff = ensure_utc(dataset.selection_cutoff_at)
        if cutoff is None or cutoff > now:
            raise ValueError("Qualification dataset cutoff cannot be in the future")
        exclusion_ratio = Fraction(len(dataset.exclusions), dataset.population_size)
        if exclusion_ratio > Fraction(profile.maximum_exclusion_ratio):
            raise ValueError("Qualification dataset exceeds the approved exclusion ratio")
        counts = {
            mode: sum(case.mode == mode for case in dataset.cases)
            for mode in ("HISTORICAL", "BLIND", "PARALLEL")
        }
        for mode, count in counts.items():
            if count < profile.mode_thresholds[mode].minimum_cases:  # type: ignore[index]
                raise ValueError(f"Qualification dataset lacks the minimum {mode} cases")

        prepared_cases: list[dict[str, Any]] = []
        historical_references: list[dict[str, Any]] = []
        for planned in sorted(dataset.cases, key=lambda item: item.case_key):
            self.projects.get_project(
                actor=actor,
                project_id=planned.project_id,
                required_roles=(ActorRole.AUDITOR, ActorRole.METHODOLOGY_OWNER),
            )
            snapshot = self.session.scalar(
                select(CalculationSnapshotRow).where(
                    CalculationSnapshotRow.id == planned.snapshot_id,
                    CalculationSnapshotRow.project_id == planned.project_id,
                    CalculationSnapshotRow.fixed.is_(True),
                )
            )
            if snapshot is None:
                raise ValueError(f"Fixed snapshot is missing for case {planned.case_key}")
            snapshot_created_at = self._required_utc(snapshot.created_at, "snapshot created_at")
            if snapshot_created_at > cutoff:
                raise ValueError(
                    f"Snapshot for case {planned.case_key} was created after dataset cutoff"
                )
            primary, snapshot_hash = self._verified_prediction(snapshot)
            if primary.currency != profile.currency or primary.grand_total <= 0:
                raise ValueError(
                    f"Prediction for case {planned.case_key} has an incompatible commercial basis"
                )
            prediction_hash = content_hash(
                {
                    "case_key": planned.case_key,
                    "mode": planned.mode,
                    "project_id": planned.project_id,
                    "snapshot_id": snapshot.id,
                    "snapshot_hash": snapshot_hash,
                    "prediction_total": self._decimal_identity(primary.grand_total),
                    "currency": primary.currency,
                }
            )
            prepared_cases.append(
                {
                    "case_key": planned.case_key,
                    "mode": planned.mode,
                    "project_id": planned.project_id,
                    "snapshot_id": snapshot.id,
                    "snapshot_hash": snapshot_hash,
                    "prediction_total": primary.grand_total,
                    "currency": primary.currency,
                    "prediction_hash": prediction_hash,
                    "stratum": planned.stratum,
                }
            )
            if planned.mode == "HISTORICAL":
                assert planned.historical_actual_id is not None
                historical_references.append(
                    self._verified_historical_reference(
                        project_id=planned.project_id,
                        case_key=planned.case_key,
                        actual_id=planned.historical_actual_id,
                        expected_currency=profile.currency,
                        expected_metric=profile.comparison_metric,
                        comparison_basis_hash=profile.comparison_basis_hash,
                        selection_cutoff_at=cutoff,
                    )
                )

        input_basis = {
            "profile_version_id": profile_row.id,
            "profile_hash": profile_row.content_hash,
            "dataset_version_id": dataset_row.id,
            "dataset_hash": dataset_row.content_hash,
            "application_build_reference": build_reference,
            "population_size": dataset.population_size,
            "cases": [self._case_input_identity(item) for item in prepared_cases],
            "exclusions": dataset.exclusions,
        }
        input_hash = content_hash(input_basis)
        if self.session.scalar(
            select(BusinessQualificationCampaignRow.id).where(
                BusinessQualificationCampaignRow.organization_id == actor.organization_id,
                BusinessQualificationCampaignRow.input_hash == input_hash,
            )
        ):
            raise ValueError("This exact qualification basis is already locked")

        campaign_id = f"business-qualification-{uuid4()}"
        campaign = BusinessQualificationCampaignRow(
            id=campaign_id,
            organization_id=actor.organization_id,
            profile_version_id=profile_row.id,
            dataset_version_id=dataset_row.id,
            profile_hash=profile_row.content_hash,
            dataset_hash=dataset_row.content_hash,
            application_build_reference=build_reference,
            status="INPUTS_LOCKED",
            input_hash=input_hash,
            payload={
                "population_size": dataset.population_size,
                "exclusion_count": len(dataset.exclusions),
                "population_evidence_hash": dataset.population_evidence_hash,
                "selection_query_hash": dataset.selection_query_hash,
                "selection_cutoff_at": dataset.selection_cutoff_at.isoformat(),
                "case_prediction_hashes": [item["prediction_hash"] for item in prepared_cases],
            },
            created_by=actor.actor_id,
            locked_at=now,
        )
        self.session.add(campaign)
        self.session.flush()
        rows_by_key: dict[str, BusinessQualificationCaseRow] = {}
        for item in prepared_cases:
            row = BusinessQualificationCaseRow(
                id=f"business-qualification-case-{uuid4()}",
                campaign_id=campaign.id,
                created_at=now,
                **item,
            )
            self.session.add(row)
            rows_by_key[row.case_key] = row
        self.session.flush()
        for reference in historical_references:
            case = rows_by_key[reference.pop("case_key")]
            self.session.add(
                BusinessQualificationReferenceRow(
                    id=f"business-qualification-reference-{uuid4()}",
                    campaign_id=campaign.id,
                    case_id=case.id,
                    registered_by=actor.actor_id,
                    registered_at=now,
                    **reference,
                )
            )
        self._audit(
            campaign=campaign,
            actor=actor,
            request_id=request_id,
            reason=reason,
            event_type="business_qualification_inputs_locked",
            payload={
                "input_hash": input_hash,
                "profile_version_id": profile_row.id,
                "profile_hash": profile_row.content_hash,
                "dataset_version_id": dataset_row.id,
                "dataset_hash": dataset_row.content_hash,
                "application_build_reference": build_reference,
                "case_count": len(prepared_cases),
                "historical_reference_count": len(historical_references),
            },
        )
        return self._view(campaign)

    def prepare_reference_evidence(
        self,
        *,
        actor: Actor,
        campaign_id: str,
        case_id: str,
        draft: QualificationReferenceEvidenceDraft,
        request_id: str,
        reason: str,
    ) -> QualificationReferenceEvidenceView:
        reason = self._required_text(reason, "reason", 2000)
        campaign, case = self._locked_case(
            actor=actor,
            campaign_id=campaign_id,
            case_id=case_id,
            required_roles=(
                ActorRole.ESTIMATOR,
                ActorRole.TECHNICAL_EXPERT,
                ActorRole.REVIEWER,
            ),
        )
        if case.mode == "HISTORICAL":
            raise ValueError("Historical references are pre-bound by the approved dataset")
        if draft.case_key != case.case_key or draft.mode != case.mode:
            raise ValueError("Reference evidence does not match the locked case")
        if draft.currency != case.currency:
            raise ValueError("Reference evidence currency differs from the locked case")
        profile, _ = self._bound_inputs(campaign)
        if draft.comparison_basis_hash != profile.comparison_basis_hash:
            raise ValueError(
                "Reference evidence commercial basis differs from the approved profile"
            )
        if draft.professional_estimator_id != actor.actor_id:
            raise ValueError("Reference preparer must be the accountable professional estimator")
        if actor.actor_id in {campaign.created_by, self._snapshot_creator(case.snapshot_id)}:
            raise ValueError("Reference preparer is not independent from the system result")
        performed_at = self._required_utc(draft.performed_at, "performed_at")
        locked_at = self._required_utc(campaign.locked_at, "locked_at")
        if performed_at < locked_at or performed_at > utc_now():
            raise ValueError(
                "Reference must be performed after inputs were locked and not in future"
            )
        revision = self.session.scalar(
            select(DocumentRevisionRow)
            .join(DocumentRow, DocumentRow.id == DocumentRevisionRow.document_id)
            .where(
                DocumentRevisionRow.id == draft.location.document_revision_id,
                DocumentRow.id == draft.location.document_id,
                DocumentRow.project_id == case.project_id,
                DocumentRow.cancelled.is_(False),
                DocumentRevisionRow.corrupt.is_(False),
                DocumentRevisionRow.protected.is_(False),
            )
        )
        if revision is None or revision.object_hash != draft.location.original_object_hash:
            raise ValueError(
                "Reference evidence location is not an exact project document revision"
            )
        identity = {
            "campaign_id": campaign.id,
            "case_id": case.id,
            "draft": draft,
            "prepared_by": actor.actor_id,
        }
        observation_id = f"observation-{content_hash(identity)[:24]}"
        existing = self.session.get(ObservationRow, observation_id)
        if existing is not None:
            return self._reference_evidence_view(existing)
        now = utc_now()
        observation = Observation(
            observation_id=observation_id,
            field_name=f"qualification_reference:{campaign.id}:{case.case_key}",
            value=draft.amount,
            unit=draft.currency,
            method=EvidenceMethod.MANUAL,
            method_version="tenderguard.qualification-reference-evidence/v1",
            source_priority=0,
            location=draft.location,
            observed_at=performed_at,
            actor_id=actor.actor_id,
            confidence=None,
            status=VerificationStatus.UNVERIFIED,
        )
        self.session.add(
            ObservationRow(
                id=observation.observation_id,
                project_id=case.project_id,
                document_revision_id=revision.id,
                field_name=observation.field_name,
                method=observation.method.value,
                method_version=observation.method_version,
                status=observation.status.value,
                payload={
                    "observation": observation.model_dump(mode="json"),
                    "qualification_reference_draft": draft.model_dump(mode="json"),
                    "campaign_id": campaign.id,
                    "case_id": case.id,
                    "prepared_by": actor.actor_id,
                },
                created_at=now,
            )
        )
        self._audit(
            campaign=campaign,
            actor=actor,
            request_id=request_id,
            reason=reason,
            event_type="business_qualification_reference_evidence_prepared",
            payload={
                "case_id": case.id,
                "observation_id": observation.observation_id,
                "evidence_hash": content_hash(identity),
            },
        )
        return QualificationReferenceEvidenceView(
            observation_id=observation.observation_id,
            campaign_id=campaign.id,
            case_id=case.id,
            case_key=case.case_key,
            mode=case.mode,
            status=VerificationStatus.UNVERIFIED,
            prepared_by=actor.actor_id,
            created_at=now,
        )

    def verify_and_register_reference(
        self,
        *,
        actor: Actor,
        campaign_id: str,
        case_id: str,
        prepared_observation_id: str,
        request_id: str,
        reason: str,
    ) -> QualificationCampaignView:
        reason = self._required_text(reason, "reason", 2000)
        campaign, case = self._locked_case(
            actor=actor,
            campaign_id=campaign_id,
            case_id=case_id,
            required_roles=(ActorRole.REVIEWER, ActorRole.AUDITOR),
        )
        if case.mode == "HISTORICAL":
            raise ValueError("Historical reference cannot be registered manually")
        if self.session.scalar(
            select(BusinessQualificationReferenceRow.id).where(
                BusinessQualificationReferenceRow.case_id == case.id
            )
        ):
            raise ValueError("Qualification case already has a reference")
        prepared = self.session.scalar(
            select(ObservationRow).where(
                ObservationRow.id == prepared_observation_id,
                ObservationRow.project_id == case.project_id,
                ObservationRow.status == VerificationStatus.UNVERIFIED.value,
            )
        )
        if prepared is None:
            raise ValueError("Prepared qualification reference evidence is unavailable")
        payload = prepared.payload
        if payload.get("campaign_id") != campaign.id or payload.get("case_id") != case.id:
            raise ValueError("Prepared evidence is not bound to this campaign case")
        prepared_by = payload.get("prepared_by")
        if not isinstance(prepared_by, str) or not prepared_by:
            raise ValueError("Prepared evidence lacks its accountable preparer")
        if actor.actor_id in {
            prepared_by,
            campaign.created_by,
            self._snapshot_creator(case.snapshot_id),
        }:
            raise ValueError("Reference verification requires an independent actor")
        draft = QualificationReferenceEvidenceDraft.model_validate(
            payload.get("qualification_reference_draft")
        )
        profile, _ = self._bound_inputs(campaign)
        if draft.comparison_basis_hash != profile.comparison_basis_hash:
            raise ValueError(
                "Prepared reference commercial basis differs from the approved profile"
            )
        observation = Observation.model_validate(payload.get("observation"))
        try:
            observed_amount = Decimal(str(observation.value))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise ValueError("Prepared qualification reference amount is invalid") from error
        if (
            draft.case_key != case.case_key
            or draft.mode != case.mode
            or observed_amount != draft.amount
            or observation.unit != draft.currency
            or observation.actor_id != prepared_by
            or observation.status is not VerificationStatus.UNVERIFIED
        ):
            raise ValueError("Prepared qualification reference evidence does not verify")
        now = utc_now()
        verified_identity = {
            "campaign_id": campaign.id,
            "case_id": case.id,
            "prepared_observation_id": prepared.id,
            "reviewed_by": actor.actor_id,
            "draft": draft,
        }
        verified_observation_id = f"observation-{content_hash(verified_identity)[:24]}"
        if self.session.get(ObservationRow, verified_observation_id) is not None:
            raise ValueError("This prepared qualification reference was already reviewed")
        verified_observation = Observation(
            observation_id=verified_observation_id,
            field_name=observation.field_name,
            value=draft.amount,
            unit=draft.currency,
            method=EvidenceMethod.RULE_ENGINE,
            method_version="tenderguard.qualification-reference-verification/v1",
            source_priority=observation.source_priority,
            location=draft.location,
            observed_at=draft.performed_at,
            actor_id=actor.actor_id,
            confidence=None,
            status=VerificationStatus.VERIFIED,
        )
        evidence_hash = content_hash(
            {
                "prepared_observation": payload,
                "prepared_observation_id": prepared.id,
                "verified_observation": verified_observation,
                "reviewed_by": actor.actor_id,
            }
        )
        reference_payload = QualificationReferencePayload(
            schema_version="tenderguard.qualification-reference/v1",
            case_key=case.case_key,
            mode=draft.mode,
            amount=draft.amount,
            currency=draft.currency,
            comparison_basis_hash=draft.comparison_basis_hash,
            reference_kind=draft.reference_kind,
            professional_estimator_id=draft.professional_estimator_id,
            independence_domain=draft.independence_domain,
            performed_at=draft.performed_at,
            evidence_hash=evidence_hash,
            prepared_by=prepared_by,
            reviewed_by=actor.actor_id,
            blinded_to_system_result=draft.blinded_to_system_result,
            no_bid_authority=draft.no_bid_authority,
        )
        self.session.add(
            ObservationRow(
                id=verified_observation.observation_id,
                project_id=case.project_id,
                document_revision_id=prepared.document_revision_id,
                field_name=verified_observation.field_name,
                method=verified_observation.method.value,
                method_version=verified_observation.method_version,
                status=verified_observation.status.value,
                payload={
                    "observation": verified_observation.model_dump(mode="json"),
                    "qualification_reference": reference_payload.model_dump(mode="json"),
                    "source_observation_ids": [prepared.id],
                },
                created_at=now,
            )
        )
        self.session.add(
            BusinessQualificationReferenceRow(
                id=f"business-qualification-reference-{uuid4()}",
                campaign_id=campaign.id,
                case_id=case.id,
                reference_kind=reference_payload.reference_kind,
                source_entity_type="OBSERVATION",
                source_entity_id=verified_observation.observation_id,
                reference_total=reference_payload.amount,
                currency=reference_payload.currency,
                evidence_hash=evidence_hash,
                independence_domain=reference_payload.independence_domain,
                professional_estimator_id=reference_payload.professional_estimator_id,
                performed_at=reference_payload.performed_at,
                payload=reference_payload.model_dump(mode="json"),
                registered_by=actor.actor_id,
                registered_at=now,
            )
        )
        self._audit(
            campaign=campaign,
            actor=actor,
            request_id=request_id,
            reason=reason,
            event_type="business_qualification_reference_registered",
            payload={
                "case_id": case.id,
                "prepared_observation_id": prepared.id,
                "verified_observation_id": verified_observation.observation_id,
                "evidence_hash": evidence_hash,
                "reference_kind": reference_payload.reference_kind,
            },
        )
        return self._view(campaign)

    def evaluate(
        self,
        *,
        actor: Actor,
        campaign_id: str,
        request_id: str,
        reason: str,
    ) -> BusinessQualificationEvaluation:
        actor.require_any(ActorRole.AUDITOR, ActorRole.METHODOLOGY_OWNER)
        reason = self._required_text(reason, "reason", 2000)
        campaign = self._campaign(
            actor=actor,
            campaign_id=campaign_id,
            lock=True,
        )
        if campaign.status != "INPUTS_LOCKED":
            raise ValueError("Only an INPUTS_LOCKED campaign can be evaluated")
        if actor.actor_id == campaign.created_by:
            raise ValueError("Campaign evaluation requires a different actor")
        profile, dataset = self._bound_inputs(campaign)
        cases = list(
            self.session.scalars(
                select(BusinessQualificationCaseRow)
                .where(BusinessQualificationCaseRow.campaign_id == campaign.id)
                .order_by(BusinessQualificationCaseRow.case_key)
            )
        )
        references = list(
            self.session.scalars(
                select(BusinessQualificationReferenceRow).where(
                    BusinessQualificationReferenceRow.campaign_id == campaign.id
                )
            )
        )
        references_by_case = {row.case_id: row for row in references}
        if len(references_by_case) != len(cases):
            raise ValueError("Every qualification case requires an immutable reference")
        reference_actors = {
            reference_actor
            for reference in references
            for reference_actor in (
                reference.registered_by,
                reference.payload.get("prepared_by"),
            )
            if isinstance(reference_actor, str)
        }
        if actor.actor_id in reference_actors:
            raise ValueError(
                "Campaign evaluation requires independence from reference preparation "
                "and registration"
            )
        measurements: list[QualificationMeasurement] = []
        for case in cases:
            self.projects.get_project(
                actor=actor,
                project_id=case.project_id,
                required_roles=(ActorRole.AUDITOR, ActorRole.METHODOLOGY_OWNER),
            )
            snapshot = self.session.get(CalculationSnapshotRow, case.snapshot_id)
            if snapshot is None or snapshot.project_id != case.project_id or not snapshot.fixed:
                raise ValueError("Locked qualification snapshot is unavailable")
            primary, snapshot_hash = self._verified_prediction(snapshot)
            if (
                snapshot_hash != case.snapshot_hash
                or primary.grand_total != case.prediction_total
                or primary.currency != case.currency
                or content_hash(
                    {
                        "case_key": case.case_key,
                        "mode": case.mode,
                        "project_id": case.project_id,
                        "snapshot_id": case.snapshot_id,
                        "snapshot_hash": case.snapshot_hash,
                        "prediction_total": self._decimal_identity(case.prediction_total),
                        "currency": case.currency,
                    }
                )
                != case.prediction_hash
            ):
                raise ValueError("Locked qualification prediction no longer verifies")
            reference = references_by_case[case.id]
            self._reverify_reference(case, reference, campaign, profile)
            measurements.append(
                QualificationMeasurement(
                    case_id=case.id,
                    case_key=case.case_key,
                    mode=case.mode,
                    prediction_total=case.prediction_total,
                    reference_total=reference.reference_total,
                    currency=reference.currency,
                    independence_domain=reference.independence_domain,
                    reference_performed_at=self._required_utc(
                        reference.performed_at,
                        "reference performed_at",
                    ),
                )
            )
        now = utc_now()
        evaluation = evaluate_business_qualification(
            campaign_id=campaign.id,
            profile_version_id=campaign.profile_version_id,
            dataset_version_id=campaign.dataset_version_id,
            profile=profile,
            measurements=tuple(measurements),
            population_size=dataset.population_size,
            exclusion_count=len(dataset.exclusions),
            evaluated_at=now,
        )
        evaluation_row = BusinessQualificationEvaluationRow(
            id=f"business-qualification-evaluation-{uuid4()}",
            campaign_id=campaign.id,
            metrics_passed=evaluation.metrics_passed,
            result_hash=evaluation.result_hash,
            payload=evaluation.model_dump(mode="json"),
            evaluated_by=actor.actor_id,
            evaluated_at=now,
        )
        self.session.add(evaluation_row)
        self.session.flush()
        for metric in evaluation.cases:
            if not metric.material:
                continue
            self.session.add(
                BusinessQualificationDiscrepancyRow(
                    id=f"business-qualification-discrepancy-{uuid4()}",
                    campaign_id=campaign.id,
                    evaluation_id=evaluation_row.id,
                    case_id=metric.case_id,
                    absolute_error=metric.absolute_error,
                    exact_ratio_numerator=str(abs(metric.exact_signed_ratio_numerator)),
                    exact_ratio_denominator=str(metric.exact_signed_ratio_denominator),
                    payload=metric.model_dump(mode="json"),
                    created_at=now,
                )
            )
        campaign.status = "EXPERT_REVIEW" if evaluation.metrics_passed else "FAILED"
        campaign.evaluated_by = actor.actor_id
        campaign.evaluated_at = now
        campaign.result_hash = evaluation.result_hash
        self._audit(
            campaign=campaign,
            actor=actor,
            request_id=request_id,
            reason=reason,
            event_type="business_qualification_evaluated",
            payload={
                "evaluation_id": evaluation_row.id,
                "result_hash": evaluation.result_hash,
                "metrics_passed": evaluation.metrics_passed,
                "resulting_status": campaign.status,
                "material_discrepancy_count": sum(metric.material for metric in evaluation.cases),
                "findings": evaluation.findings,
            },
        )
        return evaluation

    def review_discrepancy(
        self,
        *,
        actor: Actor,
        campaign_id: str,
        discrepancy_id: str,
        command: DiscrepancyReviewCommand,
        request_id: str,
        reason: str,
    ) -> QualificationCampaignView:
        actor.require_any(ActorRole.AUDITOR, ActorRole.METHODOLOGY_OWNER)
        reason = self._required_text(reason, "reason", 2000)
        campaign = self._campaign(actor=actor, campaign_id=campaign_id, lock=True)
        if campaign.status not in {"EXPERT_REVIEW", "FAILED"}:
            raise ValueError("Campaign is not awaiting discrepancy analysis")
        discrepancy = self.session.scalar(
            select(BusinessQualificationDiscrepancyRow).where(
                BusinessQualificationDiscrepancyRow.id == discrepancy_id,
                BusinessQualificationDiscrepancyRow.campaign_id == campaign.id,
            )
        )
        if discrepancy is None:
            raise LookupError(discrepancy_id)
        case = self.session.get(BusinessQualificationCaseRow, discrepancy.case_id)
        if case is None:
            raise ValueError("Qualification discrepancy case is missing")
        self.projects.get_project(
            actor=actor,
            project_id=case.project_id,
            required_roles=(ActorRole.AUDITOR, ActorRole.METHODOLOGY_OWNER),
        )
        reference = self.session.scalar(
            select(BusinessQualificationReferenceRow).where(
                BusinessQualificationReferenceRow.case_id == case.id
            )
        )
        excluded_actors = {
            campaign.created_by,
            campaign.evaluated_by,
            case and self._snapshot_creator(case.snapshot_id),
            reference.registered_by if reference else None,
            reference.payload.get("prepared_by") if reference else None,
        }
        if actor.actor_id in excluded_actors:
            raise ValueError("Discrepancy review requires an independent actor")
        if self.session.scalar(
            select(BusinessQualificationDiscrepancyReviewRow.id).where(
                BusinessQualificationDiscrepancyReviewRow.discrepancy_id == discrepancy.id
            )
        ):
            raise ValueError("Qualification discrepancy already has an immutable review")
        profile, _ = self._bound_inputs(campaign)
        if command.reason_code not in profile.allowed_discrepancy_reason_codes:
            raise ValueError("Discrepancy reason code is not approved by methodology")
        observations = list(
            self.session.scalars(
                select(ObservationRow).where(
                    ObservationRow.project_id == case.project_id,
                    ObservationRow.id.in_(command.evidence_observation_ids),
                    ObservationRow.status == VerificationStatus.VERIFIED.value,
                )
            )
        )
        if len(observations) != len(command.evidence_observation_ids):
            raise ValueError("Every discrepancy review evidence item must be verified")
        evidence_basis = [
            {
                "id": row.id,
                "document_revision_id": row.document_revision_id,
                "payload": row.payload,
            }
            for row in sorted(observations, key=lambda item: item.id)
        ]
        evidence_hash = content_hash(evidence_basis)
        now = utc_now()
        self.session.add(
            BusinessQualificationDiscrepancyReviewRow(
                id=f"business-qualification-review-{uuid4()}",
                discrepancy_id=discrepancy.id,
                decision=command.decision,
                reason_code=command.reason_code,
                root_cause=command.root_cause,
                corrective_action=command.corrective_action,
                evidence_hash=evidence_hash,
                evidence_observation_ids=sorted(command.evidence_observation_ids),
                reviewed_by=actor.actor_id,
                reviewed_at=now,
            )
        )
        self._audit(
            campaign=campaign,
            actor=actor,
            request_id=request_id,
            reason=reason,
            event_type="business_qualification_discrepancy_reviewed",
            payload={
                "discrepancy_id": discrepancy.id,
                "case_id": case.id,
                "decision": command.decision,
                "reason_code": command.reason_code,
                "evidence_hash": evidence_hash,
            },
        )
        return self._view(campaign)

    def approve_campaign(
        self,
        *,
        actor: Actor,
        campaign_id: str,
        request_id: str,
        reason: str,
    ) -> QualificationCampaignView:
        actor.require_any(ActorRole.METHODOLOGY_OWNER)
        reason = self._required_text(reason, "reason", 2000)
        campaign = self._campaign(actor=actor, campaign_id=campaign_id, lock=True)
        if campaign.status != "EXPERT_REVIEW":
            raise ValueError("Only a metrics-passing campaign can be approved")
        self._require_campaign_project_access(actor, campaign)
        if actor.actor_id in {campaign.created_by, campaign.evaluated_by}:
            raise ValueError("Final qualification approval requires an independent actor")
        profile, dataset = self._bound_inputs(campaign)
        evaluation_row = self.session.scalar(
            select(BusinessQualificationEvaluationRow).where(
                BusinessQualificationEvaluationRow.campaign_id == campaign.id
            )
        )
        if evaluation_row is None or not evaluation_row.metrics_passed:
            raise ValueError("Passing immutable qualification evaluation is missing")
        evaluation = BusinessQualificationEvaluation.model_validate(evaluation_row.payload)
        if (
            evaluation.result_hash != evaluation_row.result_hash
            or evaluation.result_hash != campaign.result_hash
        ):
            raise ValueError("Qualification evaluation integrity check failed")
        discrepancies = list(
            self.session.scalars(
                select(BusinessQualificationDiscrepancyRow).where(
                    BusinessQualificationDiscrepancyRow.campaign_id == campaign.id
                )
            )
        )
        reviews = (
            list(
                self.session.scalars(
                    select(BusinessQualificationDiscrepancyReviewRow).where(
                        BusinessQualificationDiscrepancyReviewRow.discrepancy_id.in_(
                            [row.id for row in discrepancies]
                        )
                    )
                )
            )
            if discrepancies
            else []
        )
        reviews_by_discrepancy = {row.discrepancy_id: row for row in reviews}
        if any(
            discrepancy.id not in reviews_by_discrepancy
            or reviews_by_discrepancy[discrepancy.id].decision != "ACCEPTED"
            for discrepancy in discrepancies
        ):
            raise ValueError("Every material discrepancy requires an accepted expert review")
        if any(review.reviewed_by == actor.actor_id for review in reviews):
            raise ValueError("Final approver cannot approve their own discrepancy review")
        reference_rows = list(
            self.session.scalars(
                select(BusinessQualificationReferenceRow).where(
                    BusinessQualificationReferenceRow.campaign_id == campaign.id
                )
            )
        )
        reference_actors = {
            reference_actor
            for reference in reference_rows
            for reference_actor in (
                reference.registered_by,
                reference.payload.get("prepared_by"),
            )
            if isinstance(reference_actor, str)
        }
        if actor.actor_id in reference_actors:
            raise ValueError(
                "Final approver must be independent from reference preparation and registration"
            )
        package_body = {
            "campaign_id": campaign.id,
            "input_hash": campaign.input_hash,
            "profile_version_id": campaign.profile_version_id,
            "profile_hash": campaign.profile_hash,
            "dataset_version_id": campaign.dataset_version_id,
            "dataset_hash": campaign.dataset_hash,
            "application_build_reference": campaign.application_build_reference,
            "evaluation_id": evaluation_row.id,
            "evaluation_result_hash": evaluation.result_hash,
            "population_size": dataset.population_size,
            "profile_schema_version": profile.schema_version,
            "accepted_discrepancy_reviews": [
                {
                    "discrepancy_id": discrepancy.id,
                    "review_id": reviews_by_discrepancy[discrepancy.id].id,
                    "evidence_hash": reviews_by_discrepancy[discrepancy.id].evidence_hash,
                }
                for discrepancy in sorted(discrepancies, key=lambda item: item.id)
            ],
        }
        package_hash = content_hash(package_body)
        now = utc_now()
        self.session.add(
            BusinessQualificationApprovalRow(
                id=f"business-qualification-approval-{uuid4()}",
                campaign_id=campaign.id,
                evaluation_id=evaluation_row.id,
                package_hash=package_hash,
                reason=reason,
                approved_by=actor.actor_id,
                approved_at=now,
            )
        )
        campaign.status = "PASSED"
        campaign.finalized_by = actor.actor_id
        campaign.finalized_at = now
        self._audit(
            campaign=campaign,
            actor=actor,
            request_id=request_id,
            reason=reason,
            event_type="business_qualification_approved",
            payload={
                "evaluation_id": evaluation_row.id,
                "evaluation_result_hash": evaluation.result_hash,
                "package_hash": package_hash,
            },
        )
        return self._view(campaign)

    def get_campaign(
        self,
        *,
        actor: Actor,
        campaign_id: str,
    ) -> QualificationCampaignView:
        return self.get_campaign_detail(
            actor=actor,
            campaign_id=campaign_id,
        ).campaign

    def get_campaign_detail(
        self,
        *,
        actor: Actor,
        campaign_id: str,
    ) -> QualificationCampaignDetail:
        campaign = self._campaign(actor=actor, campaign_id=campaign_id)
        case_rows = list(
            self.session.scalars(
                select(BusinessQualificationCaseRow)
                .where(BusinessQualificationCaseRow.campaign_id == campaign.id)
                .order_by(BusinessQualificationCaseRow.case_key)
            )
        )
        for case in case_rows:
            self.projects.get_project(
                actor=actor,
                project_id=case.project_id,
                required_roles=(ActorRole.AUDITOR, ActorRole.METHODOLOGY_OWNER),
            )
        references = {
            row.case_id: row
            for row in self.session.scalars(
                select(BusinessQualificationReferenceRow).where(
                    BusinessQualificationReferenceRow.campaign_id == campaign.id
                )
            )
        }
        discrepancy_rows = list(
            self.session.scalars(
                select(BusinessQualificationDiscrepancyRow)
                .where(BusinessQualificationDiscrepancyRow.campaign_id == campaign.id)
                .order_by(BusinessQualificationDiscrepancyRow.id)
            )
        )
        reviews = (
            {
                row.discrepancy_id: row
                for row in self.session.scalars(
                    select(BusinessQualificationDiscrepancyReviewRow).where(
                        BusinessQualificationDiscrepancyReviewRow.discrepancy_id.in_(
                            [item.id for item in discrepancy_rows]
                        )
                    )
                )
            }
            if discrepancy_rows
            else {}
        )
        evaluation_row = self.session.scalar(
            select(BusinessQualificationEvaluationRow).where(
                BusinessQualificationEvaluationRow.campaign_id == campaign.id
            )
        )
        evaluation = (
            BusinessQualificationEvaluation.model_validate(evaluation_row.payload)
            if evaluation_row is not None
            else None
        )
        return QualificationCampaignDetail(
            campaign=self._view(campaign),
            cases=tuple(
                QualificationCaseView(
                    case_id=case.id,
                    case_key=case.case_key,
                    mode=case.mode,
                    project_id=case.project_id,
                    snapshot_id=case.snapshot_id,
                    snapshot_hash=case.snapshot_hash,
                    prediction_total=case.prediction_total,
                    currency=case.currency,
                    stratum=case.stratum,
                    reference_registered=case.id in references,
                )
                for case in case_rows
            ),
            discrepancies=tuple(
                self._discrepancy_view(
                    discrepancy,
                    reviews.get(discrepancy.id),
                )
                for discrepancy in discrepancy_rows
            ),
            evaluation=evaluation,
        )

    def _require_campaign_project_access(
        self,
        actor: Actor,
        campaign: BusinessQualificationCampaignRow,
    ) -> None:
        project_ids = set(
            self.session.scalars(
                select(BusinessQualificationCaseRow.project_id).where(
                    BusinessQualificationCaseRow.campaign_id == campaign.id
                )
            )
        )
        if not project_ids:
            raise ValueError("Qualification campaign has no cases")
        for project_id in sorted(project_ids):
            self.projects.get_project(
                actor=actor,
                project_id=project_id,
                required_roles=(
                    ActorRole.AUDITOR,
                    ActorRole.METHODOLOGY_OWNER,
                ),
            )

    def _campaign(
        self,
        *,
        actor: Actor,
        campaign_id: str,
        lock: bool = False,
    ) -> BusinessQualificationCampaignRow:
        actor.require_any(ActorRole.AUDITOR, ActorRole.METHODOLOGY_OWNER)
        query = select(BusinessQualificationCampaignRow).where(
            BusinessQualificationCampaignRow.id == campaign_id,
            BusinessQualificationCampaignRow.organization_id == actor.organization_id,
        )
        if lock:
            query = query.with_for_update()
        campaign = self.session.scalar(query)
        if campaign is None:
            raise LookupError(campaign_id)
        return campaign

    def _locked_case(
        self,
        *,
        actor: Actor,
        campaign_id: str,
        case_id: str,
        required_roles: tuple[ActorRole, ...],
    ) -> tuple[BusinessQualificationCampaignRow, BusinessQualificationCaseRow]:
        actor.require_any(*required_roles)
        campaign = self.session.scalar(
            select(BusinessQualificationCampaignRow)
            .where(
                BusinessQualificationCampaignRow.id == campaign_id,
                BusinessQualificationCampaignRow.organization_id == actor.organization_id,
            )
            .with_for_update()
        )
        if campaign is None:
            raise LookupError(campaign_id)
        if campaign.status != "INPUTS_LOCKED":
            raise ValueError("Qualification reference intake is closed")
        case = self.session.scalar(
            select(BusinessQualificationCaseRow).where(
                BusinessQualificationCaseRow.id == case_id,
                BusinessQualificationCaseRow.campaign_id == campaign.id,
            )
        )
        if case is None:
            raise LookupError(case_id)
        self.projects.get_project(
            actor=actor,
            project_id=case.project_id,
            required_roles=required_roles,
        )
        return campaign, case

    def _bound_inputs(
        self,
        campaign: BusinessQualificationCampaignRow,
    ) -> tuple[BusinessQualificationProfile, BusinessQualificationDataset]:
        profile, profile_row = load_approved_profile(
            session=self.session,
            settings=self.settings,
            version_id=campaign.profile_version_id,
            expected_content_hash=campaign.profile_hash,
            expected_kind=BUSINESS_QUALIFICATION_PROFILE_KIND,
            profile_type=BusinessQualificationProfile,
        )
        dataset, dataset_row = load_approved_profile(
            session=self.session,
            settings=self.settings,
            version_id=campaign.dataset_version_id,
            expected_content_hash=campaign.dataset_hash,
            expected_kind=BUSINESS_QUALIFICATION_DATASET_KIND,
            profile_type=BusinessQualificationDataset,
        )
        self._require_governed_organization(
            profile_row.payload,
            campaign.organization_id,
        )
        self._require_governed_organization(
            dataset_row.payload,
            campaign.organization_id,
        )
        if (
            profile.expected_application_build_reference != campaign.application_build_reference
            or self.settings.application_build_reference != campaign.application_build_reference
        ):
            raise ValueError("Campaign application build identity no longer verifies")
        case_rows = list(
            self.session.scalars(
                select(BusinessQualificationCaseRow)
                .where(BusinessQualificationCaseRow.campaign_id == campaign.id)
                .order_by(BusinessQualificationCaseRow.case_key)
            )
        )
        recomputed_input_hash = content_hash(
            {
                "profile_version_id": campaign.profile_version_id,
                "profile_hash": campaign.profile_hash,
                "dataset_version_id": campaign.dataset_version_id,
                "dataset_hash": campaign.dataset_hash,
                "application_build_reference": campaign.application_build_reference,
                "population_size": dataset.population_size,
                "cases": [self._case_input_identity(row) for row in case_rows],
                "exclusions": dataset.exclusions,
            }
        )
        if (
            recomputed_input_hash != campaign.input_hash
            or campaign.payload.get("population_size") != dataset.population_size
            or campaign.payload.get("exclusion_count") != len(dataset.exclusions)
            or campaign.payload.get("population_evidence_hash") != dataset.population_evidence_hash
            or campaign.payload.get("selection_query_hash") != dataset.selection_query_hash
            or campaign.payload.get("selection_cutoff_at")
            != dataset.selection_cutoff_at.isoformat()
            or campaign.payload.get("case_prediction_hashes")
            != [row.prediction_hash for row in case_rows]
        ):
            raise ValueError("Campaign locked input basis does not verify")
        return profile, dataset

    def _verified_prediction(
        self,
        snapshot: CalculationSnapshotRow,
    ) -> tuple[CalculationResult, str]:
        payload = read_verified_snapshot(
            object_store=self.object_store,
            snapshot=snapshot,
        )
        primary = CalculationResult.model_validate(payload.get("primary"))
        independent = IndependentValidationResult.model_validate(payload.get("independent"))
        run = self.session.scalar(
            select(CalculationRunRow).where(
                CalculationRunRow.id == snapshot.calculation_run_id,
                CalculationRunRow.project_id == snapshot.project_id,
            )
        )
        if (
            not snapshot.fixed
            or not independent.passed
            or run is None
            or run.status != "VALIDATED"
            or run.engine_version != primary.engine_version
            or run.currency != primary.currency
            or run.grand_total != primary.grand_total
            or run.payload.get("primary") != primary.model_dump(mode="json")
            or run.payload.get("independent_validation") != independent.model_dump(mode="json")
        ):
            raise ValueError("Qualification prediction lacks a verified fixed calculation")
        return primary, snapshot.snapshot_hash

    def _verified_historical_reference(
        self,
        *,
        project_id: str,
        case_key: str,
        actual_id: str,
        expected_currency: str,
        expected_metric: str,
        comparison_basis_hash: str,
        selection_cutoff_at: datetime,
    ) -> dict[str, Any]:
        actual = self.session.scalar(
            select(ActualRecordRow).where(
                ActualRecordRow.id == actual_id,
                ActualRecordRow.project_id == project_id,
                ActualRecordRow.verified.is_(True),
                ActualRecordRow.is_current.is_(True),
            )
        )
        if (
            actual is None
            or actual.entity_type != "PROJECT"
            or actual.entity_id != project_id
            or actual.metric != expected_metric
            or actual.unit != expected_currency
            or actual.value <= 0
            or self._required_utc(actual.created_at, "actual created_at") > selection_cutoff_at
        ):
            raise ValueError(f"Historical actual for case {case_key} is not qualified")
        observation = self.session.scalar(
            select(ObservationRow)
            .join(
                DocumentRevisionRow,
                DocumentRevisionRow.id == ObservationRow.document_revision_id,
            )
            .join(DocumentRow, DocumentRow.id == DocumentRevisionRow.document_id)
            .where(
                ObservationRow.id == actual.source_observation_id,
                ObservationRow.project_id == project_id,
                ObservationRow.status == VerificationStatus.VERIFIED.value,
                DocumentRow.project_id == project_id,
                DocumentRow.cancelled.is_(False),
                DocumentRevisionRow.corrupt.is_(False),
                DocumentRevisionRow.protected.is_(False),
            )
        )
        if observation is None:
            raise ValueError(f"Historical actual for case {case_key} lacks verified evidence")
        if (
            self._required_utc(observation.created_at, "observation created_at")
            > selection_cutoff_at
            or observation.payload.get("comparison_basis_hash") != comparison_basis_hash
        ):
            raise ValueError(
                f"Historical actual for case {case_key} has an unbound comparison basis"
            )
        raw = observation.payload.get("observation")
        if not isinstance(raw, dict):
            raise ValueError("Historical actual observation payload is invalid")
        try:
            observed_value = Decimal(str(raw.get("value")))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise ValueError("Historical actual observation amount is invalid") from error
        if observed_value != actual.value or raw.get("unit") != actual.unit:
            raise ValueError("Historical actual does not reproduce its source observation")
        events = [
            AuditIntegrityService._event(row)
            for row in self.session.scalars(
                select(AuditEventRow)
                .where(
                    AuditEventRow.aggregate_type == "project",
                    AuditEventRow.aggregate_id == project_id,
                )
                .order_by(AuditEventRow.sequence)
            )
        ]
        if not events or not verify_chain(events, self.settings.audit_verification_keyring):
            raise ValueError("Historical project audit chain does not verify")
        recorded = [
            event
            for event in events
            if event.event_type == "actual_fact_recorded"
            and event.payload.get("actual_id") == actual.id
            and event.payload.get("source_observation_id") == observation.id
        ]
        verified = [
            event
            for event in events
            if event.event_type == "actual_fact_verified"
            and event.payload.get("actual_id") == actual.id
        ]
        try:
            recorded_value = Decimal(str(recorded[0].payload.get("value")))
        except (IndexError, InvalidOperation, TypeError, ValueError):
            recorded_value = Decimal("NaN")
        if (
            len(recorded) != 1
            or len(verified) != 1
            or recorded[0].actor_id == verified[0].actor_id
            or recorded[0].actor_id != actual.payload.get("created_by")
            or verified[0].actor_id != actual.payload.get("verified_by")
            or recorded[0].payload.get("actual_key") != actual.actual_key
            or recorded[0].payload.get("entity_type") != actual.entity_type
            or recorded[0].payload.get("entity_id") != actual.entity_id
            or recorded[0].payload.get("metric") != actual.metric
            or recorded_value != actual.value
            or recorded[0].payload.get("unit") != actual.unit
            or recorded[0].occurred_at > selection_cutoff_at
            or verified[0].occurred_at > selection_cutoff_at
        ):
            raise ValueError("Historical actual four-eyes audit evidence does not verify")
        evidence_payload = {
            "comparison_basis_hash": comparison_basis_hash,
            "actual": {
                "id": actual.id,
                "project_id": actual.project_id,
                "actual_key": actual.actual_key,
                "entity_type": actual.entity_type,
                "entity_id": actual.entity_id,
                "metric": actual.metric,
                "value": actual.value,
                "unit": actual.unit,
                "verified": actual.verified,
                "source_observation_id": actual.source_observation_id,
                "occurred_on": actual.occurred_on,
                "payload": actual.payload,
            },
            "observation": observation.payload,
            "recorded_audit_hash": recorded[0].event_hash,
            "verified_audit_hash": verified[0].event_hash,
        }
        return {
            "case_key": case_key,
            "reference_kind": "VERIFIED_ACTUAL",
            "source_entity_type": "ACTUAL_RECORD",
            "source_entity_id": actual.id,
            "reference_total": actual.value,
            "currency": actual.unit,
            "evidence_hash": content_hash(evidence_payload),
            "independence_domain": "VERIFIED_PROJECT_ACTUAL",
            "professional_estimator_id": None,
            "performed_at": datetime.combine(
                actual.occurred_on,
                datetime.min.time(),
                tzinfo=UTC,
            ),
            "payload": canonical_data(evidence_payload),
        }

    def _reverify_reference(
        self,
        case: BusinessQualificationCaseRow,
        reference: BusinessQualificationReferenceRow,
        campaign: BusinessQualificationCampaignRow,
        profile: BusinessQualificationProfile,
    ) -> None:
        if (
            reference.campaign_id != campaign.id
            or reference.currency != case.currency
            or reference.reference_total <= 0
        ):
            raise ValueError("Qualification reference commercial basis is invalid")
        if case.mode == "HISTORICAL":
            if (
                reference.reference_kind != "VERIFIED_ACTUAL"
                or reference.source_entity_type != "ACTUAL_RECORD"
                or content_hash(reference.payload) != reference.evidence_hash
                or reference.payload.get("comparison_basis_hash") != profile.comparison_basis_hash
            ):
                raise ValueError("Historical qualification reference integrity failed")
            return
        if reference.source_entity_type != "OBSERVATION" or reference.registered_by in {
            campaign.created_by,
            self._snapshot_creator(case.snapshot_id),
        }:
            raise ValueError("Professional qualification reference is not independent")
        observation = self.session.scalar(
            select(ObservationRow).where(
                ObservationRow.id == reference.source_entity_id,
                ObservationRow.project_id == case.project_id,
                ObservationRow.status == VerificationStatus.VERIFIED.value,
            )
        )
        if observation is None:
            raise ValueError("Professional qualification reference evidence is missing")
        payload = QualificationReferencePayload.model_validate(
            observation.payload.get("qualification_reference")
        )
        source_ids = observation.payload.get("source_observation_ids")
        if (
            not isinstance(source_ids, list)
            or len(source_ids) != 1
            or payload.case_key != case.case_key
            or payload.mode != case.mode
            or payload.amount != reference.reference_total
            or payload.currency != reference.currency
            or payload.comparison_basis_hash != profile.comparison_basis_hash
            or payload.evidence_hash != reference.evidence_hash
            or payload.model_dump(mode="json") != reference.payload
            or payload.reviewed_by != reference.registered_by
        ):
            raise ValueError("Professional qualification reference does not reproduce")
        prepared = self.session.get(ObservationRow, source_ids[0])
        if (
            prepared is None
            or prepared.project_id != case.project_id
            or prepared.payload.get("prepared_by") != payload.prepared_by
            or prepared.payload.get("campaign_id") != campaign.id
            or prepared.payload.get("case_id") != case.id
        ):
            raise ValueError("Professional qualification reference source does not verify")
        expected_hash = content_hash(
            {
                "prepared_observation": prepared.payload,
                "prepared_observation_id": prepared.id,
                "verified_observation": Observation.model_validate(
                    observation.payload.get("observation")
                ),
                "reviewed_by": reference.registered_by,
            }
        )
        if expected_hash != reference.evidence_hash:
            raise ValueError("Professional qualification evidence hash does not verify")

    def _snapshot_creator(self, snapshot_id: str) -> str:
        creator = self.session.scalar(
            select(CalculationSnapshotRow.created_by).where(
                CalculationSnapshotRow.id == snapshot_id
            )
        )
        if creator is None:
            raise ValueError("Qualification snapshot creator is unavailable")
        return creator

    def _audit(
        self,
        *,
        campaign: BusinessQualificationCampaignRow,
        actor: Actor,
        request_id: str,
        reason: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        self.projects.record_event(
            aggregate_type="business_qualification_campaign",
            aggregate_id=campaign.id,
            event_type=event_type,
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload=payload,
        )

    def _view(
        self,
        campaign: BusinessQualificationCampaignRow,
    ) -> QualificationCampaignView:
        cases = list(
            self.session.scalars(
                select(BusinessQualificationCaseRow.id).where(
                    BusinessQualificationCaseRow.campaign_id == campaign.id
                )
            )
        )
        references = list(
            self.session.scalars(
                select(BusinessQualificationReferenceRow.id).where(
                    BusinessQualificationReferenceRow.campaign_id == campaign.id
                )
            )
        )
        discrepancies = list(
            self.session.scalars(
                select(BusinessQualificationDiscrepancyRow.id).where(
                    BusinessQualificationDiscrepancyRow.campaign_id == campaign.id
                )
            )
        )
        reviewed = (
            list(
                self.session.scalars(
                    select(BusinessQualificationDiscrepancyReviewRow.id).where(
                        BusinessQualificationDiscrepancyReviewRow.discrepancy_id.in_(discrepancies)
                    )
                )
            )
            if discrepancies
            else []
        )
        approval = self.session.scalar(
            select(BusinessQualificationApprovalRow).where(
                BusinessQualificationApprovalRow.campaign_id == campaign.id
            )
        )
        return QualificationCampaignView(
            campaign_id=campaign.id,
            organization_id=campaign.organization_id,
            profile_version_id=campaign.profile_version_id,
            dataset_version_id=campaign.dataset_version_id,
            application_build_reference=campaign.application_build_reference,
            status=campaign.status,
            input_hash=campaign.input_hash,
            case_count=len(cases),
            reference_count=len(references),
            material_discrepancy_count=len(discrepancies),
            reviewed_discrepancy_count=len(reviewed),
            created_by=campaign.created_by,
            locked_at=self._required_utc(campaign.locked_at, "locked_at"),
            evaluated_by=campaign.evaluated_by,
            evaluated_at=ensure_utc(campaign.evaluated_at),
            finalized_by=campaign.finalized_by,
            finalized_at=ensure_utc(campaign.finalized_at),
            result_hash=campaign.result_hash,
            approval_package_hash=approval.package_hash if approval else None,
        )

    @staticmethod
    def _reference_evidence_view(
        row: ObservationRow,
    ) -> QualificationReferenceEvidenceView:
        draft = QualificationReferenceEvidenceDraft.model_validate(
            row.payload.get("qualification_reference_draft")
        )
        return QualificationReferenceEvidenceView(
            observation_id=row.id,
            campaign_id=str(row.payload["campaign_id"]),
            case_id=str(row.payload["case_id"]),
            case_key=draft.case_key,
            mode=draft.mode,
            status=VerificationStatus(row.status),
            prepared_by=str(row.payload["prepared_by"]),
            created_at=BusinessQualificationService._required_utc(
                row.created_at,
                "created_at",
            ),
        )

    @staticmethod
    def _discrepancy_view(
        row: BusinessQualificationDiscrepancyRow,
        review: BusinessQualificationDiscrepancyReviewRow | None,
    ) -> QualificationDiscrepancyView:
        return QualificationDiscrepancyView(
            discrepancy_id=row.id,
            case_id=row.case_id,
            absolute_error=row.absolute_error,
            exact_ratio_numerator=row.exact_ratio_numerator,
            exact_ratio_denominator=row.exact_ratio_denominator,
            review_id=review.id if review else None,
            review_decision=review.decision if review else None,
            review_reason_code=review.reason_code if review else None,
            reviewed_by=review.reviewed_by if review else None,
            reviewed_at=ensure_utc(review.reviewed_at) if review else None,
        )

    @staticmethod
    def _require_governed_organization(
        payload: dict[str, Any],
        organization_id: str,
    ) -> None:
        governance = payload.get("_governance")
        if not isinstance(governance, dict) or governance.get("organization_id") != organization_id:
            raise LookupError("Controlled qualification input")

    @staticmethod
    def _required_text(value: str, field: str, maximum: int) -> str:
        if not value or value != value.strip():
            raise ValueError(f"{field} is required without surrounding whitespace")
        if len(value) > maximum:
            raise ValueError(f"{field} exceeds {maximum} characters")
        return value

    @staticmethod
    def _required_utc(value: datetime | None, field: str) -> datetime:
        normalized = ensure_utc(value)
        if normalized is None:
            raise ValueError(f"{field} is missing")
        return normalized

    @staticmethod
    def _decimal_identity(value: Decimal) -> str:
        if not value.is_finite():
            raise ValueError("Qualification amount must be finite")
        return format(value.normalize(), "f")

    @staticmethod
    def _case_input_identity(
        value: dict[str, Any] | BusinessQualificationCaseRow,
    ) -> dict[str, Any]:
        if isinstance(value, dict):
            prediction_total = value["prediction_total"]
            if not isinstance(prediction_total, Decimal):
                prediction_total = Decimal(str(prediction_total))
            return {
                "case_key": value["case_key"],
                "mode": value["mode"],
                "project_id": value["project_id"],
                "snapshot_id": value["snapshot_id"],
                "snapshot_hash": value["snapshot_hash"],
                "prediction_total": BusinessQualificationService._decimal_identity(
                    prediction_total
                ),
                "currency": value["currency"],
                "prediction_hash": value["prediction_hash"],
                "stratum": value["stratum"],
            }
        return {
            "case_key": value.case_key,
            "mode": value.mode,
            "project_id": value.project_id,
            "snapshot_id": value.snapshot_id,
            "snapshot_hash": value.snapshot_hash,
            "prediction_total": BusinessQualificationService._decimal_identity(
                value.prediction_total
            ),
            "currency": value.currency,
            "prediction_hash": value.prediction_hash,
            "stratum": value.stratum,
        }
