from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderguard.application.approvals import ApprovalService
from tenderguard.application.controlled_version_integrity import (
    require_bound_controlled_version,
)
from tenderguard.application.document_set_integrity import (
    require_confirmed_document_set_integrity,
    require_observation_in_document_set,
)
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
    PriceSourceType,
    PriceStatus,
    VerificationStatus,
)
from tenderguard.domain.models import (
    CommercialBasis,
    DomainModel,
    NomenclatureMatch,
    NormalizedPrice,
    Observation,
    PriceQuote,
    PriceSourceReference,
)
from tenderguard.domain.nomenclature import (
    approve_analogue,
    assess_exact_match,
    catalog_retrieval_evidence,
)
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
    BoqLineRow,
    ControlledVersionRow,
    NomenclatureMatchRow,
    NormalizedPriceRow,
    ObservationRow,
    PriceDecisionRow,
    PriceQuoteRow,
    ProjectRow,
    QuantityRow,
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


class CatalogItemView(DomainModel):
    canonical_item_id: str
    attributes: dict[str, str]
    critical_attributes: tuple[str, ...]
    critical_price: bool
    retrieval_exact_identifier: bool = False
    retrieval_matched_terms: tuple[str, ...] = ()
    retrieval_matched_critical_attributes: tuple[str, ...] = ()


class NomenclatureSourceItemView(DomainModel):
    source_item_id: str
    boq_line_id: str
    line_key: str
    wbs_node_id: str
    work_code: str
    description: str
    unit: str


class NomenclatureEvidenceCandidateView(DomainModel):
    observation: Observation
    attributes: dict[str, str]


class NomenclatureContextView(DomainModel):
    project_id: str
    project_state: ApprovalState
    document_set_revision_id: str
    catalog_version_id: str
    source_items: tuple[NomenclatureSourceItemView, ...]
    selected_source_item_id: str | None = None
    selected_source_description: str | None = None
    catalog_items: tuple[CatalogItemView, ...]
    catalog_items_truncated: bool
    evidence_field_name: str
    evidence_candidates: tuple[NomenclatureEvidenceCandidateView, ...]
    evidence_candidates_truncated: bool
    retrieval_notice: str = (
        "Candidate order is lexical retrieval only and is not evidence of technical equivalence."
    )


class NomenclatureReviewContextView(DomainModel):
    match: NomenclatureMatchView
    source_attributes_observation_id: str
    source_observation: Observation
    proposal_reason: str | None = None
    equivalence_rule_version_id: str | None = None
    approval_task_statuses: dict[str, str] = Field(default_factory=dict)
    finalization_allowed: bool
    finalization_blockers: tuple[str, ...]


class AnalogueProposalCommand(DomainModel):
    analogue_class: MatchClass


class PriceQuoteDraft(DomainModel):
    item_id: str = Field(min_length=1, max_length=128)
    supplier_id: str | None = None
    evidence_class: PriceEvidenceClass
    source_reference: PriceSourceReference
    source_observation_id: str = Field(min_length=1)
    technical_attributes: dict[str, str]
    amount: Decimal = Field(gt=0, max_digits=38, decimal_places=12)
    basis: CommercialBasis
    quote_date: date
    valid_until: date | None
    lead_time_days: int | None = Field(default=None, ge=0)
    available: bool | None
    source_reliability: Decimal = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validity_and_source_identity_are_consistent(self) -> PriceQuoteDraft:
        if self.valid_until is not None and self.valid_until < self.quote_date:
            raise ValueError("Price quote validity cannot end before the quote date")
        if self.evidence_class is PriceEvidenceClass.COMMERCIAL_QUOTE and not self.supplier_id:
            raise ValueError("A commercial quote requires a supplier identity")
        allowed_classes = {
            PriceSourceType.FGIS_CS: {PriceEvidenceClass.OFFICIAL_OR_PRIMARY},
            PriceSourceType.WON_TENDER: {PriceEvidenceClass.INTERNAL_HISTORY},
            PriceSourceType.MARKETPLACE: {PriceEvidenceClass.INDEPENDENT_MARKET},
            PriceSourceType.SUPPLIER_WEBSITE: {PriceEvidenceClass.INDEPENDENT_MARKET},
            PriceSourceType.SUPPLIER_QUOTE: {PriceEvidenceClass.COMMERCIAL_QUOTE},
            PriceSourceType.OTHER_OFFICIAL: {PriceEvidenceClass.OFFICIAL_OR_PRIMARY},
        }
        if self.evidence_class not in allowed_classes[self.source_reference.source_type]:
            raise ValueError("Price evidence class conflicts with the declared source type")
        return self

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

    @model_validator(mode="after")
    def adjustment_references_are_unique(self) -> NormalizePriceCommand:
        if len(self.adjustment_ids) != len(set(self.adjustment_ids)):
            raise ValueError("Normalization adjustment references must be unique")
        return self


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


class PriceQuoteSummaryView(DomainModel):
    quote: PriceQuote
    source_origin_id: str
    normalized_prices: tuple[NormalizedPriceView, ...] = ()


class PriceDecisionSummaryView(DomainModel):
    decision_id: str
    status: PriceStatus
    amount_per_unit: Decimal | None = None
    currency: str | None = None
    unit: str | None = None
    policy_version_id: str
    derived_observation_id: str | None = None
    evaluation_id: str | None = None
    as_of: date | None = None
    normalized_price_ids: tuple[str, ...] = ()
    source_origin_ids: tuple[str, ...] = ()
    approval_task_ids: tuple[str, ...] = ()
    rfq_request_id: str | None = None


class PriceItemContextView(DomainModel):
    project_id: str
    item_id: str
    match_id: str
    match_class: MatchClass
    critical_price: bool
    required_critical_attributes: tuple[str, ...]
    technical_attributes: dict[str, str]
    document_set_revision_id: str | None
    catalog_version_id: str
    price_policy_version_id: str
    normalization_rounding_scale: int
    normalization_rounding_mode: str
    target_basis: CommercialBasis
    normalization_references: dict[str, dict[str, dict[str, Any]]]
    quotes: tuple[PriceQuoteSummaryView, ...]
    current_decision: PriceDecisionSummaryView | None = None


class PriceQuoteCandidateView(DomainModel):
    project_id: str
    item_id: str
    source_observation_id: str
    source_origin_id: str
    draft: PriceQuoteDraft
    target_basis: CommercialBasis
    price_policy_version_id: str
    required_reference_types: tuple[str, ...]
    required_adjustment_kinds: tuple[str, ...]


class BoqPriceNameMatchView(DomainModel):
    match_id: str
    status: str
    match_class: MatchClass
    boq_item_name: str
    source_item_id: str
    canonical_item_id: str | None
    source_attributes: dict[str, str]
    canonical_attributes: dict[str, str]
    mismatched_attributes: tuple[str, ...]
    missing_attributes: tuple[str, ...]
    catalog_version_id: str
    assessment_method: str | None


class BoqSourcePriceView(DomainModel):
    quote_id: str
    evidence_class: PriceEvidenceClass
    source_reference: PriceSourceReference
    source_observation_id: str
    source_origin_id: str
    source_locator: str
    source_document_revision_id: str
    observed_at: datetime
    quote_date: date
    valid_until: date | None
    available: bool | None
    lead_time_days: int | None
    raw_amount: Decimal
    raw_currency: str
    raw_unit: str
    normalized_prices: tuple[NormalizedPriceView, ...]
    technical_attributes: dict[str, str]


class BoqProposedPriceView(DomainModel):
    status: str
    workflow_status: str
    amount_per_unit: Decimal | None = None
    currency: str | None = None
    unit: str | None = None
    decision_id: str | None = None
    as_of: date | None = None
    selection_method: str | None = None
    normalized_price_ids: tuple[str, ...] = ()
    rationale: tuple[str, ...]


class BoqPriceMatrixRowView(DomainModel):
    row_id: str
    boq_line_id: str
    line_key: str
    wbs_node_id: str
    work_code: str
    boq_item_name: str
    boq_unit: str
    quantity: Decimal | None
    quantity_status: str
    item_id: str
    cost_category: str | None
    basis_kind: str | None
    row_status: str
    blockers: tuple[str, ...]
    name_match: BoqPriceNameMatchView | None
    won_tender_prices: tuple[BoqSourcePriceView, ...]
    fgis_cs_prices: tuple[BoqSourcePriceView, ...]
    market_prices: tuple[BoqSourcePriceView, ...]
    other_prices: tuple[BoqSourcePriceView, ...]
    proposed_price: BoqProposedPriceView


