from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path, PurePosixPath

import pytest

from tenderguard.application.governance import GovernanceService
from tenderguard.application.operational_qualification import load_approved_profile
from tenderguard.application.projects import ProjectService
from tenderguard.application.recovery_verification import RecoveryVerificationService
from tenderguard.config import Settings
from tenderguard.domain.calculation import (
    AtomicCostInput,
    CalculationPolicy,
    calculate_primary,
    create_snapshot,
    validate_independently,
)
from tenderguard.domain.common import canonical_json, content_hash, utc_now
from tenderguard.domain.enums import ActorRole, CostCategory
from tenderguard.domain.operational_qualification import (
    RecoveryExerciseManifest,
    RecoveryProfile,
)
from tenderguard.domain.scenarios import (
    ScenarioDefinition,
    ScenarioOverride,
    calculate_scenario,
)
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.database import (
    create_database_engine,
    create_schema_for_tests,
    create_session_factory,
)
from tenderguard.infrastructure.object_store import LocalObjectStore
from tenderguard.infrastructure.orm import (
    AdapterQualificationRow,
    ApprovalRecordRow,
    ApprovalTaskRow,
    CalculationRunRow,
    CalculationSnapshotRow,
    ControlledVersionRow,
    CostInputRow,
    DocumentSetRevisionRow,
    ProjectRow,
    ScenarioRunRow,
)

AUDIT_KEY = "recovery-test-audit-key-that-is-at-least-32-bytes"
AUDIT_KEY_ID = "recovery-test-key-1"
BUILD_REFERENCE = "git:" + "3" * 40


def _actor(actor_id: str, role: ActorRole) -> Actor:
    return Actor(
        actor_id=actor_id,
        organization_id="org-recovery",
        roles=frozenset({role}),
    )


def _recovery_profile(snapshot_id: str) -> dict[str, object]:
    return {
        "schema_version": "tenderguard.recovery-profile/v1",
        "source_environment": "development",
        "restore_environment": "test",
        "expected_application_build_reference": BUILD_REFERENCE,
        "maximum_rpo_seconds": 60,
        "maximum_rto_seconds": 300,
        "require_worm": False,
        "require_external_audit_anchor": False,
        "require_oidc_configuration": False,
        "require_export_signing_configuration": False,
        "require_integration_signing_configuration": False,
        "required_adapter_qualification_ids": ["adapter-recovery-1"],
        "required_golden_snapshot_ids": [snapshot_id],
    }


def _exercise(
    *,
    profile_version_id: str,
    profile_content_hash: str,
) -> RecoveryExerciseManifest:
    now = datetime.now(UTC)
    return RecoveryExerciseManifest(
        schema_version="tenderguard.recovery-exercise/v1",
        exercise_id="recovery-exercise-2026-q3",
        profile_version_id=profile_version_id,
        profile_content_hash=profile_content_hash,
        source_environment="development",
        restore_environment="test",
        incident_at=now - timedelta(seconds=10),
        restored_database_point_at=now - timedelta(seconds=20),
        restoration_started_at=now - timedelta(seconds=5),
        database_backup_reference="backup://database/base-2026-q3+wal",
        object_store_backup_reference="backup://objects/version-manifest-2026-q3",
        identity_binding_evidence_reference="evidence://identity/dr-2026-q3",
        connector_binding_evidence_reference="evidence://connectors/dr-2026-q3",
        secrets_manager_evidence_reference="evidence://secrets/dr-2026-q3",
        executed_by="recovery-operator",
        change_reference="CHG-2026-DR-003",
    )


