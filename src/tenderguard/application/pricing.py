from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderguard.application.approvals import ApprovalService
from tenderguard.application.evidence_independence import (
    require_distinct_qualified_independence,
)
from tenderguard.application.projects import ProjectService
from tenderguard.application.stage_gates import pricing_stage_blockers
from tenderguard.config import Settings
from tenderguard.domain.approvals import ApprovalSubject
from tenderguard.domain.common import content_hash, ensure_utc, utc_now
from tenderguard.domain.enums import (
    ActorRole,
    ApprovalReason,
    ApprovalState,
    EvidenceMethod,
    MatchClass,
    PriceEvidenceClass,
    PriceStatus,
    VerificationStatus,
    VersionStatus,
)
from tenderguard.domain.models import (
    CommercialBasis,
    DomainModel,
    NomenclatureMatch,
    Observation,
    PriceQuote,
)
from tenderguard.domain.nomenclature import approve_analogue, assess_exact_match
from tenderguard.domain.pricing import (
    NormalizationRequest,
    PriceAdjustment,
    TriangulationResult,
    evaluate_triangulation,
    normalize_quote,
)
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.object_store import ObjectStore
from tenderguard.infrastructure.orm import (
    AdapterQualificationRow,
    ApprovalRecordRow,
    ApprovalTaskRow,
    ControlledVersionRow,
    NomenclatureMatchRow,
    NormalizedPriceRow,
    ObservationRow,
    PriceDecisionRow,
    PriceQuoteRow,
    ProjectControlledVersionRow,
    RfqRequestRow,
)


class NomenclatureAssessmentDraft(DomainModel):
    source_item_id: str = Field(min_length=1, max_length=128)
    canonical_item_id: str = Field(min_length=1, max_length=128)
    source_attributes_observation_id: str = Field(min_length=1)


class NomenclatureMatchView(DomainModel):
    match: NomenclatureMatch
    status: VerificationStatus
    catalog_version_id: str
    supersedes_match_id: str | None = None
    approval_task_ids: tuple[str, ...] = ()


class AnalogueProposalCommand(DomainModel):
    analogue_class: MatchClass


class PriceQuoteDraft(DomainModel):
    item_id: str = Field(min_length=1, max_length=128)
    supplier_id: str | None = None
    evidence_class: PriceEvidenceClass
    source_observation_id: str = Field(min_length=1)
    technical_attributes: dict[str, str]
    amount: Decimal = Field(gt=0)
    basis: CommercialBasis
    quote_date: date
    valid_until: date | None
    lead_time_days: int | None = Field(default=None, ge=0)
    available: bool | None
    source_reliability: Decimal = Field(ge=0, le=1)

    def evidence_value(self) -> dict[str, Any]:
        return self.model_dump(
            mode="json",
            exclude={"source_observation_id"},
        )


class PriceQuoteView(DomainModel):
    quote: PriceQuote
    source_origin_id: str
    normalized_price_id: str | None = None


class NormalizePriceCommand(DomainModel):
    quote_id: str = Field(min_length=1)
    unit_conversion_id: str | None = None
    fx_rate_id: str | None = None
    adjustment_ids: tuple[str, ...] = ()
    region_adjustment_id: str | None = None
    party_adjustment_id: str | None = None
    payment_adjustment_id: str | None = None


class NormalizedPriceView(DomainModel):
    normalized_price_id: str
    quote_id: str
    amount_per_unit: Decimal
    currency: str
    unit: str
    formula_hash: str
    policy_version_id: str


class PriceDecisionView(DomainModel):
    decision_id: str
    item_id: str
    status: PriceStatus
    amount_per_unit: Decimal | None
    currency: str | None
    unit: str | None
    derived_observation_id: str | None
    triangulation: TriangulationResult
    relative_spread: Decimal | None
    approval_task_ids: tuple[str, ...] = ()
    rfq_request_id: str | None = None
    project_state: ApprovalState


