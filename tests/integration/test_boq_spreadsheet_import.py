from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from openpyxl import Workbook
from sqlalchemy import func, select

from tenderguard.application.boq import (
    AttachImportedQuantityCommand,
    BoqLineDraft,
    BoqService,
    BoqSpreadsheetMappingCommand,
    BoqSpreadsheetQuantityCommand,
    CostComponentDraft,
)
from tenderguard.application.boq_spreadsheet_import import (
    BOQ_XLSX_PROFILE_KIND,
    BOQ_XLSX_PROFILE_PURPOSE,
    BoqSpreadsheetImportService,
)
from tenderguard.application.evidence import (
    EvidenceService,
    ManualEvidenceDecisionCommand,
)
from tenderguard.config import Settings
from tenderguard.domain.boq_spreadsheet import BoqXlsxProfile
from tenderguard.domain.common import content_hash, utc_now
from tenderguard.domain.enums import (
    ActorRole,
    ApprovalDecision,
    ApprovalState,
    EvidenceMethod,
)
from tenderguard.infrastructure.auth import Actor
from tenderguard.infrastructure.database import (
    create_database_engine,
    create_schema_for_tests,
    create_session_factory,
)
from tenderguard.infrastructure.intake import inspect_intake
from tenderguard.infrastructure.object_store import LocalObjectStore
from tenderguard.infrastructure.orm import (
    AdapterQualificationRow,
    ApprovalRecordRow,
    BoqLineRow,
    ControlledVersionRow,
    DocumentRevisionRow,
    DocumentRow,
    DocumentSetRevisionRow,
    ExtractionRunRow,
    FileManifestRow,
    ObservationRow,
    ProjectRow,
)
from tests.integration.support import (
    add_document_set_confirmation_audit,
    add_governed_controlled_version,
    add_project_controlled_version_binding,
    project_memberships,
)


def _workbook(*, hidden_content: bool = False) -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "BoQ"
    worksheet.append(["ID", "Description", "Unit", "Quantity"])
    worksheet.append([1, "Cable", "m", 10])
    worksheet.append([2, "Joint", "pcs", 2])
    if hidden_content:
        worksheet.column_dimensions["F"].hidden = True
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def _profile(*, profile_id: str, workbook_sha256: str) -> BoqXlsxProfile:
    return BoqXlsxProfile(
        profile_version_id=profile_id,
        worksheet_name="BoQ",
        header_row=1,
        data_start_row=2,
        data_end_row=3,
        position_id={"column": 1, "header": "ID"},
        description={"column": 2, "header": "Description"},
        unit={"column": 3, "header": "Unit"},
        quantity={"column": 4, "header": "Quantity"},
        position_id_pattern=r"^\d+$",
        allowed_units=("m", "pcs"),
        quantity_decimal_separator=".",
        expected_workbook_sha256=workbook_sha256,
    )


