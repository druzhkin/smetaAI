from __future__ import annotations

from datetime import date, datetime
from io import BytesIO
from pathlib import Path

import pytest
from sqlalchemy import select

from tenderguard.application.actuals import ActualRecordDraft, ActualsService
from tenderguard.application.business_qualification import (
    BusinessQualificationService,
    DiscrepancyReviewCommand,
)
from tenderguard.application.governance import GovernanceService
from tenderguard.application.projects import ProjectService
from tenderguard.config import Settings
from tenderguard.domain.business_qualification import (
    QualificationReferenceEvidenceDraft,
)
from tenderguard.domain.calculation import (
    AtomicCostInput,
    CalculationPolicy,
    calculate_primary,
    create_snapshot,
    validate_independently,
)
from tenderguard.domain.common import canonical_json, content_hash, utc_now
from tenderguard.domain.enums import (
    ActorRole,
    ApprovalState,
    CostCategory,
    EvidenceMethod,
)
from tenderguard.domain.models import EvidenceLocation
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.database import (
    create_database_engine,
    create_schema_for_tests,
    create_session_factory,
)
from tenderguard.infrastructure.object_store import LocalObjectStore
from tenderguard.infrastructure.orm import (
    BusinessQualificationCaseRow,
    BusinessQualificationDiscrepancyRow,
    CalculationRunRow,
    CalculationSnapshotRow,
    DocumentRevisionRow,
    DocumentRow,
    ObservationRow,
    ProjectRow,
)
from tests.integration.support import project_memberships

AUDIT_KEY = "business-qualification-audit-key-at-least-32-bytes"
BUILD_REFERENCE = "git:" + ("7" * 40)
ORGANIZATION_ID = "org-business-qualification"
COMPARISON_BASIS_HASH = "6" * 64


def _actor(actor_id: str, role: ActorRole) -> Actor:
    return Actor(actor_id, ORGANIZATION_ID, frozenset({role}))


