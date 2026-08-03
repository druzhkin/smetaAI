from __future__ import annotations

import json
from collections import deque
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from tenderguard.application.fgiscs_acquisition import (
    FGIS_CS_ACQUISITION_POLICY_KIND,
    FGIS_CS_ACQUISITION_POLICY_PURPOSE,
    FgisCsAcquisitionService,
    prepare_fgiscs_material_acquisition,
)
from tenderguard.application.pricing import NomenclatureAssessmentDraft, PricingService
from tenderguard.config import Settings
from tenderguard.domain.common import content_hash
from tenderguard.domain.enums import (
    ActorRole,
    ApprovalState,
    EvidenceMethod,
    PriceSourceType,
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
    BoqLineRow,
    ControlledVersionRow,
    DocumentSetRevisionRow,
    FgisCsAcquisitionRow,
    ObservationRow,
    PriceQuoteRow,
    ProjectRow,
)
from tenderguard.integrations.fgiscs_public import FgisCsPublicApi
from tests.integration.support import (
    add_document_set_confirmation_audit,
    add_governed_controlled_version,
    add_project_controlled_version_binding,
    project_memberships,
)

_RESOURCE_CODE = "02.3.01.02-1102"


class _Headers:
    def get_content_type(self) -> str:
        return "application/json"


class _Response:
    status = 200
    headers = _Headers()

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self, _: int) -> bytes:
        return self.payload


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def _install_fgiscs_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = deque(
        [
            _Response(_json([{"id": 331, "name": "Moscow region"}])),
            _Response(_json([{"id": 127, "name": "Moscow price zone"}])),
            _Response(_json([{"id": 426, "name": "Q2 2026"}])),
            _Response(
                _json(
                    {
                        "items": [
                            {
                                "id": 1,
                                "ksrType": 1,
                                "items": [
                                    {
                                        "code": _RESOURCE_CODE,
                                        "name": "Official natural construction sand, fine",
                                        "unitName": "m3",
                                        "aggregatedPrice": "409.89",
                                        "estimatedPrice": "1054.48",
                                        "distancePrice": "623.91",
                                        "procureStorageCostPercent": "2.00",
                                        "ksrType": 1,
                                        "id": 3655581,
                                    }
                                ],
                            }
                        ],
                        "total": 1,
                    }
                )
            ),
        ]
    )

    class _Connection:
        def request(self, *args: object, **kwargs: object) -> None:
            return None

        def getresponse(self) -> _Response:
            return responses.popleft()

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "tenderguard.integrations.fgiscs_public.http.client.HTTPSConnection",
        lambda *args, **kwargs: _Connection(),
    )