class PricingService:
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

    def assess_nomenclature(
        self,
        *,
        actor: Actor,
        project_id: str,
        draft: NomenclatureAssessmentDraft,
        request_id: str,
        reason: str,
    ) -> NomenclatureMatchView:
        project = self._require_pricing_state(
            actor,
            project_id,
            required_roles=(ActorRole.PROCUREMENT, ActorRole.TECHNICAL_EXPERT),
        )
        catalog = self._bound_version(project.id, "catalog", "catalog")
        item = self._catalog_item(catalog, draft.canonical_item_id)
        observation = self._verified_observation(
            project.id,
            draft.source_attributes_observation_id,
        )
        source_attributes = observation.payload.get("observation", {}).get("value")
        if not isinstance(source_attributes, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in source_attributes.items()
        ):
            raise ValueError("Nomenclature evidence must contain string attributes")
        canonical_attributes = item.get("attributes")
        critical_attributes = item.get("critical_attributes")
        if not isinstance(canonical_attributes, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in canonical_attributes.items()
        ):
            raise ValueError("Catalog item attributes are invalid")
        if not isinstance(critical_attributes, list) or not all(
            isinstance(value, str) for value in critical_attributes
        ):
            raise ValueError("Catalog item critical_attributes are invalid")
        match_id = f"nomenclature-match-{uuid4()}"
        match = assess_exact_match(
            match_id=match_id,
            source_item_id=draft.source_item_id,
            canonical_item_id=draft.canonical_item_id,
            required_critical_attributes=frozenset(critical_attributes),
            source_attributes={str(key): str(value) for key, value in source_attributes.items()},
            canonical_attributes={
                str(key): str(value) for key, value in canonical_attributes.items()
            },
        )
        status = (
            VerificationStatus.VERIFIED
            if match.match_class is MatchClass.EXACT
            else VerificationStatus.REJECTED
            if match.match_class is MatchClass.TECHNICALLY_UNACCEPTABLE
            else VerificationStatus.CONFLICT
        )
        previous = self.session.scalar(
            select(NomenclatureMatchRow)
            .where(
                NomenclatureMatchRow.project_id == project.id,
                NomenclatureMatchRow.source_item_id == draft.source_item_id,
                NomenclatureMatchRow.is_current.is_(True),
            )
            .with_for_update()
        )
        now = utc_now()
        if previous is not None:
            previous.is_current = False
            previous.updated_at = now
        row = NomenclatureMatchRow(
            id=match_id,
            project_id=project.id,
            source_item_id=draft.source_item_id,
            canonical_item_id=draft.canonical_item_id,
            match_class=match.match_class.value,
            status=status.value,
            catalog_version_id=catalog.id,
            supersedes_match_id=previous.id if previous else None,
            is_current=True,
            payload={
                "match": match.model_dump(mode="json"),
                "source_attributes_observation_id": observation.id,
                "assessed_by": actor.actor_id,
                "assessment_method": "DETERMINISTIC_CRITICAL_ATTRIBUTE_COMPARISON",
                "critical_price": bool(item.get("critical_price")),
                "document_set_revision_id": project.current_document_set_revision_id,
            },
            created_at=now,
            updated_at=now,
        )
        self.session.add(row)
        self._project_service().record_event(
            aggregate_type="project",
            aggregate_id=project.id,
            event_type="nomenclature_assessed",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "match_id": match_id,
                "source_item_id": draft.source_item_id,
                "canonical_item_id": draft.canonical_item_id,
                "match_class": match.match_class,
                "status": status,
                "catalog_version_id": catalog.id,
                "supersedes_match_id": row.supersedes_match_id,
            },
        )
        return self._match_view(row)

    def propose_analogue(
        self,
        *,
        actor: Actor,
        project_id: str,
        match_id: str,
        command: AnalogueProposalCommand,
        request_id: str,
        reason: str,
    ) -> NomenclatureMatchView:
        project = self._require_pricing_state(
            actor,
            project_id,
            required_roles=(ActorRole.TECHNICAL_EXPERT,),
        )
        source = self._current_match(project.id, match_id)
        if source.match_class not in {
            MatchClass.TECHNICALLY_UNACCEPTABLE.value,
            MatchClass.CONDITIONALLY_ACCEPTABLE_ANALOGUE.value,
            MatchClass.FUNCTIONAL_ANALOGUE.value,
        }:
            raise ValueError("Only a mismatched or prior analogue assessment can be proposed")
        base_match = NomenclatureMatch.model_validate(source.payload["match"])
        if base_match.missing_attributes:
            raise ValueError("An analogue cannot be proposed with missing critical attributes")
        rules = self._bound_version(
            project.id,
            "nomenclature_equivalence_rules",
            "nomenclature_equivalence_rules",
        )
        self._validate_equivalence_rule(base_match, command.analogue_class, rules)
        proposed_id = f"nomenclature-match-{uuid4()}"
        proposed = base_match.model_copy(
            update={
                "match_id": proposed_id,
                "match_class": command.analogue_class,
                "verified_by": None,
                "verified_at": None,
            }
        )
        now = utc_now()
        source.is_current = False
        source.updated_at = now
        row = NomenclatureMatchRow(
            id=proposed_id,
            project_id=project.id,
            source_item_id=source.source_item_id,
            canonical_item_id=source.canonical_item_id,
            match_class=command.analogue_class.value,
            status=VerificationStatus.IN_REVIEW.value,
            catalog_version_id=source.catalog_version_id,
            supersedes_match_id=source.id,
            is_current=True,
            payload={
                **source.payload,
                "match": proposed.model_dump(mode="json"),
                "proposed_by": actor.actor_id,
                "equivalence_rule_version_id": rules.id,
                "proposal_reason": reason,
            },
            created_at=now,
            updated_at=now,
        )
        self.session.add(row)
        self.session.flush()
        approval = self._approval_service().plan(
            actor=actor,
            project_id=project.id,
            subjects=(
                ApprovalSubject(
                    entity_type="nomenclature_match",
                    entity_id=row.id,
                    reasons=frozenset({ApprovalReason.TECHNICAL_ANALOGUE}),
                ),
            ),
            request_id=request_id,
            reason=reason,
        )
        task_ids = tuple(approval.task_ids_by_key.values())
        row.payload = {
            **row.payload,
            "approval_task_ids": list(task_ids),
            "approval_findings": [
                finding.model_dump(mode="json") for finding in approval.plan.findings
            ],
        }
        self._project_service().record_event(
            aggregate_type="project",
            aggregate_id=project.id,
            event_type="nomenclature_analogue_proposed",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "match_id": row.id,
                "analogue_class": command.analogue_class,
                "equivalence_rule_version_id": rules.id,
                "approval_task_ids": list(task_ids),
            },
        )
        return self._match_view(row)

    def finalize_analogue(
        self,
        *,
        actor: Actor,
        project_id: str,
        match_id: str,
        request_id: str,
        reason: str,
    ) -> NomenclatureMatchView:
        project = self._require_pricing_state(
            actor,
            project_id,
            required_roles=(
                ActorRole.PROCUREMENT,
                ActorRole.TECHNICAL_EXPERT,
                ActorRole.REVIEWER,
            ),
        )
        row = self._current_match(project.id, match_id)
        if row.status != VerificationStatus.IN_REVIEW.value:
            raise ValueError("Only an IN_REVIEW analogue can be finalized")
        task_ids = tuple(row.payload.get("approval_task_ids", []))
        if not task_ids:
            raise ValueError("Analogue has no policy-mandated approval task")
        tasks = list(
            self.session.scalars(
                select(ApprovalTaskRow).where(
                    ApprovalTaskRow.project_id == project.id,
                    ApprovalTaskRow.id.in_(task_ids),
                    ApprovalTaskRow.status == "APPROVED",
                )
            )
        )
        if len(tasks) != len(task_ids):
            raise ValueError("Analogue approvals are incomplete")
        approval = self.session.scalar(
            select(ApprovalRecordRow)
            .where(
                ApprovalRecordRow.task_id.in_(task_ids),
                ApprovalRecordRow.decision == "APPROVED",
            )
            .order_by(ApprovalRecordRow.decided_at.desc())
        )
        if approval is None:
            raise ValueError("Approved analogue task has no approval record")
        match = approve_analogue(
            NomenclatureMatch.model_validate(row.payload["match"]),
            analogue_class=MatchClass(row.match_class),
            equivalence_rule_version_id=str(row.payload.get("equivalence_rule_version_id")),
            verified_by=approval.decided_by,
            verified_at=ensure_utc(approval.decided_at) or utc_now(),
        )
        row.status = VerificationStatus.VERIFIED.value
        row.payload = {
            **row.payload,
            "match": match.model_dump(mode="json"),
            "verified_by": approval.decided_by,
            "approval_record_id": approval.id,
            "finalized_by": actor.actor_id,
        }
        row.updated_at = utc_now()
        self._project_service().record_event(
            aggregate_type="project",
            aggregate_id=project.id,
            event_type="nomenclature_analogue_finalized",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "match_id": row.id,
                "approval_record_id": approval.id,
                "verified_by": approval.decided_by,
            },
        )
        return self._match_view(row)

    def record_quote(
        self,
        *,
        actor: Actor,
        project_id: str,
        draft: PriceQuoteDraft,
        request_id: str,
        reason: str,
    ) -> PriceQuoteView:
        project = self._require_pricing_state(
            actor,
            project_id,
            required_roles=(ActorRole.PROCUREMENT,),
        )
        match = self._verified_match_for_item(project.id, draft.item_id)
        self._validate_quote_technical_attributes(match, draft.technical_attributes)
        observation = self._verified_observation(project.id, draft.source_observation_id)
        if content_hash(observation.payload.get("observation", {}).get("value")) != content_hash(
            draft.evidence_value()
        ):
            raise ValueError("Verified quote evidence does not reproduce the submitted quote")
        source_origin_id = self._validate_price_evidence_class(
            project.id,
            observation,
            draft.evidence_class,
        )
        identity = {
            "project_id": project.id,
            "draft": draft,
            "source_origin_id": source_origin_id,
        }
        quote_id = f"price-quote-{content_hash(identity)[:24]}"
        existing = self.session.get(PriceQuoteRow, quote_id)
        if existing is not None:
            return self._quote_view(existing)
        quote = PriceQuote(
            quote_id=quote_id,
            item_id=draft.item_id,
            supplier_id=draft.supplier_id,
            evidence_class=draft.evidence_class,
            source_observation_id=draft.source_observation_id,
            technical_attributes=draft.technical_attributes,
            amount=draft.amount,
            basis=draft.basis,
            quote_date=draft.quote_date,
            valid_until=draft.valid_until,
            lead_time_days=draft.lead_time_days,
            available=draft.available,
            source_reliability=draft.source_reliability,
            status=PriceStatus.UNNORMALIZED,
        )
        now = utc_now()
        row = PriceQuoteRow(
            id=quote.quote_id,
            project_id=project.id,
            item_id=quote.item_id,
            status=quote.status.value,
            quote_date=quote.quote_date,
            valid_until=quote.valid_until,
            amount=quote.amount,
            currency=quote.basis.currency,
            source_observation_id=observation.id,
            payload={
                "quote": quote.model_dump(mode="json"),
                "source_origin_id": source_origin_id,
                "recorded_by": actor.actor_id,
                "nomenclature_match_id": match.id,
            },
            created_at=now,
            updated_at=now,
        )
        self.session.add(row)
        self._project_service().record_event(
            aggregate_type="project",
            aggregate_id=project.id,
            event_type="price_quote_recorded",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "quote_id": quote.quote_id,
                "item_id": quote.item_id,
                "evidence_class": quote.evidence_class,
                "source_observation_id": observation.id,
                "source_origin_id": source_origin_id,
            },
        )
        return self._quote_view(row)

    def normalize_price(
        self,
        *,
        actor: Actor,
        project_id: str,
        command: NormalizePriceCommand,
        request_id: str,
        reason: str,
    ) -> NormalizedPriceView:
        project = self._require_pricing_state(
            actor,
            project_id,
            required_roles=(ActorRole.PROCUREMENT, ActorRole.ESTIMATOR),
        )
        row = self.session.scalar(
            select(PriceQuoteRow)
            .where(
                PriceQuoteRow.id == command.quote_id,
                PriceQuoteRow.project_id == project.id,
            )
            .with_for_update()
        )
        if row is None:
            raise LookupError(command.quote_id)
        quote = PriceQuote.model_validate(row.payload["quote"]).model_copy(
            update={"status": PriceStatus(row.status)}
        )
        policy = self._bound_version(project.id, "price_policy", "price_policy")
        request = self._normalization_request(quote, command, policy)
        normalized = normalize_quote(quote, request, normalized_at=utc_now())
        existing = self.session.get(NormalizedPriceRow, normalized.normalized_price_id)
        if existing is None:
            self.session.add(
                NormalizedPriceRow(
                    id=normalized.normalized_price_id,
                    quote_id=row.id,
                    amount_per_unit=normalized.amount_per_unit,
                    currency=normalized.target_basis.currency,
                    formula_hash=normalized.normalization_formula.removeprefix("sha256:"),
                    payload={
                        "normalized_price": normalized.model_dump(mode="json"),
                        "policy_version_id": policy.id,
                        "normalization_command": command.model_dump(mode="json"),
                    },
                    created_at=normalized.normalized_at,
                )
            )
        row.status = PriceStatus.NORMALIZED.value
        row.updated_at = utc_now()
        self._project_service().record_event(
            aggregate_type="project",
            aggregate_id=project.id,
            event_type="price_normalized",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "quote_id": row.id,
                "normalized_price_id": normalized.normalized_price_id,
                "formula_hash": normalized.normalization_formula,
                "policy_version_id": policy.id,
            },
        )
        return NormalizedPriceView(
            normalized_price_id=normalized.normalized_price_id,
            quote_id=row.id,
            amount_per_unit=normalized.amount_per_unit,
            currency=normalized.target_basis.currency,
            unit=normalized.target_basis.unit,
            formula_hash=normalized.normalization_formula.removeprefix("sha256:"),
            policy_version_id=policy.id,
        )

    def evaluate_item_price(
        self,
        *,
        actor: Actor,
        project_id: str,
        item_id: str,
        as_of: date,
        request_id: str,
        reason: str,
    ) -> PriceDecisionView:
        project_service = self._project_service()
        project = project_service.get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            required_roles=(
                ActorRole.PROCUREMENT,
                ActorRole.ESTIMATOR,
                ActorRole.TECHNICAL_EXPERT,
            ),
        )
        if ApprovalState(project.state) not in {
            ApprovalState.PRICING_IN_PROGRESS,
            ApprovalState.RFQ_REQUIRED,
            ApprovalState.EXPERT_REVIEW,
        }:
            raise ValueError("Price evaluation requires a pricing or pricing-review state")
        match = self._verified_match_for_item(project.id, item_id)
        policy = self._bound_version(project.id, "price_policy", "price_policy")
        normalized_rows = list(
            self.session.scalars(
                select(NormalizedPriceRow)
                .join(PriceQuoteRow, PriceQuoteRow.id == NormalizedPriceRow.quote_id)
                .where(
                    PriceQuoteRow.project_id == project.id,
                    PriceQuoteRow.item_id == item_id,
                    PriceQuoteRow.status == PriceStatus.NORMALIZED.value,
                )
            )
        )
        normalized_rows = [
            row for row in normalized_rows if row.payload.get("policy_version_id") == policy.id
        ]
        quote_rows = {
            row.id: row
            for row in self.session.scalars(
                select(PriceQuoteRow).where(
                    PriceQuoteRow.project_id == project.id,
                    PriceQuoteRow.item_id == item_id,
                )
            )
        }
        quotes = tuple(
            PriceQuote.model_validate(quote_rows[row.quote_id].payload["quote"]).model_copy(
                update={"status": PriceStatus.NORMALIZED}
            )
            for row in normalized_rows
        )
        triangulation = evaluate_triangulation(
            item_id=item_id,
            quotes=quotes,
            as_of=as_of,
            critical=bool(match.payload.get("critical_price")),
        )
        eligible_ids = set(triangulation.quote_ids)
        eligible_normalized = [row for row in normalized_rows if row.quote_id in eligible_ids]
        origins = {
            str(quote_rows[row.quote_id].payload.get("source_origin_id"))
            for row in eligible_normalized
        }
        origins_are_independent = (
            bool(eligible_normalized)
            and "None" not in origins
            and len(origins) == len(eligible_normalized)
        )
        amounts = tuple(sorted(row.amount_per_unit for row in eligible_normalized))
        selected_amount = self._median(amounts) if amounts else None
        relative_spread = (
            (max(amounts) - min(amounts)) / selected_amount
            if selected_amount is not None and selected_amount != 0 and len(amounts) > 1
            else Decimal("0")
            if selected_amount is not None
            else None
        )
        evaluation_identity = {
            "project_id": project.id,
            "item_id": item_id,
            "policy_version_id": policy.id,
            "normalized_price_ids": sorted(row.id for row in eligible_normalized),
            "as_of": as_of,
            "triangulation": triangulation,
            "relative_spread": relative_spread,
        }
        evaluation_id = f"price-evaluation-{content_hash(evaluation_identity)[:24]}"
        approval_task_ids: tuple[str, ...] = ()
        approval_findings: tuple[Any, ...] = ()
        status = triangulation.resulting_status
        if triangulation.passed and not origins_are_independent:
            status = PriceStatus.RFQ_REQUIRED
        elif triangulation.passed:
            selection_method = policy.payload.get("selection_method")
            if selection_method != "MEDIAN":
                status = PriceStatus.EXPERT_REVIEW_REQUIRED
            else:
                approval = self._approval_service().plan(
                    actor=actor,
                    project_id=project.id,
                    subjects=(
                        ApprovalSubject(
                            entity_type="price_evaluation",
                            entity_id=evaluation_id,
                            reasons=frozenset({ApprovalReason.HIGH_PRICE_SPREAD}),
                            price_spread=relative_spread,
                        ),
                    ),
                    request_id=request_id,
                    reason=reason,
                )
                approval_task_ids = tuple(approval.task_ids_by_key.values())
                approval_findings = approval.plan.findings
                tasks = list(
                    self.session.scalars(
                        select(ApprovalTaskRow).where(ApprovalTaskRow.id.in_(approval_task_ids))
                    )
                )
                if approval_findings or any(task.status != "APPROVED" for task in tasks):
                    status = PriceStatus.EXPERT_REVIEW_REQUIRED
                else:
                    status = PriceStatus.VERIFIED

        previous = self.session.scalar(
            select(PriceDecisionRow)
            .where(
                PriceDecisionRow.project_id == project.id,
                PriceDecisionRow.item_id == item_id,
                PriceDecisionRow.is_current.is_(True),
            )
            .with_for_update()
        )
        if previous is not None:
            previous.is_current = False
        now = utc_now()
        decision_id = f"price-decision-{uuid4()}"
        derived_observation_id = None
        target_basis = self._target_basis(policy, item_id)
        if status is PriceStatus.VERIFIED:
            if selected_amount is None:
                raise ValueError("Verified price decision has no normalized amount")
            derived_observation_id = self._record_verified_rate_observation(
                project_id=project.id,
                item_id=item_id,
                amount=selected_amount,
                target_basis=target_basis,
                normalized_rows=eligible_normalized,
                quote_rows=quote_rows,
                policy=policy,
                evaluation_id=evaluation_id,
                actor=actor,
            )
        row = PriceDecisionRow(
            id=decision_id,
            project_id=project.id,
            item_id=item_id,
            status=status.value,
            amount_per_unit=selected_amount,
            currency=target_basis.currency if selected_amount is not None else None,
            unit=target_basis.unit if selected_amount is not None else None,
            policy_version_id=policy.id,
            derived_observation_id=derived_observation_id,
            supersedes_decision_id=previous.id if previous else None,
            is_current=True,
            payload={
                "evaluation_id": evaluation_id,
                "triangulation": triangulation.model_dump(mode="json"),
                "normalized_price_ids": [row.id for row in eligible_normalized],
                "source_origin_ids": sorted(origins),
                "origins_are_independent": origins_are_independent,
                "selection_method": policy.payload.get("selection_method"),
                "relative_spread": (str(relative_spread) if relative_spread is not None else None),
                "approval_task_ids": list(approval_task_ids),
                "approval_findings": [
                    finding.model_dump(mode="json") for finding in approval_findings
                ],
                "evaluated_by": actor.actor_id,
                "as_of": as_of.isoformat(),
                "document_set_revision_id": project.current_document_set_revision_id,
            },
            created_at=now,
        )
        self.session.add(row)
        self.session.flush()
        rfq_request_id = None
        if status is PriceStatus.RFQ_REQUIRED:
            rfq_request_id = self._open_rfq(
                project_id=project.id,
                item_id=item_id,
                decision=row,
                missing_classes=triangulation.missing_evidence_classes,
                origins_are_independent=origins_are_independent,
                actor=actor,
            )
        elif status is PriceStatus.VERIFIED:
            self._close_rfqs(project.id, item_id, row.id, actor.actor_id)
        self._sync_project_pricing_state(
            project=project,
            decision_status=status,
            actor=actor,
            request_id=request_id,
        )
        project_service.record_event(
            aggregate_type="project",
            aggregate_id=project.id,
            event_type="price_decision_recorded",
            actor=actor,
            request_id=request_id,
            reason=reason,
            payload={
                "price_decision_id": row.id,
                "evaluation_id": evaluation_id,
                "item_id": item_id,
                "status": status,
                "derived_observation_id": derived_observation_id,
                "rfq_request_id": rfq_request_id,
                "approval_task_ids": list(approval_task_ids),
            },
        )
        return PriceDecisionView(
            decision_id=row.id,
            item_id=item_id,
            status=status,
            amount_per_unit=selected_amount,
            currency=row.currency,
            unit=row.unit,
            derived_observation_id=derived_observation_id,
            triangulation=triangulation,
            relative_spread=relative_spread,
            approval_task_ids=approval_task_ids,
            rfq_request_id=rfq_request_id,
            project_state=ApprovalState(project.state),
        )

    def _normalization_request(
        self,
        quote: PriceQuote,
        command: NormalizePriceCommand,
        policy: ControlledVersionRow,
    ) -> NormalizationRequest:
        target = self._target_basis(policy, quote.item_id)
        conversion_rate = Decimal("1")
        conversion_id = None
        if quote.basis.unit != target.unit:
            conversion = self._policy_reference(
                policy,
                "unit_conversions",
                command.unit_conversion_id,
            )
            if (
                conversion.get("source_unit") != quote.basis.unit
                or conversion.get("target_unit") != target.unit
            ):
                raise ValueError("Unit conversion does not match quote and target units")
            conversion_rate = self._decimal_parameter(
                conversion,
                "source_units_per_target_unit",
            )
            conversion_id = command.unit_conversion_id
        elif command.unit_conversion_id is not None:
            raise ValueError("Unit conversion reference is extraneous")

        fx_rate = Decimal("1")
        fx_rate_id = None
        if quote.basis.currency != target.currency:
            fx = self._policy_reference(policy, "fx_rates", command.fx_rate_id)
            if (
                fx.get("source_currency") != quote.basis.currency
                or fx.get("target_currency") != target.currency
            ):
                raise ValueError("FX rate does not match quote and target currencies")
            fx_rate = self._decimal_parameter(fx, "rate")
            fx_rate_id = command.fx_rate_id
        elif command.fx_rate_id is not None:
            raise ValueError("FX reference is extraneous")

        adjustments = tuple(
            PriceAdjustment.model_validate(
                {
                    **self._policy_reference(policy, "adjustments", adjustment_id),
                    "adjustment_id": adjustment_id,
                }
            )
            for adjustment_id in command.adjustment_ids
        )
        for section, reference_id in (
            ("region_adjustments", command.region_adjustment_id),
            ("party_adjustments", command.party_adjustment_id),
            ("payment_adjustments", command.payment_adjustment_id),
        ):
            if reference_id is not None:
                self._policy_reference(policy, section, reference_id)
        return NormalizationRequest(
            target_basis=target,
            source_units_per_target_unit=conversion_rate,
            unit_conversion_id=conversion_id,
            target_currency_per_source_currency=fx_rate,
            fx_rate_id=fx_rate_id,
            adjustments=adjustments,
            region_adjustment_id=command.region_adjustment_id,
            party_adjustment_id=command.party_adjustment_id,
            payment_adjustment_id=command.payment_adjustment_id,
        )

    def _record_verified_rate_observation(
        self,
        *,
        project_id: str,
        item_id: str,
        amount: Decimal,
        target_basis: CommercialBasis,
        normalized_rows: list[NormalizedPriceRow],
        quote_rows: dict[str, PriceQuoteRow],
        policy: ControlledVersionRow,
        evaluation_id: str,
        actor: Actor,
    ) -> str:
        source_observation_ids = tuple(
            str(quote_rows[row.quote_id].source_observation_id) for row in normalized_rows
        )
        first_source = self._verified_observation(project_id, source_observation_ids[0])
        source_model = Observation.model_validate(first_source.payload["observation"])
        identity = {
            "evaluation_id": evaluation_id,
            "amount": amount,
            "basis": target_basis,
        }
        observation_id = f"observation-{content_hash(identity)[:24]}"
        if self.session.get(ObservationRow, observation_id) is not None:
            return observation_id
        observation = Observation(
            observation_id=observation_id,
            field_name=f"normalized_unit_rate:{item_id}",
            value=amount,
            unit=target_basis.unit,
            method=EvidenceMethod.RULE_ENGINE,
            method_version=policy.id,
            source_priority=min(
                Observation.model_validate(
                    self._verified_observation(project_id, source_id).payload["observation"]
                ).source_priority
                for source_id in source_observation_ids
            ),
            location=source_model.location,
            observed_at=utc_now(),
            actor_id=actor.actor_id,
            status=VerificationStatus.VERIFIED,
        )
        self.session.add(
            ObservationRow(
                id=observation.observation_id,
                project_id=project_id,
                document_revision_id=source_model.location.document_revision_id,
                field_name=observation.field_name,
                method=observation.method.value,
                method_version=observation.method_version,
                status=observation.status.value,
                payload={
                    "observation": observation.model_dump(mode="json"),
                    "source_observation_ids": list(source_observation_ids),
                    "source_normalized_price_ids": [row.id for row in normalized_rows],
                    "price_evaluation_id": evaluation_id,
                    "basis_type": "NORMALIZED_PRICE",
                    "unit_rate": str(amount),
                    "currency": target_basis.currency,
                    "unit": target_basis.unit,
                    "price_policy_version_id": policy.id,
                },
                created_at=observation.observed_at,
            )
        )
        return observation_id

    def _validate_price_evidence_class(
        self,
        project_id: str,
        observation: ObservationRow,
        evidence_class: PriceEvidenceClass,
    ) -> str:
        leaf_ids = require_distinct_qualified_independence(
            self.session,
            project_id=project_id,
            observations=(observation,),
        )
        leaves = list(
            self.session.scalars(select(ObservationRow).where(ObservationRow.id.in_(leaf_ids)))
        )
        qualification_ids = {str(row.payload.get("adapter_qualification_id")) for row in leaves}
        qualifications = list(
            self.session.scalars(
                select(AdapterQualificationRow).where(
                    AdapterQualificationRow.id.in_(qualification_ids)
                )
            )
        )
        if any(
            evidence_class.value
            not in qualification.payload.get("supported_price_evidence_classes", [])
            for qualification in qualifications
        ):
            raise ValueError("Price evidence class is outside the source adapter qualification")
        origins = {row.payload.get("source_origin_id") for row in leaves}
        if len(origins) != 1 or None in origins:
            raise ValueError("A quote extraction must resolve to one controlled source origin")
        return str(next(iter(origins)))

    @staticmethod
    def _validate_quote_technical_attributes(
        match: NomenclatureMatchRow,
        quote_attributes: dict[str, str],
    ) -> None:
        model = NomenclatureMatch.model_validate(match.payload["match"])
        for attribute in model.required_critical_attributes:
            expected = model.source_attributes.get(attribute)
            actual = quote_attributes.get(attribute)
            if (
                not expected
                or not actual
                or " ".join(expected.casefold().split()) != " ".join(actual.casefold().split())
            ):
                raise ValueError(f"Quote does not match verified critical attribute: {attribute}")

    def _validate_equivalence_rule(
        self,
        match: NomenclatureMatch,
        analogue_class: MatchClass,
        rules: ControlledVersionRow,
    ) -> None:
        if analogue_class not in {
            MatchClass.FUNCTIONAL_ANALOGUE,
            MatchClass.CONDITIONALLY_ACCEPTABLE_ANALOGUE,
        }:
            raise ValueError("Only an explicit analogue class can be proposed")
        raw_rules = rules.payload.get("rules")
        if not isinstance(raw_rules, list):
            raise ValueError("Equivalence rule pack has no rules")
        applicable = next(
            (
                item
                for item in raw_rules
                if isinstance(item, dict)
                and item.get("canonical_item_id") == match.canonical_item_id
                and analogue_class.value in item.get("allowed_analogue_classes", [])
            ),
            None,
        )
        if applicable is None:
            raise ValueError("No approved equivalence rule permits this analogue class")
        permitted = applicable.get("permitted_mismatched_attributes")
        if not isinstance(permitted, list) or not all(isinstance(item, str) for item in permitted):
            raise ValueError("Equivalence rule has invalid mismatch constraints")
        if not match.mismatched_attributes <= set(permitted):
            raise ValueError("Critical mismatch is not permitted by the equivalence rule")

    def _verified_match_for_item(
        self,
        project_id: str,
        item_id: str,
    ) -> NomenclatureMatchRow:
        row = self.session.scalar(
            select(NomenclatureMatchRow).where(
                NomenclatureMatchRow.project_id == project_id,
                NomenclatureMatchRow.source_item_id == item_id,
                NomenclatureMatchRow.is_current.is_(True),
                NomenclatureMatchRow.status == VerificationStatus.VERIFIED.value,
            )
        )
        if row is None or row.match_class in {
            MatchClass.TECHNICALLY_UNACCEPTABLE.value,
            MatchClass.INSUFFICIENT_DATA.value,
        }:
            raise ValueError("Item lacks a verified technically acceptable nomenclature match")
        return row

    def _current_match(
        self,
        project_id: str,
        match_id: str,
    ) -> NomenclatureMatchRow:
        row = self.session.scalar(
            select(NomenclatureMatchRow)
            .where(
                NomenclatureMatchRow.id == match_id,
                NomenclatureMatchRow.project_id == project_id,
                NomenclatureMatchRow.is_current.is_(True),
            )
            .with_for_update()
        )
        if row is None:
            raise LookupError(match_id)
        return row

    def _verified_observation(
        self,
        project_id: str,
        observation_id: str,
    ) -> ObservationRow:
        row = self.session.scalar(
            select(ObservationRow).where(
                ObservationRow.id == observation_id,
                ObservationRow.project_id == project_id,
                ObservationRow.status == VerificationStatus.VERIFIED.value,
            )
        )
        if row is None:
            raise ValueError("A verified project-scoped observation is required")
        return row

    def _bound_version(
        self,
        project_id: str,
        purpose: str,
        kind: str,
    ) -> ControlledVersionRow:
        row = self.session.scalar(
            select(ControlledVersionRow)
            .join(
                ProjectControlledVersionRow,
                ProjectControlledVersionRow.controlled_version_id == ControlledVersionRow.id,
            )
            .where(
                ProjectControlledVersionRow.project_id == project_id,
                ProjectControlledVersionRow.purpose == purpose,
                ControlledVersionRow.kind == kind,
                ControlledVersionRow.status == VersionStatus.APPROVED.value,
            )
        )
        if row is None:
            raise ValueError(f"A bound approved {kind} version is required")
        return row

    @staticmethod
    def _catalog_item(
        catalog: ControlledVersionRow,
        canonical_item_id: str,
    ) -> dict[str, Any]:
        items = catalog.payload.get("items")
        item = items.get(canonical_item_id) if isinstance(items, dict) else None
        if not isinstance(item, dict):
            raise ValueError("Canonical item is absent from the approved catalog")
        return item

    @staticmethod
    def _policy_reference(
        policy: ControlledVersionRow,
        section: str,
        reference_id: str | None,
    ) -> dict[str, Any]:
        if not reference_id:
            raise ValueError(f"{section} reference is required")
        values = policy.payload.get(section)
        value = values.get(reference_id) if isinstance(values, dict) else None
        if not isinstance(value, dict):
            raise ValueError(f"Reference {reference_id} is absent from approved {section}")
        return value

    @staticmethod
    def _decimal_parameter(payload: dict[str, Any], field_name: str) -> Decimal:
        try:
            value = Decimal(str(payload[field_name]))
        except (KeyError, InvalidOperation, TypeError, ValueError) as error:
            raise ValueError(f"Controlled decimal parameter is invalid: {field_name}") from error
        if value <= 0:
            raise ValueError(f"Controlled decimal parameter must be positive: {field_name}")
        return value

    def _target_basis(
        self,
        policy: ControlledVersionRow,
        item_id: str,
    ) -> CommercialBasis:
        mapping = policy.payload.get("item_target_basis_ids")
        target_basis_id = mapping.get(item_id) if isinstance(mapping, dict) else None
        if not isinstance(target_basis_id, str):
            raise ValueError("Item has no target commercial basis in the approved price policy")
        target = self._policy_reference(policy, "target_bases", target_basis_id)
        return CommercialBasis.model_validate(target)

    def _open_rfq(
        self,
        *,
        project_id: str,
        item_id: str,
        decision: PriceDecisionRow,
        missing_classes: tuple[PriceEvidenceClass, ...],
        origins_are_independent: bool,
        actor: Actor,
    ) -> str:
        now = utc_now()
        identity = {
            "project_id": project_id,
            "item_id": item_id,
            "missing_classes": missing_classes,
            "origins_are_independent": origins_are_independent,
            "price_policy_version_id": decision.policy_version_id,
        }
        rfq_id = f"rfq-{content_hash(identity)[:24]}"
        existing = self.session.get(RfqRequestRow, rfq_id)
        payload = {
            "missing_evidence_classes": [item.value for item in missing_classes],
            "independent_source_origin_required": not origins_are_independent,
            "created_by": actor.actor_id,
        }
        if existing is None:
            self.session.add(
                RfqRequestRow(
                    id=rfq_id,
                    project_id=project_id,
                    item_id=item_id,
                    status="OPEN",
                    price_decision_id=decision.id,
                    payload=payload,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            existing.status = "OPEN"
            existing.price_decision_id = decision.id
            existing.payload = payload
            existing.updated_at = now
        return rfq_id

    def _close_rfqs(
        self,
        project_id: str,
        item_id: str,
        decision_id: str,
        actor_id: str,
    ) -> None:
        now = utc_now()
        requests = list(
            self.session.scalars(
                select(RfqRequestRow).where(
                    RfqRequestRow.project_id == project_id,
                    RfqRequestRow.item_id == item_id,
                    RfqRequestRow.status == "OPEN",
                )
            )
        )
        for request in requests:
            request.status = "CLOSED"
            request.updated_at = now
            request.payload = {
                **request.payload,
                "resolved_by_price_decision_id": decision_id,
                "resolved_by": actor_id,
                "resolved_at": now.isoformat(),
            }

    def _sync_project_pricing_state(
        self,
        *,
        project: Any,
        decision_status: PriceStatus,
        actor: Actor,
        request_id: str,
    ) -> None:
        service = self._project_service()
        state = ApprovalState(project.state)
        target: ApprovalState | None = None
        if (
            decision_status is PriceStatus.RFQ_REQUIRED
            and state is ApprovalState.PRICING_IN_PROGRESS
        ):
            target = ApprovalState.RFQ_REQUIRED
        elif decision_status is PriceStatus.EXPERT_REVIEW_REQUIRED and state in {
            ApprovalState.PRICING_IN_PROGRESS,
            ApprovalState.RFQ_REQUIRED,
        }:
            target = ApprovalState.EXPERT_REVIEW
        elif (
            decision_status is PriceStatus.VERIFIED
            and state in {ApprovalState.RFQ_REQUIRED, ApprovalState.EXPERT_REVIEW}
            and not pricing_stage_blockers(self.session, project.id)
        ):
            target = ApprovalState.PRICING_IN_PROGRESS
        if target is not None:
            service.transition(
                actor=actor,
                project_id=project.id,
                to_state=target,
                expected_row_version=project.row_version,
                request_id=request_id,
                reason=f"Pricing decision requires project state {target.value}",
            )

    def _require_pricing_state(
        self,
        actor: Actor,
        project_id: str,
        *,
        required_roles: tuple[ActorRole, ...],
    ) -> Any:
        project = self._project_service().get_project(
            actor=actor,
            project_id=project_id,
            lock=True,
            required_roles=required_roles,
        )
        if ApprovalState(project.state) not in {
            ApprovalState.PRICING_IN_PROGRESS,
            ApprovalState.RFQ_REQUIRED,
        }:
            raise ValueError("Pricing changes require PRICING_IN_PROGRESS or RFQ_REQUIRED")
        return project

    def _approval_service(self) -> ApprovalService:
        return ApprovalService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )

    def _project_service(self) -> ProjectService:
        return ProjectService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )

    @staticmethod
    def _median(values: tuple[Decimal, ...]) -> Decimal | None:
        if not values:
            return None
        middle = len(values) // 2
        if len(values) % 2:
            return values[middle]
        return (values[middle - 1] + values[middle]) / Decimal("2")

    @staticmethod
    def _match_view(row: NomenclatureMatchRow) -> NomenclatureMatchView:
        return NomenclatureMatchView(
            match=NomenclatureMatch.model_validate(row.payload["match"]),
            status=VerificationStatus(row.status),
            catalog_version_id=row.catalog_version_id,
            supersedes_match_id=row.supersedes_match_id,
            approval_task_ids=tuple(row.payload.get("approval_task_ids", [])),
        )

    @staticmethod
    def _quote_view(row: PriceQuoteRow) -> PriceQuoteView:
        quote = PriceQuote.model_validate(row.payload["quote"]).model_copy(
            update={"status": PriceStatus(row.status)}
        )
        return PriceQuoteView(
            quote=quote,
            source_origin_id=str(row.payload.get("source_origin_id")),
        )