def _add_project_and_snapshot(
    *,
    session: object,
    store: LocalObjectStore,
    project_id: str,
    snapshot_creator: str,
    calculation_version: object,
    actors: tuple[Actor, ...],
    now: datetime,
) -> tuple[str, str, str]:
    session.add(  # type: ignore[attr-defined]
        ProjectRow(
            id=project_id,
            organization_id=ORGANIZATION_ID,
            code=project_id.upper(),
            name=f"Qualification project {project_id}",
            state=ApprovalState.APPROVED_FOR_BID.value,
            current_document_set_revision_id=f"document-set-{project_id}",
            row_version=1,
            created_at=now,
            updated_at=now,
        )
    )
    session.add_all(  # type: ignore[attr-defined]
        project_memberships(
            project_id,
            actors,
            owner_id=actors[0].actor_id,
            now=now,
        )
    )
    document_id = f"document-{project_id}"
    revision_id = f"revision-{project_id}"
    document_hash = content_hash({"project_id": project_id, "source": "qualification"})
    session.add(  # type: ignore[attr-defined]
        DocumentRow(
            id=document_id,
            project_id=project_id,
            logical_key="qualification-source",
            title="Qualification source evidence",
            document_type="QUALIFICATION_EVIDENCE",
            critical=True,
            cancelled=False,
            created_at=now,
            updated_at=now,
        )
    )
    session.add(  # type: ignore[attr-defined]
        DocumentRevisionRow(
            id=revision_id,
            document_id=document_id,
            revision_label="1",
            issue_date=now.date(),
            object_hash=document_hash,
            object_key=f"objects/{document_hash}",
            original_filename="qualification-evidence.pdf",
            media_type="application/pdf",
            size_bytes=1,
            supersedes_revision_id=None,
            is_current=True,
            corrupt=False,
            protected=False,
            inspection_payload={"qualification_fixture": True},
            created_at=now,
            updated_at=now,
        )
    )
    atomic_input = AtomicCostInput(
        cost_input_id=f"input-{project_id}",
        line_id=f"line-{project_id}",
        wbs_node_id=f"wbs-{project_id}",
        semantic_key=f"qualification-total-{project_id}",
        category=CostCategory.LOGISTICS,
        quantity="1",
        unit="project",
        unit_rate="100",
        currency="RUB",
        approved_assumption_id=f"approved-assumption-{project_id}",
    )
    policy = CalculationPolicy(
        policy_version=calculation_version.version_id,  # type: ignore[attr-defined]
        currency="RUB",
        line_rounding_scale=2,
        total_rounding_scale=2,
        rounding_mode="ROUND_HALF_UP",
        independent_tolerance="0",
        expected_semantic_keys=frozenset({atomic_input.semantic_key}),
    )
    primary = calculate_primary(
        (atomic_input,),
        policy,
        engine_version=calculation_version.version_id,  # type: ignore[attr-defined]
        calculated_at=now,
    )
    independent = validate_independently(
        (atomic_input,),
        primary,
        policy,
        validator_version=f"independent:{calculation_version.version_id}",  # type: ignore[attr-defined]
        validated_at=now,
    )
    snapshot = create_snapshot(
        project_id=project_id,
        document_set_revision_id=f"document-set-{project_id}",
        inputs=(atomic_input,),
        policy=policy,
        controlled_versions=(calculation_version,),  # type: ignore[arg-type]
        primary=primary,
        independent=independent,
        created_by=snapshot_creator,
        created_at=now,
    )
    stored = store.put(
        BytesIO(
            canonical_json(
                {
                    "snapshot": snapshot,
                    "inputs": (atomic_input,),
                    "policy": policy,
                    "controlled_versions": (calculation_version,),
                    "primary": primary,
                    "independent": independent,
                }
            )
        )
    )
    run_id = f"run-{project_id}"
    session.add(  # type: ignore[attr-defined]
        CalculationRunRow(
            id=run_id,
            project_id=project_id,
            engine_version=calculation_version.version_id,  # type: ignore[attr-defined]
            status="VALIDATED",
            currency=primary.currency,
            grand_total=primary.grand_total,
            payload={
                "primary": primary.model_dump(mode="json"),
                "independent_validation": independent.model_dump(mode="json"),
                "policy": policy.model_dump(mode="json"),
            },
            created_at=now,
        )
    )
    session.add(  # type: ignore[attr-defined]
        CalculationSnapshotRow(
            id=snapshot.snapshot_id,
            project_id=project_id,
            calculation_run_id=run_id,
            document_set_revision_id=snapshot.document_set_revision_id,
            input_hash=snapshot.input_hash,
            output_hash=snapshot.output_hash,
            snapshot_hash=snapshot.snapshot_hash,
            fixed=True,
            object_key=stored.object_key,
            created_by=snapshot_creator,
            created_at=now,
        )
    )
    return snapshot.snapshot_id, document_id, revision_id