def _build_restored_state(
    tmp_path: Path,
) -> tuple[
    Settings,
    LocalObjectStore,
    LocalObjectStore,
    object,
    str,
    str,
    str,
]:
    settings = Settings(
        app_env="test",
        application_build_reference=BUILD_REFERENCE,
        database_url="sqlite+pysqlite://",
        local_object_store_path=tmp_path / "objects",
        local_quarantine_store_path=tmp_path / "quarantine",
        allow_insecure_dev_auth=True,
        audit_signing_key=AUDIT_KEY,
        audit_signing_key_id=AUDIT_KEY_ID,
    )
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    sessions = create_session_factory(engine)
    store = LocalObjectStore(tmp_path / "objects")
    quarantine = LocalObjectStore(tmp_path / "quarantine")
    creator = _actor("methodology-creator", ActorRole.METHODOLOGY_OWNER)
    approver = _actor("methodology-approver", ActorRole.METHODOLOGY_OWNER)
    estimator = _actor("estimator-recovery", ActorRole.ESTIMATOR)

    with sessions.begin() as session:
        governance = GovernanceService(
            session=session,
            settings=settings,
            object_store=store,
        )
        calculation_version = governance.create_version(
            actor=creator,
            kind="calculation_model",
            version_label="recovery-golden-v1",
            payload={"purpose": "recovery deterministic replay"},
            request_id="request-calculation-version",
            reason="Create controlled calculation version for recovery fixture",
        )
        calculation_version = governance.approve_version(
            actor=approver,
            version_id=calculation_version.version_id,
            request_id="request-approve-calculation-version",
            reason="Independently approve recovery calculation version",
        )
        scenario_version = governance.create_version(
            actor=creator,
            kind="scenario_policy",
            version_label="recovery-scenario-v1",
            payload={
                "scenarios": {
                    "high-quantity": {
                        "name": "Golden recovery scenario",
                        "overrides": [
                            {
                                "cost_input_id": "golden-input-1",
                                "quantity": "3",
                                "evidence_or_assumption_id": ("BOUND_SCENARIO_POLICY"),
                                "reason": ("Approved recovery scenario quantity"),
                            }
                        ],
                    }
                }
            },
            request_id="request-scenario-version",
            reason="Create controlled scenario version for recovery fixture",
        )
        scenario_version = governance.approve_version(
            actor=approver,
            version_id=scenario_version.version_id,
            request_id="request-approve-scenario-version",
            reason="Independently approve recovery scenario version",
        )
        project = ProjectService(
            session=session,
            settings=settings,
            object_store=store,
        ).create_project(
            actor=estimator,
            code="DR-001",
            name="Recovery golden project",
            request_id="request-recovery-project",
            reason="Create deterministic golden recovery project",
        )
        document_set_id = "document-set-recovery-golden"
        session.add(
            DocumentSetRevisionRow(
                id=document_set_id,
                project_id=project.id,
                manifest_hash=content_hash([]),
                revision_ids=[],
                status="CONFIRMED",
                created_by=estimator.actor_id,
                created_at=utc_now(),
                confirmed_by=approver.actor_id,
                confirmed_at=utc_now(),
            )
        )
        project_row = session.get(ProjectRow, project.id)
        assert project_row is not None
        project_row.current_document_set_revision_id = document_set_id

        now = utc_now()
        atomic_input = AtomicCostInput(
            cost_input_id="golden-input-1",
            line_id="golden-line-1",
            wbs_node_id="golden-wbs-1",
            semantic_key="golden-derived-logistics",
            category=CostCategory.LOGISTICS,
            quantity="2",
            unit="lot",
            unit_rate="1250.50",
            currency="RUB",
            approved_assumption_id="approved-assumption-golden-1",
        )
        policy = CalculationPolicy(
            policy_version=calculation_version.version_id,
            currency="RUB",
            line_rounding_scale=2,
            total_rounding_scale=2,
            rounding_mode="ROUND_HALF_UP",
            independent_tolerance="0",
            expected_semantic_keys=frozenset({"golden-derived-logistics"}),
        )
        primary = calculate_primary(
            (atomic_input,),
            policy,
            engine_version=calculation_version.version_id,
            calculated_at=now,
        )
        independent = validate_independently(
            (atomic_input,),
            primary,
            policy,
            validator_version=f"independent:{calculation_version.version_id}",
            validated_at=now,
        )
        snapshot = create_snapshot(
            project_id=project.id,
            document_set_revision_id=document_set_id,
            inputs=(atomic_input,),
            policy=policy,
            controlled_versions=(calculation_version,),
            primary=primary,
            independent=independent,
            created_by=estimator.actor_id,
            created_at=now,
        )
        stored_snapshot = store.put(
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
        run_id = "calculation-run-recovery-golden"
        session.add(
            CalculationRunRow(
                id=run_id,
                project_id=project.id,
                engine_version=calculation_version.version_id,
                status="VALIDATED",
                currency="RUB",
                grand_total=primary.grand_total,
                payload={
                    "primary": primary.model_dump(mode="json"),
                    "independent_validation": independent.model_dump(mode="json"),
                    "policy": policy.model_dump(mode="json"),
                },
                created_at=now,
            )
        )
        session.add(
            CostInputRow(
                id="cost-input-recovery-golden",
                project_id=project.id,
                calculation_run_id=run_id,
                semantic_key=atomic_input.semantic_key,
                category=atomic_input.category.value,
                amount_basis_id=atomic_input.approved_assumption_id,
                payload=atomic_input.model_dump(mode="json"),
                created_at=now,
            )
        )
        session.add(
            ApprovalTaskRow(
                id="approval-task-recovery-golden",
                project_id=project.id,
                task_type="COST_ASSUMPTION_REVIEW",
                entity_type="cost_assumption",
                entity_id=(f"{atomic_input.line_id}:{atomic_input.semantic_key}"),
                assigned_role=ActorRole.METHODOLOGY_OWNER.value,
                status="APPROVED",
                required=True,
                payload={"purpose": "recovery fixture"},
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            ApprovalRecordRow(
                id="approved-assumption-golden-1",
                task_id="approval-task-recovery-golden",
                decision="APPROVED",
                decided_by=approver.actor_id,
                reason="Approve golden recovery input",
                payload={"unit_rate": "1250.50", "currency": "RUB"},
                decided_at=now,
            )
        )
        session.add(
            CalculationSnapshotRow(
                id=snapshot.snapshot_id,
                project_id=project.id,
                calculation_run_id=run_id,
                document_set_revision_id=document_set_id,
                input_hash=snapshot.input_hash,
                output_hash=snapshot.output_hash,
                snapshot_hash=snapshot.snapshot_hash,
                fixed=True,
                object_key=stored_snapshot.object_key,
                created_by=estimator.actor_id,
                created_at=now,
            )
        )
        scenario_definition = ScenarioDefinition(
            scenario_id="high-quantity",
            scenario_version=scenario_version.version_id,
            name="Golden recovery scenario",
            overrides=(
                ScenarioOverride(
                    cost_input_id=atomic_input.cost_input_id,
                    quantity="3",
                    evidence_or_assumption_id=scenario_version.version_id,
                    reason="Approved recovery scenario quantity",
                ),
            ),
        )
        scenario_result = calculate_scenario(
            (atomic_input,),
            scenario_definition,
            policy,
            calculated_at=now,
        )
        session.add(
            ScenarioRunRow(
                id="scenario-run-recovery-golden",
                project_id=project.id,
                base_calculation_run_id=run_id,
                scenario_version=scenario_version.version_id,
                status="VALIDATED",
                grand_total=scenario_result.primary.grand_total,
                payload={
                    "base_snapshot_id": snapshot.snapshot_id,
                    "base_snapshot_hash": snapshot.snapshot_hash,
                    "scenario_key": scenario_definition.scenario_id,
                    "definition": scenario_definition.model_dump(mode="json"),
                    "result": scenario_result.model_dump(mode="json"),
                    "input_signature": content_hash(
                        {
                            "base_snapshot_hash": snapshot.snapshot_hash,
                            "scenario_policy_version_id": (scenario_version.version_id),
                            "definition": scenario_definition,
                        }
                    ),
                    "executed_by": estimator.actor_id,
                },
                created_at=now,
            )
        )
        session.add(
            AdapterQualificationRow(
                id="adapter-recovery-1",
                adapter_name="recovery-test-adapter",
                adapter_version="1.0.0",
                status="APPROVED",
                valid_until=None,
                test_evidence_hash="c" * 64,
                payload={"qualification": "fixture"},
                approved_by=approver.actor_id,
                approved_at=now,
            )
        )
        profile_version = governance.create_version(
            actor=creator,
            kind="recovery_profile",
            version_label="recovery-profile-2026-q3",
            payload=_recovery_profile(snapshot.snapshot_id),
            request_id="request-recovery-profile",
            reason="Create governed recovery objectives",
        )
        profile_version = governance.approve_version(
            actor=approver,
            version_id=profile_version.version_id,
            request_id="request-approve-recovery-profile",
            reason="Independently approve recovery objectives",
        )

    return (
        settings,
        store,
        quarantine,
        engine,
        profile_version.version_id,
        profile_version.content_hash,
        snapshot.snapshot_id,
    )


def test_recovery_verification_replays_golden_snapshot_and_all_integrity_layers(
    tmp_path: Path,
) -> None:
    (
        settings,
        store,
        quarantine,
        engine,
        profile_version_id,
        profile_content_hash,
        snapshot_id,
    ) = _build_restored_state(tmp_path)
    sessions = create_session_factory(engine)
    try:
        with sessions() as session:
            profile, _ = load_approved_profile(
                session=session,
                settings=settings,
                version_id=profile_version_id,
                expected_content_hash=profile_content_hash,
                expected_kind="recovery_profile",
                profile_type=RecoveryProfile,
            )
            result = RecoveryVerificationService(
                session=session,
                settings=settings,
                object_store=store,
                quarantine_store=quarantine,
            ).verify(
                profile_version_id=profile_version_id,
                profile_content_hash=profile_content_hash,
                profile=profile,
                exercise=_exercise(
                    profile_version_id=profile_version_id,
                    profile_content_hash=profile_content_hash,
                ),
            )

        assert result.status == "TECHNICAL_VERIFICATION_PASSED"
        assert result.evidence["counts"]["calculation_snapshots"] == 1
        assert result.evidence["counts"]["scenario_runs"] == 1
        assert result.evidence["independent_reviewer_signoff_required"] is True
        assert all(finding.passed for finding in result.findings)
        assert snapshot_id in profile.required_golden_snapshot_ids
    finally:
        engine.dispose()


def test_recovery_verification_blocks_corrupt_snapshot_object(
    tmp_path: Path,
) -> None:
    (
        settings,
        store,
        quarantine,
        engine,
        profile_version_id,
        profile_content_hash,
        snapshot_id,
    ) = _build_restored_state(tmp_path)
    sessions = create_session_factory(engine)
    try:
        with sessions() as session:
            snapshot = session.get(CalculationSnapshotRow, snapshot_id)
            assert snapshot is not None
            snapshot_path = (store.root / PurePosixPath(snapshot.object_key)).resolve()
            assert store.root in snapshot_path.parents
            snapshot_path.unlink()
            profile, _ = load_approved_profile(
                session=session,
                settings=settings,
                version_id=profile_version_id,
                expected_content_hash=profile_content_hash,
                expected_kind="recovery_profile",
                profile_type=RecoveryProfile,
            )
            result = RecoveryVerificationService(
                session=session,
                settings=settings,
                object_store=store,
                quarantine_store=quarantine,
            ).verify(
                profile_version_id=profile_version_id,
                profile_content_hash=profile_content_hash,
                profile=profile,
                exercise=_exercise(
                    profile_version_id=profile_version_id,
                    profile_content_hash=profile_content_hash,
                ),
            )

        assert result.status == "FAILED"
        failed_codes = {finding.code for finding in result.findings if not finding.passed}
        assert "CALCULATION_SNAPSHOT_REPLAY" in failed_codes
        assert "GOLDEN_SNAPSHOT_SET" in failed_codes
    finally:
        engine.dispose()


def test_qualification_profile_loader_rejects_payload_tampering(
    tmp_path: Path,
) -> None:
    (
        settings,
        _store,
        _quarantine,
        engine,
        profile_version_id,
        profile_content_hash,
        _snapshot_id,
    ) = _build_restored_state(tmp_path)
    sessions = create_session_factory(engine)
    try:
        with sessions.begin() as session:
            row = session.get(ControlledVersionRow, profile_version_id)
            assert row is not None
            row.payload = {
                **row.payload,
                "maximum_rto_seconds": 999_999,
            }
        with (
            sessions() as session,
            pytest.raises(ValueError, match="not validly approved"),
        ):
            load_approved_profile(
                session=session,
                settings=settings,
                version_id=profile_version_id,
                expected_content_hash=profile_content_hash,
                expected_kind="recovery_profile",
                profile_type=RecoveryProfile,
            )
    finally:
        engine.dispose()
