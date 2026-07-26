from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from tenderguard.api.main import create_app
from tenderguard.application.approvals import (
    ApprovalDecisionCommand,
    ApprovalService,
)
from tenderguard.application.evidence import (
    EvidenceService,
    ManualEvidenceDecisionCommand,
    ObservationDraft,
)
from tenderguard.application.projects import OptimisticLockError
from tenderguard.config import Settings
from tenderguard.domain.common import content_hash
from tenderguard.domain.enums import (
    ActorRole,
    ApprovalDecision,
    ApprovalState,
    EvidenceMethod,
    VerificationStatus,
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
    ApprovalRecordRow,
    ApprovalTaskRow,
    ControlledVersionRow,
    DocumentRevisionRow,
    DocumentRow,
    DocumentSetRevisionRow,
    ObservationRow,
    ProjectControlledVersionRow,
    ProjectRow,
)
from tests.integration.support import (
    add_governed_controlled_version,
    project_memberships,
)


def test_manual_evidence_requires_governed_independent_review(
    tmp_path: Path,
) -> None:
    settings, factory, store, actors, _engine = _setup(tmp_path)
    author, reviewer, _creator, _approver = actors
    observed_at = datetime(2026, 7, 24, 9, 30, tzinfo=UTC)

    with factory.begin() as session:
        service = EvidenceService(
            session=session,
            settings=settings,
            object_store=store,
        )
        context = service.manual_evidence_context(
            actor=author,
            project_id="project-manual-evidence",
        )
        assert context.policy_version_id == "manual-evidence-policy-v1"
        assert context.review_role is ActorRole.REVIEWER
        assert context.document_set_revision_id == "document-set-v1"
        assert tuple(document.document_revision_id for document in context.documents) == (
            "revision-v1",
        )

        with pytest.raises(ValueError, match="bound policy version"):
            service.record_observation(
                actor=author,
                project_id="project-manual-evidence",
                draft=_draft(
                    method_version="operator-invented-version",
                    observed_at=observed_at,
                ),
                request_id="request-invalid-method",
                reason="Attempt an ungoverned manual correction",
            )

        observation = service.record_observation(
            actor=author,
            project_id="project-manual-evidence",
            draft=_draft(
                method_version=context.policy_version_id,
                observed_at=observed_at,
            ),
            request_id="request-record-manual-evidence",
            reason="Correct the extracted pipeline diameter from the confirmed drawing",
        )
        assert observation.status is VerificationStatus.UNVERIFIED
        assert observation.method is EvidenceMethod.MANUAL
        task = session.query(ApprovalTaskRow).filter_by(entity_id=observation.observation_id).one()
        assert task.task_type == "MANUAL_EVIDENCE_REVIEW"
        assert task.assigned_role == ActorRole.REVIEWER.value
        assert task.payload["source_observation_hash"]
        assert task.payload["document_set_revision_id"] == "document-set-v1"

        author_review = service.manual_evidence_review(
            actor=author,
            project_id="project-manual-evidence",
            observation_id=observation.observation_id,
        )
        assert not author_review.decision_allowed
        assert "FOUR_EYES_SOURCE_AUTHOR" in author_review.decision_blockers
        assert "FOUR_EYES_TASK_CREATOR" in author_review.decision_blockers

        independent_review = service.manual_evidence_review(
            actor=reviewer,
            project_id="project-manual-evidence",
            observation_id=observation.observation_id,
        )
        assert independent_review.decision_allowed
        assert independent_review.decision_blockers == ()

        with pytest.raises(ValueError, match="dedicated workflow"):
            ApprovalService(
                session=session,
                settings=settings,
                object_store=store,
            ).decide(
                actor=reviewer,
                project_id="project-manual-evidence",
                task_id=independent_review.task_id,
                command=ApprovalDecisionCommand(
                    decision=ApprovalDecision.APPROVED,
                    reason="Attempt the generic approval path",
                    expected_task_updated_at=independent_review.task_updated_at,
                    evidence_ids=(observation.observation_id,),
                ),
                request_id="request-generic-approval",
            )

        with pytest.raises(OptimisticLockError):
            service.decide_manual_evidence(
                actor=reviewer,
                project_id="project-manual-evidence",
                observation_id=observation.observation_id,
                command=ManualEvidenceDecisionCommand(
                    decision=ApprovalDecision.APPROVED,
                    reason="Attempt with a stale task timestamp",
                    expected_task_updated_at=(
                        independent_review.task_updated_at + timedelta(seconds=1)
                    ),
                ),
                request_id="request-stale-review",
            )

        result = service.decide_manual_evidence(
            actor=reviewer,
            project_id="project-manual-evidence",
            observation_id=observation.observation_id,
            command=ManualEvidenceDecisionCommand(
                decision=ApprovalDecision.APPROVED,
                reason="Confirmed against drawing revision A and its exact source locator",
                expected_task_updated_at=independent_review.task_updated_at,
            ),
            request_id="request-approve-manual-evidence",
        )
        assert result.verified_observation is not None
        assert result.verified_observation.status is VerificationStatus.VERIFIED
        assert result.verified_observation.method is EvidenceMethod.RULE_ENGINE
        assert result.verified_observation.method_version == "manual-evidence-policy-v1"
        assert result.verified_observation.actor_id == reviewer.actor_id

        source = session.get(ObservationRow, observation.observation_id)
        verified = session.get(
            ObservationRow,
            result.verified_observation.observation_id,
        )
        approval = session.get(ApprovalRecordRow, result.approval_id)
        decided_task = session.get(ApprovalTaskRow, result.review.task_id)
        assert source is not None
        assert source.status == VerificationStatus.UNVERIFIED.value
        assert verified is not None
        assert verified.status == VerificationStatus.VERIFIED.value
        assert verified.payload["source_observation_ids"] == [source.id]
        assert verified.payload["approval_record_id"] == result.approval_id
        assert approval is not None
        assert approval.decided_by == reviewer.actor_id
        assert decided_task is not None
        assert decided_task.status == ApprovalDecision.APPROVED.value

        with pytest.raises(ValueError, match="TASK_NOT_PENDING"):
            service.decide_manual_evidence(
                actor=reviewer,
                project_id="project-manual-evidence",
                observation_id=observation.observation_id,
                command=ManualEvidenceDecisionCommand(
                    decision=ApprovalDecision.APPROVED,
                    reason="Attempt to approve the same immutable task twice",
                    expected_task_updated_at=result.review.task_updated_at,
                ),
                request_id="request-repeat-review",
            )