def test_business_qualification_requires_locked_blind_references_and_four_eyes(
    tmp_path: Path,
) -> None:
    settings = Settings(
        app_env="test",
        application_build_reference=BUILD_REFERENCE,
        database_url="sqlite+pysqlite://",
        local_object_store_path=tmp_path / "objects",
        local_quarantine_store_path=tmp_path / "quarantine",
        audit_signing_key=AUDIT_KEY,
        audit_signing_key_id="business-qualification-key-1",
    )
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    sessions = create_session_factory(engine)
    store = LocalObjectStore(tmp_path / "objects")

    governance_creator = _actor("governance-creator", ActorRole.METHODOLOGY_OWNER)
    final_approver = _actor("final-approver", ActorRole.METHODOLOGY_OWNER)
    campaign_creator = _actor("campaign-creator", ActorRole.AUDITOR)
    evaluator = _actor("campaign-evaluator", ActorRole.AUDITOR)
    discrepancy_reviewer = _actor(
        "discrepancy-reviewer",
        ActorRole.METHODOLOGY_OWNER,
    )
    actual_recorder = _actor("actual-recorder", ActorRole.PROCUREMENT)
    actual_reviewer = _actor("actual-reviewer", ActorRole.AUDITOR)
    blind_preparer = _actor("blind-professional", ActorRole.TECHNICAL_EXPERT)
    parallel_preparer = _actor("parallel-professional", ActorRole.TECHNICAL_EXPERT)
    blind_self_reviewer = _actor("blind-professional", ActorRole.REVIEWER)
    parallel_self_reviewer = _actor("parallel-professional", ActorRole.REVIEWER)
    reference_reviewer = _actor("reference-reviewer", ActorRole.REVIEWER)
    all_actors = (
        campaign_creator,
        evaluator,
        discrepancy_reviewer,
        final_approver,
        actual_recorder,
        actual_reviewer,
        blind_preparer,
        blind_self_reviewer,
        parallel_preparer,
        parallel_self_reviewer,
        reference_reviewer,
    )
    project_ids = ("project-historical", "project-blind", "project-parallel")
    snapshot_creators = {project_id: f"system-estimator-{project_id}" for project_id in project_ids}
    locations: dict[str, EvidenceLocation] = {}
    snapshots: dict[str, str] = {}

    with sessions.begin() as session:
        governance = GovernanceService(
            session=session,
            settings=settings,
            object_store=store,
        )
        calculation_version = governance.create_version(
            actor=governance_creator,
            kind="calculation_model",
            version_label="qualification-calculation-v1",
            payload={"purpose": "business qualification fixture"},
            request_id="request-create-calculation-version",
            reason="Create deterministic calculation basis",
        )
        calculation_version = governance.approve_version(
            actor=final_approver,
            version_id=calculation_version.version_id,
            request_id="request-approve-calculation-version",
            reason="Independently approve deterministic calculation basis",
        )
        now = utc_now()
        for project_id in project_ids:
            snapshot_id, document_id, revision_id = _add_project_and_snapshot(
                session=session,
                store=store,
                project_id=project_id,
                snapshot_creator=snapshot_creators[project_id],
                calculation_version=calculation_version,
                actors=all_actors,
                now=now,
            )
            snapshots[project_id] = snapshot_id
            session.flush()
            revision = session.get(DocumentRevisionRow, revision_id)
            assert revision is not None
            locations[project_id] = EvidenceLocation(
                document_id=document_id,
                document_revision_id=revision_id,
                original_object_hash=revision.object_hash,
                locator_kind="PAGE",
                locator="page:1",
                page=1,
            )
        session.flush()
        session.add(
            ObservationRow(
                id="observation-historical-total",
                project_id="project-historical",
                document_revision_id=locations["project-historical"].document_revision_id,
                field_name="actual_project_total",
                method=EvidenceMethod.RULE_ENGINE.value,
                method_version="verified-financial-ledger-v1",
                status="VERIFIED",
                payload={
                    "comparison_basis_hash": COMPARISON_BASIS_HASH,
                    "observation": {
                        "value": "105",
                        "unit": "RUB",
                        "location": locations["project-historical"].model_dump(mode="json"),
                    },
                },
                created_at=now,
            )
        )

    with sessions.begin() as session:
        actuals = ActualsService(session=session, settings=settings, object_store=store)
        recorded = actuals.record_actual(
            actor=actual_recorder,
            project_id="project-historical",
            draft=ActualRecordDraft(
                actual_key="verified-project-total",
                entity_type="PROJECT",
                entity_id="project-historical",
                metric="PROJECT_TOTAL_COST",
                value="105",
                unit="RUB",
                source_observation_id="observation-historical-total",
                occurred_on=date(2026, 7, 1),
            ),
            request_id="request-record-project-actual",
            reason="Record reconciled final project cost",
        )
        actual_id = recorded.actual.actual_id
        actuals.verify_actual(
            actor=actual_reviewer,
            project_id="project-historical",
            actual_id=actual_id,
            request_id="request-verify-project-actual",
            reason="Independently verify final project cost against ledger evidence",
        )

    with sessions.begin() as session:
        governance = GovernanceService(
            session=session,
            settings=settings,
            object_store=store,
        )
        threshold = {
            "minimum_cases": 1,
            "maximum_case_absolute_percentage_error": "20",
            "maximum_mean_absolute_percentage_error": "20",
            "maximum_absolute_bias_percentage": "20",
            "material_discrepancy_percentage": "1",
        }
        profile = governance.create_version(
            actor=governance_creator,
            kind="business_qualification_profile",
            version_label="business-qualification-profile-2026-q3",
            payload={
                "schema_version": "tenderguard.business-qualification-profile/v1",
                "expected_application_build_reference": BUILD_REFERENCE,
                "currency": "RUB",
                "comparison_metric": "PROJECT_TOTAL_COST",
                "comparison_basis_hash": COMPARISON_BASIS_HASH,
                "mode_thresholds": {
                    "HISTORICAL": threshold,
                    "BLIND": threshold,
                    "PARALLEL": threshold,
                },
                "maximum_exclusion_ratio": "0",
                "minimum_blind_independence_domains": 1,
                "minimum_parallel_span_days": 1,
                "display_scale": 4,
                "rounding_mode": "ROUND_HALF_UP",
                "allowed_discrepancy_reason_codes": ["FINAL_SCOPE_VARIANCE"],
            },
            request_id="request-create-qualification-profile",
            reason="Create owner-defined business qualification limits",
        )
        profile = governance.approve_version(
            actor=final_approver,
            version_id=profile.version_id,
            request_id="request-approve-qualification-profile",
            reason="Independently approve business qualification limits",
        )
        cutoff = utc_now()
        dataset = governance.create_version(
            actor=governance_creator,
            kind="business_qualification_dataset",
            version_label="business-qualification-dataset-2026-q3",
            payload={
                "schema_version": "tenderguard.business-qualification-dataset/v1",
                "population_definition": "Closed three-project qualification population",
                "population_evidence_hash": "1" * 64,
                "selection_method": "All eligible projects; no post-selection exclusions",
                "selection_query_hash": "2" * 64,
                "selection_cutoff_at": cutoff.isoformat(),
                "population_size": 3,
                "cases": [
                    {
                        "case_key": "historical-1",
                        "mode": "HISTORICAL",
                        "project_id": "project-historical",
                        "snapshot_id": snapshots["project-historical"],
                        "historical_actual_id": actual_id,
                        "stratum": "historical-water",
                    },
                    {
                        "case_key": "blind-1",
                        "mode": "BLIND",
                        "project_id": "project-blind",
                        "snapshot_id": snapshots["project-blind"],
                        "stratum": "blind-water",
                    },
                    {
                        "case_key": "parallel-1",
                        "mode": "PARALLEL",
                        "project_id": "project-parallel",
                        "snapshot_id": snapshots["project-parallel"],
                        "stratum": "parallel-water",
                    },
                ],
                "exclusions": [],
            },
            request_id="request-create-qualification-dataset",
            reason="Lock complete deterministic qualification population",
        )
        dataset = governance.approve_version(
            actor=final_approver,
            version_id=dataset.version_id,
            request_id="request-approve-qualification-dataset",
            reason="Independently approve qualification population without exclusions",
        )
        profile_id = profile.version_id
        profile_hash = profile.content_hash
        dataset_id = dataset.version_id
        dataset_hash = dataset.content_hash

    with sessions.begin() as session:
        service = BusinessQualificationService(
            session=session,
            settings=settings,
            object_store=store,
        )
        campaign = service.create_campaign(
            actor=campaign_creator,
            profile_version_id=profile_id,
            profile_content_hash=profile_hash,
            dataset_version_id=dataset_id,
            dataset_content_hash=dataset_hash,
            request_id="request-lock-qualification-campaign",
            reason="Lock predictions before professional reference intake",
        )
        assert campaign.status == "INPUTS_LOCKED"
        assert campaign.reference_count == 1
        campaign_id = campaign.campaign_id
        cases = {
            row.mode: row.id
            for row in session.scalars(
                select(BusinessQualificationCaseRow).where(
                    BusinessQualificationCaseRow.campaign_id == campaign_id
                )
            )
        }

        with pytest.raises(ValueError, match="different actor"):
            service.evaluate(
                actor=campaign_creator,
                campaign_id=campaign_id,
                request_id="request-invalid-self-evaluation",
                reason="Self-evaluation must be rejected",
            )
        with pytest.raises(ValueError, match="Every qualification case"):
            service.evaluate(
                actor=evaluator,
                campaign_id=campaign_id,
                request_id="request-premature-evaluation",
                reason="Evaluation without references must fail closed",
            )

    prepared_ids: dict[str, str] = {}
    for mode, project_id, preparer, self_reviewer in (
        ("BLIND", "project-blind", blind_preparer, blind_self_reviewer),
        (
            "PARALLEL",
            "project-parallel",
            parallel_preparer,
            parallel_self_reviewer,
        ),
    ):
        with sessions.begin() as session:
            service = BusinessQualificationService(
                session=session,
                settings=settings,
                object_store=store,
            )
            evidence = service.prepare_reference_evidence(
                actor=preparer,
                campaign_id=campaign_id,
                case_id=cases[mode],
                draft=QualificationReferenceEvidenceDraft(
                    case_key=f"{mode.lower()}-1",
                    mode=mode,
                    amount="100",
                    currency="RUB",
                    comparison_basis_hash=COMPARISON_BASIS_HASH,
                    reference_kind=(
                        "PROFESSIONAL_ESTIMATE" if mode == "BLIND" else "PARALLEL_ESTIMATE"
                    ),
                    professional_estimator_id=preparer.actor_id,
                    independence_domain=f"independent-office-{mode.lower()}",
                    performed_at=utc_now(),
                    location=locations[project_id],
                    blinded_to_system_result=mode == "BLIND",
                    no_bid_authority=True,
                ),
                request_id=f"request-prepare-{mode.lower()}-reference",
                reason="Record independent professional reference after forecast lock",
            )
            prepared_ids[mode] = evidence.observation_id
            with pytest.raises(ValueError, match="independent actor"):
                service.verify_and_register_reference(
                    actor=self_reviewer,
                    campaign_id=campaign_id,
                    case_id=cases[mode],
                    prepared_observation_id=evidence.observation_id,
                    request_id=f"request-invalid-self-review-{mode.lower()}",
                    reason="Self-review must be rejected",
                )

        with sessions.begin() as session:
            service = BusinessQualificationService(
                session=session,
                settings=settings,
                object_store=store,
            )
            updated = service.verify_and_register_reference(
                actor=reference_reviewer,
                campaign_id=campaign_id,
                case_id=cases[mode],
                prepared_observation_id=prepared_ids[mode],
                request_id=f"request-register-{mode.lower()}-reference",
                reason="Independently verify and register professional reference",
            )
            assert updated.reference_count in {2, 3}

    with sessions.begin() as session:
        service = BusinessQualificationService(
            session=session,
            settings=settings,
            object_store=store,
        )
        evaluation = service.evaluate(
            actor=evaluator,
            campaign_id=campaign_id,
            request_id="request-evaluate-business-qualification",
            reason="Evaluate exact unrounded qualification metrics",
        )
        assert evaluation.metrics_passed
        assert sum(metric.material for metric in evaluation.cases) == 1
        campaign = service.get_campaign(actor=evaluator, campaign_id=campaign_id)
        assert campaign.status == "EXPERT_REVIEW"
        discrepancy = session.scalar(
            select(BusinessQualificationDiscrepancyRow).where(
                BusinessQualificationDiscrepancyRow.campaign_id == campaign_id
            )
        )
        assert discrepancy is not None
        discrepancy_id = discrepancy.id

    with sessions.begin() as session:
        service = BusinessQualificationService(
            session=session,
            settings=settings,
            object_store=store,
        )
        reviewed = service.review_discrepancy(
            actor=discrepancy_reviewer,
            campaign_id=campaign_id,
            discrepancy_id=discrepancy_id,
            command=DiscrepancyReviewCommand(
                decision="ACCEPTED",
                reason_code="FINAL_SCOPE_VARIANCE",
                root_cause="Verified final scope exceeded the bid-stage scope.",
                corrective_action="Calibrate scope completeness rules from accepted final facts.",
                evidence_observation_ids=("observation-historical-total",),
            ),
            request_id="request-review-material-discrepancy",
            reason="Classify the material variance using verified final evidence",
        )
        assert reviewed.reviewed_discrepancy_count == 1

    with sessions.begin() as session:
        service = BusinessQualificationService(
            session=session,
            settings=settings,
            object_store=store,
        )
        approved = service.approve_campaign(
            actor=final_approver,
            campaign_id=campaign_id,
            request_id="request-approve-business-qualification",
            reason="Approve passing campaign after independent discrepancy review",
        )
        assert approved.status == "PASSED"
        assert approved.result_hash == evaluation.result_hash
        assert approved.approval_package_hash is not None
        assert approved.finalized_at is not None
        campaign_reference = f"business_qualification_campaign:{campaign_id}"
        gate = {
            "status": "PASSED",
            "evidence_hash": "9" * 64,
            "owner_id": "process-owner",
            "approved_by": "independent-gate-approver",
            "approved_at": approved.finalized_at.isoformat(),
            "environment": "qualification",
        }
        production_payload = {
            "all_gates_complete": True,
            "business_qualification": {
                "campaign_id": campaign_id,
                "package_hash": approved.approval_package_hash,
                "approved_by": final_approver.actor_id,
                "approved_at": approved.finalized_at.isoformat(),
                "environment": "qualification",
            },
            "gates": {
                name: dict(gate)
                for name in (
                    "historical_projects",
                    "blind_estimator_comparison",
                    "parallel_operation",
                    "variance_resolution",
                    "rules_and_catalog_calibration",
                    "damaged_conflicting_document_resilience",
                    "load_test",
                    "security_review",
                    "backup_restore",
                    "methodology_approval",
                )
            },
        }
        for gate_name in (
            "historical_projects",
            "blind_estimator_comparison",
            "parallel_operation",
            "variance_resolution",
        ):
            production_payload["gates"][gate_name] = {
                **production_payload["gates"][gate_name],
                "evidence_hash": approved.approval_package_hash,
                "source_reference": campaign_reference,
            }
        project_service = ProjectService(
            session=session,
            settings=settings,
            object_store=store,
        )
        assert project_service._production_qualification_evidence_valid(
            production_payload,
            organization_id=ORGANIZATION_ID,
        )
        production_payload["business_qualification"]["package_hash"] = "0" * 64
        assert not project_service._production_qualification_evidence_valid(
            production_payload,
            organization_id=ORGANIZATION_ID,
        )
        with pytest.raises(ValueError, match="lacks complete governed evidence"):
            GovernanceService(
                session=session,
                settings=settings,
                object_store=store,
            ).create_version(
                actor=governance_creator,
                kind="production_qualification",
                version_label="invalid-production-qualification",
                payload={"all_gates_complete": True},
                request_id="request-invalid-production-qualification",
                reason="Incomplete production evidence must be rejected",
            )
        production_payload["business_qualification"]["package_hash"] = (
            approved.approval_package_hash
        )
        governance = GovernanceService(
            session=session,
            settings=settings,
            object_store=store,
        )
        production_version = governance.create_version(
            actor=governance_creator,
            kind="production_qualification",
            version_label="production-qualification-2026-q3",
            payload=production_payload,
            request_id="request-create-production-qualification",
            reason="Bind all gates to immutable business qualification evidence",
        )
        production_version = governance.approve_version(
            actor=final_approver,
            version_id=production_version.version_id,
            request_id="request-approve-production-qualification",
            reason="Formally approve verified complete production evidence",
        )
        assert production_version.status.value == "APPROVED"

    engine.dispose()