def _setup(
    tmp_path: Path,
    *,
    hidden_content: bool = False,
):
    settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite://",
        local_object_store_path=tmp_path / "objects",
        local_quarantine_store_path=tmp_path / "quarantine",
        allow_insecure_dev_auth=True,
        audit_signing_key="test-audit-signing-key-at-least-32-bytes",
        boq_xlsx_adapter="tenderguard-boq-xlsx",
        boq_xlsx_adapter_qualification_id="boq-xlsx-qualification-v1",
        boq_xlsx_worker_actor_id="boq-xlsx-worker",
    )
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    factory = create_session_factory(engine)
    store = LocalObjectStore(tmp_path / "objects")
    content = _workbook(hidden_content=hidden_content)
    stored = store.put(BytesIO(content))
    manifest = inspect_intake("source.xlsx", content, settings)
    now = utc_now()
    service_actor = Actor(
        actor_id="boq-xlsx-worker",
        organization_id="org-1",
        roles=frozenset({ActorRole.SYSTEM}),
    )
    profile_creator = Actor(
        actor_id="profile-creator",
        organization_id="org-1",
        roles=frozenset({ActorRole.METHODOLOGY_OWNER}),
    )
    profile_approver = Actor(
        actor_id="profile-approver",
        organization_id="org-1",
        roles=frozenset({ActorRole.METHODOLOGY_OWNER, ActorRole.APPROVER}),
    )
    profile_id = "boq-xlsx-profile-v1"
    profile = _profile(
        profile_id=profile_id,
        workbook_sha256=stored.object_hash,
    )

    with factory.begin() as session:
        session.add(
            ProjectRow(
                id="project-boq-import",
                organization_id="org-1",
                code="BOQ-IMPORT",
                name="BoQ spreadsheet import",
                state=ApprovalState.EXTRACTION_IN_PROGRESS.value,
                current_document_set_revision_id="document-set-boq-import",
                row_version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            DocumentRow(
                id="document-boq",
                project_id="project-boq-import",
                logical_key="boq",
                title="BoQ source",
                document_type="BOQ",
                critical=True,
                cancelled=False,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            DocumentRevisionRow(
                id="revision-boq",
                document_id="document-boq",
                revision_label="1",
                issue_date=None,
                object_hash=stored.object_hash,
                object_key=stored.object_key,
                original_filename="source.xlsx",
                media_type=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                size_bytes=stored.size_bytes,
                supersedes_revision_id=None,
                is_current=True,
                corrupt=False,
                protected=False,
                inspection_payload=manifest.model_dump(mode="json"),
                created_at=now,
                updated_at=now,
            )
        )
        for entry in manifest.entries:
            session.add(
                FileManifestRow(
                    id=entry.entry_id,
                    document_revision_id="revision-boq",
                    archive_path=entry.archive_path,
                    object_hash=entry.sha256,
                    size_bytes=entry.size_bytes,
                    media_type=entry.media_type,
                    corrupt=entry.corrupt,
                    protected=entry.protected,
                    nested_archive=entry.nested_archive,
                    inspection_payload=entry.model_dump(mode="json"),
                )
            )
        document_set = DocumentSetRevisionRow(
            id="document-set-boq-import",
            project_id="project-boq-import",
            manifest_hash=content_hash(["revision-boq"]),
            revision_ids=["revision-boq"],
            status="CONFIRMED",
            created_by=profile_creator.actor_id,
            created_at=now,
            confirmed_by=profile_approver.actor_id,
            confirmed_at=now,
        )
        session.add(document_set)
        session.add(
            AdapterQualificationRow(
                id="boq-xlsx-qualification-v1",
                adapter_name="tenderguard-boq-xlsx",
                adapter_version="1.0.0",
                status="APPROVED",
                valid_until=None,
                test_evidence_hash="1" * 64,
                payload={
                    "organization_id": "org-1",
                    "service_actor_id": service_actor.actor_id,
                    "supported_methods": [EvidenceMethod.TABLE_PARSER.value],
                },
                approved_by=profile_approver.actor_id,
                approved_at=now,
            )
        )
        add_document_set_confirmation_audit(
            session=session,
            settings=settings,
            object_store=store,
            row=document_set,
            actor=profile_approver,
        )
        controlled_profile = ControlledVersionRow(
            id=profile_id,
            kind=BOQ_XLSX_PROFILE_KIND,
            version_label="1",
            content_hash="0" * 64,
            status="DRAFT",
            payload={
                "profile": profile.model_dump(mode="json"),
                "source_priority": 10,
                "allowed_project_states": [ApprovalState.EXTRACTION_IN_PROGRESS.value],
                "required_adapter_name": "tenderguard-boq-xlsx",
                "required_adapter_version": "1.0.0",
            },
            approved_by=None,
            approved_at=None,
        )
        add_governed_controlled_version(
            session=session,
            settings=settings,
            object_store=store,
            row=controlled_profile,
            organization_id="org-1",
            creator=profile_creator,
            approver=profile_approver,
        )
        add_project_controlled_version_binding(
            session=session,
            settings=settings,
            object_store=store,
            project_id="project-boq-import",
            version=controlled_profile,
            purpose=BOQ_XLSX_PROFILE_PURPOSE,
            actor=profile_approver,
        )
    return settings, factory, store, service_actor, engine


def _enable_mapping_workflow(
    *,
    settings: Settings,
    factory,
    store: LocalObjectStore,
) -> tuple[Actor, Actor, Actor]:
    author = Actor(
        actor_id="boq-mapping-author",
        organization_id="org-1",
        roles=frozenset({ActorRole.TECHNICAL_EXPERT}),
    )
    reviewer = Actor(
        actor_id="boq-mapping-reviewer",
        organization_id="org-1",
        roles=frozenset({ActorRole.REVIEWER}),
    )
    estimator = Actor(
        actor_id="boq-estimator",
        organization_id="org-1",
        roles=frozenset({ActorRole.ESTIMATOR}),
    )
    creator = Actor(
        actor_id="manual-policy-creator",
        organization_id="org-1",
        roles=frozenset({ActorRole.METHODOLOGY_OWNER}),
    )
    approver = Actor(
        actor_id="manual-policy-approver",
        organization_id="org-1",
        roles=frozenset({ActorRole.METHODOLOGY_OWNER}),
    )
    now = utc_now()
    with factory.begin() as session:
        project = session.get(ProjectRow, "project-boq-import")
        assert project is not None
        project.state = ApprovalState.BOQ_IN_PROGRESS.value
        project.row_version += 1
        project.updated_at = now
        session.add_all(
            project_memberships(
                project.id,
                (author, reviewer, estimator, creator, approver),
                owner_id=creator.actor_id,
                now=now,
            )
        )
        policy = ControlledVersionRow(
            id="manual-evidence-policy-boq-mapping-v1",
            kind="manual_evidence_policy",
            version_label="1",
            content_hash="0" * 64,
            status="DRAFT",
            payload={
                "review_role": ActorRole.REVIEWER.value,
                "allowed_project_states": [ApprovalState.BOQ_IN_PROGRESS.value],
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
            creator=creator,
            approver=approver,
        )
        add_project_controlled_version_binding(
            session=session,
            settings=settings,
            object_store=store,
            project_id=project.id,
            version=policy,
            purpose="manual_evidence_policy",
            actor=creator,
        )
        quantity_policy = ControlledVersionRow(
            id="quantity-policy-boq-import-v1",
            kind="quantity_policy",
            version_label="1",
            content_hash="0" * 64,
            status="DRAFT",
            payload={
                "policy": {
                    "absolute_tolerance": "0",
                    "relative_tolerance": "0",
                    "allow_zero": False,
                    "allow_negative": False,
                }
            },
            approved_by=None,
            approved_at=None,
        )
        add_governed_controlled_version(
            session=session,
            settings=settings,
            object_store=store,
            row=quantity_policy,
            organization_id="org-1",
            creator=creator,
            approver=approver,
        )
        add_project_controlled_version_binding(
            session=session,
            settings=settings,
            object_store=store,
            project_id=project.id,
            version=quantity_policy,
            purpose="quantity_policy",
            actor=creator,
        )
    return author, reviewer, estimator


def test_governed_import_creates_only_unverified_idempotent_observations(
    tmp_path: Path,
) -> None:
    settings, factory, store, actor, engine = _setup(tmp_path)
    try:
        with factory.begin() as session:
            first = BoqSpreadsheetImportService(
                session=session,
                settings=settings,
                object_store=store,
            ).import_current_workbook(
                actor=actor,
                project_id="project-boq-import",
                document_revision_id="revision-boq",
                adapter_qualification_id="boq-xlsx-qualification-v1",
                request_id="import-1",
                reason="Import exact profile-pinned BoQ rows for independent review",
            )
        assert first.status.value == "UNVERIFIED"
        assert not first.reused_existing_run
        assert len(first.observation_ids) == 2

        with factory.begin() as session:
            repeated = BoqSpreadsheetImportService(
                session=session,
                settings=settings,
                object_store=store,
            ).import_current_workbook(
                actor=actor,
                project_id="project-boq-import",
                document_revision_id="revision-boq",
                adapter_qualification_id="boq-xlsx-qualification-v1",
                request_id="import-2",
                reason="Replay the same governed import without duplication",
            )
        assert repeated.reused_existing_run
        assert repeated.extraction_run_id == first.extraction_run_id
        assert repeated.observation_ids == first.observation_ids

        with factory() as session:
            assert session.scalar(select(func.count()).select_from(ExtractionRunRow)) == 1
            rows = tuple(
                session.scalars(select(ObservationRow).order_by(ObservationRow.field_name))
            )
            assert len(rows) == 2
            assert all(row.status == "UNVERIFIED" for row in rows)
            assert all(row.method == EvidenceMethod.TABLE_PARSER.value for row in rows)
            assert all(
                row.payload["adapter_qualification_id"] == "boq-xlsx-qualification-v1"
                for row in rows
            )
            values = tuple(row.payload["observation"]["value"] for row in rows)
            assert {value["source_position_id"] for value in values} == {"1", "2"}
            assert {value["quantity"] for value in values} == {"10", "2"}
            assert all("price" not in value for value in values)
            assert all("proposed_price" not in value for value in values)
            assert all(
                value["cells"]["description"]["coordinate"] in {"B2", "B3"} for value in values
            )
    finally:
        engine.dispose()


def test_governed_import_rejects_hidden_content_without_partial_rows(
    tmp_path: Path,
) -> None:
    settings, factory, store, actor, engine = _setup(
        tmp_path,
        hidden_content=True,
    )
    try:
        with (
            pytest.raises(ValueError, match="extraction is BLOCKED"),
            factory.begin() as session,
        ):
            BoqSpreadsheetImportService(
                session=session,
                settings=settings,
                object_store=store,
            ).import_current_workbook(
                actor=actor,
                project_id="project-boq-import",
                document_revision_id="revision-boq",
                adapter_qualification_id="boq-xlsx-qualification-v1",
                request_id="blocked-import",
                reason="Attempt import of structurally blocked workbook",
            )
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(ObservationRow)) == 0
            assert session.scalar(select(func.count()).select_from(ExtractionRunRow)) == 0
    finally:
        engine.dispose()


def test_governed_import_rejects_adapter_substitution(
    tmp_path: Path,
) -> None:
    settings, factory, store, actor, engine = _setup(tmp_path)
    try:
        with factory.begin() as session:
            qualification = session.get(
                AdapterQualificationRow,
                "boq-xlsx-qualification-v1",
            )
            assert qualification is not None
            qualification.adapter_version = "substituted-version"
        with (
            pytest.raises(ValueError, match="qualification does not match"),
            factory.begin() as session,
        ):
            BoqSpreadsheetImportService(
                session=session,
                settings=settings,
                object_store=store,
            ).import_current_workbook(
                actor=actor,
                project_id="project-boq-import",
                document_revision_id="revision-boq",
                adapter_qualification_id="boq-xlsx-qualification-v1",
                request_id="substitution",
                reason="Attempt adapter substitution",
            )
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(ObservationRow)) == 0
    finally:
        engine.dispose()


def test_governed_import_rejects_unconfigured_application_binding(
    tmp_path: Path,
) -> None:
    settings, factory, store, actor, engine = _setup(tmp_path)
    unconfigured = settings.model_copy(
        update={
            "boq_xlsx_adapter": None,
            "boq_xlsx_adapter_qualification_id": None,
            "boq_xlsx_worker_actor_id": None,
        }
    )
    try:
        with (
            pytest.raises(ValueError, match="worker binding is not configured"),
            factory.begin() as session,
        ):
            BoqSpreadsheetImportService(
                session=session,
                settings=unconfigured,
                object_store=store,
            ).import_current_workbook(
                actor=actor,
                project_id="project-boq-import",
                document_revision_id="revision-boq",
                adapter_qualification_id="boq-xlsx-qualification-v1",
                request_id="unconfigured-import",
                reason="Reject a parser that is not bound in application settings",
            )
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(ObservationRow)) == 0
            assert session.scalar(select(func.count()).select_from(ExtractionRunRow)) == 0
    finally:
        engine.dispose()


