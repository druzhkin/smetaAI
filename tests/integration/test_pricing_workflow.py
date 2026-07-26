from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tenderguard.api.main import create_app
from tenderguard.application.approvals import ApprovalDecisionCommand, ApprovalService
from tenderguard.application.contracts import (
    ContractCostImpactCommand,
    ContractService,
    ContractTermDecisionCommand,
    ContractTermDraft,
)
from tenderguard.application.pricing import (
    NomenclatureAssessmentDraft,
    NormalizePriceCommand,
    PriceQuoteDraft,
    PricingService,
)
from tenderguard.application.projects import ProjectService
from tenderguard.config import Settings
from tenderguard.domain.common import content_hash
from tenderguard.domain.enums import (
    ActorRole,
    ApprovalDecision,
    ApprovalState,
    ContractTermKind,
    EvidenceMethod,
    PriceEvidenceClass,
    PriceStatus,
    VatBasis,
    VerificationStatus,
)
from tenderguard.domain.models import CommercialBasis, EvidenceLocation, Observation
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.database import (
    create_database_engine,
    create_schema_for_tests,
    create_session_factory,
)
from tenderguard.infrastructure.object_store import LocalObjectStore
from tenderguard.infrastructure.orm import (
    AdapterQualificationRow,
    BoqLineRow,
    ControlledVersionRow,
    DocumentSetRevisionRow,
    NormalizedPriceRow,
    ObservationRow,
    PriceDecisionRow,
    ProjectRow,
    QuantityRow,
    RfqRequestRow,
    RiskCalculationRow,
    RiskItemRow,
)
from tests.integration.support import (
    add_document_set_confirmation_audit,
    add_governed_controlled_version,
    add_project_controlled_version_binding,
    approval_task_updated_at,
    project_memberships,
)


