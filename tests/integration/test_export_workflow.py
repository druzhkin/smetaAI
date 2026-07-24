import base64
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select

from tenderguard.api.main import create_app
from tenderguard.config import Settings
from tenderguard.domain.audit import AuditEvent, append_event
from tenderguard.domain.calculation import (
    AtomicCostInput,
    CalculationPolicy,
    calculate_primary,
    create_snapshot,
    validate_independently,
)
from tenderguard.domain.common import canonical_data, canonical_json, content_hash
from tenderguard.domain.enums import (
    ActorRole,
    ApprovalState,
    CostCategory,
    VerificationStatus,
    VersionStatus,
)
from tenderguard.domain.exports import EXPORT_FORMAT, EXPORT_SCHEMA_VERSION
from tenderguard.domain.models import ControlledVersion
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.database import (
    create_database_engine,
    create_schema_for_tests,
    create_session_factory,
)
from tenderguard.infrastructure.object_store import LocalObjectStore
from tenderguard.infrastructure.orm import (
    AuditEventRow,
    CalculationRunRow,
    CalculationSnapshotRow,
    ControlledVersionRow,
    CostInputRow,
    DocumentRevisionRow,
    DocumentRow,
    DocumentSetRevisionRow,
    ExportArtifactRow,
    ObservationRow,
    OutboxEventRow,
    ProjectControlledVersionRow,
    ProjectRow,
    ReleaseDecisionRow,
    WorkflowTransitionRow,
)
from tests.integration.support import project_memberships

NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
PRIVATE_KEY_B64 = base64.b64encode(bytes(range(32))).decode("ascii")
AUDIT_KEY = "export-workflow-audit-key-at-least-32-bytes"


def _headers(actor: str, roles: str) -> dict[str, str]:
    return {
        "X-Dev-Actor": actor,
        "X-Dev-Organization": "org-export",
        "X-Dev-Roles": roles,
    }


def _controlled_version(
    *,
    version_id: str,
    kind: str,
    version_label: str,
    payload: dict[str, object],
) -> ControlledVersionRow:
    governed_payload = {
        **payload,
        "_governance": {
            "organization_id": "org-export",
            "created_by": "method-author",
            "created_at": (NOW - timedelta(days=2)).isoformat(),
        },
    }
    return ControlledVersionRow(
        id=version_id,
        kind=kind,
        version_label=version_label,
        content_hash=content_hash(
            {
                "kind": kind,
                "version_label": version_label,
                "payload": governed_payload,
            }
        ),
        status=VersionStatus.APPROVED.value,
        payload=governed_payload,
        approved_by="method-approver",
        approved_at=NOW - timedelta(days=1),
    )


def _audit_row(event: AuditEvent) -> AuditEventRow:
    return AuditEventRow(
        id=event.event_id,
        aggregate_type=event.aggregate_type,
        aggregate_id=event.aggregate_id,
        sequence=event.sequence,
        event_type=event.event_type,
        actor_id=event.actor_id,
        actor_roles=list(event.actor_roles),
        request_id=event.request_id,
        reason=event.reason,
        payload=canonical_data(event.payload),
        previous_hash=event.previous_hash,
        event_hash=event.event_hash,
        signature=event.signature,
        occurred_at=event.occurred_at,
    )