def test_governed_import_replay_rejects_tampered_run(
    tmp_path: Path,
) -> None:
    settings, factory, store, actor, engine = _setup(tmp_path)
    try:
        with factory.begin() as session:
            created = BoqSpreadsheetImportService(
                session=session,
                settings=settings,
                object_store=store,
            ).import_current_workbook(
                actor=actor,
                project_id="project-boq-import",
                document_revision_id="revision-boq",
                adapter_qualification_id="boq-xlsx-qualification-v1",
                request_id="initial-import",
                reason="Create a governed import for integrity replay",
            )
        with factory.begin() as session:
            run = session.get(ExtractionRunRow, created.extraction_run_id)
            assert run is not None
            run.payload = {**run.payload, "row_count": 999}
        with (
            pytest.raises(RuntimeError, match="fails integrity replay"),
            factory.begin() as session,
        ):
            BoqSpreadsheetImportService(
                session=session,
                settings=settings,
                object_store=store,
            ).import_current_workbook(
                actor=actor,
                project_id="project-boq-import",
                document_revision_id="revision-boq",
                adapter_qualification_id="boq-xlsx-qualification-v1",
                request_id="tampered-replay",
                reason="Reject a tampered extraction-run replay",
            )
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(ObservationRow)) == 2
            assert session.scalar(select(func.count()).select_from(ExtractionRunRow)) == 1
    finally:
        engine.dispose()


