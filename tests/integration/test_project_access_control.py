from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tenderguard.api.main import create_app
from tenderguard.application.projects import ProjectNotFoundError, ProjectService
from tenderguard.config import Settings
from tenderguard.domain.enums import ActorRole, ProjectAccessLevel
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.database import (
    create_database_engine,
    create_schema_for_tests,
    create_session_factory,
)
from tenderguard.infrastructure.object_store import LocalObjectStore
from tenderguard.infrastructure.orm import AuditEventRow, ProjectMembershipRow


def _headers(actor: str, roles: str) -> dict[str, str]:
    return {
        "X-Dev-Actor": actor,
        "X-Dev-Organization": "org-1",
        "X-Dev-Roles": roles,
    }


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        database_url="sqlite+pysqlite://",
        local_object_store_path=tmp_path / "objects",
        allow_insecure_dev_auth=True,
        audit_signing_key="test-audit-signing-key-at-least-32-bytes",
    )


def test_project_acl_hides_same_tenant_projects_and_preserves_membership_history(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    app = create_app(
        settings,
        engine=engine,
        object_store=LocalObjectStore(tmp_path / "objects"),
    )
    owner = _headers("owner-estimator", "ESTIMATOR")
    scoped_user = _headers("scoped-user", "REVIEWER,APPROVER")
    outsider = _headers("same-tenant-outsider", "ESTIMATOR")
    infrastructure_admin = _headers("infrastructure-admin", "ADMIN")
    system = _headers("unqualified-system", "SYSTEM")
    mixed_system = _headers("mixed-system", "SYSTEM,ESTIMATOR")

    with TestClient(app) as client:
        mixed_identity_creation = client.post(
            "/v1/projects",
            headers=mixed_system,
            json={
                "code": "ACL-MIXED",
                "name": "Forbidden mixed identity",
                "reason": "Service identities must not enter human workflows",
            },
        )
        assert mixed_identity_creation.status_code == 403

        created = client.post(
            "/v1/projects",
            headers=owner,
            json={"code": "ACL-1", "name": "Restricted tender", "reason": "Create owner ACL"},
        )
        assert created.status_code == 201, created.text
        project_id = created.json()["id"]

        for denied in (outsider, infrastructure_admin, system):
            response = client.get(f"/v1/projects/{project_id}", headers=denied)
            assert response.status_code == 404

        invalid_system_grant = client.post(
            f"/v1/projects/{project_id}/members",
            headers=owner,
            json={
                "principal_id": "unqualified-system",
                "roles": ["SYSTEM"],
                "reason": "SYSTEM must not receive human ACL membership",
            },
        )
        assert invalid_system_grant.status_code == 422

        granted = client.post(
            f"/v1/projects/{project_id}/members",
            headers=owner,
            json={
                "principal_id": "scoped-user",
                "roles": ["REVIEWER"],
                "access_level": "MEMBER",
                "reason": "Reviewer needs project access",
            },
        )
        assert granted.status_code == 200, granted.text
        assert granted.json()["version"] == 1
        assert client.get(f"/v1/projects/{project_id}", headers=scoped_user).status_code == 200
        assert (
            client.get(f"/v1/projects/{project_id}/members", headers=scoped_user).status_code == 403
        )

        with create_session_factory(engine)() as session:
            service = ProjectService(
                session=session,
                settings=settings,
                object_store=LocalObjectStore(tmp_path / "objects"),
            )
            actor = Actor(
                "scoped-user",
                "org-1",
                frozenset({ActorRole.REVIEWER, ActorRole.APPROVER}),
            )
            with pytest.raises(HTTPException) as denied_role:
                service.get_project(
                    actor=actor,
                    project_id=project_id,
                    required_roles=(ActorRole.APPROVER,),
                )
            assert denied_role.value.status_code == 403
            assert (
                service.get_project(
                    actor=actor,
                    project_id=project_id,
                    required_roles=(ActorRole.REVIEWER,),
                ).id
                == project_id
            )

        expanded = client.post(
            f"/v1/projects/{project_id}/members",
            headers=owner,
            json={
                "principal_id": "scoped-user",
                "roles": ["REVIEWER", "APPROVER"],
                "access_level": "MEMBER",
                "reason": "Approval duty was assigned explicitly",
            },
        )
        assert expanded.status_code == 200, expanded.text
        assert expanded.json()["version"] == 2
        assert (
            expanded.json()["supersedes_membership_id"] == granted.json()["membership_revision_id"]
        )

        revoked = client.post(
            f"/v1/projects/{project_id}/members/scoped-user/revoke",
            headers=owner,
            json={"reason": "Reviewer rotated off the tender"},
        )
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["status"] == "REVOKED"
        assert revoked.json()["version"] == 3
        assert client.get(f"/v1/projects/{project_id}", headers=scoped_user).status_code == 404

        last_owner = client.post(
            f"/v1/projects/{project_id}/members/owner-estimator/revoke",
            headers=owner,
            json={"reason": "Must retain a recoverable owner"},
        )
        assert last_owner.status_code == 422
        assert "last active project owner" in last_owner.json()["detail"]

        with create_session_factory(engine)() as session:
            history = list(
                session.query(ProjectMembershipRow)
                .filter(
                    ProjectMembershipRow.project_id == project_id,
                    ProjectMembershipRow.principal_id == "scoped-user",
                )
                .order_by(ProjectMembershipRow.version)
            )
            assert [row.version for row in history] == [1, 2, 3]
            assert [row.status for row in history] == ["ACTIVE", "ACTIVE", "REVOKED"]
            membership_events = list(
                session.query(AuditEventRow)
                .filter(
                    AuditEventRow.aggregate_id == project_id,
                    AuditEventRow.event_type.in_(
                        {
                            "project_membership_granted",
                            "project_membership_revoked",
                        }
                    ),
                )
                .order_by(AuditEventRow.sequence)
            )
            assert [event.event_type for event in membership_events] == [
                "project_membership_granted",
                "project_membership_granted",
                "project_membership_revoked",
            ]


def test_service_lookup_requires_exact_project_membership_role(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    factory = create_session_factory(engine)
    store = LocalObjectStore(tmp_path / "objects")
    owner = Actor("owner", "org-1", frozenset({ActorRole.ESTIMATOR}))
    outsider = Actor("outsider", "org-1", frozenset({ActorRole.ESTIMATOR}))

    with factory.begin() as session:
        service = ProjectService(session=session, settings=settings, object_store=store)
        project = service.create_project(
            actor=owner,
            code="ACL-SERVICE",
            name="Service ACL",
            request_id="request-create",
            reason="Create explicit owner",
        )
        service.grant_project_membership(
            actor=owner,
            project_id=project.id,
            principal_id="reviewer",
            roles=(ActorRole.REVIEWER,),
            access_level=ProjectAccessLevel.MEMBER,
            request_id="request-grant",
            reason="Grant reviewer only",
        )

    with factory() as session:
        service = ProjectService(session=session, settings=settings, object_store=store)
        with pytest.raises(ProjectNotFoundError):
            service.get_project(
                actor=outsider,
                project_id=project.id,
                required_roles=(ActorRole.ESTIMATOR,),
            )
        reviewer = Actor(
            "reviewer",
            "org-1",
            frozenset({ActorRole.REVIEWER, ActorRole.APPROVER}),
        )
        with pytest.raises(HTTPException) as denied:
            service.get_project(
                actor=reviewer,
                project_id=project.id,
                required_roles=(ActorRole.APPROVER,),
            )
        assert denied.value.status_code == 403
        assert (
            service.get_project(
                actor=reviewer,
                project_id=project.id,
                required_roles=(ActorRole.REVIEWER,),
            ).id
            == project.id
        )


def test_membership_timestamps_are_timezone_aware(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    owner = Actor("owner", "org-1", frozenset({ActorRole.ESTIMATOR}))
    with create_session_factory(engine).begin() as session:
        service = ProjectService(
            session=session,
            settings=settings,
            object_store=LocalObjectStore(tmp_path / "objects"),
        )
        project = service.create_project(
            actor=owner,
            code="ACL-TIME",
            name="Membership time",
            request_id="request-time",
            reason="Verify auditable time",
        )
        memberships = service.list_project_memberships(actor=owner, project_id=project.id)
        assert memberships[0].created_at <= datetime.now(UTC)