class BoqPriceMatrixView(DomainModel):
    project_id: str
    generated_at: datetime
    rows: tuple[BoqPriceMatrixRowView, ...]
    blocked_row_count: int = Field(ge=0)
    release_warning: str


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

    def boq_price_matrix(
        self,
        *,
        actor: Actor,
        project_id: str,
    ) -> BoqPriceMatrixView:
        project = self._project_service().get_project(
            actor=actor,
            project_id=project_id,
            required_roles=(
                ActorRole.ESTIMATOR,
                ActorRole.PROCUREMENT,
                ActorRole.REVIEWER,
                ActorRole.APPROVER,
                ActorRole.AUDITOR,
            ),
        )
        line_rows = tuple(
            self.session.scalars(
                select(BoqLineRow)
                .where(
                    BoqLineRow.project_id == project.id,
                    BoqLineRow.is_current.is_(True),
                )
                .order_by(BoqLineRow.line_key, BoqLineRow.id)
            )
        )
        quantity_by_line = {
            row.boq_line_id: row
            for row in self.session.scalars(
                select(QuantityRow).where(
                    QuantityRow.boq_line_id.in_(tuple(line.id for line in line_rows)),
                    QuantityRow.is_current.is_(True),
                )
            )
        }
        match_by_item = {
            row.source_item_id: row
            for row in self.session.scalars(
                select(NomenclatureMatchRow).where(
                    NomenclatureMatchRow.project_id == project.id,
                    NomenclatureMatchRow.is_current.is_(True),
                )
            )
        }
        decision_by_item = {
            row.item_id: row
            for row in self.session.scalars(
                select(PriceDecisionRow).where(
                    PriceDecisionRow.project_id == project.id,
                    PriceDecisionRow.is_current.is_(True),
                )
            )
        }
        try:
            policy = self._bound_version(project.id, "price_policy", "price_policy")
        except (LookupError, RuntimeError, TypeError, ValueError):
            policy = None

        rows: list[BoqPriceMatrixRowView] = []
        for line in line_rows:
            raw_components = line.payload.get("cost_components")
            components = (
                raw_components if isinstance(raw_components, list) and raw_components else [None]
            )
            quantity = quantity_by_line.get(line.id)
            for component_index, raw_component in enumerate(components):
                component = raw_component if isinstance(raw_component, dict) else {}
                raw_item_id = component.get("semantic_key")
                item_id = (
                    raw_item_id
                    if isinstance(raw_item_id, str) and raw_item_id
                    else f"{line.id}:INVALID_COMPONENT:{component_index + 1}"
                )
                blockers: list[str] = []
                if line.status != VerificationStatus.VERIFIED.value:
                    blockers.append("BOQ_LINE_NOT_VERIFIED")
                if raw_component is None or not isinstance(raw_component, dict):
                    blockers.append("BOQ_COST_COMPONENT_INVALID")
                if not isinstance(raw_item_id, str) or not raw_item_id:
                    blockers.append("BOQ_ITEM_ID_MISSING")
                if quantity is None:
                    blockers.append("QUANTITY_MISSING")
                    quantity_status = "MISSING"
                else:
                    quantity_status = quantity.status
                    if quantity.status != VerificationStatus.VERIFIED.value:
                        blockers.append("QUANTITY_NOT_VERIFIED")
                    if quantity.unit != line.unit:
                        blockers.append("QUANTITY_UNIT_MISMATCH")

                match_row = match_by_item.get(item_id)
                name_match: BoqPriceNameMatchView | None = None
                match_ready = False
                if match_row is None:
                    blockers.append("NOMENCLATURE_MATCH_MISSING")
                else:
                    try:
                        match = NomenclatureMatch.model_validate(match_row.payload.get("match"))
                    except (TypeError, ValueError):
                        blockers.append("NOMENCLATURE_MATCH_PAYLOAD_INVALID")
                    else:
                        name_match = BoqPriceNameMatchView(
                            match_id=match_row.id,
                            status=match_row.status,
                            match_class=match.match_class,
                            boq_item_name=line.description,
                            source_item_id=match.source_item_id,
                            canonical_item_id=match.canonical_item_id,
                            source_attributes=dict(sorted(match.source_attributes.items())),
                            canonical_attributes=dict(sorted(match.canonical_attributes.items())),
                            mismatched_attributes=tuple(sorted(match.mismatched_attributes)),
                            missing_attributes=tuple(sorted(match.missing_attributes)),
                            catalog_version_id=match_row.catalog_version_id,
                            assessment_method=(
                                str(match_row.payload["assessment_method"])
                                if isinstance(
                                    match_row.payload.get("assessment_method"),
                                    str,
                                )
                                else None
                            ),
                        )
                        if match_row.status != VerificationStatus.VERIFIED.value:
                            blockers.append("NOMENCLATURE_MATCH_NOT_VERIFIED")
                        if match.match_class in {
                            MatchClass.TECHNICALLY_UNACCEPTABLE,
                            MatchClass.INSUFFICIENT_DATA,
                        }:
                            blockers.append("NOMENCLATURE_MATCH_NOT_ACCEPTABLE")
                        integrity_blockers = self._nomenclature_match_integrity_blockers(
                            project,
                            match_row,
                        )
                        blockers.extend(f"NOMENCLATURE_{blocker}" for blocker in integrity_blockers)
                        match_ready = (
                            match_row.status == VerificationStatus.VERIFIED.value
                            and match.match_class
                            not in {
                                MatchClass.TECHNICALLY_UNACCEPTABLE,
                                MatchClass.INSUFFICIENT_DATA,
                            }
                            and not integrity_blockers
                        )

                won_tender_prices: list[BoqSourcePriceView] = []
                fgis_cs_prices: list[BoqSourcePriceView] = []
                market_prices: list[BoqSourcePriceView] = []
                other_prices: list[BoqSourcePriceView] = []
                if match_ready and match_row is not None:
                    quote_rows = tuple(
                        self.session.scalars(
                            select(PriceQuoteRow)
                            .where(
                                PriceQuoteRow.project_id == project.id,
                                PriceQuoteRow.item_id == item_id,
                            )
                            .order_by(PriceQuoteRow.quote_date.desc(), PriceQuoteRow.id)
                        )
                    )
                    for quote_row in quote_rows:
                        try:
                            quote = self._validated_quote_row(
                                project_id=project.id,
                                row=quote_row,
                                match=match_row,
                            )
                            observation_row = self._verified_observation(
                                project.id,
                                quote.source_observation_id,
                            )
                            observation = self._validated_observation(observation_row)
                            source_origin_id = quote_row.payload.get("source_origin_id")
                            if not isinstance(source_origin_id, str) or not source_origin_id:
                                raise ValueError("Price source origin is absent")
                            normalized_views: list[NormalizedPriceView] = []
                            normalized_rows = tuple(
                                self.session.scalars(
                                    select(NormalizedPriceRow)
                                    .where(NormalizedPriceRow.quote_id == quote.quote_id)
                                    .order_by(
                                        NormalizedPriceRow.created_at,
                                        NormalizedPriceRow.id,
                                    )
                                )
                            )
                            for normalized_row in normalized_rows:
                                if (
                                    policy is None
                                    or normalized_row.payload.get("policy_version_id") != policy.id
                                ):
                                    continue
                                self._require_normalized_row_integrity(
                                    project_id=project.id,
                                    row=normalized_row,
                                    quote=quote,
                                    policy=policy,
                                )
                                normalized = NormalizedPrice.model_validate(
                                    normalized_row.payload["normalized_price"]
                                )
                                normalized_views.append(
                                    NormalizedPriceView(
                                        normalized_price_id=normalized_row.id,
                                        quote_id=normalized_row.quote_id,
                                        amount_per_unit=normalized_row.amount_per_unit,
                                        currency=normalized_row.currency,
                                        unit=normalized.target_basis.unit,
                                        formula_hash=normalized_row.formula_hash,
                                        policy_version_id=policy.id,
                                    )
                                )
                            source_view = BoqSourcePriceView(
                                quote_id=quote.quote_id,
                                evidence_class=quote.evidence_class,
                                source_reference=quote.source_reference,
                                source_observation_id=quote.source_observation_id,
                                source_origin_id=source_origin_id,
                                source_locator=observation.location.locator,
                                source_document_revision_id=(
                                    observation.location.document_revision_id
                                ),
                                observed_at=observation.observed_at,
                                quote_date=quote.quote_date,
                                valid_until=quote.valid_until,
                                available=quote.available,
                                lead_time_days=quote.lead_time_days,
                                raw_amount=quote.amount,
                                raw_currency=quote.basis.currency,
                                raw_unit=quote.basis.unit,
                                normalized_prices=tuple(normalized_views),
                                technical_attributes=dict(
                                    sorted(quote.technical_attributes.items())
                                ),
                            )
                        except (LookupError, RuntimeError, TypeError, ValueError):
                            blockers.append(f"PRICE_SOURCE_INTEGRITY_FAILED:{quote_row.id}")
                            continue
                        if quote.source_reference.source_type is PriceSourceType.WON_TENDER:
                            won_tender_prices.append(source_view)
                        elif quote.source_reference.source_type is PriceSourceType.FGIS_CS:
                            fgis_cs_prices.append(source_view)
                        elif quote.source_reference.source_type in {
                            PriceSourceType.MARKETPLACE,
                            PriceSourceType.SUPPLIER_WEBSITE,
                        }:
                            market_prices.append(source_view)
                        else:
                            other_prices.append(source_view)

                if not won_tender_prices:
                    blockers.append("WON_TENDER_PRICE_MISSING")
                if not fgis_cs_prices:
                    blockers.append("FGIS_CS_PRICE_MISSING")
                if not market_prices:
                    blockers.append("MARKET_PRICE_MISSING")
                if policy is None:
                    blockers.append("PRICE_POLICY_INTEGRITY_FAILED")

                decision_row = decision_by_item.get(item_id)
                decision: PriceDecisionSummaryView | None = None
                if decision_row is None:
                    blockers.append("PRICE_DECISION_MISSING")
                else:
                    try:
                        decision = self.require_price_decision_integrity(decision_row)
                    except (LookupError, RuntimeError, TypeError, ValueError):
                        blockers.append("PRICE_DECISION_INTEGRITY_FAILED")
                    else:
                        if decision.status is not PriceStatus.VERIFIED:
                            blockers.append(f"PRICE_DECISION_NOT_VERIFIED:{decision.status.value}")

                blockers = list(dict.fromkeys(blockers))
                if decision is not None and decision_row is not None and not blockers:
                    rationale = (
                        f"Approved selection method: "
                        f"{decision_row.payload.get('selection_method')}.",
                        f"The verified decision uses "
                        f"{len(decision.normalized_price_ids)} normalized source prices.",
                        "FGIS CS, won-tender and independent market source names "
                        "are exposed for operator comparison.",
                        "This row status does not replace the project bid-release gates.",
                    )
                    proposed = BoqProposedPriceView(
                        status="VERIFIED",
                        workflow_status=decision.status.value,
                        amount_per_unit=decision.amount_per_unit,
                        currency=decision.currency,
                        unit=decision.unit,
                        decision_id=decision.decision_id,
                        as_of=decision.as_of,
                        selection_method=(
                            str(decision_row.payload["selection_method"])
                            if isinstance(
                                decision_row.payload.get("selection_method"),
                                str,
                            )
                            else None
                        ),
                        normalized_price_ids=decision.normalized_price_ids,
                        rationale=rationale,
                    )
                    row_status = "VERIFIED"
                else:
                    proposed = BoqProposedPriceView(
                        status="BLOCKED",
                        workflow_status=(
                            decision.status.value if decision is not None else "MISSING"
                        ),
                        rationale=(
                            "The proposed price is withheld because mandatory "
                            "evidence or approval gates are incomplete.",
                            *tuple(f"Blocker: {blocker}" for blocker in blockers),
                        ),
                    )
                    row_status = "BLOCKED"

                rows.append(
                    BoqPriceMatrixRowView(
                        row_id=f"{line.id}:{component_index + 1}",
                        boq_line_id=line.id,
                        line_key=line.line_key,
                        wbs_node_id=line.wbs_node_id,
                        work_code=line.work_code,
                        boq_item_name=line.description,
                        boq_unit=line.unit,
                        quantity=quantity.value if quantity is not None else None,
                        quantity_status=quantity_status,
                        item_id=item_id,
                        cost_category=(
                            str(component["category"])
                            if isinstance(component.get("category"), str)
                            else None
                        ),
                        basis_kind=(
                            str(component["basis_kind"])
                            if isinstance(component.get("basis_kind"), str)
                            else None
                        ),
                        row_status=row_status,
                        blockers=tuple(blockers),
                        name_match=name_match,
                        won_tender_prices=tuple(won_tender_prices),
                        fgis_cs_prices=tuple(fgis_cs_prices),
                        market_prices=tuple(market_prices),
                        other_prices=tuple(other_prices),
                        proposed_price=proposed,
                    )
                )
        return BoqPriceMatrixView(
            project_id=project.id,
            generated_at=utc_now(),
            rows=tuple(rows),
            blocked_row_count=sum(row.row_status == "BLOCKED" for row in rows),
            release_warning=(
                "A verified row price is not an APPROVED_FOR_BID decision. "
                "The project release gate remains authoritative."
            ),
        )

    def nomenclature_context(
        self,
        *,
        actor: Actor,
        project_id: str,
        catalog_query: str | None,
        evidence_field_name: str,
        source_item_id: str | None,
        limit: int,
    ) -> NomenclatureContextView:
        if limit < 1 or limit > 100:
            raise ValueError("Nomenclature context limit must be between 1 and 100")
        evidence_field_name = evidence_field_name.strip()
        if not evidence_field_name or len(evidence_field_name) > 300:
            raise ValueError("Nomenclature evidence field name must contain 1 to 300 characters")
        if evidence_field_name != "technical_attributes":
            raise ValueError(
                "Nomenclature evidence field must be the controlled technical_attributes field"
            )
        normalized_query = catalog_query.strip().casefold() if catalog_query else ""
        if len(normalized_query) > 200:
            raise ValueError("Nomenclature catalog query exceeds 200 characters")
        normalized_source_item_id = source_item_id.strip() if source_item_id else None
        if normalized_source_item_id is not None and (
            not normalized_source_item_id or len(normalized_source_item_id) > 128
        ):
            raise ValueError("Nomenclature source item ID is invalid")
        project = self._project_service().get_project(
            actor=actor,
            project_id=project_id,
            required_roles=(ActorRole.PROCUREMENT, ActorRole.TECHNICAL_EXPERT),
        )
        if ApprovalState(project.state) not in {
            ApprovalState.PRICING_IN_PROGRESS,
            ApprovalState.RFQ_REQUIRED,
        }:
            raise ValueError("Nomenclature changes require PRICING_IN_PROGRESS or RFQ_REQUIRED")
        document_set = require_confirmed_document_set_integrity(
            session=self.session,
            settings=self.settings,
            project_id=project.id,
            document_set_revision_id=project.current_document_set_revision_id,
        )
        catalog = self._bound_version(project.id, "catalog", "catalog")
        source_items = self._current_nomenclature_source_items(project.id)
        selected_source = None
        if normalized_source_item_id is not None:
            selected_source = next(
                (item for item in source_items if item.source_item_id == normalized_source_item_id),
                None,
            )
            if selected_source is None:
                raise ValueError(
                    "Nomenclature source item is not a unique current verified BoQ component"
                )
        all_items = self._validated_catalog_items(catalog)
        matching_items = [
            item
            for item in all_items
            if not normalized_query
            or normalized_query
            in " ".join(
                (
                    item.canonical_item_id,
                    *item.attributes.keys(),
                    *item.attributes.values(),
                )
            ).casefold()
        ]
        if selected_source is not None:
            ranked_items: list[CatalogItemView] = []
            for item in matching_items:
                retrieval = catalog_retrieval_evidence(
                    source_name=selected_source.description,
                    canonical_item_id=item.canonical_item_id,
                    attributes=item.attributes,
                    critical_attributes=item.critical_attributes,
                )
                ranked_items.append(
                    item.model_copy(
                        update={
                            "retrieval_exact_identifier": (retrieval.exact_identifier_mentioned),
                            "retrieval_matched_terms": retrieval.matched_terms,
                            "retrieval_matched_critical_attributes": (
                                retrieval.matched_critical_attributes
                            ),
                        }
                    )
                )
            matching_items = sorted(
                ranked_items,
                key=lambda item: (
                    -int(item.retrieval_exact_identifier),
                    -len(item.retrieval_matched_critical_attributes),
                    -len(item.retrieval_matched_terms),
                    item.canonical_item_id,
                ),
            )
            bound_evidence_field_name = f"{evidence_field_name}:{selected_source.source_item_id}"
            rows = list(
                self.session.scalars(
                    select(ObservationRow)
                    .where(
                        ObservationRow.project_id == project_id,
                        ObservationRow.document_revision_id.in_(tuple(document_set.revision_ids)),
                        ObservationRow.field_name == bound_evidence_field_name,
                        ObservationRow.status == VerificationStatus.VERIFIED.value,
                    )
                    .order_by(ObservationRow.created_at.desc(), ObservationRow.id)
                    .limit(limit + 1)
                )
            )
        else:
            rows = []
        evidence_candidates: list[NomenclatureEvidenceCandidateView] = []
        for row in rows[:limit]:
            observation = self._validated_observation(row)
            assert selected_source is not None
            attributes = self._nomenclature_evidence_attributes(
                observation,
                expected_source_item_id=selected_source.source_item_id,
            )
            evidence_candidates.append(
                NomenclatureEvidenceCandidateView(
                    observation=observation,
                    attributes=attributes,
                )
            )
        return NomenclatureContextView(
            project_id=project_id,
            project_state=ApprovalState(project.state),
            document_set_revision_id=document_set.id,
            catalog_version_id=catalog.id,
            source_items=source_items,
            selected_source_item_id=(
                selected_source.source_item_id if selected_source is not None else None
            ),
            selected_source_description=(
                selected_source.description if selected_source is not None else None
            ),
            catalog_items=tuple(matching_items[:limit]),
            catalog_items_truncated=len(matching_items) > limit,
            evidence_field_name=evidence_field_name,
            evidence_candidates=tuple(evidence_candidates),
            evidence_candidates_truncated=len(rows) > limit,
        )

    def nomenclature_review_context(
        self,
        *,
        actor: Actor,
        project_id: str,
        match_id: str,
    ) -> NomenclatureReviewContextView:
        project = self._project_service().get_project(
            actor=actor,
            project_id=project_id,
            required_roles=(
                ActorRole.PROCUREMENT,
                ActorRole.TECHNICAL_EXPERT,
                ActorRole.REVIEWER,
            ),
        )
        row = self.session.scalar(
            select(NomenclatureMatchRow).where(
                NomenclatureMatchRow.id == match_id,
                NomenclatureMatchRow.project_id == project_id,
            )
        )
        if row is None:
            raise LookupError(match_id)
        blockers = self._nomenclature_match_integrity_blockers(project, row)
        task_ids = tuple(row.payload.get("approval_task_ids", []))
        tasks = list(
            self.session.scalars(
                select(ApprovalTaskRow).where(
                    ApprovalTaskRow.project_id == project_id,
                    ApprovalTaskRow.id.in_(task_ids),
                )
            )
        )
        task_statuses = {task.id: task.status for task in tasks}
        if ApprovalState(project.state) not in {
            ApprovalState.PRICING_IN_PROGRESS,
            ApprovalState.RFQ_REQUIRED,
        }:
            blockers.append("PROJECT_STATE_NOT_ALLOWED")
        if not row.is_current:
            blockers.append("MATCH_SUPERSEDED")
        if row.status != VerificationStatus.IN_REVIEW.value:
            blockers.append("MATCH_NOT_IN_REVIEW")
        if not task_ids:
            blockers.append("APPROVAL_TASKS_MISSING")
        elif set(task_statuses) != set(task_ids):
            blockers.append("APPROVAL_TASKS_INCOMPLETE")
        elif any(status != "APPROVED" for status in task_statuses.values()):
            blockers.append("APPROVALS_NOT_COMPLETE")
        approval_count = len(
            list(
                self.session.scalars(
                    select(ApprovalRecordRow).where(
                        ApprovalRecordRow.task_id.in_(task_ids),
                        ApprovalRecordRow.decision == "APPROVED",
                    )
                )
            )
        )
        if task_ids and approval_count != len(task_ids):
            blockers.append("APPROVAL_RECORDS_INCOMPLETE")
        source_observation_id = row.payload.get("source_attributes_observation_id")
        if not isinstance(source_observation_id, str) or not source_observation_id:
            raise RuntimeError("Nomenclature match source observation identity is missing")
        source_observation = self._validated_observation(
            self._verified_observation(project_id, source_observation_id)
        )
        proposal_reason = row.payload.get("proposal_reason")
        equivalence_rule_version_id = row.payload.get("equivalence_rule_version_id")
        return NomenclatureReviewContextView(
            match=self._match_view(row),
            source_attributes_observation_id=source_observation_id,
            source_observation=source_observation,
            proposal_reason=(proposal_reason if isinstance(proposal_reason, str) else None),
            equivalence_rule_version_id=(
                equivalence_rule_version_id
                if isinstance(equivalence_rule_version_id, str)
                else None
            ),
            approval_task_statuses=task_statuses,
            finalization_allowed=not blockers,
            finalization_blockers=tuple(dict.fromkeys(blockers)),
        )

    def assess_nomenclature(
        self,
        *,
        actor: Actor,
        project_id: str,
        draft: NomenclatureAssessmentDraft,
        request_id: str,
        reason: str,
    ) -> NomenclatureMatchView:
        reason = reason.strip()
        if not reason or len(reason) > 2000:
            raise ValueError("Nomenclature assessment reason must contain 1 to 2000 characters")
        project = self._require_pricing_state(
            actor,
            project_id,
            required_roles=(ActorRole.PROCUREMENT, ActorRole.TECHNICAL_EXPERT),
        )
        document_set = require_confirmed_document_set_integrity(
            session=self.session,
            settings=self.settings,
            project_id=project.id,
            document_set_revision_id=project.current_document_set_revision_id,
        )
        self._require_current_boq_item(project.id, draft.source_item_id)
        catalog = self._bound_version(project.id, "catalog", "catalog")
        item = self._catalog_item(catalog, draft.canonical_item_id)
        observation = self._verified_observation(
            project.id,
            draft.source_attributes_observation_id,
        )
        require_observation_in_document_set(
            document_revision_ids=document_set.revision_ids,
            document_revision_id=observation.document_revision_id,
        )
        source_attributes = self._nomenclature_evidence_attributes(
            self._validated_observation(observation),
            expected_source_item_id=draft.source_item_id,
        )
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
                "document_set_revision_id": document_set.id,
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
        reason = reason.strip()
        if not reason or len(reason) > 2000:
            raise ValueError("Nomenclature analogue reason must contain 1 to 2000 characters")
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
        reason = reason.strip()
        if not reason or len(reason) > 2000:
            raise ValueError("Nomenclature finalization reason must contain 1 to 2000 characters")
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

    def price_item_context(
        self,
        *,
        actor: Actor,
        project_id: str,
        item_id: str,
    ) -> PriceItemContextView:
        project = self._project_service().get_project(
            actor=actor,
            project_id=project_id,
            required_roles=(
                ActorRole.PROCUREMENT,
                ActorRole.ESTIMATOR,
                ActorRole.TECHNICAL_EXPERT,
                ActorRole.REVIEWER,
                ActorRole.APPROVER,
                ActorRole.AUDITOR,
            ),
        )
        match_row = self._verified_match_for_item(project.id, item_id)
        match = NomenclatureMatch.model_validate(match_row.payload["match"])
        policy = self._bound_version(project.id, "price_policy", "price_policy")
        target_basis = self._target_basis(policy, item_id)
        quote_rows = list(
            self.session.scalars(
                select(PriceQuoteRow)
                .where(
                    PriceQuoteRow.project_id == project.id,
                    PriceQuoteRow.item_id == item_id,
                )
                .order_by(PriceQuoteRow.created_at, PriceQuoteRow.id)
            )
        )
        summaries: list[PriceQuoteSummaryView] = []
        for quote_row in quote_rows:
            quote = self._validated_quote_row(
                project_id=project.id,
                row=quote_row,
                match=match_row,
            )
            normalized_rows = list(
                self.session.scalars(
                    select(NormalizedPriceRow)
                    .where(NormalizedPriceRow.quote_id == quote_row.id)
                    .order_by(NormalizedPriceRow.created_at, NormalizedPriceRow.id)
                )
            )
            normalized_views: list[NormalizedPriceView] = []
            for normalized_row in normalized_rows:
                if normalized_row.payload.get("policy_version_id") != policy.id:
                    continue
                self._require_normalized_row_integrity(
                    project_id=project.id,
                    row=normalized_row,
                    quote=quote,
                    policy=policy,
                )
                normalized_model = NormalizedPrice.model_validate(
                    normalized_row.payload["normalized_price"]
                )
                normalized_views.append(
                    NormalizedPriceView(
                        normalized_price_id=normalized_row.id,
                        quote_id=normalized_row.quote_id,
                        amount_per_unit=normalized_row.amount_per_unit,
                        currency=normalized_row.currency,
                        unit=normalized_model.target_basis.unit,
                        formula_hash=normalized_row.formula_hash,
                        policy_version_id=policy.id,
                    )
                )
            source_origin_id = quote_row.payload.get("source_origin_id")
            if not isinstance(source_origin_id, str):
                raise ValueError("Price quote has no controlled source origin")
            summaries.append(
                PriceQuoteSummaryView(
                    quote=quote,
                    source_origin_id=source_origin_id,
                    normalized_prices=tuple(normalized_views),
                )
            )
        current_decision_row = self.session.scalar(
            select(PriceDecisionRow).where(
                PriceDecisionRow.project_id == project.id,
                PriceDecisionRow.item_id == item_id,
                PriceDecisionRow.is_current.is_(True),
            )
        )
        current_decision = (
            self.require_price_decision_integrity(current_decision_row)
            if current_decision_row is not None
            else None
        )
        return PriceItemContextView(
            project_id=project.id,
            item_id=item_id,
            match_id=match_row.id,
            match_class=MatchClass(match_row.match_class),
            critical_price=bool(match_row.payload.get("critical_price")),
            required_critical_attributes=tuple(sorted(match.required_critical_attributes)),
            technical_attributes=dict(sorted(match.source_attributes.items())),
            document_set_revision_id=project.current_document_set_revision_id,
            catalog_version_id=match_row.catalog_version_id,
            price_policy_version_id=policy.id,
            normalization_rounding_scale=self._price_rounding_scale(policy),
            normalization_rounding_mode=self._price_rounding_mode(policy),
            target_basis=target_basis,
            normalization_references=self._normalization_reference_catalog(policy),
            quotes=tuple(summaries),
            current_decision=current_decision,
        )

    def price_quote_candidate(
        self,
        *,
        actor: Actor,
        project_id: str,
        item_id: str,
        source_observation_id: str,
    ) -> PriceQuoteCandidateView:
        project = self._require_pricing_state(
            actor,
            project_id,
            required_roles=(
                ActorRole.PROCUREMENT,
                ActorRole.ESTIMATOR,
                ActorRole.TECHNICAL_EXPERT,
            ),
        )
        match = self._verified_match_for_item(project.id, item_id)
        draft, source_origin_id = self._quote_draft_from_observation(
            project_id=project.id,
            item_id=item_id,
            source_observation_id=source_observation_id,
            match=match,
        )
        policy = self._bound_version(project.id, "price_policy", "price_policy")
        target = self._target_basis(policy, item_id)
        required_references: list[str] = []
        if draft.basis.unit != target.unit:
            required_references.append("unit_conversion")
        if draft.basis.currency != target.currency:
            required_references.append("fx_rate")
        if draft.basis.region != target.region:
            required_references.append("region_adjustment")
        if draft.basis.party_quantity != target.party_quantity:
            required_references.append("party_adjustment")
        if draft.basis.payment_terms != target.payment_terms:
            required_references.append("payment_adjustment")
        required_adjustments: list[str] = []
        if target.delivery_included and not draft.basis.delivery_included:
            required_adjustments.append("delivery")
        if target.unloading_included and not draft.basis.unloading_included:
            required_adjustments.append("unloading")
        return PriceQuoteCandidateView(
            project_id=project.id,
            item_id=item_id,
            source_observation_id=source_observation_id,
            source_origin_id=source_origin_id,
            draft=draft,
            target_basis=target,
            price_policy_version_id=policy.id,
            required_reference_types=tuple(required_references),
            required_adjustment_kinds=tuple(required_adjustments),
        )

    def record_quote_from_observation(
        self,
        *,
        actor: Actor,
        project_id: str,
        item_id: str,
        source_observation_id: str,
        request_id: str,
        reason: str,
    ) -> PriceQuoteView:
        candidate = self.price_quote_candidate(
            actor=actor,
            project_id=project_id,
            item_id=item_id,
            source_observation_id=source_observation_id,
        )
        return self.record_quote(
            actor=actor,
            project_id=project_id,
            draft=candidate.draft,
            request_id=request_id,
            reason=reason,
        )

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
            draft.source_reference.source_type,
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
            source_reference=draft.source_reference,
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
        match = self._verified_match_for_item(project.id, row.item_id)
        quote = self._validated_quote_row(
            project_id=project.id,
            row=row,
            match=match,
        )
        policy = self._bound_version(project.id, "price_policy", "price_policy")
        request = self._normalization_request(project.id, quote, command, policy)
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
        else:
            self._require_normalized_row_integrity(
                project_id=project.id,
                row=existing,
                quote=quote,
                policy=policy,
                expected_command=command,
            )
        if row.status == PriceStatus.UNNORMALIZED.value:
            row.status = PriceStatus.NORMALIZED.value
            row.updated_at = utc_now()
        elif row.status != PriceStatus.NORMALIZED.value:
            raise ValueError("Only an unnormalized quote can enter normalized state")
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
        validated_quotes: dict[str, PriceQuote] = {}
        for normalized_row in normalized_rows:
            quote_row = quote_rows.get(normalized_row.quote_id)
            if quote_row is None:
                raise ValueError("Normalized price has no project-scoped source quote")
            quote = self._validated_quote_row(
                project_id=project.id,
                row=quote_row,
                match=match,
            )
            self._require_normalized_row_integrity(
                project_id=project.id,
                row=normalized_row,
                quote=quote,
                policy=policy,
            )
            validated_quotes[normalized_row.quote_id] = quote
        quotes = tuple(
            validated_quotes[normalized_row.quote_id] for normalized_row in normalized_rows
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
                missing_source_groups=triangulation.missing_source_groups,
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
            amount_per_unit=(selected_amount if status is PriceStatus.VERIFIED else None),
            currency=row.currency if status is PriceStatus.VERIFIED else None,
            unit=row.unit if status is PriceStatus.VERIFIED else None,
            derived_observation_id=derived_observation_id,
            triangulation=triangulation,
            relative_spread=relative_spread,
            approval_task_ids=approval_task_ids,
            rfq_request_id=rfq_request_id,
            project_state=ApprovalState(project.state),
        )

    def _normalization_request(
        self,
        project_id: str,
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
        for adjustment in adjustments:
            evidence = self._verified_observation(project_id, adjustment.evidence_id)
            evidence_model = Observation.model_validate(evidence.payload["observation"])
            try:
                evidenced_amount = Decimal(str(evidence_model.value))
            except (InvalidOperation, TypeError, ValueError) as error:
                raise ValueError(
                    f"Normalization adjustment evidence is not decimal: {adjustment.adjustment_id}"
                ) from error
            if (
                evidenced_amount != adjustment.amount_per_target_unit
                or evidence_model.unit != target.unit
                or evidence.payload.get("currency") != target.currency
            ):
                raise ValueError(
                    f"Normalization adjustment evidence does not reproduce the "
                    f"approved amount/basis: {adjustment.adjustment_id}"
                )
        if quote.basis.region != target.region:
            reference = self._policy_reference(
                policy,
                "region_adjustments",
                command.region_adjustment_id,
            )
            if (
                reference.get("source_region") != quote.basis.region
                or reference.get("target_region") != target.region
            ):
                raise ValueError("Region adjustment does not match quote and target regions")
        elif command.region_adjustment_id is not None:
            raise ValueError("Region adjustment reference is extraneous")
        if quote.basis.party_quantity != target.party_quantity:
            reference = self._policy_reference(
                policy,
                "party_adjustments",
                command.party_adjustment_id,
            )
            source_party_quantity = self._decimal_parameter(
                reference,
                "source_party_quantity",
            )
            target_party_quantity = self._decimal_parameter(
                reference,
                "target_party_quantity",
            )
            if (
                source_party_quantity != quote.basis.party_quantity
                or target_party_quantity != target.party_quantity
            ):
                raise ValueError("Party adjustment does not match quote and target quantities")
        elif command.party_adjustment_id is not None:
            raise ValueError("Party adjustment reference is extraneous")
        if quote.basis.payment_terms != target.payment_terms:
            reference = self._policy_reference(
                policy,
                "payment_adjustments",
                command.payment_adjustment_id,
            )
            if (
                reference.get("source_payment_terms") != quote.basis.payment_terms
                or reference.get("target_payment_terms") != target.payment_terms
            ):
                raise ValueError("Payment adjustment does not match quote and target terms")
        elif command.payment_adjustment_id is not None:
            raise ValueError("Payment adjustment reference is extraneous")
        return NormalizationRequest(
            policy_version_id=policy.id,
            target_basis=target,
            source_units_per_target_unit=conversion_rate,
            unit_conversion_id=conversion_id,
            target_currency_per_source_currency=fx_rate,
            fx_rate_id=fx_rate_id,
            adjustments=adjustments,
            region_adjustment_id=command.region_adjustment_id,
            party_adjustment_id=command.party_adjustment_id,
            payment_adjustment_id=command.payment_adjustment_id,
            rounding_scale=self._price_rounding_scale(policy),
            rounding_mode=self._price_rounding_mode(policy),
        )

    @staticmethod
    def _price_rounding_scale(policy: ControlledVersionRow) -> int:
        value = policy.payload.get("normalization_rounding_scale")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 12:
            raise ValueError("Approved price policy lacks a valid normalization_rounding_scale")
        return value

    @staticmethod
    def _price_rounding_mode(policy: ControlledVersionRow) -> str:
        value = policy.payload.get("normalization_rounding_mode")
        if value not in {"ROUND_HALF_UP", "ROUND_HALF_EVEN"}:
            raise ValueError("Approved price policy lacks a supported normalization_rounding_mode")
        return str(value)

    def _quote_draft_from_observation(
        self,
        *,
        project_id: str,
        item_id: str,
        source_observation_id: str,
        match: NomenclatureMatchRow,
    ) -> tuple[PriceQuoteDraft, str]:
        observation = self._verified_observation(project_id, source_observation_id)
        value = observation.payload.get("observation", {}).get("value")
        if not isinstance(value, dict):
            raise ValueError("Price quote observation does not contain a structured quote")
        draft = PriceQuoteDraft.model_validate(
            {
                **value,
                "source_observation_id": source_observation_id,
            }
        )
        if draft.item_id != item_id:
            raise ValueError("Price quote observation belongs to another item")
        self._validate_quote_technical_attributes(match, draft.technical_attributes)
        source_origin_id = self._validate_price_evidence_class(
            project_id,
            observation,
            draft.evidence_class,
            draft.source_reference.source_type,
        )
        return draft, source_origin_id

    @staticmethod
    def _normalization_reference_catalog(
        policy: ControlledVersionRow,
    ) -> dict[str, dict[str, dict[str, Any]]]:
        result: dict[str, dict[str, dict[str, Any]]] = {}
        for section in (
            "unit_conversions",
            "fx_rates",
            "adjustments",
            "region_adjustments",
            "party_adjustments",
            "payment_adjustments",
        ):
            raw_section = policy.payload.get(section, {})
            if not isinstance(raw_section, dict) or not all(
                isinstance(reference_id, str) and isinstance(payload, dict)
                for reference_id, payload in raw_section.items()
            ):
                raise ValueError(f"Approved price policy section is invalid: {section}")
            result[section] = {
                reference_id: payload for reference_id, payload in sorted(raw_section.items())
            }
        return result

    def require_price_decision_integrity(
        self,
        row: PriceDecisionRow,
    ) -> PriceDecisionSummaryView:
        policy = self._bound_version(
            row.project_id,
            "price_policy",
            "price_policy",
        )
        if row.policy_version_id != policy.id:
            raise ValueError("Current price decision uses a stale price policy")
        match = self._verified_match_for_item(row.project_id, row.item_id)
        normalized_price_ids = row.payload.get("normalized_price_ids", [])
        source_origin_ids = row.payload.get("source_origin_ids", [])
        approval_task_ids = row.payload.get("approval_task_ids", [])
        if any(
            not isinstance(values, list)
            or not all(isinstance(value, str) for value in values)
            or len(values) != len(set(values))
            for values in (
                normalized_price_ids,
                source_origin_ids,
                approval_task_ids,
            )
        ):
            raise ValueError("Current price decision contains invalid lineage identifiers")
        raw_as_of = row.payload.get("as_of")
        try:
            as_of = date.fromisoformat(raw_as_of) if isinstance(raw_as_of, str) else None
        except ValueError as error:
            raise ValueError("Current price decision has an invalid as-of date") from error
        if as_of is None:
            raise ValueError("Current price decision has no as-of date")
        loaded_normalized_rows = list(
            self.session.scalars(
                select(NormalizedPriceRow).where(NormalizedPriceRow.id.in_(normalized_price_ids))
            )
        )
        if len(loaded_normalized_rows) != len(normalized_price_ids):
            raise ValueError("Current price decision has missing normalized inputs")
        normalized_rows_by_id = {
            normalized_row.id: normalized_row for normalized_row in loaded_normalized_rows
        }
        normalized_rows = [
            normalized_rows_by_id[normalized_price_id]
            for normalized_price_id in normalized_price_ids
        ]
        quote_rows = {
            quote_row.id: quote_row
            for quote_row in self.session.scalars(
                select(PriceQuoteRow).where(
                    PriceQuoteRow.id.in_(
                        {normalized_row.quote_id for normalized_row in normalized_rows}
                    ),
                    PriceQuoteRow.project_id == row.project_id,
                    PriceQuoteRow.item_id == row.item_id,
                )
            )
        }
        quotes: list[PriceQuote] = []
        origins: set[str] = set()
        for normalized_row in normalized_rows:
            if normalized_row.payload.get("policy_version_id") != policy.id:
                raise ValueError("Current price decision includes another price policy")
            quote_row = quote_rows.get(normalized_row.quote_id)
            if quote_row is None:
                raise ValueError("Current price decision has a missing project-scoped quote")
            quote = self._validated_quote_row(
                project_id=row.project_id,
                row=quote_row,
                match=match,
            )
            self._require_normalized_row_integrity(
                project_id=row.project_id,
                row=normalized_row,
                quote=quote,
                policy=policy,
            )
            quotes.append(quote)
            source_origin = quote_row.payload.get("source_origin_id")
            if not isinstance(source_origin, str) or not source_origin:
                raise ValueError("Current price decision quote has no controlled source origin")
            origins.add(source_origin)
        triangulation = evaluate_triangulation(
            item_id=row.item_id,
            quotes=tuple(quotes),
            as_of=as_of,
            critical=bool(match.payload.get("critical_price")),
        )
        raw_triangulation = row.payload.get("triangulation")
        if (
            not isinstance(raw_triangulation, dict)
            or TriangulationResult.model_validate(raw_triangulation) != triangulation
            or set(triangulation.quote_ids) != set(quote_rows)
        ):
            raise ValueError("Current price decision failed triangulation integrity validation")
        origins_are_independent = bool(normalized_rows) and len(origins) == len(normalized_rows)
        if row.payload.get("origins_are_independent") is not origins_are_independent or tuple(
            sorted(origins)
        ) != tuple(source_origin_ids):
            raise ValueError("Current price decision failed source-origin integrity validation")
        amounts = tuple(sorted(item.amount_per_unit for item in normalized_rows))
        selected_amount = self._median(amounts) if amounts else None
        relative_spread = (
            (max(amounts) - min(amounts)) / selected_amount
            if selected_amount is not None and selected_amount != 0 and len(amounts) > 1
            else Decimal("0")
            if selected_amount is not None
            else None
        )
        stored_relative_spread = row.payload.get("relative_spread")
        try:
            parsed_relative_spread = (
                Decimal(stored_relative_spread) if isinstance(stored_relative_spread, str) else None
            )
        except InvalidOperation as error:
            raise ValueError("Current price decision has an invalid relative spread") from error
        target_basis = self._target_basis(policy, row.item_id)
        if (
            row.amount_per_unit != selected_amount
            or row.currency != (target_basis.currency if selected_amount is not None else None)
            or row.unit != (target_basis.unit if selected_amount is not None else None)
            or parsed_relative_spread != relative_spread
            or row.payload.get("selection_method") != policy.payload.get("selection_method")
        ):
            raise ValueError(
                "Current price decision failed deterministic amount integrity validation"
            )
        evaluation_identity = {
            "project_id": row.project_id,
            "item_id": row.item_id,
            "policy_version_id": policy.id,
            "normalized_price_ids": sorted(normalized_price_ids),
            "as_of": as_of,
            "triangulation": triangulation,
            "relative_spread": relative_spread,
        }
        expected_evaluation_id = f"price-evaluation-{content_hash(evaluation_identity)[:24]}"
        evaluation_id = row.payload.get("evaluation_id")
        if evaluation_id != expected_evaluation_id:
            raise ValueError("Current price decision failed evaluation identity validation")
        project_row = self.session.get(ProjectRow, row.project_id)
        if project_row is None:
            raise LookupError(row.project_id)
        if (
            row.payload.get("document_set_revision_id")
            != project_row.current_document_set_revision_id
        ):
            raise ValueError("Current price decision uses a stale document-set revision")
        approval_findings = row.payload.get("approval_findings", [])
        if not isinstance(approval_findings, list):
            raise ValueError("Current price decision has invalid approval findings")
        approval_tasks = list(
            self.session.scalars(
                select(ApprovalTaskRow).where(
                    ApprovalTaskRow.id.in_(approval_task_ids),
                    ApprovalTaskRow.project_id == row.project_id,
                    ApprovalTaskRow.entity_type == "price_evaluation",
                    ApprovalTaskRow.entity_id == evaluation_id,
                )
            )
        )
        if len(approval_tasks) != len(approval_task_ids):
            raise ValueError("Current price decision has invalid approval-task lineage")
        status = PriceStatus(row.status)
        if status is PriceStatus.RFQ_REQUIRED:
            if triangulation.passed and origins_are_independent:
                raise ValueError("RFQ price decision has no reproduced RFQ condition")
        elif status is PriceStatus.EXPERT_REVIEW_REQUIRED:
            if not triangulation.passed or not origins_are_independent:
                raise ValueError("Expert-review price decision conflicts with triangulation")
            if (
                row.payload.get("selection_method") == "MEDIAN"
                and not approval_findings
                and not approval_task_ids
            ):
                raise ValueError("Expert-review price decision has no reproduced review condition")
        elif status is PriceStatus.VERIFIED:
            if (
                not triangulation.passed
                or not origins_are_independent
                or row.payload.get("selection_method") != "MEDIAN"
                or approval_findings
                or any(task.status != "APPROVED" for task in approval_tasks)
            ):
                raise ValueError("Verified price decision no longer satisfies approval conditions")
        else:
            raise ValueError("Current price decision has an unusable status")
        if status is PriceStatus.VERIFIED:
            if row.derived_observation_id is None or selected_amount is None:
                raise ValueError("Verified price decision has no derived rate observation")
            observation_row = self._verified_observation(
                row.project_id,
                row.derived_observation_id,
            )
            observation = Observation.model_validate(observation_row.payload["observation"])
            source_normalized_price_ids = observation_row.payload.get("source_normalized_price_ids")
            if not isinstance(source_normalized_price_ids, list) or not all(
                isinstance(identifier, str) for identifier in source_normalized_price_ids
            ):
                raise ValueError("Verified price observation has invalid normalized-price lineage")
            try:
                observation_amount = Decimal(str(observation.value))
            except (InvalidOperation, TypeError, ValueError) as error:
                raise ValueError("Verified price observation has an invalid amount") from error
            if (
                not observation_amount.is_finite()
                or observation_amount != selected_amount
                or observation.unit != target_basis.unit
                or observation.method_version != policy.id
                or observation_row.payload.get("basis_type") != "NORMALIZED_PRICE"
                or observation_row.payload.get("unit_rate") != str(selected_amount)
                or observation_row.payload.get("currency") != target_basis.currency
                or observation_row.payload.get("unit") != target_basis.unit
                or observation_row.payload.get("price_evaluation_id") != evaluation_id
                or set(source_normalized_price_ids) != set(normalized_price_ids)
            ):
                raise ValueError("Verified price decision failed derived-observation integrity")
        elif row.derived_observation_id is not None:
            raise ValueError("Unverified price decision cannot expose a verified rate observation")
        rfq_request_id = self.session.scalar(
            select(RfqRequestRow.id).where(RfqRequestRow.price_decision_id == row.id)
        )
        if status is PriceStatus.RFQ_REQUIRED and rfq_request_id is None:
            raise ValueError("RFQ price decision has no RFQ request")
        return PriceDecisionSummaryView(
            decision_id=row.id,
            status=status,
            amount_per_unit=(row.amount_per_unit if status is PriceStatus.VERIFIED else None),
            currency=row.currency if status is PriceStatus.VERIFIED else None,
            unit=row.unit if status is PriceStatus.VERIFIED else None,
            policy_version_id=row.policy_version_id,
            derived_observation_id=row.derived_observation_id,
            evaluation_id=evaluation_id,
            as_of=as_of,
            normalized_price_ids=tuple(normalized_price_ids),
            source_origin_ids=tuple(source_origin_ids),
            approval_task_ids=tuple(approval_task_ids),
            rfq_request_id=rfq_request_id,
        )

    def _validated_quote_row(
        self,
        *,
        project_id: str,
        row: PriceQuoteRow,
        match: NomenclatureMatchRow,
    ) -> PriceQuote:
        raw_quote = row.payload.get("quote")
        if not isinstance(raw_quote, dict):
            raise ValueError("Price quote payload is not reproducible")
        quote = PriceQuote.model_validate(raw_quote).model_copy(
            update={"status": PriceStatus(row.status)}
        )
        if (
            quote.quote_id != row.id
            or quote.item_id != row.item_id
            or quote.source_observation_id != row.source_observation_id
            or quote.amount != row.amount
            or quote.basis.currency != row.currency
            or quote.quote_date != row.quote_date
            or quote.valid_until != row.valid_until
        ):
            raise ValueError("Price quote row differs from its immutable evidence payload")
        self._validate_quote_technical_attributes(match, quote.technical_attributes)
        observation = self._verified_observation(project_id, quote.source_observation_id)
        reproduced_draft = PriceQuoteDraft.model_validate(
            quote.model_dump(mode="json", exclude={"quote_id", "status"})
        )
        if content_hash(observation.payload.get("observation", {}).get("value")) != content_hash(
            reproduced_draft.evidence_value()
        ):
            raise ValueError("Price quote no longer reproduces its verified source observation")
        source_origin_id = self._validate_price_evidence_class(
            project_id,
            observation,
            quote.evidence_class,
            quote.source_reference.source_type,
        )
        if row.payload.get("source_origin_id") != source_origin_id:
            raise ValueError("Price quote source origin differs from qualified evidence")
        return quote

    def _require_normalized_row_integrity(
        self,
        *,
        project_id: str,
        row: NormalizedPriceRow,
        quote: PriceQuote,
        policy: ControlledVersionRow,
        expected_command: NormalizePriceCommand | None = None,
    ) -> None:
        raw_command = row.payload.get("normalization_command")
        raw_normalized = row.payload.get("normalized_price")
        if not isinstance(raw_command, dict) or not isinstance(raw_normalized, dict):
            raise ValueError("Normalized price lacks reproducible inputs or output")
        command = NormalizePriceCommand.model_validate(raw_command)
        if expected_command is not None and command != expected_command:
            raise ValueError("Normalized price identity collides with a different command")
        if command.quote_id != row.quote_id or quote.quote_id != row.quote_id:
            raise ValueError("Normalized price command does not match its source quote")
        normalized_at = ensure_utc(row.created_at)
        if normalized_at is None:
            raise ValueError("Normalized price timestamp is missing")
        request = self._normalization_request(project_id, quote, command, policy)
        recalculated = normalize_quote(quote, request, normalized_at=normalized_at)
        stored = NormalizedPrice.model_validate(raw_normalized)
        if (
            row.payload.get("policy_version_id") != policy.id
            or stored != recalculated
            or row.id != recalculated.normalized_price_id
            or row.quote_id != recalculated.quote_id
            or row.amount_per_unit != recalculated.amount_per_unit
            or row.currency != recalculated.target_basis.currency
            or row.formula_hash != recalculated.normalization_formula.removeprefix("sha256:")
        ):
            raise ValueError("Normalized price failed deterministic integrity validation")

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
        source_type: PriceSourceType,
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
        if any(
            source_type.value not in qualification.payload.get("supported_price_source_types", [])
            for qualification in qualifications
        ):
            raise ValueError("Price source type is outside the source adapter qualification")
        origins = {row.payload.get("source_origin_id") for row in leaves}
        if len(origins) != 1 or None in origins:
            raise ValueError("A quote extraction must resolve to one controlled source origin")
        source_origin_id = str(next(iter(origins)))
        if any(
            source_origin_id not in qualification.payload.get("supported_price_source_origins", [])
            for qualification in qualifications
        ):
            raise ValueError("Price source origin is outside the source adapter qualification")
        return source_origin_id

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
        project = self.session.get(ProjectRow, project_id)
        if project is None:
            raise LookupError(project_id)
        self._require_nomenclature_match_integrity(project, row)
        return row

    def require_verified_nomenclature_match(
        self,
        *,
        project_id: str,
        item_id: str,
    ) -> NomenclatureMatchRow:
        """Return a current match only after replaying its governed evidence."""

        return self._verified_match_for_item(project_id, item_id)

    def require_current_nomenclature_source_item(
        self,
        *,
        project_id: str,
        item_id: str,
    ) -> NomenclatureSourceItemView:
        matches = tuple(
            item
            for item in self._current_nomenclature_source_items(project_id)
            if item.source_item_id == item_id
        )
        if len(matches) != 1:
            raise ValueError(
                "Nomenclature source item must identify exactly one current verified "
                "BoQ cost component"
            )
        return matches[0]

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
        project = self.session.get(ProjectRow, project_id)
        if project is None:
            raise LookupError(project_id)
        self._require_nomenclature_match_integrity(project, row)
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
        self._validated_observation(row)
        return row

    @staticmethod
    def _validated_observation(row: ObservationRow) -> Observation:
        observation = Observation.model_validate(row.payload.get("observation"))
        if (
            row.id != observation.observation_id
            or row.document_revision_id != observation.location.document_revision_id
            or row.field_name != observation.field_name
            or row.method != observation.method.value
            or row.method_version != observation.method_version
            or row.status != observation.status.value
            or observation.status is not VerificationStatus.VERIFIED
        ):
            raise RuntimeError("Verified nomenclature evidence identity does not reproduce")
        return observation

    def _current_nomenclature_source_items(
        self,
        project_id: str,
    ) -> tuple[NomenclatureSourceItemView, ...]:
        lines = list(
            self.session.scalars(
                select(BoqLineRow).where(
                    BoqLineRow.project_id == project_id,
                    BoqLineRow.status == VerificationStatus.VERIFIED.value,
                    BoqLineRow.is_current.is_(True),
                )
            )
        )
        result: list[NomenclatureSourceItemView] = []
        seen: set[str] = set()
        for line in lines:
            components = line.payload.get("cost_components")
            if not isinstance(components, list):
                raise RuntimeError("Verified BoQ line cost-component plan is invalid")
            for component in components:
                source_item_id = (
                    component.get("semantic_key") if isinstance(component, dict) else None
                )
                if (
                    not isinstance(source_item_id, str)
                    or not source_item_id
                    or source_item_id != source_item_id.strip()
                ):
                    raise RuntimeError("Verified BoQ line cost-component identity is invalid")
                if source_item_id in seen:
                    raise ValueError(
                        "Nomenclature source item must be globally unique across "
                        "current verified BoQ components"
                    )
                seen.add(source_item_id)
                result.append(
                    NomenclatureSourceItemView(
                        source_item_id=source_item_id,
                        boq_line_id=line.id,
                        line_key=line.line_key,
                        wbs_node_id=line.wbs_node_id,
                        work_code=line.work_code,
                        description=line.description,
                        unit=line.unit,
                    )
                )
        return tuple(sorted(result, key=lambda item: (item.line_key, item.source_item_id)))

    def _require_current_boq_item(self, project_id: str, item_id: str) -> None:
        if not any(
            item.source_item_id == item_id
            for item in self._current_nomenclature_source_items(project_id)
        ):
            raise ValueError(
                "Nomenclature source item must identify exactly one current verified "
                "BoQ cost component"
            )

    @staticmethod
    def _nomenclature_evidence_attributes(
        observation: Observation,
        *,
        expected_source_item_id: str,
    ) -> dict[str, str]:
        expected_field_name = f"technical_attributes:{expected_source_item_id}"
        if observation.field_name != expected_field_name:
            raise ValueError("Nomenclature evidence field is not bound to the BoQ source item")
        value = observation.value
        if not isinstance(value, dict):
            raise ValueError("Nomenclature evidence must contain a structured item binding")
        source_item_id = value.get("source_item_id")
        attributes = value.get("attributes")
        if source_item_id != expected_source_item_id:
            raise ValueError("Nomenclature evidence belongs to another BoQ source item")
        if (
            not isinstance(attributes, dict)
            or not attributes
            or not all(
                isinstance(key, str)
                and key
                and key == key.strip()
                and isinstance(attribute_value, str)
                and attribute_value
                and attribute_value == attribute_value.strip()
                for key, attribute_value in attributes.items()
            )
        ):
            raise ValueError("Nomenclature evidence attributes are invalid")
        return {
            str(key): str(attribute_value) for key, attribute_value in sorted(attributes.items())
        }

    def _nomenclature_match_integrity_blockers(
        self,
        project: ProjectRow,
        row: NomenclatureMatchRow,
    ) -> list[str]:
        blockers: list[str] = []
        try:
            match = NomenclatureMatch.model_validate(row.payload.get("match"))
        except (TypeError, ValueError):
            return ["MATCH_PAYLOAD_INVALID"]
        if (
            row.id != match.match_id
            or row.source_item_id != match.source_item_id
            or row.canonical_item_id != match.canonical_item_id
            or row.match_class != match.match_class.value
        ):
            blockers.append("MATCH_IDENTITY_FAILED")
        try:
            self._require_current_boq_item(project.id, row.source_item_id)
        except (LookupError, RuntimeError, ValueError):
            blockers.append("BOQ_ITEM_NOT_CURRENT")
        try:
            catalog = self._bound_version(project.id, "catalog", "catalog")
        except (LookupError, RuntimeError, ValueError):
            catalog = None
            blockers.append("CATALOG_INTEGRITY_FAILED")
        if catalog is not None:
            if row.catalog_version_id != catalog.id:
                blockers.append("CATALOG_VERSION_MISMATCH")
            elif not isinstance(row.canonical_item_id, str) or not row.canonical_item_id:
                blockers.append("CATALOG_ITEM_INVALID")
            else:
                try:
                    item = self._catalog_item(catalog, row.canonical_item_id)
                except (LookupError, RuntimeError, ValueError):
                    blockers.append("CATALOG_ITEM_INVALID")
                else:
                    if match.canonical_attributes != item[
                        "attributes"
                    ] or match.required_critical_attributes != frozenset(
                        item["critical_attributes"]
                    ):
                        blockers.append("CATALOG_ATTRIBUTES_MISMATCH")
        try:
            document_set = require_confirmed_document_set_integrity(
                session=self.session,
                settings=self.settings,
                project_id=project.id,
                document_set_revision_id=project.current_document_set_revision_id,
            )
        except (LookupError, RuntimeError, TypeError, ValueError):
            document_set = None
            blockers.append("DOCUMENT_SET_INTEGRITY_FAILED")
        if document_set is None or row.payload.get("document_set_revision_id") != document_set.id:
            blockers.append("DOCUMENT_SET_MISMATCH")
        source_observation_id = row.payload.get("source_attributes_observation_id")
        if not isinstance(source_observation_id, str) or not source_observation_id:
            blockers.append("SOURCE_OBSERVATION_MISSING")
        else:
            try:
                observation = self._validated_observation(
                    self._verified_observation(project.id, source_observation_id)
                )
            except (LookupError, RuntimeError, TypeError, ValueError):
                blockers.append("SOURCE_OBSERVATION_INVALID")
            else:
                if (
                    document_set is None
                    or observation.location.document_revision_id not in document_set.revision_ids
                ):
                    blockers.append("SOURCE_DOCUMENT_SET_INVALID")
                try:
                    reproduced_attributes = self._nomenclature_evidence_attributes(
                        observation,
                        expected_source_item_id=row.source_item_id,
                    )
                except (TypeError, ValueError):
                    blockers.append("SOURCE_OBSERVATION_BINDING_INVALID")
                else:
                    if match.source_attributes != reproduced_attributes:
                        blockers.append("SOURCE_ATTRIBUTES_MISMATCH")
        equivalence_version_id = row.payload.get("equivalence_rule_version_id")
        if equivalence_version_id is not None:
            try:
                equivalence = self._bound_version(
                    project.id,
                    "nomenclature_equivalence_rules",
                    "nomenclature_equivalence_rules",
                )
            except (LookupError, RuntimeError, ValueError):
                blockers.append("EQUIVALENCE_RULE_INTEGRITY_FAILED")
            else:
                if equivalence.id != equivalence_version_id:
                    blockers.append("EQUIVALENCE_RULE_VERSION_MISMATCH")
        return list(dict.fromkeys(blockers))

    def _require_nomenclature_match_integrity(
        self,
        project: ProjectRow,
        row: NomenclatureMatchRow,
    ) -> None:
        blockers = self._nomenclature_match_integrity_blockers(project, row)
        if blockers:
            raise ValueError("Nomenclature match integrity failed: " + ", ".join(blockers))

    def _bound_version(
        self,
        project_id: str,
        purpose: str,
        kind: str,
    ) -> ControlledVersionRow:
        project = self.session.get(ProjectRow, project_id)
        if project is None:
            raise LookupError(project_id)
        return require_bound_controlled_version(
            session=self.session,
            settings=self.settings,
            project_id=project_id,
            organization_id=project.organization_id,
            purpose=purpose,
            kind=kind,
        )

    @staticmethod
    def _validated_catalog_items(
        catalog: ControlledVersionRow,
    ) -> tuple[CatalogItemView, ...]:
        raw_items = catalog.payload.get("items")
        if not isinstance(raw_items, dict) or not raw_items:
            raise ValueError("Approved catalog contains no canonical items")
        items: list[CatalogItemView] = []
        for canonical_item_id, raw_item in raw_items.items():
            if (
                not isinstance(canonical_item_id, str)
                or not canonical_item_id
                or canonical_item_id != canonical_item_id.strip()
                or len(canonical_item_id) > 128
                or not isinstance(raw_item, dict)
            ):
                raise ValueError("Approved catalog item identity is invalid")
            attributes = raw_item.get("attributes")
            critical_attributes = raw_item.get("critical_attributes")
            critical_price = raw_item.get("critical_price")
            if (
                not isinstance(attributes, dict)
                or not attributes
                or not all(
                    isinstance(key, str)
                    and key
                    and key == key.strip()
                    and len(key) <= 200
                    and isinstance(value, str)
                    and value
                    and value == value.strip()
                    and len(value) <= 1000
                    for key, value in attributes.items()
                )
                or not isinstance(critical_attributes, list)
                or not critical_attributes
                or not all(
                    isinstance(attribute, str) and attribute in attributes
                    for attribute in critical_attributes
                )
                or len(critical_attributes) != len(set(critical_attributes))
                or not isinstance(critical_price, bool)
            ):
                raise ValueError("Approved catalog item attributes or criticality are invalid")
            items.append(
                CatalogItemView(
                    canonical_item_id=canonical_item_id,
                    attributes={str(key): str(value) for key, value in attributes.items()},
                    critical_attributes=tuple(critical_attributes),
                    critical_price=critical_price,
                )
            )
        return tuple(sorted(items, key=lambda item: item.canonical_item_id))

    def _catalog_item(
        self,
        catalog: ControlledVersionRow,
        canonical_item_id: str,
    ) -> dict[str, Any]:
        item = next(
            (
                item
                for item in self._validated_catalog_items(catalog)
                if item.canonical_item_id == canonical_item_id
            ),
            None,
        )
        if item is None:
            raise ValueError("Canonical item is absent from the approved catalog")
        return {
            "attributes": item.attributes,
            "critical_attributes": list(item.critical_attributes),
            "critical_price": item.critical_price,
        }

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
        raw_value = payload.get(field_name)
        literal = str(raw_value)
        if len(literal) > 80 or re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", literal) is None:
            raise ValueError(f"Controlled decimal parameter is invalid: {field_name}")
        try:
            value = Decimal(literal)
        except (InvalidOperation, TypeError, ValueError) as error:
            raise ValueError(f"Controlled decimal parameter is invalid: {field_name}") from error
        decimal_tuple = value.as_tuple()
        if not value.is_finite() or value <= 0:
            raise ValueError(f"Controlled decimal parameter must be positive: {field_name}")
        if not isinstance(decimal_tuple.exponent, int):
            raise ValueError(f"Controlled decimal parameter is invalid: {field_name}")
        fractional_digits = max(-decimal_tuple.exponent, 0)
        if len(decimal_tuple.digits) > 38 or fractional_digits > 24:
            raise ValueError(
                f"Controlled decimal parameter exceeds supported precision: {field_name}"
            )
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
        missing_source_groups: tuple[str, ...],
        origins_are_independent: bool,
        actor: Actor,
    ) -> str:
        now = utc_now()
        identity = {
            "project_id": project_id,
            "item_id": item_id,
            "missing_classes": missing_classes,
            "missing_source_groups": missing_source_groups,
            "origins_are_independent": origins_are_independent,
            "price_policy_version_id": decision.policy_version_id,
        }
        rfq_id = f"rfq-{content_hash(identity)[:24]}"
        existing = self.session.get(RfqRequestRow, rfq_id)
        payload = {
            "missing_evidence_classes": [item.value for item in missing_classes],
            "missing_source_groups": list(missing_source_groups),
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