def _seed_released_snapshot(
    *,
    settings: Settings,
    engine: Engine,
    store: LocalObjectStore,
) -> tuple[str, str]:
    session_factory = create_session_factory(engine)
    project_id = "project-export"
    document_set_id = "document-set-export"
    release_id = "release-export"
    calculation_version = _controlled_version(
        version_id="version-calculation-export",
        kind="calculation_model",
        version_label="calc-1",
        payload={"engine": "deterministic-test"},
    )
    export_template = _controlled_version(
        version_id="version-export-template",
        kind="export_template",
        version_label="signed-json-1",
        payload={
            "schema_version": EXPORT_SCHEMA_VERSION,
            "format": EXPORT_FORMAT,
        },
    )
    versions = tuple(
        ControlledVersion(
            kind=row.kind,
            version_id=row.id,
            content_hash=row.content_hash,
            status=VersionStatus.APPROVED,
            approved_by=row.approved_by,
            approved_at=row.approved_at,
        )
        for row in (calculation_version, export_template)
    )
    atomic_input = AtomicCostInput(
        cost_input_id="material-pipe",
        line_id="boq-line-pipe",
        wbs_node_id="wbs-pipe",
        semantic_key="pipe-material",
        category=CostCategory.MATERIAL,
        quantity=Decimal("10"),
        unit="m",
        unit_rate=Decimal("125.50"),
        currency="RUB",
        source_observation_id="observation-pipe-price",
    )
    policy = CalculationPolicy(
        policy_version=calculation_version.id,
        currency="RUB",
        line_rounding_scale=2,
        total_rounding_scale=2,
        rounding_mode="ROUND_HALF_UP",
        independent_tolerance=Decimal("0.00"),
        expected_semantic_keys=frozenset({"pipe-material"}),
    )
    primary = calculate_primary(
        (atomic_input,),
        policy,
        engine_version=calculation_version.id,
        calculated_at=NOW - timedelta(hours=3),
    )
    independent = validate_independently(
        (atomic_input,),
        primary,
        policy,
        validator_version=f"independent:{calculation_version.id}",
        validated_at=NOW - timedelta(hours=3),
    )
    snapshot = create_snapshot(
        project_id=project_id,
        document_set_revision_id=document_set_id,
        inputs=(atomic_input,),
        policy=policy,
        controlled_versions=versions,
        primary=primary,
        independent=independent,
        created_by="estimator-export",
        created_at=NOW - timedelta(hours=3),
    )
    snapshot_payload = canonical_json(
        {
            "snapshot": snapshot,
            "inputs": (atomic_input,),
            "policy": policy,
            "controlled_versions": versions,
            "primary": primary,
            "independent": independent,
        }
    )
    stored_snapshot = store.put(BytesIO(snapshot_payload))
    release_time = NOW - timedelta(hours=1)
    audit_events: list[AuditEvent] = []
    for event_id, event_type, occurred_at, payload in (
        (
            "audit-project-created",
            "project_created",
            NOW - timedelta(hours=4),
            {"code": "EXP-001"},
        ),
        (
            "audit-release-transition",
            "workflow_transition",
            release_time - timedelta(seconds=1),
            {
                "from_state": ApprovalState.EXPERT_REVIEW,
                "to_state": ApprovalState.APPROVED_FOR_INTERNAL_USE,
            },
        ),
        (
            "audit-release-decision",
            "internal_release_decided",
            release_time,
            {
                "decision_id": release_id,
                "allowed": True,
                "resulting_state": ApprovalState.APPROVED_FOR_INTERNAL_USE,
                "finding_codes": [],
            },
        ),
    ):
        event = append_event(
            previous=audit_events[-1] if audit_events else None,
            event_id=event_id,
            aggregate_type="project",
            aggregate_id=project_id,
            event_type=event_type,
            actor_id="approver-release",
            actor_roles=("APPROVER",),
            request_id=f"request-{event_id}",
            reason="Fixture release evidence",
            occurred_at=occurred_at,
            payload=payload,
            signing_key=settings.audit_signing_key.get_secret_value().encode("utf-8"),
        )
        audit_events.append(event)
    with session_factory.begin() as session:
        session.add(
            ProjectRow(
                id=project_id,
                organization_id="org-export",
                code="EXP-001",
                name="Signed export test project",
                state=ApprovalState.APPROVED_FOR_INTERNAL_USE.value,
                current_document_set_revision_id=document_set_id,
                row_version=12,
                created_at=NOW - timedelta(days=5),
                updated_at=release_time,
            )
        )
        session.add_all(
            project_memberships(
                project_id,
                (
                    Actor(
                        "estimator-export",
                        "org-export",
                        frozenset({ActorRole.ESTIMATOR}),
                    ),
                    Actor(
                        "approver-export",
                        "org-export",
                        frozenset({ActorRole.APPROVER}),
                    ),
                    Actor(
                        "auditor-export",
                        "org-export",
                        frozenset({ActorRole.AUDITOR}),
                    ),
                ),
                owner_id="approver-export",
                now=NOW - timedelta(days=5),
            )
        )
        session.add(
            DocumentRow(
                id="document-export",
                project_id=project_id,
                logical_key="tender-main",
                title="Tender requirements",
                document_type="TENDER",
                critical=True,
                cancelled=False,
                created_at=NOW - timedelta(days=4),
                updated_at=NOW - timedelta(days=4),
            )
        )
        session.add(
            DocumentRevisionRow(
                id="document-revision-export",
                document_id="document-export",
                revision_label="1",
                issue_date=NOW.date(),
                object_hash="a" * 64,
                object_key="objects/aa/" + "a" * 64,
                original_filename="tender.pdf",
                media_type="application/pdf",
                size_bytes=100,
                is_current=True,
                corrupt=False,
                protected=False,
                inspection_payload={"pages": 1},
                created_at=NOW - timedelta(days=4),
                updated_at=NOW - timedelta(days=4),
            )
        )
        session.add(
            DocumentSetRevisionRow(
                id=document_set_id,
                project_id=project_id,
                manifest_hash="b" * 64,
                revision_ids=["document-revision-export"],
                status="CONFIRMED",
                created_by="reviewer-docs",
                created_at=NOW - timedelta(days=3),
                confirmed_by="reviewer-docs-2",
                confirmed_at=NOW - timedelta(days=3),
            )
        )
        session.add(
            ObservationRow(
                id="observation-pipe-price",
                project_id=project_id,
                document_revision_id="document-revision-export",
                field_name="pipe_price",
                method="MANUAL_VERIFIED",
                method_version="1",
                status=VerificationStatus.VERIFIED.value,
                payload={
                    "value": "125.50",
                    "unit": "RUB/m",
                    "locator": {"page": 1, "table": "price"},
                },
                created_at=NOW - timedelta(days=2),
            )
        )
        session.add_all([calculation_version, export_template])
        session.add_all(
            [
                ProjectControlledVersionRow(
                    project_id=project_id,
                    controlled_version_id=calculation_version.id,
                    purpose="calculation_model",
                    bound_by="method-approver",
                    bound_at=NOW - timedelta(days=1),
                ),
                ProjectControlledVersionRow(
                    project_id=project_id,
                    controlled_version_id=export_template.id,
                    purpose="export_template",
                    bound_by="method-approver",
                    bound_at=NOW - timedelta(days=1),
                ),
            ]
        )
        session.add(
            CalculationRunRow(
                id="calculation-run-export",
                project_id=project_id,
                engine_version=calculation_version.id,
                status="VALIDATED",
                currency="RUB",
                grand_total=primary.grand_total,
                payload={
                    "primary": primary.model_dump(mode="json"),
                    "independent_validation": independent.model_dump(mode="json"),
                    "policy": policy.model_dump(mode="json"),
                },
                created_at=NOW - timedelta(hours=3),
            )
        )
        session.add(
            CostInputRow(
                id="cost-row-export",
                project_id=project_id,
                calculation_run_id="calculation-run-export",
                semantic_key=atomic_input.semantic_key,
                category=atomic_input.category.value,
                amount_basis_id=atomic_input.source_observation_id,
                payload=atomic_input.model_dump(mode="json"),
                created_at=NOW - timedelta(hours=3),
            )
        )
        session.add(
            CalculationSnapshotRow(
                id=snapshot.snapshot_id,
                project_id=project_id,
                calculation_run_id="calculation-run-export",
                document_set_revision_id=document_set_id,
                input_hash=snapshot.input_hash,
                output_hash=snapshot.output_hash,
                snapshot_hash=snapshot.snapshot_hash,
                fixed=True,
                object_key=stored_snapshot.object_key,
                created_by=snapshot.created_by,
                created_at=snapshot.created_at,
            )
        )
        session.add(
            ReleaseDecisionRow(
                id=release_id,
                project_id=project_id,
                snapshot_id=snapshot.snapshot_id,
                requested_state=ApprovalState.APPROVED_FOR_INTERNAL_USE.value,
                resulting_state=ApprovalState.APPROVED_FOR_INTERNAL_USE.value,
                allowed=True,
                payload={
                    "requested_state": ApprovalState.APPROVED_FOR_INTERNAL_USE.value,
                    "resulting_state": ApprovalState.APPROVED_FOR_INTERNAL_USE.value,
                    "allowed": True,
                    "findings": [],
                },
                decided_by="approver-release",
                decided_at=release_time,
            )
        )
        session.add(
            WorkflowTransitionRow(
                id="workflow-release-export",
                project_id=project_id,
                from_state=ApprovalState.EXPERT_REVIEW.value,
                to_state=ApprovalState.APPROVED_FOR_INTERNAL_USE.value,
                actor_id="approver-release",
                reason="Fixture release evidence",
                occurred_at=release_time - timedelta(seconds=1),
            )
        )
        session.add_all([_audit_row(event) for event in audit_events])
    return project_id, snapshot.snapshot_id


