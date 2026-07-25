from __future__ import annotations

import json
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from tenderguard.application.audit_integrity import AuditIntegrityService
from tenderguard.application.exports import ExportPackageService
from tenderguard.application.operational_qualification import build_result_envelope
from tenderguard.application.scenarios import ScenarioService
from tenderguard.application.snapshot_integrity import read_verified_snapshot
from tenderguard.config import Settings
from tenderguard.domain.audit import AuditEvent, verify_chain
from tenderguard.domain.calculation import (
    AtomicCostInput,
    CalculationPolicy,
    calculate_primary,
    validate_independently,
)
from tenderguard.domain.common import canonical_data, content_hash, ensure_utc, utc_now
from tenderguard.domain.exports import SignedExportPackage, verify_signed_export_package
from tenderguard.domain.models import (
    CalculationResult,
    CalculationSnapshot,
    ControlledVersion,
    IndependentValidationResult,
)
from tenderguard.domain.operational_qualification import (
    QualificationFinding,
    QualificationResultEnvelope,
    RecoveryExerciseManifest,
    RecoveryProfile,
)
from tenderguard.domain.scenarios import (
    ScenarioDefinition,
    ScenarioResult,
    calculate_scenario,
)
from tenderguard.infrastructure.database import CURRENT_SCHEMA_REVISION
from tenderguard.infrastructure.object_store import ObjectStore
from tenderguard.infrastructure.orm import (
    AdapterQualificationRow,
    ApprovalRecordRow,
    ApprovalTaskRow,
    AuditCheckpointRow,
    AuditEventRow,
    CalculationRunRow,
    CalculationSnapshotRow,
    CommercialCostModelRow,
    ControlledVersionRow,
    CostInputRow,
    DocumentRevisionRow,
    DocumentRow,
    DocumentSetRevisionRow,
    ExportArtifactRow,
    NormativeCalculationRow,
    ObservationRow,
    ProjectRow,
    QuarantinedUploadRow,
    ReleaseDecisionRow,
    RiskCalculationRow,
    ScenarioRunRow,
)