def test_imported_row_requires_independent_mapping_before_boq_authoring(
    tmp_path: Path,
) -> None:
    settings, factory, store, worker, engine = _setup(tmp_path)
    try:
        with factory.begin() as session:
            imported = BoqSpreadsheetImportService(
                session=session,
                settings=settings,
                object_store=store,
            ).import_current_workbook(
                actor=worker,
                project_id="project-boq-import",
                document_revision_id="revision-boq",
                adapter_qualification_id="boq-xlsx-qualification-v1",
                request_id="request-import-before-mapping",
                reason="Import exact spreadsheet rows before governed mapping",
            )
        author, reviewer, estimator = _enable_mapping_workflow(
            settings=settings,
            factory=factory,
            store=store,
        )

        with factory.begin() as session:
            service = BoqService(session=session, settings=settings, object_store=store)
            estimator_context = service.spreadsheet_candidate_context(
                actor=estimator,
                project_id="project-boq-import",
                limit=100,
            )
            assert len(estimator_context.candidates) == 2
            assert not estimator_context.candidates[0].proposal_allowed
            assert "MAPPING_ROLE_REQUIRED" in estimator_context.candidates[0].proposal_blockers

            context = service.spreadsheet_candidate_context(
                actor=author,
                project_id="project-boq-import",
                limit=100,
            )
            source = next(
                item
                for item in context.candidates
                if item.source_observation.observation_id == imported.observation_ids[0]
            )
            assert source.proposal_allowed
            proposal = service.propose_spreadsheet_mapping(
                actor=author,
                project_id="project-boq-import",
                source_observation_id=source.source_observation.observation_id,
                command=BoqSpreadsheetMappingCommand(
                    work_code="CABLE-INSTALLATION",
                    description="Cable installation",
                    unit="m",
                    expected_source_observation_hash=source.source_observation_hash,
                    proposed_at=utc_now(),
                ),
                request_id="request-propose-boq-mapping",
                reason="Classify the imported row without promoting its quantity",
            )
            assert proposal.status.value == "UNVERIFIED"
            assert proposal.value["quantity_promoted"] is False
            assert "quantity" not in proposal.value
            quantity_proposal = service.propose_spreadsheet_quantity(
                actor=author,
                project_id="project-boq-import",
                source_observation_id=source.source_observation.observation_id,
                command=BoqSpreadsheetQuantityCommand(
                    expected_source_observation_hash=source.source_observation_hash,
                    proposed_at=utc_now(),
                ),
                request_id="request-propose-boq-quantity",
                reason="Submit the exact imported quantity for separate independent review",
            )
            assert str(quantity_proposal.value) == "10"
            assert quantity_proposal.unit == "m"
            assert quantity_proposal.field_name == f"boq_quantity:{source.source_item_id}"

            with pytest.raises(ValueError, match="active mapping proposal"):
                service.propose_spreadsheet_mapping(
                    actor=author,
                    project_id="project-boq-import",
                    source_observation_id=source.source_observation.observation_id,
                    command=BoqSpreadsheetMappingCommand(
                        work_code="CABLE-INSTALLATION",
                        description="Cable installation",
                        unit="m",
                        expected_source_observation_hash=source.source_observation_hash,
                        proposed_at=utc_now(),
                    ),
                    request_id="request-duplicate-boq-mapping",
                    reason="A second live proposal must be rejected",
                )

        with factory.begin() as session:
            evidence = EvidenceService(session=session, settings=settings, object_store=store)
            review = evidence.manual_evidence_review(
                actor=reviewer,
                project_id="project-boq-import",
                observation_id=proposal.observation_id,
            )
            assert review.decision_allowed
            decided = evidence.decide_manual_evidence(
                actor=reviewer,
                project_id="project-boq-import",
                observation_id=proposal.observation_id,
                command=ManualEvidenceDecisionCommand(
                    decision=ApprovalDecision.APPROVED,
                    reason="Independently checked the row identity, description and unit",
                    expected_task_updated_at=review.task_updated_at,
                ),
                request_id="request-approve-boq-mapping",
            )
            assert decided.verified_observation is not None
            quantity_review = evidence.manual_evidence_review(
                actor=reviewer,
                project_id="project-boq-import",
                observation_id=quantity_proposal.observation_id,
            )
            assert quantity_review.decision_allowed
            quantity_decided = evidence.decide_manual_evidence(
                actor=reviewer,
                project_id="project-boq-import",
                observation_id=quantity_proposal.observation_id,
                command=ManualEvidenceDecisionCommand(
                    decision=ApprovalDecision.APPROVED,
                    reason="Independently checked the exact quantity cell and unit",
                    expected_task_updated_at=quantity_review.task_updated_at,
                ),
                request_id="request-approve-boq-quantity",
            )
            assert quantity_decided.verified_observation is not None

        with factory.begin() as session:
            service = BoqService(
                session=session,
                settings=settings,
                object_store=store,
            )
            authoring = service.authoring_context(
                actor=author,
                project_id="project-boq-import",
                evidence_field_name="boq_line",
                limit=100,
            )
            assert [item.work_code for item in authoring.evidence_candidates] == [
                "CABLE-INSTALLATION"
            ]
            assert authoring.evidence_candidates[0].unit == "m"
            assert authoring.evidence_candidates[0].description == "Cable installation"
            with pytest.raises(ValueError, match="description"):
                service.create_line(
                    actor=author,
                    project_id="project-boq-import",
                    draft=BoqLineDraft(
                        line_key="line-1",
                        wbs_node_id="wbs-1",
                        work_code="CABLE-INSTALLATION",
                        description="Different unreviewed description",
                        unit="m",
                        evidence_observation_ids=(
                            authoring.evidence_candidates[0].observation.observation_id,
                        ),
                        cost_components=(
                            {
                                "semantic_key": "boq-source-cable",
                                "category": "MATERIAL",
                                "basis_kind": "MARKET",
                                "sign": 1,
                                "factor_ids": (),
                            },
                        ),
                        critical_quantity=False,
                    ),
                    request_id="request-reject-unreviewed-description",
                    reason="The line must reproduce the reviewed canonical description",
                )
            line = service.create_line(
                actor=author,
                project_id="project-boq-import",
                draft=BoqLineDraft(
                    line_key="line-1",
                    wbs_node_id="wbs-1",
                    work_code="CABLE-INSTALLATION",
                    description="Cable installation",
                    unit="m",
                    evidence_observation_ids=(
                        authoring.evidence_candidates[0].observation.observation_id,
                    ),
                    cost_components=(
                        CostComponentDraft(
                            semantic_key="boq-source-cable",
                            category="MATERIAL",
                            basis_kind="MARKET",
                        ),
                    ),
                    critical_quantity=False,
                ),
                request_id="request-create-reviewed-imported-line",
                reason="Create the line from the independently reviewed spreadsheet identity",
            )
            critical_line = service.create_line(
                actor=author,
                project_id="project-boq-import",
                draft=BoqLineDraft(
                    line_key="line-critical-1",
                    wbs_node_id="wbs-critical-1",
                    work_code="CABLE-INSTALLATION",
                    description="Cable installation",
                    unit="m",
                    evidence_observation_ids=(
                        authoring.evidence_candidates[0].observation.observation_id,
                    ),
                    cost_components=(
                        CostComponentDraft(
                            semantic_key="boq-source-cable-critical",
                            category="MATERIAL",
                            basis_kind="MARKET",
                        ),
                    ),
                    critical_quantity=True,
                ),
                request_id="request-create-critical-imported-line",
                reason="Create a critical line to prove single-source quantity is blocked",
            )

        with factory.begin() as session:
            verified_line = BoqService(
                session=session,
                settings=settings,
                object_store=store,
            ).verify_line(
                actor=reviewer,
                project_id="project-boq-import",
                line_id=line.line_id,
                expected_line_updated_at=line.updated_at,
                request_id="request-verify-imported-line",
                reason="Independently verify the exact imported line structure",
            )
            assert verified_line.status.value == "VERIFIED"
            verified_critical_line = BoqService(
                session=session,
                settings=settings,
                object_store=store,
            ).verify_line(
                actor=reviewer,
                project_id="project-boq-import",
                line_id=critical_line.line_id,
                expected_line_updated_at=critical_line.updated_at,
                request_id="request-verify-critical-imported-line",
                reason="Independently verify critical line before quantity hard-stop test",
            )
            assert verified_critical_line.status.value == "VERIFIED"

        with factory.begin() as session:
            line_row = session.get(BoqLineRow, line.line_id)
            assert line_row is not None
            original_line_payload = dict(line_row.payload)
            line_row.payload = {
                **line_row.payload,
                "document_set_revision_id": "tampered-document-set",
            }

        with factory.begin() as session:
            drifted_context = BoqService(
                session=session,
                settings=settings,
                object_store=store,
            ).initial_quantity_context(
                actor=author,
                project_id="project-boq-import",
                line_id=line.line_id,
            )
            assert not drifted_context.recording_allowed
            assert "DOCUMENT_SET_MISMATCH" in drifted_context.recording_blockers

        with factory.begin() as session:
            line_row = session.get(BoqLineRow, line.line_id)
            assert line_row is not None
            line_row.payload = original_line_payload

        with factory.begin() as session:
            service = BoqService(session=session, settings=settings, object_store=store)
            quantity_context = service.initial_quantity_context(
                actor=author,
                project_id="project-boq-import",
                line_id=line.line_id,
            )
            assert quantity_context.recording_allowed
            assert len(quantity_context.evidence_candidates) == 1
            critical_context = service.initial_quantity_context(
                actor=author,
                project_id="project-boq-import",
                line_id=critical_line.line_id,
            )
            assert not critical_context.recording_allowed
            assert "INDEPENDENT_QUANTITY_EVIDENCE_REQUIRED" in critical_context.recording_blockers
            quantity_evidence = quantity_context.evidence_candidates[0]
            attached = service.attach_imported_quantity(
                actor=author,
                project_id="project-boq-import",
                line_id=line.line_id,
                command=AttachImportedQuantityCommand(
                    source_observation_id=quantity_evidence.observation.observation_id,
                    expected_source_observation_hash=quantity_evidence.observation_hash,
                    expected_line_updated_at=quantity_context.line.updated_at,
                ),
                request_id="request-attach-reviewed-imported-quantity",
                reason="Attach only the independently reviewed spreadsheet quantity",
            )
            assert attached.validation.passed
            assert str(attached.quantity.value) == "10"
            assert attached.quantity.unit == "m"
            repeated_context = service.initial_quantity_context(
                actor=author,
                project_id="project-boq-import",
                line_id=line.line_id,
            )
            assert not repeated_context.recording_allowed
            assert "CURRENT_QUANTITY_ALREADY_EXISTS" in repeated_context.recording_blockers

        with factory.begin() as session:
            approval = session.get(ApprovalRecordRow, decided.approval_id)
            assert approval is not None
            original_approval_payload = dict(approval.payload)
            approval.payload = {
                **approval.payload,
                "verified_observation_id": "tampered-derived-observation",
            }

        with (
            factory.begin() as session,
            pytest.raises(RuntimeError, match="decision payload"),
        ):
            BoqService(
                session=session,
                settings=settings,
                object_store=store,
            ).authoring_context(
                actor=author,
                project_id="project-boq-import",
                evidence_field_name="boq_line",
                limit=100,
            )

        with factory.begin() as session:
            approval = session.get(ApprovalRecordRow, decided.approval_id)
            assert approval is not None
            approval.payload = original_approval_payload
            assert decided.verified_observation is not None
            derived = session.get(
                ObservationRow,
                decided.verified_observation.observation_id,
            )
            assert derived is not None
            raw_observation = dict(derived.payload["observation"])
            raw_value = dict(raw_observation["value"])
            raw_value["description"] = "Tampered verified description"
            raw_observation["value"] = raw_value
            derived.payload = {**derived.payload, "observation": raw_observation}

        with (
            factory.begin() as session,
            pytest.raises(RuntimeError, match="derived content"),
        ):
            BoqService(
                session=session,
                settings=settings,
                object_store=store,
            ).authoring_context(
                actor=author,
                project_id="project-boq-import",
                evidence_field_name="boq_line",
                limit=100,
            )
    finally:
        engine.dispose()