def test_signed_export_api_is_idempotent_verifiable_and_fail_closed(
    tmp_path: Path,
) -> None:
    settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite://",
        allow_insecure_dev_auth=True,
        audit_signing_key=AUDIT_KEY,
        export_signing_key_id="export-key-2026-01",
        export_signing_private_key_b64=PRIVATE_KEY_B64,
    )
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    store = LocalObjectStore(tmp_path / "objects")
    project_id, snapshot_id = _seed_released_snapshot(
        settings=settings,
        engine=engine,
        store=store,
    )
    app = create_app(settings, engine=engine, object_store=store)
    with TestClient(app) as client:
        denied = client.post(
            f"/v1/projects/{project_id}/exports",
            headers=_headers("estimator-export", "ESTIMATOR"),
            json={"snapshot_id": snapshot_id, "reason": "Must not be authorized"},
        )
        assert denied.status_code == 403

        created = client.post(
            f"/v1/projects/{project_id}/exports",
            headers=_headers("approver-export", "APPROVER"),
            json={
                "snapshot_id": snapshot_id,
                "reason": "Generate approved signed audit package",
            },
        )
        assert created.status_code == 201, created.text
        artifact = created.json()
        artifact_id = artifact["artifact_id"]
        assert artifact["signature_algorithm"] == "Ed25519"
        assert artifact["signing_key_id"] == "export-key-2026-01"

        repeated = client.post(
            f"/v1/projects/{project_id}/exports",
            headers=_headers("approver-export", "APPROVER"),
            json={
                "snapshot_id": snapshot_id,
                "reason": "Idempotent retry",
            },
        )
        assert repeated.status_code == 201
        assert repeated.json()["artifact_id"] == artifact_id

        verified = client.get(
            f"/v1/projects/{project_id}/exports/{artifact_id}/verify",
            headers=_headers("auditor-export", "AUDITOR"),
        )
        assert verified.status_code == 200, verified.text
        assert verified.json()["valid"] is True
        assert verified.json()["manifest"]["audit_cutoff_event_hash"]

        downloaded = client.get(
            f"/v1/projects/{project_id}/exports/{artifact_id}/content",
            headers=_headers("auditor-export", "AUDITOR"),
        )
        assert downloaded.status_code == 200
        assert downloaded.headers["etag"] == f'"{artifact["object_hash"]}"'
        package = json.loads(downloaded.content)
        assert package["manifest"]["snapshot_id"] == snapshot_id
        assert set(package["contents"]) == {
            "approvals.json",
            "audit_chain.json",
            "controlled_versions.json",
            "lineage.json",
            "project.json",
            "release_decision.json",
            "snapshot.json",
            "workflow.json",
        }

        session_factory = create_session_factory(engine)
        with session_factory.begin() as session:
            release = session.get(ReleaseDecisionRow, "release-export")
            assert release is not None
            release.decided_by = "tampered-release-actor"
        divergent_release = client.get(
            f"/v1/projects/{project_id}/exports/{artifact_id}/verify",
            headers=_headers("auditor-export", "AUDITOR"),
        )
        assert divergent_release.status_code == 409
        assert "release decision differs" in divergent_release.json()["detail"]
        with session_factory.begin() as session:
            release = session.get(ReleaseDecisionRow, "release-export")
            assert release is not None
            release.decided_by = "approver-release"

        with session_factory.begin() as session:
            assert session.scalar(select(func.count(ExportArtifactRow.id))) == 1
            assert (
                session.scalar(
                    select(func.count(OutboxEventRow.id)).where(
                        OutboxEventRow.topic == "export.package.generated"
                    )
                )
                == 1
            )
            project = session.get(ProjectRow, project_id)
            assert project is not None
            project.state = ApprovalState.BLOCKED.value

        still_verifiable = client.get(
            f"/v1/projects/{project_id}/exports/{artifact_id}/verify",
            headers=_headers("auditor-export", "AUDITOR"),
        )
        assert still_verifiable.status_code == 200
        blocked_regeneration = client.post(
            f"/v1/projects/{project_id}/exports",
            headers=_headers("approver-export", "APPROVER"),
            json={"snapshot_id": snapshot_id, "reason": "Must remain blocked"},
        )
        assert blocked_regeneration.status_code == 422

        store._path_for(artifact["object_hash"]).write_bytes(b"tampered")
        tampered = client.get(
            f"/v1/projects/{project_id}/exports/{artifact_id}/verify",
            headers=_headers("auditor-export", "AUDITOR"),
        )
        assert tampered.status_code == 409
        assert "failed verification" in tampered.json()["detail"]