def test_manual_evidence_review_fails_closed_after_document_set_drift(
    tmp_path: Path,
) -> None:
    settings, factory, store, actors, _engine = _setup(tmp_path)
    author, reviewer, _creator, _approver = actors

    with factory.begin() as session:
        service = EvidenceService(
            session=session,
            settings=settings,
            object_store=store,
        )
        observation = service.record_observation(
            actor=author,
            project_id="project-manual-evidence",
            draft=_draft(
                method_version="manual-evidence-policy-v1",
                observed_at=datetime(2026, 7, 24, 10, 0, tzinfo=UTC),
            ),
            request_id="request-record-before-drift",
            reason="Record against the current confirmed drawing",
        )
        project = session.get(ProjectRow, "project-manual-evidence")
        assert project is not None
        project.current_document_set_revision_id = "document-set-v2"
        project.row_version += 1

        review = service.manual_evidence_review(
            actor=reviewer,
            project_id=project.id,
            observation_id=observation.observation_id,
        )
        assert not review.decision_allowed
        assert "MANUAL_EVIDENCE_SCOPE_MISMATCH" in review.decision_blockers
        assert "TASK_INTEGRITY_FAILED" in review.decision_blockers

        with pytest.raises(ValueError, match="MANUAL_EVIDENCE_SCOPE_MISMATCH"):
            service.decide_manual_evidence(
                actor=reviewer,
                project_id=project.id,
                observation_id=observation.observation_id,
                command=ManualEvidenceDecisionCommand(
                    decision=ApprovalDecision.APPROVED,
                    reason="A stale document basis must not be approvable",
                    expected_task_updated_at=review.task_updated_at,
                ),
                request_id="request-review-after-drift",
            )