def _configured_system(tmp_path: Path):
    settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite://",
        local_object_store_path=tmp_path / "objects",
        local_quarantine_store_path=tmp_path / "quarantine",
        audit_signing_key="test-audit-signing-key-at-least-32-bytes",
        fgiscs_adapter="tenderguard-fgiscs-public",
        fgiscs_adapter_qualification_id="qualification-fgiscs-v1",
        fgiscs_worker_actor_id="fgiscs-worker",
    )
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    factory = create_session_factory(engine)
    store = LocalObjectStore(tmp_path / "objects")
    now = datetime(2026, 7, 31, 9, 30, tzinfo=UTC)
    estimator = Actor(
        "estimator-1",
        "org-1",
        frozenset({ActorRole.ESTIMATOR, ActorRole.PROCUREMENT, ActorRole.TECHNICAL_EXPERT}),
    )
    confirmer = Actor(
        "document-confirmer",
        "org-1",
        frozenset({ActorRole.REVIEWER, ActorRole.APPROVER}),
    )
    catalog_creator = Actor("catalog-creator", "org-1", frozenset({ActorRole.CATALOG_OWNER}))
    catalog_approver = Actor(
        "catalog-approver",
        "org-1",
        frozenset({ActorRole.CATALOG_OWNER, ActorRole.APPROVER}),
    )
    policy_creator = Actor(
        "policy-creator",
        "org-1",
        frozenset({ActorRole.METHODOLOGY_OWNER}),
    )
    policy_approver = Actor(
        "policy-approver",
        "org-1",
        frozenset({ActorRole.METHODOLOGY_OWNER}),
    )
    system_actor = Actor(
        "fgiscs-worker",
        "org-1",
        frozenset({ActorRole.SYSTEM}),
    )
    with factory.begin() as session:
        session.add(
            ProjectRow(
                id="project-fgiscs",
                organization_id="org-1",
                code="FGIS-1",
                name="Governed FGIS CS acquisition",
                state=ApprovalState.PRICING_IN_PROGRESS.value,
                current_document_set_revision_id="document-set-fgiscs",
                row_version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.add_all(
            project_memberships(
                "project-fgiscs",
                (estimator,),
                owner_id=estimator.actor_id,
                now=now,
            )
        )
        document_set = DocumentSetRevisionRow(
            id="document-set-fgiscs",
            project_id="project-fgiscs",
            manifest_hash=content_hash(["revision-fgiscs"]),
            revision_ids=["revision-fgiscs"],
            status="CONFIRMED",
            created_by=estimator.actor_id,
            created_at=now,
            confirmed_by=confirmer.actor_id,
            confirmed_at=now,
        )
        session.add(document_set)
        add_document_set_confirmation_audit(
            session=session,
            settings=settings,
            object_store=store,
            row=document_set,
            actor=confirmer,
        )
        session.add(
            BoqLineRow(
                id="boq-line-sand",
                project_id="project-fgiscs",
                line_key="sand",
                wbs_node_id="wbs-sand",
                work_code="SAND",
                description="Sand from the tender BoQ, grade I, fine",
                unit="m3",
                status=VerificationStatus.VERIFIED.value,
                supersedes_line_id=None,
                is_current=True,
                payload={
                    "cost_components": [
                        {
                            "semantic_key": "sand-source",
                            "category": "MATERIAL",
                            "basis_kind": "MARKET",
                        }
                    ]
                },
                created_at=now,
                updated_at=now,
            )
        )
        catalog = ControlledVersionRow(
            id="catalog-fgiscs-v1",
            kind="catalog",
            version_label="1",
            content_hash="0" * 64,
            status="DRAFT",
            payload={
                "items": {
                    "sand-canonical": {
                        "attributes": {"grade": "I", "fraction": "fine"},
                        "critical_attributes": ["grade", "fraction"],
                        "critical_price": True,
                    }
                }
            },
            approved_by=None,
            approved_at=None,
        )
        add_governed_controlled_version(
            session=session,
            settings=settings,
            object_store=store,
            row=catalog,
            organization_id="org-1",
            creator=catalog_creator,
            approver=catalog_approver,
        )
        add_project_controlled_version_binding(
            session=session,
            settings=settings,
            object_store=store,
            project_id="project-fgiscs",
            version=catalog,
            purpose="catalog",
            actor=catalog_approver,
        )
        policy = ControlledVersionRow(
            id="fgiscs-policy-v1",
            kind=FGIS_CS_ACQUISITION_POLICY_KIND,
            version_label="1",
            content_hash="0" * 64,
            status="DRAFT",
            payload={
                "subject_name": "Moscow region",
                "price_zone_name": "Moscow price zone",
                "period_name": "Q2 2026",
                "allowed_project_states": [ApprovalState.PRICING_IN_PROGRESS.value],
                "required_adapter_name": "tenderguard-fgiscs-public",
                "required_adapter_version": "1.0.0",
                "source_origin_id": "fgiscs.minstroyrf.ru",
            },
            approved_by=None,
            approved_at=None,
        )
        add_governed_controlled_version(
            session=session,
            settings=settings,
            object_store=store,
            row=policy,
            organization_id="org-1",
            creator=policy_creator,
            approver=policy_approver,
        )
        add_project_controlled_version_binding(
            session=session,
            settings=settings,
            object_store=store,
            project_id="project-fgiscs",
            version=policy,
            purpose=FGIS_CS_ACQUISITION_POLICY_PURPOSE,
            actor=policy_approver,
        )
        session.add(
            AdapterQualificationRow(
                id="qualification-fgiscs-v1",
                adapter_name="tenderguard-fgiscs-public",
                adapter_version="1.0.0",
                status="APPROVED",
                valid_until=date(2027, 7, 31),
                test_evidence_hash="d" * 64,
                payload={
                    "supported_methods": [EvidenceMethod.EXTERNAL_SYSTEM.value],
                    "supported_price_source_types": [PriceSourceType.FGIS_CS.value],
                    "supported_price_source_origins": ["fgiscs.minstroyrf.ru"],
                    "organization_id": "org-1",
                    "service_actor_id": system_actor.actor_id,
                },
                approved_by=policy_approver.actor_id,
                approved_at=now,
            )
        )
        observation = Observation(
            observation_id="observation-sand-attributes",
            field_name="technical_attributes:sand-source",
            value={
                "source_item_id": "sand-source",
                "attributes": {"grade": "I", "fraction": "fine"},
            },
            unit=None,
            method=EvidenceMethod.RULE_ENGINE,
            method_version="test-reconciliation-v1",
            source_priority=1,
            location=EvidenceLocation(
                document_id="document-fgiscs",
                document_revision_id="revision-fgiscs",
                original_object_hash="e" * 64,
                locator_kind="XLSX_ROW",
                locator="boq.xlsx::Sheet1::A10:D10",
            ),
            observed_at=now,
            actor_id=estimator.actor_id,
            status=VerificationStatus.VERIFIED,
        )
        session.add(
            ObservationRow(
                id=observation.observation_id,
                project_id="project-fgiscs",
                document_revision_id="revision-fgiscs",
                field_name=observation.field_name,
                method=observation.method.value,
                method_version=observation.method_version,
                status=observation.status.value,
                payload={"observation": observation.model_dump(mode="json")},
                created_at=now,
            )
        )
        session.flush()
        match = PricingService(
            session=session,
            settings=settings,
            object_store=store,
        ).assess_nomenclature(
            actor=estimator,
            project_id="project-fgiscs",
            draft=NomenclatureAssessmentDraft(
                source_item_id="sand-source",
                canonical_item_id="sand-canonical",
                source_attributes_observation_id=observation.observation_id,
            ),
            request_id="assess-sand",
            reason="Deterministically compare the critical BoQ attributes",
        )
        assert match.status is VerificationStatus.VERIFIED
    return settings, factory, store, system_actor, estimator, engine


def _prepare(
    *,
    settings: Settings,
    factory,
    store: LocalObjectStore,
    system_actor: Actor,
):
    with factory() as session:
        context = FgisCsAcquisitionService(
            session=session,
            settings=settings,
            object_store=store,
        ).request_context(
            actor=system_actor,
            project_id="project-fgiscs",
            item_id="sand-source",
            resource_code=_RESOURCE_CODE,
        )
    prepared = prepare_fgiscs_material_acquisition(
        api=FgisCsPublicApi(max_response_bytes=settings.integration_max_response_bytes),
        request=context.lookup_request,
        expected_context_hash=context.context_hash,
        object_store=store,
        max_response_bytes=settings.integration_max_response_bytes,
    )
    return context, prepared


def test_governed_fgiscs_acquisition_replays_source_and_never_creates_price(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, factory, store, system_actor, estimator, engine = _configured_system(tmp_path)
    _install_fgiscs_responses(monkeypatch)
    context, prepared = _prepare(
        settings=settings,
        factory=factory,
        store=store,
        system_actor=system_actor,
    )
    try:
        with factory.begin() as session:
            view = FgisCsAcquisitionService(
                session=session,
                settings=settings,
                object_store=store,
            ).record_stored_acquisition(
                actor=system_actor,
                project_id="project-fgiscs",
                item_id="sand-source",
                resource_code=_RESOURCE_CODE,
                expected_context_hash=context.context_hash,
                prepared=prepared,
                request_id="acquire-sand-fgiscs",
                reason="Retain the exact official source response for independent review",
            )
            assert view.status is VerificationStatus.UNVERIFIED
            assert view.ready_for_pricing is False
            assert view.artifact.result.price is not None
            assert view.artifact.result.price.source_item_name == (
                "Official natural construction sand, fine"
            )
            assert "APPROVED_FGIS_MAPPING_REQUIRED" in view.pricing_blockers
            assert "COMMERCIAL_BASIS_NOT_ESTABLISHED" in view.pricing_blockers

        with factory() as session:
            listed = FgisCsAcquisitionService(
                session=session,
                settings=settings,
                object_store=store,
            ).list_for_item(
                actor=estimator,
                project_id="project-fgiscs",
                item_id="sand-source",
            )
            assert listed.boq_item_name == "Sand from the tender BoQ, grade I, fine"
            assert len(listed.acquisitions) == 1
            assert listed.acquisitions[0].basis_current is True
            assert session.query(FgisCsAcquisitionRow).count() == 1
            assert session.query(PriceQuoteRow).count() == 0
    finally:
        engine.dispose()


def test_governed_fgiscs_acquisition_rejects_changed_project_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, factory, store, system_actor, _, engine = _configured_system(tmp_path)
    _install_fgiscs_responses(monkeypatch)
    context, prepared = _prepare(
        settings=settings,
        factory=factory,
        store=store,
        system_actor=system_actor,
    )
    try:
        with factory.begin() as session:
            project = session.get(ProjectRow, "project-fgiscs")
            assert project is not None
            project.row_version += 1
        with pytest.raises(ValueError, match="context changed"), factory.begin() as session:
            FgisCsAcquisitionService(
                session=session,
                settings=settings,
                object_store=store,
            ).record_stored_acquisition(
                actor=system_actor,
                project_id="project-fgiscs",
                item_id="sand-source",
                resource_code=_RESOURCE_CODE,
                expected_context_hash=context.context_hash,
                prepared=prepared,
                request_id="stale-fgiscs-context",
                reason="This stale request must be rejected",
            )
        with factory() as session:
            assert session.query(FgisCsAcquisitionRow).count() == 0
            assert session.query(PriceQuoteRow).count() == 0
    finally:
        engine.dispose()


def test_governed_fgiscs_acquisition_rejects_tampered_raw_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, factory, store, system_actor, _, engine = _configured_system(tmp_path)
    _install_fgiscs_responses(monkeypatch)
    context, prepared = _prepare(
        settings=settings,
        factory=factory,
        store=store,
        system_actor=system_actor,
    )
    raw_hash = prepared.artifact.exchanges[-1].response_object_hash
    raw_path = store.root / raw_hash[:2] / raw_hash
    raw_path.write_bytes(b'{"items":[]}')
    try:
        with pytest.raises(RuntimeError, match="SHA-256 verification"), factory.begin() as session:
            FgisCsAcquisitionService(
                session=session,
                settings=settings,
                object_store=store,
            ).record_stored_acquisition(
                actor=system_actor,
                project_id="project-fgiscs",
                item_id="sand-source",
                resource_code=_RESOURCE_CODE,
                expected_context_hash=context.context_hash,
                prepared=prepared,
                request_id="tampered-fgiscs-object",
                reason="This tampered source response must be rejected",
            )
        with factory() as session:
            assert session.query(FgisCsAcquisitionRow).count() == 0
            assert session.query(PriceQuoteRow).count() == 0
    finally:
        engine.dispose()