class RecoveryVerificationService:
    def __init__(
        self,
        *,
        session: Session,
        settings: Settings,
        object_store: ObjectStore,
        quarantine_store: ObjectStore,
    ) -> None:
        self.session = session
        self.settings = settings
        self.object_store = object_store
        self.quarantine_store = quarantine_store

    def verify(
        self,
        *,
        profile_version_id: str,
        profile_content_hash: str,
        profile: RecoveryProfile,
        exercise: RecoveryExerciseManifest,
    ) -> QualificationResultEnvelope:
        verification_started_at = utc_now()
        findings: list[QualificationFinding] = []
        counts: dict[str, int] = {}

        self._check_exercise_binding(
            findings=findings,
            profile_version_id=profile_version_id,
            profile_content_hash=profile_content_hash,
            profile=profile,
            exercise=exercise,
        )
        self._check_recovery_objectives(findings, profile, exercise)
        self._check_schema(findings)
        self._check_runtime_bindings(findings, profile)
        self._check_stores(findings, profile)
        self._check_adapters(findings, profile)
        self._check_controlled_versions(findings, counts)
        self._check_document_sets(findings, counts)
        self._check_object_references(findings, counts)
        valid_snapshot_ids = self._check_snapshots(findings, counts)
        self._check_golden_set(findings, profile, valid_snapshot_ids)
        self._check_scenarios(findings, counts)
        self._check_exports(findings, counts)
        self._check_audit(findings, counts, profile)

        completed_at = utc_now()
        rto_seconds = self._seconds_between(exercise.restoration_started_at, completed_at)
        self._add(
            findings,
            code="RTO_WITHIN_APPROVED_LIMIT",
            passed=Decimal("0") <= rto_seconds <= profile.maximum_rto_seconds,
            message=(
                "Measured recovery duration is within the approved RTO"
                if Decimal("0") <= rto_seconds <= profile.maximum_rto_seconds
                else "Measured recovery duration exceeds the approved RTO"
            ),
            measured_seconds=str(rto_seconds),
            approved_maximum_seconds=profile.maximum_rto_seconds,
        )
        status = (
            "TECHNICAL_VERIFICATION_PASSED"
            if findings and all(finding.passed for finding in findings)
            else "FAILED"
        )
        evidence: dict[str, object] = {
            "exercise_id": exercise.exercise_id,
            "exercise_manifest_hash": content_hash(exercise),
            "source_environment": exercise.source_environment,
            "restore_environment": exercise.restore_environment,
            "application_build_reference": self.settings.application_build_reference,
            "database_backup_reference": exercise.database_backup_reference,
            "object_store_backup_reference": exercise.object_store_backup_reference,
            "identity_binding_evidence_reference": (exercise.identity_binding_evidence_reference),
            "connector_binding_evidence_reference": (exercise.connector_binding_evidence_reference),
            "secrets_manager_evidence_reference": (exercise.secrets_manager_evidence_reference),
            "executed_by": exercise.executed_by,
            "change_reference": exercise.change_reference,
            "measured_rpo_seconds": str(
                self._seconds_between(
                    exercise.restored_database_point_at,
                    exercise.incident_at,
                )
            ),
            "measured_rto_seconds": str(rto_seconds),
            "counts": counts,
            "independent_reviewer_signoff_required": True,
        }
        return build_result_envelope(
            qualification_type="RECOVERY",
            status=status,
            profile_version_id=profile_version_id,
            profile_content_hash=profile_content_hash,
            started_at=verification_started_at,
            completed_at=completed_at,
            findings=tuple(findings),
            evidence=evidence,
        )

    def _check_exercise_binding(
        self,
        *,
        findings: list[QualificationFinding],
        profile_version_id: str,
        profile_content_hash: str,
        profile: RecoveryProfile,
        exercise: RecoveryExerciseManifest,
    ) -> None:
        passed = bool(
            exercise.profile_version_id == profile_version_id
            and exercise.profile_content_hash == profile_content_hash
            and exercise.source_environment == profile.source_environment
            and exercise.restore_environment == profile.restore_environment
            and exercise.restore_environment == self.settings.app_env
        )
        self._add(
            findings,
            code="RECOVERY_EXERCISE_PROFILE_BINDING",
            passed=passed,
            message=(
                "Exercise is bound to the approved profile and current isolated runtime"
                if passed
                else "Exercise/profile/runtime environment binding is inconsistent"
            ),
            runtime_environment=self.settings.app_env,
        )

    def _check_recovery_objectives(
        self,
        findings: list[QualificationFinding],
        profile: RecoveryProfile,
        exercise: RecoveryExerciseManifest,
    ) -> None:
        rpo_seconds = self._seconds_between(
            exercise.restored_database_point_at,
            exercise.incident_at,
        )
        self._add(
            findings,
            code="RPO_WITHIN_APPROVED_LIMIT",
            passed=Decimal("0") <= rpo_seconds <= profile.maximum_rpo_seconds,
            message=(
                "Declared restored point is within the approved RPO"
                if Decimal("0") <= rpo_seconds <= profile.maximum_rpo_seconds
                else "Declared restored point exceeds the approved RPO"
            ),
            measured_seconds=str(rpo_seconds),
            approved_maximum_seconds=profile.maximum_rpo_seconds,
        )

    def _check_schema(self, findings: list[QualificationFinding]) -> None:
        revision = self.session.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one_or_none()
        self._add(
            findings,
            code="DATABASE_SCHEMA_CURRENT",
            passed=revision == CURRENT_SCHEMA_REVISION,
            message=(
                "Restored database schema is at the exact application revision"
                if revision == CURRENT_SCHEMA_REVISION
                else "Restored database schema does not match the application revision"
            ),
            actual_revision=revision,
            expected_revision=CURRENT_SCHEMA_REVISION,
        )

    def _check_runtime_bindings(
        self,
        findings: list[QualificationFinding],
        profile: RecoveryProfile,
    ) -> None:
        oidc_configured = bool(
            self.settings.oidc_issuer
            and self.settings.oidc_audience
            and self.settings.oidc_jwks_url
            and (not self.settings.operator_ui_enabled or self.settings.oidc_web_client_id)
        )
        checks = (
            (
                "APPLICATION_BUILD_BINDING",
                (
                    self.settings.application_build_reference
                    == profile.expected_application_build_reference
                ),
                "Restored runtime uses the exact approved application build",
                "Restored runtime build differs from the approved profile",
            ),
            (
                "OIDC_CONFIGURATION_PRESENT",
                not profile.require_oidc_configuration or oidc_configured,
                "Required OIDC binding is configured",
                "Required OIDC binding is incomplete",
            ),
            (
                "EXPORT_SIGNING_CONFIGURATION_PRESENT",
                (
                    not profile.require_export_signing_configuration
                    or self.settings.export_signing_configured
                ),
                "Required export-signing binding is configured",
                "Required export-signing binding is incomplete",
            ),
            (
                "INTEGRATION_SIGNING_CONFIGURATION_PRESENT",
                (
                    not profile.require_integration_signing_configuration
                    or self.settings.integration_signing_configured
                ),
                "Required integration-signing binding is configured",
                "Required integration-signing binding is incomplete",
            ),
        )
        for code, passed, success, failure in checks:
            self._add(
                findings,
                code=code,
                passed=bool(passed),
                message=success if passed else failure,
            )

    def _check_stores(
        self,
        findings: list[QualificationFinding],
        profile: RecoveryProfile,
    ) -> None:
        for code, store in (
            ("EVIDENCE_STORE_REACHABLE", self.object_store),
            ("QUARANTINE_STORE_REACHABLE", self.quarantine_store),
        ):
            try:
                healthy = store.healthcheck()
            except Exception:
                healthy = False
            self._add(
                findings,
                code=code,
                passed=healthy,
                message=(
                    "Restored object store is reachable"
                    if healthy
                    else "Restored object store is not reachable"
                ),
            )
        worm_valid = not profile.require_worm
        if profile.require_worm:
            try:
                if (
                    self.settings.s3_required_object_lock_mode is not None
                    and self.settings.s3_minimum_retention_days is not None
                ):
                    worm_valid = self.object_store.retention_status().satisfies(
                        required_mode=self.settings.s3_required_object_lock_mode,
                        minimum_days=self.settings.s3_minimum_retention_days,
                    )
            except Exception:
                worm_valid = False
        self._add(
            findings,
            code="EVIDENCE_STORE_WORM",
            passed=worm_valid,
            message=(
                "Approved WORM requirement is satisfied"
                if worm_valid
                else "Approved WORM requirement is not satisfied"
            ),
        )

    def _check_adapters(
        self,
        findings: list[QualificationFinding],
        profile: RecoveryProfile,
    ) -> None:
        rows = {
            row.id: row
            for row in self.session.scalars(
                select(AdapterQualificationRow).where(
                    AdapterQualificationRow.id.in_(profile.required_adapter_qualification_ids)
                )
            )
        }
        today = utc_now().date()
        invalid = [
            identifier
            for identifier in profile.required_adapter_qualification_ids
            if (
                identifier not in rows
                or rows[identifier].status != "APPROVED"
                or self._adapter_expired(rows[identifier], today)
            )
        ]
        self._add(
            findings,
            code="REQUIRED_ADAPTER_QUALIFICATIONS",
            passed=not invalid,
            message=(
                "All approved recovery-profile adapter bindings are present and current"
                if not invalid
                else "Required adapter qualification is absent, unapproved, or expired"
            ),
            invalid_ids=self._summarize_ids(invalid),
        )

    def _check_controlled_versions(
        self,
        findings: list[QualificationFinding],
        counts: dict[str, int],
    ) -> None:
        rows = list(
            self.session.scalars(select(ControlledVersionRow).order_by(ControlledVersionRow.id))
        )
        invalid = [
            row.id
            for row in rows
            if row.content_hash
            != content_hash(
                {
                    "kind": row.kind,
                    "version_label": row.version_label,
                    "payload": row.payload,
                }
            )
        ]
        counts["controlled_versions"] = len(rows)
        self._add(
            findings,
            code="CONTROLLED_VERSION_HASHES",
            passed=not invalid,
            message=(
                "All controlled-version payload hashes verify"
                if not invalid
                else "One or more controlled-version payload hashes do not verify"
            ),
            invalid_ids=self._summarize_ids(invalid),
        )

    def _check_document_sets(
        self,
        findings: list[QualificationFinding],
        counts: dict[str, int],
    ) -> None:
        revisions = {
            revision.id: document.project_id
            for revision, document in self.session.execute(
                select(DocumentRevisionRow, DocumentRow).join(
                    DocumentRow,
                    DocumentRow.id == DocumentRevisionRow.document_id,
                )
            ).all()
        }
        sets = list(
            self.session.scalars(select(DocumentSetRevisionRow).order_by(DocumentSetRevisionRow.id))
        )
        invalid: list[str] = []
        for item in sets:
            if (
                item.manifest_hash != content_hash(item.revision_ids)
                or len(item.revision_ids) != len(set(item.revision_ids))
                or item.status not in {"DRAFT", "CONFIRMED", "SUPERSEDED"}
                or (
                    item.status in {"CONFIRMED", "SUPERSEDED"}
                    and (not item.confirmed_by or ensure_utc(item.confirmed_at) is None)
                )
                or (
                    item.status == "DRAFT"
                    and (item.confirmed_by is not None or item.confirmed_at is not None)
                )
                or any(
                    revision_id not in revisions or revisions[revision_id] != item.project_id
                    for revision_id in item.revision_ids
                )
            ):
                invalid.append(item.id)
        projects = list(self.session.scalars(select(ProjectRow).order_by(ProjectRow.id)))
        for project in projects:
            if project.current_document_set_revision_id is None:
                continue
            matching = [
                item
                for item in sets
                if item.id == project.current_document_set_revision_id
                and item.project_id == project.id
                and item.status == "CONFIRMED"
            ]
            if len(matching) != 1:
                invalid.append(f"project:{project.id}")
        counts["document_sets"] = len(sets)
        counts["projects"] = len(projects)
        self._add(
            findings,
            code="DOCUMENT_SET_MANIFESTS",
            passed=not invalid,
            message=(
                "Document-set manifests and current project bindings verify"
                if not invalid
                else "Document-set manifest or current project binding is invalid"
            ),
            invalid_ids=self._summarize_ids(invalid),
        )

    def _check_object_references(
        self,
        findings: list[QualificationFinding],
        counts: dict[str, int],
    ) -> None:
        documents = list(
            self.session.scalars(select(DocumentRevisionRow).order_by(DocumentRevisionRow.id))
        )
        quarantined = list(
            self.session.scalars(select(QuarantinedUploadRow).order_by(QuarantinedUploadRow.id))
        )
        document_failures = self._verify_objects(
            store=self.object_store,
            references=[
                (row.id, row.object_hash, row.object_key, row.size_bytes) for row in documents
            ],
        )
        quarantine_failures = self._verify_objects(
            store=self.quarantine_store,
            references=[
                (row.id, row.object_hash, row.object_key, row.size_bytes) for row in quarantined
            ],
        )
        counts["document_revisions"] = len(documents)
        counts["quarantined_uploads"] = len(quarantined)
        self._add(
            findings,
            code="DOCUMENT_OBJECTS",
            passed=not document_failures,
            message=(
                "Every restored document revision object verifies"
                if not document_failures
                else "A restored document revision object is absent or corrupt"
            ),
            invalid_ids=self._summarize_ids(document_failures),
        )
        self._add(
            findings,
            code="QUARANTINE_OBJECTS",
            passed=not quarantine_failures,
            message=(
                "Every restored quarantine object verifies"
                if not quarantine_failures
                else "A restored quarantine object is absent or corrupt"
            ),
            invalid_ids=self._summarize_ids(quarantine_failures),
        )

    def _check_snapshots(
        self,
        findings: list[QualificationFinding],
        counts: dict[str, int],
    ) -> set[str]:
        snapshots = list(
            self.session.scalars(select(CalculationSnapshotRow).order_by(CalculationSnapshotRow.id))
        )
        valid: set[str] = set()
        invalid: list[str] = []
        for snapshot in snapshots:
            try:
                self._verify_snapshot(snapshot)
            except Exception:
                invalid.append(snapshot.id)
            else:
                valid.add(snapshot.id)
        counts["calculation_snapshots"] = len(snapshots)
        self._add(
            findings,
            code="CALCULATION_SNAPSHOT_REPLAY",
            passed=not invalid,
            message=(
                "Every fixed snapshot, run, atomic input, and independent replay verifies"
                if not invalid
                else "A calculation snapshot failed deterministic replay"
            ),
            invalid_ids=self._summarize_ids(invalid),
        )
        return valid

    def _verify_snapshot(self, snapshot: CalculationSnapshotRow) -> None:
        if not snapshot.fixed:
            raise RuntimeError("Persisted calculation snapshot is not fixed")
        payload = read_verified_snapshot(
            object_store=self.object_store,
            snapshot=snapshot,
        )
        stored_snapshot = CalculationSnapshot.model_validate(payload["snapshot"])
        inputs = tuple(AtomicCostInput.model_validate(item) for item in payload["inputs"])
        policy = CalculationPolicy.model_validate(payload["policy"])
        primary = CalculationResult.model_validate(payload["primary"])
        independent = IndependentValidationResult.model_validate(payload["independent"])
        document_set = self.session.get(
            DocumentSetRevisionRow,
            snapshot.document_set_revision_id,
        )
        if (
            stored_snapshot.created_by != snapshot.created_by
            or ensure_utc(stored_snapshot.created_at) != ensure_utc(snapshot.created_at)
            or not stored_snapshot.fixed
            or document_set is None
            or document_set.project_id != snapshot.project_id
            or document_set.status not in {"CONFIRMED", "SUPERSEDED"}
            or not document_set.confirmed_by
            or ensure_utc(document_set.confirmed_at) is None
        ):
            raise RuntimeError("Snapshot metadata or document-set binding differs")
        replayed_primary = calculate_primary(
            inputs,
            policy,
            engine_version=primary.engine_version,
            calculated_at=primary.calculated_at,
        )
        replayed_independent = validate_independently(
            inputs,
            replayed_primary,
            policy,
            validator_version=independent.validator_version,
            validated_at=independent.validated_at,
        )
        if replayed_primary != primary or replayed_independent != independent:
            raise RuntimeError("Snapshot deterministic calculation replay differs")
        run = self.session.get(CalculationRunRow, snapshot.calculation_run_id)
        expected_status = "VALIDATED" if independent.passed else "FAILED_VALIDATION"
        if (
            run is None
            or run.project_id != snapshot.project_id
            or run.engine_version != primary.engine_version
            or run.status != expected_status
            or run.currency != primary.currency
            or Decimal(run.grand_total) != primary.grand_total
            or run.payload.get("primary") != primary.model_dump(mode="json")
            or run.payload.get("independent_validation") != independent.model_dump(mode="json")
            or run.payload.get("policy") != policy.model_dump(mode="json")
        ):
            raise RuntimeError("Calculation run differs from its snapshot")
        input_rows = list(
            self.session.scalars(
                select(CostInputRow)
                .where(CostInputRow.calculation_run_id == run.id)
                .order_by(CostInputRow.semantic_key, CostInputRow.id)
            )
        )
        expected_inputs = sorted(
            (item.model_dump(mode="json") for item in inputs),
            key=lambda item: (str(item["semantic_key"]), str(item["cost_input_id"])),
        )
        actual_inputs = sorted(
            (row.payload for row in input_rows),
            key=lambda item: (str(item["semantic_key"]), str(item["cost_input_id"])),
        )
        if actual_inputs != expected_inputs or any(
            row.project_id != snapshot.project_id for row in input_rows
        ):
            raise RuntimeError("Atomic cost-input records differ from their snapshot")
        input_by_payload_id = {str(row.payload["cost_input_id"]): row for row in input_rows}
        for item in inputs:
            row = input_by_payload_id.get(item.cost_input_id)
            basis_id = (
                item.source_observation_id
                or item.approved_assumption_id
                or item.normative_rate_id
                or item.risk_reserve_id
                or item.derived_cost_model_id
            )
            if (
                row is None
                or row.semantic_key != item.semantic_key
                or row.category != item.category.value
                or row.amount_basis_id != basis_id
                or not self._cost_basis_valid(
                    project_id=snapshot.project_id,
                    item=item,
                )
            ):
                raise RuntimeError("Atomic cost-input evidence lineage is invalid")
        controlled = tuple(
            ControlledVersion.model_validate(item) for item in payload["controlled_versions"]
        )
        if (
            not controlled
            or len({version.version_id for version in controlled}) != len(controlled)
            or policy.policy_version not in {version.version_id for version in controlled}
            or primary.engine_version != policy.policy_version
        ):
            raise RuntimeError("Snapshot calculation policy is not governed")
        version_rows = {
            row.id: row
            for row in self.session.scalars(
                select(ControlledVersionRow).where(
                    ControlledVersionRow.id.in_([version.version_id for version in controlled])
                )
            )
        }
        for version in controlled:
            version_row = version_rows.get(version.version_id)
            if (
                version_row is None
                or version_row.kind != version.kind
                or version_row.content_hash != version.content_hash
                or version_row.status != version.status.value
                or version_row.approved_by != version.approved_by
                or ensure_utc(version_row.approved_at) != ensure_utc(version.approved_at)
            ):
                raise RuntimeError("Controlled version differs from snapshot binding")

    def _cost_basis_valid(
        self,
        *,
        project_id: str,
        item: AtomicCostInput,
    ) -> bool:
        if item.source_observation_id is not None:
            observation_row = self.session.get(
                ObservationRow,
                item.source_observation_id,
            )
            return bool(
                observation_row is not None
                and observation_row.project_id == project_id
                and observation_row.status == "VERIFIED"
            )
        if item.approved_assumption_id is not None:
            record = self.session.get(ApprovalRecordRow, item.approved_assumption_id)
            task = self.session.get(ApprovalTaskRow, record.task_id) if record is not None else None
            return bool(
                record is not None
                and task is not None
                and task.project_id == project_id
                and task.entity_type == "cost_assumption"
                and task.entity_id == f"{item.line_id}:{item.semantic_key}"
                and task.status == "APPROVED"
                and record.decision == "APPROVED"
            )
        if item.normative_rate_id is not None:
            normative_row = self.session.get(
                NormativeCalculationRow,
                item.normative_rate_id,
            )
            return bool(
                normative_row is not None
                and normative_row.project_id == project_id
                and normative_row.status == "VALIDATED"
                and normative_row.artifact_hash is not None
            )
        if item.risk_reserve_id is not None:
            risk_row = self.session.get(
                RiskCalculationRow,
                item.risk_reserve_id,
            )
            return bool(
                risk_row is not None
                and risk_row.project_id == project_id
                and risk_row.status == "VALIDATED"
            )
        if item.derived_cost_model_id is not None:
            commercial_row = self.session.get(
                CommercialCostModelRow,
                item.derived_cost_model_id,
            )
            return bool(
                commercial_row is not None
                and commercial_row.project_id == project_id
                and commercial_row.status == "VALIDATED"
            )
        return False

    def _check_golden_set(
        self,
        findings: list[QualificationFinding],
        profile: RecoveryProfile,
        valid_snapshot_ids: set[str],
    ) -> None:
        invalid = sorted(set(profile.required_golden_snapshot_ids) - valid_snapshot_ids)
        self._add(
            findings,
            code="GOLDEN_SNAPSHOT_SET",
            passed=not invalid,
            message=(
                "Every approved golden snapshot was restored and independently replayed"
                if not invalid
                else "An approved golden snapshot is missing or failed replay"
            ),
            invalid_ids=self._summarize_ids(invalid),
        )

    def _check_scenarios(
        self,
        findings: list[QualificationFinding],
        counts: dict[str, int],
    ) -> None:
        rows = list(self.session.scalars(select(ScenarioRunRow).order_by(ScenarioRunRow.id)))
        invalid: list[str] = []
        for row in rows:
            try:
                snapshot = self.session.scalar(
                    select(CalculationSnapshotRow).where(
                        CalculationSnapshotRow.calculation_run_id == row.base_calculation_run_id,
                        CalculationSnapshotRow.project_id == row.project_id,
                    )
                )
                if snapshot is None:
                    raise RuntimeError("Scenario base snapshot is missing")
                snapshot_payload = read_verified_snapshot(
                    object_store=self.object_store,
                    snapshot=snapshot,
                )
                inputs = tuple(
                    AtomicCostInput.model_validate(item) for item in snapshot_payload["inputs"]
                )
                policy = CalculationPolicy.model_validate(snapshot_payload["policy"])
                definition = ScenarioDefinition.model_validate(row.payload["definition"])
                result = ScenarioResult.model_validate(row.payload["result"])
                version = self.session.get(
                    ControlledVersionRow,
                    row.scenario_version,
                )
                scenario_key = row.payload.get("scenario_key")
                raw_scenarios = version.payload.get("scenarios") if version is not None else None
                raw_definition = (
                    raw_scenarios.get(scenario_key)
                    if isinstance(raw_scenarios, dict) and isinstance(scenario_key, str)
                    else None
                )
                if not isinstance(raw_definition, dict):
                    raise RuntimeError("Scenario definition is absent from its approved policy")
                expected_definition = ScenarioDefinition.model_validate(
                    {
                        **ScenarioService._bind_policy_evidence(
                            raw_definition,
                            policy_version_id=row.scenario_version,
                            inputs=inputs,
                        ),
                        "scenario_id": scenario_key,
                        "scenario_version": row.scenario_version,
                    }
                )
                expected_signature = content_hash(
                    {
                        "base_snapshot_hash": snapshot.snapshot_hash,
                        "scenario_policy_version_id": row.scenario_version,
                        "definition": definition,
                    }
                )
                if (
                    row.payload.get("base_snapshot_id") != snapshot.id
                    or row.payload.get("base_snapshot_hash") != snapshot.snapshot_hash
                    or row.payload.get("input_signature") != expected_signature
                    or definition != expected_definition
                    or definition.scenario_version != row.scenario_version
                    or version is None
                    or version.kind != "scenario_policy"
                    or version.status != "APPROVED"
                    or version.content_hash
                    != content_hash(
                        {
                            "kind": version.kind,
                            "version_label": version.version_label,
                            "payload": version.payload,
                        }
                    )
                    or not self._scenario_evidence_valid(
                        project_id=row.project_id,
                        scenario_policy_version_id=row.scenario_version,
                        definition=definition,
                    )
                ):
                    raise RuntimeError("Scenario source binding is invalid")
                replay = calculate_scenario(
                    inputs,
                    definition,
                    policy,
                    calculated_at=result.primary.calculated_at,
                )
                expected_status = "VALIDATED" if result.independent.passed else "FAILED_VALIDATION"
                if (
                    replay != result
                    or row.status != expected_status
                    or Decimal(row.grand_total) != result.primary.grand_total
                ):
                    raise RuntimeError("Scenario replay differs")
            except Exception:
                invalid.append(row.id)
        counts["scenario_runs"] = len(rows)
        self._add(
            findings,
            code="SCENARIO_REPLAY",
            passed=not invalid,
            message=(
                "Every scenario run and independent result replays exactly"
                if not invalid
                else "A scenario run failed deterministic replay"
            ),
            invalid_ids=self._summarize_ids(invalid),
        )

    def _scenario_evidence_valid(
        self,
        *,
        project_id: str,
        scenario_policy_version_id: str,
        definition: ScenarioDefinition,
    ) -> bool:
        for override in definition.overrides:
            if override.evidence_or_assumption_id == scenario_policy_version_id:
                continue
            observation = self.session.get(
                ObservationRow,
                override.evidence_or_assumption_id,
            )
            if (
                observation is not None
                and observation.project_id == project_id
                and observation.status == "VERIFIED"
                and ScenarioService._basis_reproduces_override(
                    observation.payload,
                    override.model_dump(mode="json"),
                )
            ):
                continue
            record = self.session.get(
                ApprovalRecordRow,
                override.evidence_or_assumption_id,
            )
            task = self.session.get(ApprovalTaskRow, record.task_id) if record is not None else None
            if (
                record is not None
                and task is not None
                and task.project_id == project_id
                and record.decision == "APPROVED"
                and ScenarioService._basis_reproduces_override(
                    record.payload,
                    override.model_dump(mode="json"),
                )
            ):
                continue
            return False
        return True

    def _check_exports(
        self,
        findings: list[QualificationFinding],
        counts: dict[str, int],
    ) -> None:
        rows = list(self.session.scalars(select(ExportArtifactRow).order_by(ExportArtifactRow.id)))
        invalid: list[str] = []
        for row in rows:
            try:
                with self.object_store.open(row.object_hash) as stream:
                    package_bytes = stream.read()
                if (
                    PurePosixPath(row.object_key).name != row.object_hash
                    or len(package_bytes) != row.size_bytes
                ):
                    raise RuntimeError("Export object metadata differs")
                package = SignedExportPackage.model_validate(json.loads(package_bytes))
                verify_signed_export_package(
                    package,
                    trusted_public_key_b64=row.signing_public_key_b64,
                    trusted_key_id=row.signing_key_id,
                )
                if (
                    package.manifest.project_id != row.project_id
                    or package.manifest.snapshot_id != row.snapshot_id
                    or package.manifest.release_decision_id != row.release_decision_id
                    or package.manifest.template_version_id != row.template_version_id
                    or package.manifest.schema_version != row.package_schema_version
                    or package.manifest.format != row.format
                    or package.signature.manifest_hash != row.manifest_hash
                    or package.signature.algorithm != row.signature_algorithm
                    or package.signature.value_b64 != row.signature
                    or package.signature.public_key_b64 != row.signing_public_key_b64
                    or package.signature.public_key_fingerprint != row.public_key_fingerprint
                ):
                    raise RuntimeError("Export row differs from signed package")
                snapshot = self.session.get(CalculationSnapshotRow, row.snapshot_id)
                if snapshot is None:
                    raise RuntimeError("Export snapshot is missing")
                verified_snapshot = read_verified_snapshot(
                    object_store=self.object_store,
                    snapshot=snapshot,
                )
                if content_hash(verified_snapshot) != content_hash(
                    package.contents["snapshot.json"]
                ):
                    raise RuntimeError("Export embeds a different snapshot")
                release = self.session.get(ReleaseDecisionRow, row.release_decision_id)
                if (
                    release is None
                    or release.project_id != row.project_id
                    or release.snapshot_id != row.snapshot_id
                    or not release.allowed
                ):
                    raise RuntimeError("Export release decision is absent or not allowed")
                expected_release = canonical_data(
                    {
                        "id": release.id,
                        "snapshot_id": release.snapshot_id,
                        "requested_state": release.requested_state,
                        "resulting_state": release.resulting_state,
                        "allowed": release.allowed,
                        "payload": release.payload,
                        "decided_by": release.decided_by,
                        "decided_at": ensure_utc(release.decided_at),
                    }
                )
                if content_hash(expected_release) != content_hash(
                    package.contents["release_decision.json"]
                ):
                    raise RuntimeError("Export release content differs from source")
                ExportPackageService(
                    session=self.session,
                    settings=self.settings,
                    object_store=self.object_store,
                )._verify_packaged_controlled_versions(package.contents["controlled_versions.json"])
                packaged_events = [
                    AuditEvent.model_validate(item) for item in package.contents["audit_chain.json"]
                ]
                if (
                    not packaged_events
                    or not verify_chain(
                        packaged_events,
                        self.settings.audit_verification_keyring,
                    )
                    or packaged_events[-1].event_hash != package.manifest.audit_cutoff_event_hash
                ):
                    raise RuntimeError("Export audit history is invalid")
            except Exception:
                invalid.append(row.id)
        counts["signed_exports"] = len(rows)
        self._add(
            findings,
            code="SIGNED_EXPORT_PACKAGES",
            passed=not invalid,
            message=(
                "Every restored signed export package verifies"
                if not invalid
                else "A restored signed export package failed verification"
            ),
            invalid_ids=self._summarize_ids(invalid),
        )

    def _check_audit(
        self,
        findings: list[QualificationFinding],
        counts: dict[str, int],
        profile: RecoveryProfile,
    ) -> None:
        rows = list(
            self.session.scalars(
                select(AuditEventRow).order_by(
                    AuditEventRow.aggregate_type,
                    AuditEventRow.aggregate_id,
                    AuditEventRow.sequence,
                )
            )
        )
        chains: dict[tuple[str, str], list[AuditEvent]] = defaultdict(list)
        invalid_chains: list[str] = []
        try:
            for row in rows:
                event = AuditIntegrityService._event(row)
                chains[(event.aggregate_type, event.aggregate_id)].append(event)
        except Exception:
            invalid_chains.append("invalid-audit-row")
        for key, chain in chains.items():
            if not verify_chain(chain, self.settings.audit_verification_keyring):
                invalid_chains.append(f"{key[0]}:{key[1]}")
        counts["audit_events"] = len(rows)
        counts["audit_chains"] = len(chains)
        self._add(
            findings,
            code="AUDIT_CHAINS",
            passed=bool(rows) and not invalid_chains,
            message=(
                "Every restored audit chain verifies"
                if rows and not invalid_chains
                else "Restored audit history is absent or fails verification"
            ),
            invalid_ids=self._summarize_ids(invalid_chains),
        )
        service = AuditIntegrityService(
            session=self.session,
            settings=self.settings,
            object_store=self.object_store,
        )
        checkpoints = list(
            self.session.scalars(select(AuditCheckpointRow).order_by(AuditCheckpointRow.id))
        )
        invalid_checkpoints: list[str] = []
        for checkpoint in checkpoints:
            try:
                manifest = service._read_checkpoint_manifest(checkpoint)
                reasons: list[str] = []
                service._verify_current_audit_against_manifest(
                    manifest=manifest,
                    reasons=reasons,
                )
                if reasons:
                    raise RuntimeError("Checkpoint terminal verification failed")
            except Exception:
                invalid_checkpoints.append(checkpoint.id)
        counts["audit_checkpoints"] = len(checkpoints)
        self._add(
            findings,
            code="AUDIT_CHECKPOINTS",
            passed=not invalid_checkpoints,
            message=(
                "Every restored audit checkpoint and anchored terminal verifies"
                if not invalid_checkpoints
                else "A restored audit checkpoint failed verification"
            ),
            invalid_ids=self._summarize_ids(invalid_checkpoints),
        )
        anchor = service.anchor_status()
        anchor_valid = not profile.require_external_audit_anchor or anchor.valid
        self._add(
            findings,
            code="EXTERNAL_AUDIT_ANCHOR",
            passed=anchor_valid,
            message=(
                "Approved external audit-anchor requirement is satisfied"
                if anchor_valid
                else "Approved external audit-anchor requirement is not satisfied"
            ),
            reasons=self._summarize_ids(list(anchor.reasons)),
        )

    @staticmethod
    def _verify_objects(
        *,
        store: ObjectStore,
        references: list[tuple[str, str, str, int]],
    ) -> list[str]:
        failures: list[str] = []
        for identifier, object_hash, object_key, size_bytes in references:
            try:
                if PurePosixPath(object_key).name != object_hash:
                    raise RuntimeError("Object key is not content-addressed")
                with store.open(object_hash) as stream:
                    payload = stream.read()
                if len(payload) != size_bytes:
                    raise RuntimeError("Object size differs from its record")
            except Exception:
                failures.append(identifier)
        return failures

    @staticmethod
    def _seconds_between(earlier: Any, later: Any) -> Decimal:
        normalized_earlier = ensure_utc(earlier)
        normalized_later = ensure_utc(later)
        if normalized_earlier is None or normalized_later is None:
            raise ValueError("Recovery timestamps are required")
        delta = normalized_later - normalized_earlier
        return Decimal(delta.days * 86_400 + delta.seconds) + Decimal(delta.microseconds) / Decimal(
            1_000_000
        )

    @staticmethod
    def _summarize_ids(values: list[str]) -> str | None:
        if not values:
            return None
        visible = values[:20]
        suffix = f" (+{len(values) - 20} more)" if len(values) > 20 else ""
        return ", ".join(visible) + suffix

    @staticmethod
    def _adapter_expired(row: AdapterQualificationRow, today: date) -> bool:
        valid_until = row.valid_until
        return valid_until is not None and valid_until < today

    @staticmethod
    def _add(
        findings: list[QualificationFinding],
        *,
        code: str,
        passed: bool,
        message: str,
        **details: str | int | bool | None,
    ) -> None:
        findings.append(
            QualificationFinding(
                code=code,
                passed=passed,
                message=message,
                details=details,
            )
        )
