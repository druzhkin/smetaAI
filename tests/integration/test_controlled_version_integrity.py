from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from tenderguard.application.controlled_version_integrity import (
    controlled_version_integrity_valid,
    require_bound_controlled_version,
    require_controlled_version_integrity,
)
from tenderguard.application.governance import GovernanceService
from tenderguard.config import Settings
from tenderguard.domain.common import utc_now
from tenderguard.domain.enums import ActorRole, ApprovalState
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.database import (
    create_database_engine,
    create_schema_for_tests,
    create_session_factory,
)
from tenderguard.infrastructure.object_store import LocalObjectStore
from tenderguard.infrastructure.orm import (
    ControlledVersionRow,
    ProjectRow,
)
from tests.integration.support import add_project_controlled_version_binding


def test_controlled_version_integrity_blocks_preapproval_and_postapproval_tampering(
    tmp_path: Path,
) -> None:
    settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite://",
        local_object_store_path=tmp_path / "objects",
        audit_signing_key="controlled-version-audit-key-at-least-32-bytes",
    )
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    sessions = create_session_factory(engine)
    store = LocalObjectStore(tmp_path / "objects")
    creator = Actor(
        "methodology-creator",
        "org-controlled-version",
        frozenset({ActorRole.METHODOLOGY_OWNER}),
    )
    approver = Actor(
        "methodology-approver",
        "org-controlled-version",
        frozenset({ActorRole.METHODOLOGY_OWNER}),
    )

    with sessions.begin() as session:
        governance = GovernanceService(
            session=session,
            settings=settings,
            object_store=store,
        )
        version = governance.create_version(
            actor=creator,
            kind="calculation_model",
            version_label="integrity-v1",
            payload={
                "policy": {
                    "currency": "RUB",
                    "line_rounding_scale": 2,
                }
            },
            request_id="request-create-integrity-version",
            reason="Create an immutable governed calculation model",
        )
        row = session.get(ControlledVersionRow, version.version_id)
        assert row is not None
        require_controlled_version_integrity(
            session=session,
            settings=settings,
            row=row,
            expected_organization_id=creator.organization_id,
            required_status="DRAFT",
        )
        original_payload = deepcopy(row.payload)
        row.payload = {
            **row.payload,
            "policy": {
                **row.payload["policy"],
                "line_rounding_scale": 9,
            },
        }
        with pytest.raises(ValueError, match="content hash"):
            governance.approve_version(
                actor=approver,
                version_id=row.id,
                request_id="request-approve-tampered-version",
                reason="A changed draft must not be approved",
            )
        row.payload = original_payload
        approved = governance.approve_version(
            actor=approver,
            version_id=row.id,
            request_id="request-approve-integrity-version",
            reason="Independently approve the exact governed content",
        )
        assert controlled_version_integrity_valid(
            session=session,
            settings=settings,
            row=row,
            expected_organization_id=creator.organization_id,
            expected_content_hash=approved.content_hash,
        )
        now = utc_now()
        session.add(
            ProjectRow(
                id="project-controlled-version",
                organization_id=creator.organization_id,
                code="CONTROLLED-VERSION-1",
                name="Controlled version integrity",
                state=ApprovalState.DRAFT.value,
                current_document_set_revision_id=None,
                row_version=1,
                created_at=now,
                updated_at=now,
            )
        )
        binding = add_project_controlled_version_binding(
            session=session,
            settings=settings,
            object_store=store,
            project_id="project-controlled-version",
            version=row,
            purpose="calculation_model",
            actor=approver,
        )
        assert (
            require_bound_controlled_version(
                session=session,
                settings=settings,
                project_id="project-controlled-version",
                organization_id=creator.organization_id,
                purpose="calculation_model",
                kind="calculation_model",
                expected_version_id=row.id,
            )
            is row
        )
        with pytest.raises(ValueError, match="requested version"):
            require_bound_controlled_version(
                session=session,
                settings=settings,
                project_id="project-controlled-version",
                organization_id=creator.organization_id,
                purpose="calculation_model",
                kind="calculation_model",
                expected_version_id="client-substituted-version",
            )
        binding.bound_by = creator.actor_id
        with pytest.raises(ValueError, match="binding audit chain"):
            require_bound_controlled_version(
                session=session,
                settings=settings,
                project_id="project-controlled-version",
                organization_id=creator.organization_id,
                purpose="calculation_model",
                kind="calculation_model",
            )
        binding.bound_by = approver.actor_id

        row.approved_by = creator.actor_id
        assert not controlled_version_integrity_valid(
            session=session,
            settings=settings,
            row=row,
            expected_organization_id=creator.organization_id,
        )
        with pytest.raises(ValueError, match="approval"):
            require_bound_controlled_version(
                session=session,
                settings=settings,
                project_id="project-controlled-version",
                organization_id=creator.organization_id,
                purpose="calculation_model",
                kind="calculation_model",
            )

    engine.dispose()