def test_manual_evidence_api_exposes_only_the_dedicated_review_path(
    tmp_path: Path,
) -> None:
    settings, _factory, store, _actors, engine = _setup(tmp_path)
    app = create_app(
        settings,
        engine=engine,
        object_store=store,
        quarantine_store=LocalObjectStore(tmp_path / "quarantine"),
    )
    assert {
        "/v1/projects/{project_id}/evidence/manual/context",
        ("/v1/projects/{project_id}/evidence/observations/{observation_id}/manual-review"),
        ("/v1/projects/{project_id}/evidence/observations/{observation_id}/manual-review/decision"),
    } <= set(app.openapi()["paths"])
    author_headers = _headers(
        "technical-author",
        "TECHNICAL_EXPERT",
        "REVIEWER",
    )
    reviewer_headers = _headers("independent-reviewer", "REVIEWER")

    with TestClient(app) as client:
        context = client.get(
            "/v1/projects/project-manual-evidence/evidence/manual/context",
            headers=author_headers,
        )
        assert context.status_code == 200, context.text
        assert context.json()["policy_version_id"] == "manual-evidence-policy-v1"

        recorded = client.post(
            "/v1/projects/project-manual-evidence/evidence/observations",
            headers={
                **author_headers,
                "Idempotency-Key": "manual-evidence-api-record",
            },
            json={
                "draft": {
                    "field_name": "pipeline.nominal_diameter",
                    "value": "500.00",
                    "unit": "mm",
                    "method": "MANUAL",
                    "method_version": "manual-evidence-policy-v1",
                    "source_priority": 10,
                    "location": {
                        "document_id": "document-v1",
                        "document_revision_id": "revision-v1",
                        "original_object_hash": "a" * 64,
                        "locator_kind": "PDF_PAGE_REGION",
                        "locator": "page=12;x=0.10;y=0.22;width=0.40;height=0.08",
                        "page": 12,
                        "table": None,
                        "sheet": None,
                        "cell_or_range": None,
                    },
                    "observed_at": "2026-07-24T10:30:00+03:00",
                    "confidence": None,
                    "adapter_qualification_id": None,
                    "basis_metadata": {},
                },
                "reason": "Correct the diameter after checking drawing revision A",
            },
        )
        assert recorded.status_code == 201, recorded.text
        observation_id = str(recorded.json()["observation_id"])

        own_review = client.get(
            (
                "/v1/projects/project-manual-evidence/evidence/observations/"
                f"{observation_id}/manual-review"
            ),
            headers=author_headers,
        )
        assert own_review.status_code == 200, own_review.text
        assert own_review.json()["decision_allowed"] is False
        assert "FOUR_EYES_SOURCE_AUTHOR" in own_review.json()["decision_blockers"]

        independent = client.get(
            (
                "/v1/projects/project-manual-evidence/evidence/observations/"
                f"{observation_id}/manual-review"
            ),
            headers=reviewer_headers,
        )
        assert independent.status_code == 200, independent.text
        assert independent.json()["decision_allowed"] is True
        assert independent.json()["submission_reason"].startswith("Correct the diameter")

        decided = client.post(
            (
                "/v1/projects/project-manual-evidence/evidence/observations/"
                f"{observation_id}/manual-review/decision"
            ),
            headers={
                **reviewer_headers,
                "Idempotency-Key": "manual-evidence-api-review",
            },
            json={
                "decision": "APPROVED",
                "reason": "Independently verified the exact drawing region and unit",
                "expected_task_updated_at": independent.json()["task_updated_at"],
            },
        )
        assert decided.status_code == 200, decided.text
        assert decided.json()["verified_observation"]["status"] == "VERIFIED"
        assert decided.json()["verified_observation"]["method"] == "RULE_ENGINE"


