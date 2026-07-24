from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from tenderguard.application.passport import PassportFactDraft, PassportService
from tenderguard.application.projects import ProjectService
from tenderguard.config import Settings
from tenderguard.domain.enums import ActorRole, ApprovalState, EvidenceMethod
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.database import (
    create_database_engine,
    create_schema_for_tests,
    create_session_factory,
)
from tenderguard.infrastructure.object_store import LocalObjectStore
from tenderguard.infrastructure.orm import (
    AdapterQualificationRow,
    ControlledVersionRow,
    ObservationRow,
    ProjectControlledVersionRow,
    ProjectPassportFactRow,
    ProjectRow,
)
from tests.integration.support import project_memberships


def test_passport_requires_independent_evidence_and_four_eyes_before_boq(
    tmp_path: Path,
) -> None:
    settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite://",
        local_object_store_path=tmp_path / "objects",
        audit_signing_key="test-audit-signing-key-at-least-32-bytes",
    )
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    factory = create_session_factory(engine)
    store = LocalObjectStore(tmp_path / "objects")
    submitter = Actor(
        "technical-1",
        "org-1",
        frozenset({ActorRole.TECHNICAL_EXPERT}),
    )
    verifier = Actor("reviewer-1", "org-1", frozenset({ActorRole.REVIEWER}))
    now = datetime(2026, 7, 23, tzinfo=UTC)

    with factory.begin() as session:
        requirements = ControlledVersionRow(
            id="document-requirements-v1",
            kind="document_requirements",
            version_label="1",
            content_hash="a" * 64,
            status="APPROVED",
            payload={
                "passport": {
                    "required_fields": ["object_address"],
                    "independently_verified_fields": ["object_address"],
                }
            },
            approved_by="methodology-owner",
            approved_at=now,
        )
        session.add_all(
            (
                ProjectRow(
                    id="project-passport",
                    organization_id="org-1",
                    code="PASSPORT-1",
                    name="Passport workflow",
                    state=ApprovalState.EXTRACTION_REVIEW.value,
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
                requirements,
                ProjectControlledVersionRow(
                    project_id="project-passport",
                    controlled_version_id=requirements.id,
                    purpose="document_requirements",
                    bound_by="methodology-owner",
                    bound_at=now,
                ),
            )
        )
        for suffix, method, domain in (
            ("parser", EvidenceMethod.TABLE_PARSER, "parser-domain"),
            ("visual", EvidenceMethod.VISUAL_MODEL, "visual-domain"),
        ):
            qualification_id = f"qualification-{suffix}"
            session.add(
                AdapterQualificationRow(
                    id=qualification_id,
                    adapter_name=f"adapter-{suffix}",
                    adapter_version="1",
                    status="APPROVED",
                    valid_until=date(2027, 7, 23),
                    test_evidence_hash=suffix[0] * 64,
                    payload={
                        "independence_domain": domain,
                        "organization_id": "org-1",
                        "supported_methods": [method.value],
                    },
                    approved_by="methodology-owner",
                    approved_at=now,
                )
            )
            session.add(
                ObservationRow(
                    id=f"observation-{suffix}",
                    project_id="project-passport",
                    document_revision_id=f"revision-{suffix}",
                    field_name="object_address",
                    method=method.value,
                    method_version="1",
                    status="UNVERIFIED",
                    payload={
                        "observation": {
                            "value": "Moscow, Test Street 1",
                            "unit": None,
                        },
                        "adapter_qualification_id": qualification_id,
                    },
                    created_at=now,
                )
            )

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

        fact = passport_service.submit_fact(
            actor=submitter,
            project_id="project-passport",
            draft=PassportFactDraft(
                field_name="object_address",
                value="Moscow, Test Street 1",
                observation_ids=("observation-parser", "observation-visual"),
            ),
            request_id="request-submit-fact",
            reason="Submit independently extracted address",
        )
        with pytest.raises(ValueError, match="different actor"):
            passport_service.verify_fact(
                actor=submitter,
                project_id="project-passport",
                fact_id=fact.fact_id,
                request_id="request-self-review",
                reason="Invalid self-review",
            )
        verified, validation = passport_service.verify_fact(
            actor=verifier,
            project_id="project-passport",
            fact_id=fact.fact_id,
            request_id="request-verify-fact",
            reason="Independent fact review",
        )
        assert verified.status.value == "VERIFIED"
        assert not validation.findings
        transitioned = project_service.transition(
            actor=submitter,
            project_id="project-passport",
            to_state=ApprovalState.BOQ_IN_PROGRESS,
            expected_row_version=1,
            request_id="request-transition",
            reason="Passport gate is satisfied",
        )
        assert transitioned.state is ApprovalState.BOQ_IN_PROGRESS
        persisted = session.get(ProjectPassportFactRow, fact.fact_id)
        assert persisted is not None and persisted.is_current

    engine.dispose()