def test_mapping_review_blocks_when_imported_source_lineage_drifts(
    tmp_path: Path,
) -> None:
    settings, factory, store, worker, engine = _setup(tmp_path)
    try:
        with factory.begin() as session:
            imported = BoqSpreadsheetImportService(
                session=session,
                settings=settings,
                object_store=store,
            ).import_current_workbook(
                actor=worker,
                project_id="project-boq-import",
                document_revision_id="revision-boq",
                adapter_qualification_id="boq-xlsx-qualification-v1",
                request_id="request-import-before-lineage-test",
                reason="Import rows for a negative lineage test",
            )
        author, reviewer, _estimator = _enable_mapping_workflow(
            settings=settings,
            factory=factory,
            store=store,
        )

        with factory.begin() as session:
            service = BoqService(session=session, settings=settings, object_store=store)
            context = service.spreadsheet_candidate_context(
                actor=author,
                project_id="project-boq-import",
                limit=100,
            )
            source = next(
                item
                for item in context.candidates
                if item.source_observation.observation_id == imported.observation_ids[0]
            )
            proposal = service.propose_spreadsheet_mapping(
                actor=author,
                project_id="project-boq-import",
                source_observation_id=source.source_observation.observation_id,
                command=BoqSpreadsheetMappingCommand(
                    work_code="CABLE-INSTALLATION",
                    description="Cable installation",
                    unit="m",
                    expected_source_observation_hash=source.source_observation_hash,
                    proposed_at=utc_now(),
                ),
                request_id="request-propose-before-lineage-drift",
                reason="Create a mapping whose upstream source must remain immutable",
            )

        with factory.begin() as session:
            source_row = session.get(ObservationRow, imported.observation_ids[0])
            assert source_row is not None
            source_row.payload = {**source_row.payload, "tampered": True}

        with factory.begin() as session:
            review = EvidenceService(
                session=session,
                settings=settings,
                object_store=store,
            ).manual_evidence_review(
                actor=reviewer,
                project_id="project-boq-import",
                observation_id=proposal.observation_id,
            )
            assert not review.decision_allowed
            assert "UPSTREAM_EVIDENCE_DRIFT" in review.decision_blockers
    finally:
        engine.dispose()