def _setup(
    tmp_path: Path,
) -> tuple[
    Settings,
    sessionmaker[Session],
    LocalObjectStore,
    tuple[Actor, Actor, Actor, Actor],
    Engine,
]:
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
    author = Actor(
        "technical-author",
        "org-1",
        frozenset({ActorRole.TECHNICAL_EXPERT, ActorRole.REVIEWER}),
    )
    reviewer = Actor(
        "independent-reviewer",
        "org-1",
        frozenset({ActorRole.REVIEWER}),
    )
    creator = Actor(
        "methodology-creator",
        "org-1",
        frozenset({ActorRole.METHODOLOGY_OWNER, ActorRole.ESTIMATOR}),
    )
    approver = Actor(
        "methodology-approver",
        "org-1",
        frozenset({ActorRole.METHODOLOGY_OWNER}),
    )
    actors = (author, reviewer, creator, approver)
    now = datetime(2026, 7, 24, 8, 0, tzinfo=UTC)
    object_hash_v1 = "a" * 64
    object_hash_v2 = "b" * 64

    with factory.begin() as session:
        session.add(
            ProjectRow(
                id="project-manual-evidence",
                organization_id="org-1",
                code="MANUAL-EVIDENCE-1",
                name="Manual evidence review",
                state=ApprovalState.EXTRACTION_REVIEW.value,
                current_document_set_revision_id="document-set-v1",
                row_version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.add_all(
            project_memberships(
                "project-manual-evidence",
                actors,
                owner_id=creator.actor_id,
                now=now,
            )
        )
        session.add_all(
            (
                DocumentRow(
                    id="document-v1",
                    project_id="project-manual-evidence",
                    logical_key="drawing-main",
                    title="Main pipeline drawing",
                    document_type="DRAWING",
                    critical=True,
                    cancelled=False,
                    created_at=now,
                    updated_at=now,
                ),
                DocumentRow(
                    id="document-v2",
                    project_id="project-manual-evidence",
                    logical_key="drawing-replacement",
                    title="Replacement pipeline drawing",
                    document_type="DRAWING",
                    critical=True,
                    cancelled=False,
                    created_at=now,
                    updated_at=now,
                ),
            )
        )
        session.flush()
        session.add_all(
            (
                _revision(
                    revision_id="revision-v1",
                    document_id="document-v1",
                    revision_label="A",
                    object_hash=object_hash_v1,
                    now=now,
                ),
                _revision(
                    revision_id="revision-v2",
                    document_id="document-v2",
                    revision_label="B",
                    object_hash=object_hash_v2,
                    now=now,
                ),
                _document_set(
                    set_id="document-set-v1",
                    revision_ids=["revision-v1"],
                    now=now,
                ),
                _document_set(
                    set_id="document-set-v2",
                    revision_ids=["revision-v2"],
                    now=now,
                ),
            )
        )
        policy = ControlledVersionRow(
            id="manual-evidence-policy-v1",
            kind="manual_evidence_policy",
            version_label="1.0.0",
            content_hash="0" * 64,
            status="DRAFT",
            payload={
                "review_role": ActorRole.REVIEWER.value,
                "allowed_project_states": [
                    ApprovalState.EXTRACTION_IN_PROGRESS.value,
                    ApprovalState.EXTRACTION_REVIEW.value,
                    ApprovalState.BOQ_IN_PROGRESS.value,
                    ApprovalState.BOQ_REVIEW.value,
                ],
            },
        )
        add_governed_controlled_version(
            session=session,
            settings=settings,
            object_store=store,
            row=policy,
            organization_id="org-1",
            creator=creator,
            approver=approver,
        )
        session.add(
            ProjectControlledVersionRow(
                project_id="project-manual-evidence",
                controlled_version_id=policy.id,
                purpose="manual_evidence_policy",
                bound_by=creator.actor_id,
                bound_at=now,
            )
        )

    return settings, factory, store, actors, engine


def _headers(actor_id: str, *roles: str) -> dict[str, str]:
    return {
        "X-Dev-Actor": actor_id,
        "X-Dev-Organization": "org-1",
        "X-Dev-Roles": ",".join(roles),
    }


def _revision(
    *,
    revision_id: str,
    document_id: str,
    revision_label: str,
    object_hash: str,
    now: datetime,
) -> DocumentRevisionRow:
    return DocumentRevisionRow(
        id=revision_id,
        document_id=document_id,
        revision_label=revision_label,
        issue_date=now.date(),
        object_hash=object_hash,
        object_key=f"sha256/{object_hash}",
        original_filename=f"{document_id}.pdf",
        media_type="application/pdf",
        size_bytes=1024,
        supersedes_revision_id=None,
        is_current=True,
        corrupt=False,
        protected=False,
        inspection_payload={},
        created_at=now,
        updated_at=now,
    )


def _document_set(
    *,
    set_id: str,
    revision_ids: list[str],
    now: datetime,
) -> DocumentSetRevisionRow:
    return DocumentSetRevisionRow(
        id=set_id,
        project_id="project-manual-evidence",
        manifest_hash=content_hash(revision_ids),
        revision_ids=revision_ids,
        status="CONFIRMED",
        created_by="technical-author",
        created_at=now,
        confirmed_by="independent-reviewer",
        confirmed_at=now,
    )


def _draft(
    *,
    method_version: str,
    observed_at: datetime,
) -> ObservationDraft:
    return ObservationDraft(
        field_name="pipeline.nominal_diameter",
        value="DN500",
        unit=None,
        method=EvidenceMethod.MANUAL,
        method_version=method_version,
        source_priority=10,
        location=EvidenceLocation(
            document_id="document-v1",
            document_revision_id="revision-v1",
            original_object_hash="a" * 64,
            locator_kind="PDF_PAGE_REGION",
            locator="page=12;x=0.10;y=0.22;width=0.40;height=0.08",
            page=12,
            table=None,
            sheet=None,
            cell_or_range=None,
        ),
        observed_at=observed_at,
        confidence=None,
        adapter_qualification_id=None,
        basis_metadata={},
    )
