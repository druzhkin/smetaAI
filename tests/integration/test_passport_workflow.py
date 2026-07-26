from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tenderguard.api.main import create_app
from tenderguard.application.passport import (
    PassportFactDecisionCommand,
    PassportFactDraft,
    PassportService,
)
from tenderguard.application.projects import OptimisticLockError, ProjectService
from tenderguard.config import Settings
from tenderguard.domain.common import content_hash
from tenderguard.domain.enums import (
    ActorRole,
    ApprovalDecision,
    ApprovalState,
    EvidenceMethod,
    VerificationStatus,
)
from tenderguard.domain.models import EvidenceLocation, Observation
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.database import (
    create_database_engine,
    create_schema_for_tests,
    create_session_factory,
)
from tenderguard.infrastructure.object_store import LocalObjectStore
from tenderguard.infrastructure.orm import (
    AdapterQualificationRow,
    ApprovalTaskRow,
    ConflictRow,
    ControlledVersionRow,
    DocumentRevisionRow,
    DocumentRow,
    DocumentSetRevisionRow,
    ObservationRow,
    ProjectPassportFactRow,
    ProjectRow,
)
from tests.integration.support import (
    add_document_set_confirmation_audit,
    add_governed_controlled_version,
    add_project_controlled_version_binding,
    project_memberships,
)