def test_critical_price_opens_rfq_then_verifies_three_way_triangulation(
    tmp_path: Path,
) -> None:
    settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite://",
        local_object_store_path=tmp_path / "objects",
        local_quarantine_store_path=tmp_path / "quarantine",
        allow_insecure_dev_auth=True,
        audit_signing_key="test-audit-signing-key-at-least-32-bytes",
    )
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    factory = create_session_factory(engine)
    store = LocalObjectStore(tmp_path / "objects")
    procurement = Actor(
        "procurement-1",
        "org-1",
        frozenset(
            {
                ActorRole.PROCUREMENT,
                ActorRole.ESTIMATOR,
                ActorRole.TECHNICAL_EXPERT,
            }
        ),
    )
    contract_reviewer = Actor(
        "contract-reviewer",
        "org-1",
        frozenset({ActorRole.REVIEWER}),
    )
    catalog_creator = Actor(
        "catalog-creator",
        "org-1",
        frozenset({ActorRole.CATALOG_OWNER}),
    )
    catalog_approver = Actor(
        "catalog-approver",
        "org-1",
        frozenset({ActorRole.CATALOG_OWNER, ActorRole.APPROVER}),
    )
    methodology_creator = Actor(
        "methodology-creator",
        "org-1",
        frozenset({ActorRole.METHODOLOGY_OWNER}),
    )
    methodology_approver = Actor(
        "methodology-approver",
        "org-1",
        frozenset({ActorRole.METHODOLOGY_OWNER}),
    )
    now = datetime(2026, 7, 23, tzinfo=UTC)
    basis = CommercialBasis(
        currency="RUB",
        vat_basis=VatBasis.INCLUSIVE,
        vat_rate=Decimal("0.20"),
        unit="m",
        package_quantity=Decimal("10"),
        party_quantity=Decimal("1000"),
        region="Moscow",
        delivery_included=True,
        unloading_included=True,
        payment_terms="30 days",
    )

    with factory.begin() as session:
        session.add(
            ProjectRow(
                id="project-pricing",
                organization_id="org-1",
                code="PRICE-1",
                name="Pricing workflow",
                state=ApprovalState.PRICING_IN_PROGRESS.value,
                current_document_set_revision_id="document-set-pricing",
                row_version=1,
                created_at=now,
                updated_at=now,
            )
        )
        pricing_revision_ids = ["revision-1", "revision-2", "revision-3"]
        document_set = DocumentSetRevisionRow(
            id="document-set-pricing",
            project_id="project-pricing",
            manifest_hash=content_hash(pricing_revision_ids),
            revision_ids=pricing_revision_ids,
            status="CONFIRMED",
            created_by=procurement.actor_id,
            created_at=now,
            confirmed_by=catalog_approver.actor_id,
            confirmed_at=now,
        )
        session.add(document_set)
        session.add_all(
            project_memberships(
                "project-pricing",
                (procurement, contract_reviewer),
                owner_id=procurement.actor_id,
                now=now,
            )
        )
        add_document_set_confirmation_audit(
            session=session,
            settings=settings,
            object_store=store,
            row=document_set,
            actor=catalog_approver,
        )
        session.add(
            BoqLineRow(
                id="boq-line-pipe",
                project_id="project-pricing",
                line_key="pipe",
                wbs_node_id="wbs-pipe",
                work_code="PIPE_INSTALLATION",
                description="Pipe material cost line",
                unit="m",
                status="VERIFIED",
                payload={
                    "cost_components": [
                        {
                            "semantic_key": "pipe-source",
                            "category": "MATERIAL",
                            "basis_kind": "MARKET",
                        }
                    ]
                },
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            QuantityRow(
                id="quantity-pipe",
                boq_line_id="boq-line-pipe",
                value=Decimal("100"),
                unit="m",
                status="VERIFIED",
                supersedes_quantity_id=None,
                is_current=True,
                payload={},
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            BoqLineRow(
                id="boq-line-risk",
                project_id="project-pricing",
                line_key="risk",
                wbs_node_id="wbs-risk",
                work_code="PROJECT_RISK_RESERVE",
                description="Project risk reserve",
                unit="project",
                status="VERIFIED",
                payload={
                    "cost_components": [
                        {
                            "semantic_key": "risk-reserve",
                            "category": "RISK",
                            "basis_kind": "RISK_MODEL",
                        }
                    ]
                },
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            QuantityRow(
                id="quantity-risk",
                boq_line_id="boq-line-risk",
                value=Decimal("1"),
                unit="project",
                status="VERIFIED",
                supersedes_quantity_id=None,
                is_current=True,
                payload={},
                created_at=now,
                updated_at=now,
            )
        )
        versions = (
            ControlledVersionRow(
                id="catalog-v1",
                kind="catalog",
                version_label="1",
                content_hash="a" * 64,
                status="APPROVED",
                payload={
                    "items": {
                        "pipe-canonical": {
                            "attributes": {
                                "diameter": "DN100",
                                "pressure": "PN16",
                            },
                            "critical_attributes": ["diameter", "pressure"],
                            "critical_price": True,
                        }
                    }
                },
                approved_by="catalog-owner",
                approved_at=now,
            ),
            ControlledVersionRow(
                id="price-policy-v1",
                kind="price_policy",
                version_label="1",
                content_hash="b" * 64,
                status="APPROVED",
                payload={
                    "selection_method": "MEDIAN",
                    "normalization_rounding_scale": 2,
                    "normalization_rounding_mode": "ROUND_HALF_UP",
                    "item_target_basis_ids": {"pipe-source": "delivered-rub"},
                    "target_bases": {"delivered-rub": basis.model_dump(mode="json")},
                    "unit_conversions": {},
                    "fx_rates": {},
                    "adjustments": {},
                    "region_adjustments": {},
                    "party_adjustments": {},
                    "payment_adjustments": {},
                },
                approved_by="methodology-owner",
                approved_at=now,
            ),
            ControlledVersionRow(
                id="approval-policy-v1",
                kind="approval_policy",
                version_label="1",
                content_hash="c" * 64,
                status="APPROVED",
                payload={
                    "rules": [
                        {
                            "reason": "HIGH_PRICE_SPREAD",
                            "assigned_role": "REVIEWER",
                            "threshold": "0.50",
                            "threshold_kind": "RELATIVE_SPREAD",
                            "required": True,
                        },
                        {
                            "reason": "CONTRACT_COST_IMPACT",
                            "assigned_role": "REVIEWER",
                            "required": True,
                        },
                    ]
                },
                approved_by="methodology-owner",
                approved_at=now,
            ),
            ControlledVersionRow(
                id="contract-risk-rules-v1",
                kind="contract_risk_rules",
                version_label="1",
                content_hash="f" * 64,
                status="APPROVED",
                payload={
                    "contract": {
                        "required_term_kinds": ["FIXED_PRICE"],
                        "independently_verified_term_kinds": [],
                        "evidence_field_names": {
                            "FIXED_PRICE": "contract_fixed_price",
                        },
                        "review_role": "REVIEWER",
                    }
                },
                approved_by="methodology-owner",
                approved_at=now,
            ),
            ControlledVersionRow(
                id="risk-model-v1",
                kind="risk_model",
                version_label="1",
                content_hash="9" * 64,
                status="APPROVED",
                payload={
                    "policy": {
                        "method": "THREE_POINT_EXPECTED_VALUE",
                        "currency": "RUB",
                        "rounding_scale": 2,
                        "rounding_mode": "ROUND_HALF_UP",
                    },
                    "minimum_risk_items": 1,
                    "independently_verified_risk_keys": [],
                    "reserve_unit": "project",
                    "reserve_cost_component": {
                        "line_id": "boq-line-risk",
                        "semantic_key": "risk-reserve",
                    },
                },
                approved_by="methodology-owner",
                approved_at=now,
            ),
        )
        for version in versions:
            catalog_owned = version.kind == "catalog"
            add_governed_controlled_version(
                session=session,
                settings=settings,
                object_store=store,
                row=version,
                organization_id="org-1",
                creator=(catalog_creator if catalog_owned else methodology_creator),
                approver=(catalog_approver if catalog_owned else methodology_approver),
            )
        for version, purpose in zip(
            versions,
            (
                "catalog",
                "price_policy",
                "approval_policy",
                "contract_risk_rules",
                "risk_model",
            ),
            strict=True,
        ):
            add_project_controlled_version_binding(
                session=session,
                settings=settings,
                object_store=store,
                project_id="project-pricing",
                version=version,
                purpose=purpose,
                actor=(catalog_approver if version.kind == "catalog" else methodology_approver),
            )
        risk_item_id = "risk-item-pricing-test"
        session.add(
            RiskItemRow(
                id=risk_item_id,
                project_id="project-pricing",
                risk_key="pricing-test-risk",
                status="VERIFIED",
                currency="RUB",
                expected_impact=Decimal("0"),
                supersedes_risk_id=None,
                is_current=True,
                payload={"test_fixture": "Pricing stage risk register"},
                created_at=now,
                updated_at=now,
            )
        )
        risk_reference = {
            "line_id": "boq-line-risk",
            "semantic_key": "risk-reserve",
        }
        session.add(
            RiskCalculationRow(
                id="risk-calculation-pricing-test",
                project_id="project-pricing",
                policy_version_id="risk-model-v1",
                status="VALIDATED",
                expected_reserve=Decimal("0"),
                currency="RUB",
                unit="project",
                supersedes_calculation_id=None,
                is_current=True,
                payload={
                    "input_signature": content_hash(
                        {
                            "risk_item_ids": [risk_item_id],
                            "risk_model_version_id": "risk-model-v1",
                            "reserve_cost_component": risk_reference,
                        }
                    ),
                    "reserve_cost_component": risk_reference,
                    "basis_type": "RISK_RESERVE",
                    "unit_rate": "0",
                    "currency": "RUB",
                    "unit": "project",
                },
                created_at=now,
            )
        )
        supported_classes = [item.value for item in PriceEvidenceClass]
        for suffix, method, domain in (
            ("parser", EvidenceMethod.TABLE_PARSER, "parser-domain"),
            ("visual", EvidenceMethod.VISUAL_MODEL, "visual-domain"),
        ):
            session.add(
                AdapterQualificationRow(
                    id=f"qualification-{suffix}",
                    adapter_name=f"adapter-{suffix}",
                    adapter_version="1",
                    status="APPROVED",
                    valid_until=date(2027, 7, 23),
                    test_evidence_hash=("d" if suffix == "parser" else "e") * 64,
                    payload={
                        "supported_methods": [method.value],
                        "supported_price_evidence_classes": supported_classes,
                        "independence_domain": domain,
                        "organization_id": "org-1",
                        "service_actor_id": "extractor",
                    },
                    approved_by="methodology-owner",
                    approved_at=now,
                )
            )
        attribute_observation = Observation(
            observation_id="observation-attributes",
            field_name="technical_attributes",
            value={"diameter": "DN100", "pressure": "PN16"},
            unit=None,
            method=EvidenceMethod.RULE_ENGINE,
            method_version="reconciliation-v1",
            source_priority=1,
            location=EvidenceLocation(
                document_id="document-1",
                document_revision_id="revision-1",
                original_object_hash="f" * 64,
                locator_kind="table",
                locator="specification[row=1]",
            ),
            observed_at=now,
            actor_id="reviewer-1",
            status=VerificationStatus.VERIFIED,
        )
        contract_observation = Observation(
            observation_id="observation-contract-fixed-price",
            field_name="contract_fixed_price",
            value="Price is fixed for the contract term",
            unit=None,
            method=EvidenceMethod.RULE_ENGINE,
            method_version="contract-rules-v1",
            source_priority=1,
            location=EvidenceLocation(
                document_id="document-1",
                document_revision_id="revision-1",
                original_object_hash="f" * 64,
                locator_kind="clause",
                locator="contract:fixed-price",
            ),
            observed_at=now,
            actor_id="contract-extractor",
            status=VerificationStatus.VERIFIED,
        )
        old_attribute_observation = attribute_observation.model_copy(
            update={
                "observation_id": "observation-attributes-old-set",
                "location": attribute_observation.location.model_copy(
                    update={"document_revision_id": "revision-old"}
                ),
            }
        )
        session.add_all(
            (
                ObservationRow(
                    id=attribute_observation.observation_id,
                    project_id="project-pricing",
                    document_revision_id="revision-1",
                    field_name=attribute_observation.field_name,
                    method=attribute_observation.method.value,
                    method_version=attribute_observation.method_version,
                    status=attribute_observation.status.value,
                    payload={"observation": attribute_observation.model_dump(mode="json")},
                    created_at=now,
                ),
                ObservationRow(
                    id=old_attribute_observation.observation_id,
                    project_id="project-pricing",
                    document_revision_id="revision-old",
                    field_name=old_attribute_observation.field_name,
                    method=old_attribute_observation.method.value,
                    method_version=old_attribute_observation.method_version,
                    status=old_attribute_observation.status.value,
                    payload={"observation": old_attribute_observation.model_dump(mode="json")},
                    created_at=now + timedelta(seconds=1),
                ),
                ObservationRow(
                    id=contract_observation.observation_id,
                    project_id="project-pricing",
                    document_revision_id="revision-1",
                    field_name=contract_observation.field_name,
                    method=contract_observation.method.value,
                    method_version=contract_observation.method_version,
                    status=contract_observation.status.value,
                    payload={"observation": contract_observation.model_dump(mode="json")},
                    created_at=now,
                ),
            )
        )

    quote_specs = (
        (
            PriceEvidenceClass.OFFICIAL_OR_PRIMARY,
            "manufacturer",
            Decimal("1000"),
        ),
        (
            PriceEvidenceClass.INDEPENDENT_MARKET,
            "market-index",
            Decimal("1020"),
        ),
        (
            PriceEvidenceClass.COMMERCIAL_QUOTE,
            "supplier-rfq",
            Decimal("1010"),
        ),
    )
    drafts: list[PriceQuoteDraft] = []
    with factory.begin() as session:
        for index, (evidence_class, origin, amount) in enumerate(quote_specs, start=1):
            draft = PriceQuoteDraft(
                item_id="pipe-source",
                supplier_id=f"supplier-{index}",
                evidence_class=evidence_class,
                source_observation_id=f"observation-quote-{index}",
                technical_attributes={"diameter": "DN100", "pressure": "PN16"},
                amount=amount,
                basis=basis,
                quote_date=date(2026, 7, 20),
                valid_until=date(2026, 8, 20),
                lead_time_days=10,
                available=True,
                source_reliability=Decimal("0.90"),
            )
            drafts.append(draft)
            leaf_ids: list[str] = []
            for suffix, method in (
                ("parser", EvidenceMethod.TABLE_PARSER),
                ("visual", EvidenceMethod.VISUAL_MODEL),
            ):
                leaf_id = f"observation-quote-{index}-{suffix}"
                leaf_ids.append(leaf_id)
                leaf = Observation(
                    observation_id=leaf_id,
                    field_name=f"price_quote:{index}",
                    value=draft.evidence_value(),
                    unit=None,
                    method=method,
                    method_version="1",
                    source_priority=1,
                    location=EvidenceLocation(
                        document_id=f"document-{index}",
                        document_revision_id=f"revision-{index}",
                        original_object_hash=str(index) * 64,
                        locator_kind="table",
                        locator=f"quote[row={index}]",
                    ),
                    observed_at=now,
                    actor_id="extractor",
                )
                session.add(
                    ObservationRow(
                        id=leaf.observation_id,
                        project_id="project-pricing",
                        document_revision_id=f"revision-{index}",
                        field_name=leaf.field_name,
                        method=leaf.method.value,
                        method_version=leaf.method_version,
                        status=leaf.status.value,
                        payload={
                            "observation": leaf.model_dump(mode="json"),
                            "adapter_qualification_id": f"qualification-{suffix}",
                            "source_origin_id": origin,
                        },
                        created_at=now,
                    )
                )
            reconciled = Observation(
                observation_id=draft.source_observation_id,
                field_name=f"price_quote:{index}",
                value=draft.evidence_value(),
                unit=None,
                method=EvidenceMethod.RULE_ENGINE,
                method_version="reconciliation-v1",
                source_priority=1,
                location=EvidenceLocation(
                    document_id=f"document-{index}",
                    document_revision_id=f"revision-{index}",
                    original_object_hash=str(index) * 64,
                    locator_kind="table",
                    locator=f"quote[row={index}]",
                ),
                observed_at=now,
                actor_id="reviewer-1",
                status=VerificationStatus.VERIFIED,
            )
            session.add(
                ObservationRow(
                    id=reconciled.observation_id,
                    project_id="project-pricing",
                    document_revision_id=f"revision-{index}",
                    field_name=reconciled.field_name,
                    method=reconciled.method.value,
                    method_version=reconciled.method_version,
                    status=reconciled.status.value,
                    payload={
                        "observation": reconciled.model_dump(mode="json"),
                        "source_observation_ids": leaf_ids,
                    },
                    created_at=now,
                )
            )

    with factory.begin() as session:
        service = PricingService(session=session, settings=settings, object_store=store)
        with pytest.raises(ValueError, match="limit must be between 1 and 100"):
            service.nomenclature_context(
                actor=procurement,
                project_id="project-pricing",
                catalog_query="pipe",
                evidence_field_name="technical_attributes",
                limit=0,
            )
        with pytest.raises(ValueError, match="field name must contain"):
            service.nomenclature_context(
                actor=procurement,
                project_id="project-pricing",
                catalog_query="pipe",
                evidence_field_name="   ",
                limit=100,
            )
        with pytest.raises(ValueError, match="query exceeds 200"):
            service.nomenclature_context(
                actor=procurement,
                project_id="project-pricing",
                catalog_query="x" * 201,
                evidence_field_name="technical_attributes",
                limit=100,
            )
        project = session.get(ProjectRow, "project-pricing")
        assert project is not None
        project.state = ApprovalState.DRAFT.value
        with pytest.raises(
            ValueError,
            match="require PRICING_IN_PROGRESS or RFQ_REQUIRED",
        ):
            service.nomenclature_context(
                actor=procurement,
                project_id="project-pricing",
                catalog_query="pipe",
                evidence_field_name="technical_attributes",
                limit=100,
            )
        project.state = ApprovalState.PRICING_IN_PROGRESS.value
        context = service.nomenclature_context(
            actor=procurement,
            project_id="project-pricing",
            catalog_query="pipe",
            evidence_field_name="technical_attributes",
            limit=1,
        )
        assert {
            candidate.observation.observation_id for candidate in context.evidence_candidates
        } == {"observation-attributes"}
        assert not context.evidence_candidates_truncated
        with pytest.raises(LookupError, match="missing-match"):
            service.nomenclature_review_context(
                actor=procurement,
                project_id="project-pricing",
                match_id="missing-match",
            )
        with pytest.raises(ValueError, match="reason must contain"):
            service.assess_nomenclature(
                actor=procurement,
                project_id="project-pricing",
                draft=NomenclatureAssessmentDraft(
                    source_item_id="pipe-source",
                    canonical_item_id="pipe-canonical",
                    source_attributes_observation_id="observation-attributes",
                ),
                request_id="request-nomenclature-blank-reason",
                reason=" ",
            )
        with pytest.raises(ValueError, match="confirmed current document set"):
            service.assess_nomenclature(
                actor=procurement,
                project_id="project-pricing",
                draft=NomenclatureAssessmentDraft(
                    source_item_id="pipe-source",
                    canonical_item_id="pipe-canonical",
                    source_attributes_observation_id="observation-attributes-old-set",
                ),
                request_id="request-nomenclature-old-evidence",
                reason="Prove that old-set technical evidence is rejected",
            )
        match = service.assess_nomenclature(
            actor=procurement,
            project_id="project-pricing",
            draft=NomenclatureAssessmentDraft(
                source_item_id="pipe-source",
                canonical_item_id="pipe-canonical",
                source_attributes_observation_id="observation-attributes",
            ),
            request_id="request-match",
            reason="Assess critical catalog attributes",
        )
        assert match.status is VerificationStatus.VERIFIED
        empty_context = service.price_item_context(
            actor=procurement,
            project_id="project-pricing",
            item_id="pipe-source",
        )
        assert empty_context.price_policy_version_id == "price-policy-v1"
        assert empty_context.normalization_rounding_scale == 2
        assert empty_context.normalization_rounding_mode == "ROUND_HALF_UP"
        assert empty_context.target_basis == basis
        assert empty_context.quotes == ()
        first_candidate = service.price_quote_candidate(
            actor=procurement,
            project_id="project-pricing",
            item_id="pipe-source",
            source_observation_id=drafts[0].source_observation_id,
        )
        assert first_candidate.draft == drafts[0]
        assert first_candidate.source_origin_id == "manufacturer"
        assert first_candidate.required_reference_types == ()
        assert first_candidate.required_adjustment_kinds == ()

        normalized_ids: list[str] = []
        for draft in drafts[:2]:
            quote = service.record_quote_from_observation(
                actor=procurement,
                project_id="project-pricing",
                item_id=draft.item_id,
                source_observation_id=draft.source_observation_id,
                request_id=f"request-{draft.evidence_class.value}",
                reason="Record independently extracted price source",
            )
            normalized = service.normalize_price(
                actor=procurement,
                project_id="project-pricing",
                command=NormalizePriceCommand(quote_id=quote.quote.quote_id),
                request_id=f"request-normalize-{quote.quote.quote_id}",
                reason="Normalize to the approved commercial basis",
            )
            normalized_ids.append(normalized.normalized_price_id)
        populated_context = service.price_item_context(
            actor=procurement,
            project_id="project-pricing",
            item_id="pipe-source",
        )
        assert len(populated_context.quotes) == 2
        assert all(len(item.normalized_prices) == 1 for item in populated_context.quotes)
        tampered_normalized = session.get(NormalizedPriceRow, normalized_ids[0])
        assert tampered_normalized is not None
        original_amount = tampered_normalized.amount_per_unit
        tampered_normalized.amount_per_unit = original_amount + Decimal("1")
        with pytest.raises(ValueError, match="integrity validation"):
            service.evaluate_item_price(
                actor=procurement,
                project_id="project-pricing",
                item_id="pipe-source",
                as_of=date(2026, 7, 23),
                request_id="request-tampered-evaluation",
                reason="Tampered normalization must fail closed",
            )
        tampered_normalized.amount_per_unit = original_amount
        first_decision = service.evaluate_item_price(
            actor=procurement,
            project_id="project-pricing",
            item_id="pipe-source",
            as_of=date(2026, 7, 23),
            request_id="request-first-evaluation",
            reason="Evaluate critical price evidence",
        )
        assert first_decision.status is PriceStatus.RFQ_REQUIRED
        assert first_decision.rfq_request_id
        assert first_decision.project_state is ApprovalState.RFQ_REQUIRED

        third_quote = service.record_quote(
            actor=procurement,
            project_id="project-pricing",
            draft=drafts[2],
            request_id="request-commercial-quote",
            reason="Record RFQ response",
        )
        service.normalize_price(
            actor=procurement,
            project_id="project-pricing",
            command=NormalizePriceCommand(quote_id=third_quote.quote.quote_id),
            request_id="request-normalize-commercial",
            reason="Normalize RFQ response",
        )
        final_decision = service.evaluate_item_price(
            actor=procurement,
            project_id="project-pricing",
            item_id="pipe-source",
            as_of=date(2026, 7, 23),
            request_id="request-final-evaluation",
            reason="Re-evaluate complete triangulation",
        )
        assert final_decision.status is PriceStatus.VERIFIED
        assert final_decision.amount_per_unit == Decimal("101")
        assert final_decision.derived_observation_id
        assert final_decision.project_state is ApprovalState.PRICING_IN_PROGRESS
        final_context = service.price_item_context(
            actor=procurement,
            project_id="project-pricing",
            item_id="pipe-source",
        )
        assert final_context.current_decision is not None
        assert final_context.current_decision.status is PriceStatus.VERIFIED
        assert final_context.current_decision.amount_per_unit == Decimal("101")
        stored_decision = session.get(PriceDecisionRow, final_decision.decision_id)
        assert stored_decision is not None
        stored_amount = stored_decision.amount_per_unit
        stored_decision.amount_per_unit = Decimal("102")
        with pytest.raises(ValueError, match="amount integrity"):
            service.price_item_context(
                actor=procurement,
                project_id="project-pricing",
                item_id="pipe-source",
            )
        stored_decision.amount_per_unit = stored_amount
        rfq = session.get(RfqRequestRow, first_decision.rfq_request_id)
        assert rfq is not None and rfq.status == "CLOSED"

        project = session.get(ProjectRow, "project-pricing")
        assert project is not None
        contract = ContractService(
            session=session,
            settings=settings,
            object_store=store,
        )
        contract_context = contract.context(
            actor=procurement,
            project_id=project.id,
            selected_kind=ContractTermKind.FIXED_PRICE,
            limit=100,
        )
        contract_term = contract.submit_term(
            actor=procurement,
            project_id=project.id,
            draft=ContractTermDraft(
                kind=ContractTermKind.FIXED_PRICE,
                value="Price is fixed for the contract term",
                observation_ids=(contract_observation.observation_id,),
            ),
            expected_document_set_revision_id=(contract_context.document_set_revision_id),
            rules_version_id=contract_context.rules_version_id,
            request_id="request-contract-term",
            reason="Submit the fixed-price clause",
        )
        contract_review = contract.decide_term(
            actor=contract_reviewer,
            project_id=project.id,
            term_id=contract_term.term_id,
            command=ContractTermDecisionCommand(
                decision=ApprovalDecision.APPROVED,
                expected_term_updated_at=contract_term.updated_at,
                expected_task_updated_at=approval_task_updated_at(
                    session,
                    contract_term.approval_task_id,
                ),
            ),
            request_id="request-contract-review",
            reason="Verify the fixed-price clause against its source",
        )
        contract_impact = contract.propose_cost_impact(
            actor=procurement,
            project_id=project.id,
            term_id=contract_review.term.term_id,
            command=ContractCostImpactCommand(
                amount=0,
                no_cost_reason=("Fixed-price exposure is represented by the approved risk model"),
            ),
            expected_term_updated_at=contract_review.term.updated_at,
            request_id="request-contract-impact",
            reason="Record the controlled cost treatment",
        )
        ApprovalService(
            session=session,
            settings=settings,
            object_store=store,
        ).decide(
            actor=contract_reviewer,
            project_id=project.id,
            task_id=contract_impact.approval_task_ids[0],
            command=ApprovalDecisionCommand(
                decision=ApprovalDecision.APPROVED,
                reason="The contract treatment is explicit and risk-linked",
                expected_task_updated_at=approval_task_updated_at(
                    session,
                    contract_impact.approval_task_ids[0],
                ),
                evidence_ids=(contract_observation.observation_id,),
            ),
            request_id="request-contract-impact-review",
        )
        contract.finalize_cost_impact(
            actor=procurement,
            project_id=project.id,
            term_id=contract_impact.term_id,
            expected_term_updated_at=contract_impact.updated_at,
            request_id="request-contract-impact-finalize",
            reason="Finalize independently approved contract treatment",
        )
        transitioned = ProjectService(
            session=session,
            settings=settings,
            object_store=store,
        ).transition(
            actor=procurement,
            project_id=project.id,
            to_state=ApprovalState.CALCULATION_IN_PROGRESS,
            expected_row_version=project.row_version,
            request_id="request-calculation-transition",
            reason="All current item prices are verified",
        )
        assert transitioned.state is ApprovalState.CALCULATION_IN_PROGRESS

    app = create_app(
        settings,
        engine=engine,
        object_store=store,
        quarantine_store=LocalObjectStore(tmp_path / "quarantine"),
    )
    with TestClient(app) as client:
        response = client.get(
            "/v1/projects/project-pricing/pricing/items/pipe-source/context",
            headers={
                "X-Dev-Actor": procurement.actor_id,
                "X-Dev-Organization": procurement.organization_id,
                "X-Dev-Roles": "PROCUREMENT,ESTIMATOR",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["current_decision"]["status"] == "VERIFIED"
        blocked_candidate = client.get(
            (
                "/v1/projects/project-pricing/pricing/items/pipe-source/"
                "quote-candidates/observation-quote-1"
            ),
            headers={
                "X-Dev-Actor": procurement.actor_id,
                "X-Dev-Organization": procurement.organization_id,
                "X-Dev-Roles": "PROCUREMENT,ESTIMATOR",
            },
        )
        assert blocked_candidate.status_code == 422
        assert "PRICING_IN_PROGRESS or RFQ_REQUIRED" in blocked_candidate.text

    engine.dispose()