def test_passport_requires_independent_evidence_and_four_eyes_before_boq(
    tmp_path: Path,
) -> None:
    settings = Settings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{(tmp_path / 'passport.db').as_posix()}",
        local_object_store_path=tmp_path / "objects",
        allow_insecure_dev_auth=True,
        audit_signing_key="test-audit-signing-key-at-least-32-bytes",
    )
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    factory = create_session_factory(engine)
    store = LocalObjectStore(tmp_path / "objects")
    submitter = Actor(
        "technical-1",
        "org-1",
        frozenset({ActorRole.TECHNICAL_EXPERT, ActorRole.REVIEWER}),
    )
    verifier = Actor("reviewer-1", "org-1", frozenset({ActorRole.REVIEWER}))
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

    with factory.begin() as session:
        requirements = ControlledVersionRow(
            id="document-requirements-v1",
            kind="document_requirements",
            version_label="1",
            content_hash="",
            status="DRAFT",
            payload={
                "passport": {
                    "required_fields": ["object_address"],
                    "independently_verified_fields": ["object_address"],
                    "optional_fields": ["project_name"],
                    "review_role": "REVIEWER",
                }
            },
            approved_by=None,
            approved_at=None,
        )
        revision_ids = ["revision-parser", "revision-visual"]
        document_set = DocumentSetRevisionRow(
            id="document-set-passport",
            project_id="project-passport",
            manifest_hash=content_hash(revision_ids),
            revision_ids=revision_ids,
            status="CONFIRMED",
            created_by=submitter.actor_id,
            created_at=now,
            confirmed_by=verifier.actor_id,
            confirmed_at=now,
        )
        session.add_all(
            (
                ProjectRow(
                    id="project-passport",
                    organization_id="org-1",
                    code="PASSPORT-1",
                    name="Passport workflow",
                    state=ApprovalState.EXTRACTION_REVIEW.value,
                    current_document_set_revision_id=document_set.id,
                    row_version=1,
                    created_at=now,
                    updated_at=now,
                ),
                *project_memberships(
                    "project-passport",
                    (submitter, verifier),
                    owner_id=submitter.actor_id,
                    now=now,
                ),
                document_set,
            )
        )
        session.flush()
        add_governed_controlled_version(
            session=session,
            settings=settings,
            object_store=store,
            row=requirements,
            organization_id="org-1",
            creator=methodology_creator,
            approver=methodology_approver,
        )
        add_project_controlled_version_binding(
            session=session,
            settings=settings,
            object_store=store,
            project_id="project-passport",
            version=requirements,
            purpose="document_requirements",
            actor=methodology_approver,
        )
        add_document_set_confirmation_audit(
            session=session,
            settings=settings,
            object_store=store,
            row=document_set,
            actor=verifier,
        )
        for suffix, method, domain in (
            ("parser", EvidenceMethod.TABLE_PARSER, "parser-domain"),
            ("visual", EvidenceMethod.VISUAL_MODEL, "visual-domain"),
        ):
            document_id = f"document-{suffix}"
            revision_id = f"revision-{suffix}"
            object_hash = ("a" if suffix == "parser" else "b") * 64
            service_actor_id = f"service-{suffix}"
            session.add(
                DocumentRow(
                    id=document_id,
                    project_id="project-passport",
                    logical_key=document_id,
                    title=f"Passport evidence {suffix}",
                    document_type="PROJECT_DOCUMENT",
                    critical=True,
                    cancelled=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                DocumentRevisionRow(
                    id=revision_id,
                    document_id=document_id,
                    revision_label="1",
                    issue_date=date(2026, 7, 23),
                    object_hash=object_hash,
                    object_key=f"objects/{object_hash}",
                    original_filename=f"{suffix}.pdf",
                    media_type="application/pdf",
                    size_bytes=100,
                    supersedes_revision_id=None,
                    is_current=True,
                    corrupt=False,
                    protected=False,
                    inspection_payload={},
                    created_at=now,
                    updated_at=now,
                )
            )
            qualification_id = f"qualification-{suffix}"
            session.add(
                AdapterQualificationRow(
                    id=qualification_id,
                    adapter_name=f"adapter-{suffix}",
                    adapter_version="1",
                    status="APPROVED",
                    valid_until=date(2027, 7, 23),
                    test_evidence_hash=object_hash,
                    payload={
                        "independence_domain": domain,
                        "organization_id": "org-1",
                        "service_actor_id": service_actor_id,
                        "supported_methods": [method.value],
                    },
                    approved_by="methodology-owner",
                    approved_at=now,
                )
            )
            observation = Observation(
                observation_id=f"observation-{suffix}",
                field_name="object_address",
                value="Moscow, Test Street 1",
                unit=None,
                method=method,
                method_version="1",
                source_priority=1,
                location=EvidenceLocation(
                    document_id=document_id,
                    document_revision_id=revision_id,
                    original_object_hash=object_hash,
                    locator_kind="table",
                    locator=f"address:{suffix}",
                    page=1,
                ),
                observed_at=now,
                actor_id=service_actor_id,
            )
            session.add(
                ObservationRow(
                    id=observation.observation_id,
                    project_id="project-passport",
                    document_revision_id=revision_id,
                    field_name=observation.field_name,
                    method=observation.method.value,
                    method_version=observation.method_version,
                    status=observation.status.value,
                    payload={
                        "observation": observation.model_dump(mode="json"),
                        "adapter_qualification_id": qualification_id,
                    },
                    created_at=now,
                )
            )
        manual_observation = Observation(
            observation_id="observation-manual-project-name",
            field_name="project_name",
            value="Unchecked manual project name",
            unit=None,
            method=EvidenceMethod.MANUAL,
            method_version="manual-evidence-policy-v1",
            source_priority=5,
            location=EvidenceLocation(
                document_id="document-parser",
                document_revision_id="revision-parser",
                original_object_hash="a" * 64,
                locator_kind="page",
                locator="page:1",
                page=1,
            ),
            observed_at=now,
            actor_id=submitter.actor_id,
        )
        session.add(
            ObservationRow(
                id=manual_observation.observation_id,
                project_id="project-passport",
                document_revision_id="revision-parser",
                field_name=manual_observation.field_name,
                method=manual_observation.method.value,
                method_version=manual_observation.method_version,
                status=manual_observation.status.value,
                payload={"observation": manual_observation.model_dump(mode="json")},
                created_at=now,
            )
        )
        derived_observation = Observation(
            observation_id="observation-derived-address",
            field_name="object_address",
            value="Moscow, Test Street 1",
            unit=None,
            method=EvidenceMethod.RULE_ENGINE,
            method_version="reconciliation-rules-v1",
            source_priority=1,
            location=EvidenceLocation(
                document_id="document-parser",
                document_revision_id="revision-parser",
                original_object_hash="a" * 64,
                locator_kind="derived",
                locator="reconciliation:address",
                page=1,
            ),
            observed_at=now,
            actor_id=verifier.actor_id,
            status=VerificationStatus.VERIFIED,
        )
        session.add(
            ObservationRow(
                id=derived_observation.observation_id,
                project_id="project-passport",
                document_revision_id="revision-parser",
                field_name=derived_observation.field_name,
                method=derived_observation.method.value,
                method_version=derived_observation.method_version,
                status=derived_observation.status.value,
                payload={
                    "observation": derived_observation.model_dump(mode="json"),
                    "source_observation_ids": [
                        "observation-parser",
                        "observation-visual",
                    ],
                },
                created_at=now,
            )
        )
        for cycle_id, source_id in (
            ("observation-cycle-a", "observation-cycle-b"),
            ("observation-cycle-b", "observation-cycle-a"),
        ):
            cycle_observation = Observation(
                observation_id=cycle_id,
                field_name="project_name",
                value="Cyclic derived project name",
                unit=None,
                method=EvidenceMethod.RULE_ENGINE,
                method_version="broken-derived-graph-v1",
                source_priority=1,
                location=EvidenceLocation(
                    document_id="document-parser",
                    document_revision_id="revision-parser",
                    original_object_hash="a" * 64,
                    locator_kind="derived",
                    locator=f"cycle:{cycle_id}",
                    page=1,
                ),
                observed_at=now,
                actor_id=verifier.actor_id,
                status=VerificationStatus.VERIFIED,
            )
            session.add(
                ObservationRow(
                    id=cycle_id,
                    project_id="project-passport",
                    document_revision_id="revision-parser",
                    field_name=cycle_observation.field_name,
                    method=cycle_observation.method.value,
                    method_version=cycle_observation.method_version,
                    status=cycle_observation.status.value,
                    payload={
                        "observation": cycle_observation.model_dump(mode="json"),
                        "source_observation_ids": [source_id],
                    },
                    created_at=now,
                )
            )

    headers = {
        "X-Dev-Actor": verifier.actor_id,
        "X-Dev-Organization": verifier.organization_id,
        "X-Dev-Roles": "REVIEWER",
    }
    with TestClient(create_app(settings)) as client:
        context_response = client.get(
            "/v1/projects/project-passport/passport/context",
            headers=headers,
            params={"field_name": "object_address"},
        )
        assert context_response.status_code == 200, context_response.text
        assert context_response.json()["requirements_version_id"] == ("document-requirements-v1")
        validation_response = client.post(
            "/v1/projects/project-passport/passport/validate",
            headers={
                **headers,
                "Idempotency-Key": "passport-api-validation-1",
            },
            json={"reason": "Persist the initial missing-field passport gate"},
        )
        assert validation_response.status_code == 200, validation_response.text
        assert len(validation_response.json()["findings"]) == 1

    with factory.begin() as session:
        passport_service = PassportService(
            session=session,
            settings=settings,
            object_store=store,
        )
        project_service = ProjectService(
            session=session,
            settings=settings,
            object_store=store,
        )
        initial = passport_service.validate_current(
            actor=verifier,
            project_id="project-passport",
            request_id="request-initial-validation",
            reason="Check required passport fields",
        )
        assert len(initial.findings) == 1
        with pytest.raises(ValueError, match="project passport stage gate"):
            project_service.transition(
                actor=submitter,
                project_id="project-passport",
                to_state=ApprovalState.BOQ_IN_PROGRESS,
                expected_row_version=1,
                request_id="request-premature-transition",
                reason="Attempt before passport verification",
            )

        context = passport_service.context(
            actor=submitter,
            project_id="project-passport",
            selected_field_name="object_address",
            limit=100,
        )
        direct_candidates = tuple(
            candidate
            for candidate in context.evidence_candidates
            if candidate.observation.observation_id in {"observation-parser", "observation-visual"}
        )
        assert len(direct_candidates) == 2
        assert all(candidate.eligible for candidate in direct_candidates)
        derived_candidate = next(
            candidate
            for candidate in context.evidence_candidates
            if candidate.observation.observation_id == "observation-derived-address"
        )
        assert not derived_candidate.eligible
        manual_context = passport_service.context(
            actor=submitter,
            project_id="project-passport",
            selected_field_name="project_name",
            limit=100,
        )
        manual_candidate = next(
            candidate
            for candidate in manual_context.evidence_candidates
            if candidate.observation.observation_id == "observation-manual-project-name"
        )
        assert not manual_candidate.eligible
        assert "MANUAL_EVIDENCE_REVIEW_REQUIRED" in manual_candidate.blockers
        with pytest.raises(ValueError, match="dedicated review"):
            passport_service.submit_fact(
                actor=submitter,
                project_id="project-passport",
                draft=PassportFactDraft(
                    field_name="project_name",
                    value="Unchecked manual project name",
                    observation_ids=("observation-manual-project-name",),
                ),
                expected_document_set_revision_id=context.document_set_revision_id,
                requirements_version_id=context.requirements_version_id,
                request_id="request-unreviewed-manual-passport",
                reason="Prove raw manual evidence cannot bypass its dedicated review",
            )
        with pytest.raises(ValueError, match="contains a cycle"):
            passport_service.submit_fact(
                actor=submitter,
                project_id="project-passport",
                draft=PassportFactDraft(
                    field_name="project_name",
                    value="Cyclic derived project name",
                    observation_ids=("observation-cycle-a",),
                ),
                expected_document_set_revision_id=context.document_set_revision_id,
                requirements_version_id=context.requirements_version_id,
                request_id="request-cyclic-passport-evidence",
                reason="Prove a cyclic derived graph cannot become a passport fact",
            )
        visual_row = session.get(ObservationRow, "observation-visual")
        assert visual_row is not None
        original_visual_payload = visual_row.payload
        visual_row.payload = {
            **original_visual_payload,
            "observation": {
                **original_visual_payload["observation"],
                "value": "Conflicting hidden leaf value",
            },
        }
        with pytest.raises(ValueError, match="do not reproduce"):
            passport_service.submit_fact(
                actor=submitter,
                project_id="project-passport",
                draft=PassportFactDraft(
                    field_name="object_address",
                    value="Moscow, Test Street 1",
                    observation_ids=("observation-derived-address",),
                ),
                expected_document_set_revision_id=context.document_set_revision_id,
                requirements_version_id=context.requirements_version_id,
                request_id="request-derived-mismatch",
                reason="Prove a derived observation cannot hide disagreeing leaves",
            )
        visual_row.payload = original_visual_payload
        with pytest.raises(OptimisticLockError, match="document set changed"):
            passport_service.submit_fact(
                actor=submitter,
                project_id="project-passport",
                draft=PassportFactDraft(
                    field_name="object_address",
                    value="Moscow, Test Street 1",
                    observation_ids=("observation-parser", "observation-visual"),
                ),
                expected_document_set_revision_id="stale-document-set",
                requirements_version_id=context.requirements_version_id,
                request_id="request-stale-context",
                reason="Prove stale passport context is rejected",
            )
        fact = passport_service.submit_fact(
            actor=submitter,
            project_id="project-passport",
            draft=PassportFactDraft(
                field_name="object_address",
                value="Moscow, Test Street 1",
                observation_ids=("observation-parser", "observation-visual"),
            ),
            expected_document_set_revision_id=context.document_set_revision_id,
            requirements_version_id=context.requirements_version_id,
            request_id="request-submit-fact",
            reason="Submit independently extracted address",
        )
        task = session.get(ApprovalTaskRow, fact.approval_task_id)
        assert task is not None and task.status == "PENDING"
        review = passport_service.context(
            actor=verifier,
            project_id="project-passport",
            selected_field_name="object_address",
            limit=100,
        ).facts[0]
        assert review.decision_allowed
        author_review = passport_service.context(
            actor=submitter,
            project_id="project-passport",
            selected_field_name="object_address",
            limit=100,
        ).facts[0]
        assert not author_review.decision_allowed
        assert "FOUR_EYES_FACT_AUTHOR" in author_review.decision_blockers
        with pytest.raises(ValueError, match="different actor"):
            passport_service.verify_fact(
                actor=submitter,
                project_id="project-passport",
                fact_id=fact.fact_id,
                expected_fact_updated_at=fact.updated_at,
                expected_task_updated_at=review.task_updated_at,
                request_id="request-self-review",
                reason="Invalid self-review",
            )
        rejected = passport_service.decide_fact(
            actor=verifier,
            project_id="project-passport",
            fact_id=fact.fact_id,
            command=PassportFactDecisionCommand(
                decision=ApprovalDecision.CHANGES_REQUESTED,
                expected_fact_updated_at=fact.updated_at,
                expected_task_updated_at=review.task_updated_at,
            ),
            request_id="request-return-fact",
            reason="Return the first fact revision to prove the review branch",
        )
        assert rejected.fact.status.value == "REJECTED"
        assert task.status == "CHANGES_REQUESTED"
        replacement = passport_service.submit_fact(
            actor=submitter,
            project_id="project-passport",
            draft=PassportFactDraft(
                field_name="object_address",
                value="Moscow, Test Street 1",
                observation_ids=("observation-parser", "observation-visual"),
            ),
            expected_document_set_revision_id=context.document_set_revision_id,
            requirements_version_id=context.requirements_version_id,
            request_id="request-submit-replacement",
            reason="Submit a replacement after the explicit review return",
        )
        replacement_review = passport_service.context(
            actor=verifier,
            project_id="project-passport",
            selected_field_name="object_address",
            limit=100,
        ).facts[0]
        assert task.status == "SUPERSEDED"
        assert task.payload["superseded_by_entity_id"] == replacement.fact_id
        with pytest.raises(OptimisticLockError, match="fact changed"):
            passport_service.verify_fact(
                actor=verifier,
                project_id="project-passport",
                fact_id=replacement.fact_id,
                expected_fact_updated_at=now,
                expected_task_updated_at=replacement_review.task_updated_at,
                request_id="request-stale-review",
                reason="Prove stale fact review is rejected",
            )
        verified, validation = passport_service.verify_fact(
            actor=verifier,
            project_id="project-passport",
            fact_id=replacement.fact_id,
            expected_fact_updated_at=replacement.updated_at,
            expected_task_updated_at=replacement_review.task_updated_at,
            request_id="request-verify-fact",
            reason="Independent fact review",
        )
        assert verified.status.value == "VERIFIED"
        assert not validation.findings
        replacement_task = session.get(
            ApprovalTaskRow,
            replacement.approval_task_id,
        )
        assert replacement_task is not None and replacement_task.status == "APPROVED"
        persisted_replacement = session.get(
            ProjectPassportFactRow,
            replacement.fact_id,
        )
        assert persisted_replacement is not None
        original_payload = persisted_replacement.payload
        persisted_replacement.payload = {
            **original_payload,
            "value": "Tampered address",
        }
        with pytest.raises(ValueError, match="integrity-failed"):
            project_service.transition(
                actor=submitter,
                project_id="project-passport",
                to_state=ApprovalState.BOQ_IN_PROGRESS,
                expected_row_version=1,
                request_id="request-tampered-transition",
                reason="Prove a green status cannot hide fact tampering",
            )
        persisted_replacement.payload = original_payload
        late_conflict_savepoint = session.begin_nested()
        session.add(
            ConflictRow(
                id="conflict-late-passport",
                project_id="project-passport",
                field_name="object_address",
                status="UNRESOLVED",
                payload={"reason": "Late cross-document discrepancy"},
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        with pytest.raises(ValueError, match="unresolved-conflict"):
            project_service.transition(
                actor=submitter,
                project_id="project-passport",
                to_state=ApprovalState.BOQ_IN_PROGRESS,
                expected_row_version=1,
                request_id="request-conflicted-transition",
                reason="Prove a late conflict invalidates the passport gate",
            )
        late_conflict_savepoint.rollback()
        transitioned = project_service.transition(
            actor=submitter,
            project_id="project-passport",
            to_state=ApprovalState.BOQ_IN_PROGRESS,
            expected_row_version=1,
            request_id="request-transition",
            reason="Passport gate is satisfied",
        )
        assert transitioned.state is ApprovalState.BOQ_IN_PROGRESS
        persisted = session.get(ProjectPassportFactRow, replacement.fact_id)
        assert persisted is not None and persisted.is_current
        assert not session.get(ProjectPassportFactRow, fact.fact_id).is_current

    engine.dispose()
