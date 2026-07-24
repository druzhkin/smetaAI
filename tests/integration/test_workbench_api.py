from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from tenderguard.api.main import create_app
from tenderguard.config import Settings
from tenderguard.infrastructure.database import (
    create_database_engine,
    create_schema_for_tests,
    create_session_factory,
)
from tenderguard.infrastructure.object_store import LocalObjectStore
from tenderguard.infrastructure.orm import (
    ActualRecordRow,
    ApprovalTaskRow,
    BoqLineRow,
    CalculationRunRow,
    CalculationSnapshotRow,
    CalibrationExampleRow,
    ControlledVersionRow,
    CostInputRow,
    DocumentRevisionRow,
    DocumentRow,
    NormalizedPriceRow,
    ObservationRow,
    PriceQuoteRow,
    ProjectPassportFactRow,
    QuantityRow,
    RiskCalculationRow,
    ScopeEvaluationRow,
    VarianceRecordRow,
    VerificationFindingRow,
)


def _headers(actor: str, *roles: str) -> dict[str, str]:
    return {
        "X-Dev-Actor": actor,
        "X-Dev-Organization": "org-1",
        "X-Dev-Roles": ",".join(roles or ("ESTIMATOR", "REVIEWER")),
    }


def _create_project(
    client: TestClient,
    *,
    headers: dict[str, str],
    code: str,
) -> str:
    response = client.post(
        "/v1/projects",
        headers=headers,
        json={
            "code": code,
            "name": f"Industrial tender {code}",
            "reason": "Create an operator workbench fixture",
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def test_estimator_only_owner_can_open_new_project_workbench_and_audit(
    tmp_path: Path,
) -> None:
    settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite://",
        local_object_store_path=tmp_path / "objects",
        allow_insecure_dev_auth=True,
        audit_signing_key="workbench-audit-signing-key-at-least-32-bytes",
    )
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    app = create_app(
        settings,
        engine=engine,
        object_store=LocalObjectStore(tmp_path / "objects"),
    )
    estimator = _headers("estimator-only", "ESTIMATOR")

    with TestClient(app) as client:
        project_id = _create_project(client, headers=estimator, code="WB-ESTIMATOR")

        workbench = client.get(
            f"/v1/projects/{project_id}/workbench",
            headers=estimator,
        )
        assert workbench.status_code == 200, workbench.text
        assert workbench.json()["project"]["state"] == "DRAFT"
        assert workbench.json()["release_decision"]["allowed"] is False

        audit = client.get(
            f"/v1/projects/{project_id}/records",
            headers=estimator,
            params={"section": "AUDIT"},
        )
        assert audit.status_code == 200, audit.text
        assert any(
            record["kind"] == "AUDIT_EVENT" and record["title"] == "project_created"
            for record in audit.json()["items"]
        )


def test_operator_read_models_are_scoped_paginated_and_fail_closed(
    tmp_path: Path,
) -> None:
    settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite://",
        local_object_store_path=tmp_path / "objects",
        allow_insecure_dev_auth=True,
        audit_signing_key="workbench-audit-signing-key-at-least-32-bytes",
    )
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    store = LocalObjectStore(tmp_path / "objects")
    app = create_app(settings, engine=engine, object_store=store)
    owner = _headers("portfolio-owner")
    outsider = _headers("same-tenant-outsider")
    technical_user = _headers("technical-reader", "TECHNICAL_EXPERT")
    independent_reviewer = _headers("independent-reviewer", "REVIEWER")

    with TestClient(app) as client:
        project_ids = [
            _create_project(client, headers=owner, code=f"WB-{index}") for index in range(1, 4)
        ]
        outsider_project_id = _create_project(
            client,
            headers=outsider,
            code="WB-OUTSIDER",
        )
        primary_id = project_ids[0]
        membership = client.post(
            f"/v1/projects/{primary_id}/members",
            headers=owner,
            json={
                "principal_id": "technical-reader",
                "roles": ["TECHNICAL_EXPERT"],
                "access_level": "MEMBER",
                "reason": "Technical scope review without commercial access",
            },
        )
        assert membership.status_code == 200, membership.text
        reviewer_membership = client.post(
            f"/v1/projects/{primary_id}/members",
            headers=owner,
            json={
                "principal_id": "independent-reviewer",
                "roles": ["REVIEWER"],
                "access_level": "MEMBER",
                "reason": "Independent approval duty for operator workflow testing",
            },
        )
        assert reviewer_membership.status_code == 200, reviewer_membership.text
        now = datetime.now(UTC)
        with create_session_factory(engine).begin() as session:
            session.add_all(
                (
                    ApprovalTaskRow(
                        id="approval-task-workbench",
                        project_id=primary_id,
                        task_type="HIGH_VALUE_REVIEW",
                        entity_type="boq_line",
                        entity_id="boq-workbench",
                        assigned_role="REVIEWER",
                        status="PENDING",
                        required=True,
                        payload={
                            "created_by": "portfolio-owner",
                            "policy_version_id": "approval-policy-test",
                        },
                        created_at=now,
                        updated_at=now,
                    ),
                    VerificationFindingRow(
                        id="finding-workbench",
                        project_id=primary_id,
                        contour="PRICE",
                        code="PRICE_EVIDENCE_MISSING",
                        severity="BLOCKER",
                        resolved=False,
                        payload={
                            "message": "Critical pump has no triangulated price",
                            "entity_ids": ["pump-1"],
                        },
                        created_at=now,
                        updated_at=now,
                    ),
                    CalculationRunRow(
                        id="calculation-workbench",
                        project_id=primary_id,
                        engine_version="test-engine-1",
                        status="VALIDATED",
                        currency="RUB",
                        grand_total=Decimal("1250000.00"),
                        payload={},
                        created_at=now,
                    ),
                    DocumentRow(
                        id="document-workbench",
                        project_id=primary_id,
                        logical_key="technical-specification",
                        title="Technical specification",
                        document_type="TECHNICAL_SPECIFICATION",
                        critical=True,
                        cancelled=False,
                        created_at=now,
                        updated_at=now,
                    ),
                    BoqLineRow(
                        id="boq-workbench",
                        project_id=primary_id,
                        line_key="PIPE-001",
                        wbs_node_id="WBS-PIPE",
                        work_code="PIPE_INSTALLATION",
                        description="Install pressure pipeline",
                        unit="m",
                        status="VERIFIED",
                        supersedes_line_id=None,
                        is_current=True,
                        payload={
                            "expected_cost_components": [
                                "LABOUR",
                                "MATERIAL",
                                "LOGISTICS",
                            ]
                        },
                        created_at=now,
                        updated_at=now,
                    ),
                    ControlledVersionRow(
                        id="policy-workbench",
                        kind="scope_rule_pack",
                        version_label="workbench-v1",
                        content_hash="b" * 64,
                        status="APPROVED",
                        payload={},
                        approved_by="methodology-owner",
                        approved_at=now,
                    ),
                    PriceQuoteRow(
                        id="quote-workbench",
                        project_id=primary_id,
                        item_id="PUMP-001",
                        status="NORMALIZED",
                        quote_date=now.date(),
                        valid_until=now.date(),
                        amount=Decimal("100000.00"),
                        currency="RUB",
                        source_observation_id=None,
                        payload={},
                        created_at=now,
                        updated_at=now,
                    ),
                    ProjectPassportFactRow(
                        id="passport-workbench",
                        project_id=primary_id,
                        field_name="project_address",
                        status="VERIFIED",
                        supersedes_fact_id=None,
                        is_current=True,
                        payload={
                            "value": "Industrial zone 1",
                            "observation_ids": ["observation-workbench"],
                            "requirements_version_id": "policy-workbench",
                            "created_by": "portfolio-owner",
                        },
                        created_at=now,
                        updated_at=now,
                    ),
                    ScopeEvaluationRow(
                        id="scope-evaluation-workbench",
                        project_id=primary_id,
                        wbs_node_id="WBS-PIPE",
                        rule_pack_version_id="policy-workbench",
                        status="PASSED",
                        input_signature="c" * 64,
                        supersedes_evaluation_id=None,
                        is_current=True,
                        payload={
                            "evaluation": {
                                "evaluated_work_codes": ["PIPE_INSTALLATION"],
                                "findings": [],
                            }
                        },
                        created_at=now,
                    ),
                    RiskCalculationRow(
                        id="risk-calculation-workbench",
                        project_id=primary_id,
                        policy_version_id="policy-workbench",
                        status="VALIDATED",
                        expected_reserve=Decimal("25000.00"),
                        currency="RUB",
                        unit="project",
                        supersedes_calculation_id=None,
                        is_current=True,
                        payload={
                            "input_signature": "d" * 64,
                            "calculation": {"findings": []},
                            "calculated_by": "portfolio-owner",
                        },
                        created_at=now,
                    ),
                    CostInputRow(
                        id="cost-input-workbench",
                        project_id=primary_id,
                        calculation_run_id="calculation-workbench",
                        semantic_key="WBS-PIPE:MATERIAL",
                        category="MATERIAL",
                        amount_basis_id="normalized-price-workbench",
                        payload={
                            "line_id": "boq-workbench",
                            "wbs_node_id": "WBS-PIPE",
                            "quantity": "10",
                            "unit": "pcs",
                            "unit_rate": "100000",
                            "currency": "RUB",
                            "sign": 1,
                            "factors": [],
                        },
                        created_at=now,
                    ),
                    CalculationSnapshotRow(
                        id="snapshot-workbench",
                        project_id=primary_id,
                        calculation_run_id="calculation-workbench",
                        document_set_revision_id="document-set-workbench",
                        input_hash="e" * 64,
                        output_hash="f" * 64,
                        snapshot_hash="1" * 64,
                        fixed=True,
                        object_key="snapshots/workbench.json",
                        created_by="portfolio-owner",
                        created_at=now,
                    ),
                )
            )
            session.flush()
            session.add_all(
                (
                    DocumentRevisionRow(
                        id="document-revision-workbench",
                        document_id="document-workbench",
                        revision_label="R1",
                        issue_date=now.date(),
                        object_hash="a" * 64,
                        object_key="documents/a",
                        original_filename="technical-specification.pdf",
                        media_type="application/pdf",
                        size_bytes=2048,
                        supersedes_revision_id=None,
                        is_current=True,
                        corrupt=False,
                        protected=False,
                        inspection_payload={},
                        created_at=now,
                        updated_at=now,
                    ),
                    QuantityRow(
                        id="quantity-workbench",
                        boq_line_id="boq-workbench",
                        value=Decimal("420.000"),
                        unit="m",
                        status="VERIFIED",
                        supersedes_quantity_id=None,
                        is_current=True,
                        payload={},
                        created_at=now,
                        updated_at=now,
                    ),
                    NormalizedPriceRow(
                        id="normalized-price-workbench",
                        quote_id="quote-workbench",
                        amount_per_unit=Decimal("105000.00"),
                        currency="RUB",
                        formula_hash="2" * 64,
                        payload={
                            "policy_version_id": "policy-workbench",
                            "normalized_price": {
                                "target_basis": {
                                    "unit": "pcs",
                                    "currency": "RUB",
                                },
                                "delivery_component": "5000.00",
                                "unloading_component": "0.00",
                            },
                        },
                        created_at=now,
                    ),
                    ObservationRow(
                        id="observation-workbench",
                        project_id=primary_id,
                        document_revision_id="document-revision-workbench",
                        field_name="project_address",
                        method="PDF_TEXT",
                        method_version="test-1",
                        status="VERIFIED",
                        payload={"value": "Industrial zone 1"},
                        created_at=now,
                    ),
                )
            )
            session.flush()
            session.add(
                ActualRecordRow(
                    id="actual-workbench",
                    project_id=primary_id,
                    actual_key="actual-material-cost",
                    entity_type="boq_line",
                    entity_id="boq-workbench",
                    metric="MATERIAL_COST",
                    value=Decimal("1100000.00"),
                    unit="RUB",
                    verified=True,
                    source_observation_id="observation-workbench",
                    occurred_on=now.date(),
                    payload={},
                    supersedes_actual_id=None,
                    is_current=True,
                    created_at=now,
                )
            )
            session.flush()
            session.add(
                VarianceRecordRow(
                    id="variance-workbench",
                    project_id=primary_id,
                    actual_record_id="actual-workbench",
                    snapshot_id="snapshot-workbench",
                    metric="MATERIAL_COST",
                    reason="MARKET_CHANGE",
                    absolute_variance=Decimal("100000.00"),
                    relative_variance=Decimal("0.10"),
                    payload={},
                    classified_by="portfolio-owner",
                    created_at=now,
                )
            )
            session.flush()
            session.add(
                CalibrationExampleRow(
                    id="calibration-workbench",
                    project_id=primary_id,
                    actual_record_id="actual-workbench",
                    variance_record_id="variance-workbench",
                    features_snapshot_id="snapshot-workbench",
                    metric="MATERIAL_COST",
                    target_value=Decimal("1100000.00"),
                    unit="RUB",
                    approved=False,
                    payload={
                        "created_by": "portfolio-owner",
                        "calibration_example": {"variance_reason": "MARKET_CHANGE"},
                    },
                    created_at=now,
                )
            )

        first_page = client.get(
            "/v1/projects",
            headers=owner,
            params={"limit": 1},
        )
        assert first_page.status_code == 200, first_page.text
        assert len(first_page.json()["items"]) == 1
        cursor = first_page.json()["next_cursor"]
        assert cursor
        second_page = client.get(
            "/v1/projects",
            headers=owner,
            params={"limit": 1, "cursor": cursor},
        )
        assert second_page.status_code == 200, second_page.text
        assert (
            second_page.json()["items"][0]["project"]["id"]
            != first_page.json()["items"][0]["project"]["id"]
        )

        portfolio = client.get("/v1/projects", headers=owner)
        assert portfolio.status_code == 200, portfolio.text
        visible_ids = {item["project"]["id"] for item in portfolio.json()["items"]}
        assert visible_ids == set(project_ids)
        assert outsider_project_id not in visible_ids
        primary = next(
            item for item in portfolio.json()["items"] if item["project"]["id"] == primary_id
        )
        assert primary["open_approval_count"] == 1
        assert primary["unresolved_blocker_count"] == 1
        assert primary["latest_total"] == "1250000.000000000000"
        assert primary["latest_currency"] == "RUB"

        technical_portfolio = client.get("/v1/projects", headers=technical_user)
        assert technical_portfolio.status_code == 200, technical_portfolio.text
        assert len(technical_portfolio.json()["items"]) == 1
        technical_item = technical_portfolio.json()["items"][0]
        assert technical_item["project"]["id"] == primary_id
        assert technical_item["latest_total"] is None
        assert technical_item["latest_currency"] is None

        search = client.get(
            "/v1/projects",
            headers=owner,
            params={"query": "WB-1"},
        )
        assert [item["project"]["id"] for item in search.json()["items"]] == [primary_id]
        assert (
            client.get(
                f"/v1/projects/{outsider_project_id}/workbench",
                headers=owner,
            ).status_code
            == 404
        )
        assert (
            client.get(
                f"/v1/projects/{primary_id}/workbench",
                headers=technical_user,
            ).status_code
            == 403
        )

        tasks = client.get("/v1/work-items", headers=owner)
        assert tasks.status_code == 200, tasks.text
        assert [item["task_id"] for item in tasks.json()["items"]] == ["approval-task-workbench"]
        owner_task = client.get(
            "/v1/work-items/approval-task-workbench",
            headers=owner,
        )
        assert owner_task.status_code == 200, owner_task.text
        assert owner_task.json()["decision_allowed"] is False
        assert owner_task.json()["decision_blockers"] == ["FOUR_EYES_TASK_CREATOR"]
        reviewer_task = client.get(
            "/v1/work-items/approval-task-workbench",
            headers=independent_reviewer,
        )
        assert reviewer_task.status_code == 200, reviewer_task.text
        assert reviewer_task.json()["decision_allowed"] is True
        assert (
            client.get(
                "/v1/work-items/approval-task-workbench",
                headers=outsider,
            ).status_code
            == 404
        )

        workbench = client.get(
            f"/v1/projects/{primary_id}/workbench",
            headers=owner,
        )
        assert workbench.status_code == 200, workbench.text
        body = workbench.json()
        assert body["release_decision"]["allowed"] is False
        assert body["release_decision"]["resulting_state"] == "BLOCKED"
        assert body["latest_total"] == "1250000.000000000000"
        assert any(item["id"] == "finding-workbench" for item in body["attention"])
        metrics = {item["code"]: item for item in body["metrics"]}
        assert metrics["DOCUMENTS"]["value"] == 1
        assert metrics["BOQ"]["value"] == 1
        assert metrics["APPROVALS"]["blocking"] == 1

        document_records = client.get(
            f"/v1/projects/{primary_id}/records",
            headers=owner,
            params={"section": "DOCUMENTS", "current_only": True},
        )
        assert document_records.status_code == 200, document_records.text
        assert any(
            item["id"] == "document-revision-workbench" for item in document_records.json()["items"]
        )
        assert (
            client.get(
                f"/v1/projects/{primary_id}/records",
                headers=technical_user,
                params={"section": "DOCUMENTS"},
            ).status_code
            == 200
        )
        boq_records = client.get(
            f"/v1/projects/{primary_id}/records",
            headers=owner,
            params={"section": "BOQ_SCOPE", "query": "pressure"},
        )
        assert boq_records.status_code == 200, boq_records.text
        boq = next(item for item in boq_records.json()["items"] if item["id"] == "boq-workbench")
        assert boq["amount"] == "420.000000000000"
        assert boq["attributes"]["quantity_status"] == "VERIFIED"
        assert (
            client.get(
                f"/v1/projects/{primary_id}/records",
                headers=technical_user,
                params={"section": "BOQ_SCOPE"},
            ).status_code
            == 200
        )
        for restricted_section in ("PRICING", "CALCULATION", "AUDIT"):
            assert (
                client.get(
                    f"/v1/projects/{primary_id}/records",
                    headers=technical_user,
                    params={"section": restricted_section},
                ).status_code
                == 403
            )

        expected_record_kinds = {
            "EVIDENCE": {"OBSERVATION", "PASSPORT_FACT"},
            "BOQ_SCOPE": {"BOQ_LINE", "SCOPE_EVALUATION"},
            "PRICING": {"PRICE_QUOTE", "NORMALIZED_PRICE"},
            "CONTRACT_RISK": {"RISK_CALCULATION"},
            "CALCULATION": {
                "CALCULATION_RUN",
                "ATOMIC_COST_INPUT",
                "SNAPSHOT",
            },
            "ACTUALS": {"ACTUAL", "VARIANCE", "CALIBRATION_EXAMPLE"},
        }
        for section, expected_kinds in expected_record_kinds.items():
            response = client.get(
                f"/v1/projects/{primary_id}/records",
                headers=owner,
                params={"section": section},
            )
            assert response.status_code == 200, response.text
            actual_kinds = {item["kind"] for item in response.json()["items"]}
            assert expected_kinds <= actual_kinds

        audit_records = client.get(
            f"/v1/projects/{primary_id}/records",
            headers=owner,
            params={"section": "AUDIT", "limit": 1},
        )
        assert audit_records.status_code == 200, audit_records.text
        assert audit_records.json()["items"][0]["kind"] == "AUDIT_EVENT"

        wrong_cursor = client.get(
            f"/v1/projects/{primary_id}/records",
            headers=owner,
            params={"section": "AUDIT", "cursor": cursor},
        )
        assert wrong_cursor.status_code == 422
        assert "another query" in wrong_cursor.json()["detail"]

        decision_path = f"/v1/projects/{primary_id}/approvals/approval-task-workbench/decision"
        stale_decision = client.post(
            decision_path,
            headers=independent_reviewer,
            json={
                "decision": "APPROVED",
                "reason": "Reviewed the underlying quantity and source location",
                "expected_task_updated_at": "2000-01-01T00:00:00Z",
                "evidence_ids": ["observation-workbench"],
            },
        )
        assert stale_decision.status_code == 409, stale_decision.text
        missing_evidence = client.post(
            decision_path,
            headers=independent_reviewer,
            json={
                "decision": "APPROVED",
                "reason": "A typed but nonexistent evidence identifier cannot be accepted",
                "expected_task_updated_at": reviewer_task.json()["item"]["updated_at"],
                "evidence_ids": ["invented-observation"],
            },
        )
        assert missing_evidence.status_code == 422, missing_evidence.text
        assert "do not exist" in missing_evidence.json()["detail"]
        decision_headers = {
            **independent_reviewer,
            "Idempotency-Key": "workbench-approval-decision-1",
        }
        decision_payload = {
            "decision": "APPROVED",
            "reason": "Reviewed the underlying quantity and exact source observation",
            "expected_task_updated_at": reviewer_task.json()["item"]["updated_at"],
            "evidence_ids": ["observation-workbench"],
        }
        decision = client.post(
            decision_path,
            headers=decision_headers,
            json=decision_payload,
        )
        assert decision.status_code == 200, decision.text
        replay = client.post(
            decision_path,
            headers=decision_headers,
            json=decision_payload,
        )
        assert replay.status_code == 200, replay.text
        assert replay.json() == decision.json()
        decided_task = client.get(
            "/v1/work-items/approval-task-workbench",
            headers=independent_reviewer,
        )
        assert decided_task.status_code == 200, decided_task.text
        assert decided_task.json()["decision_allowed"] is False
        assert decided_task.json()["decision_blockers"] == ["TASK_NOT_PENDING"]
        assert decided_task.json()["decisions"][0]["decision"] == "APPROVED"
        assert decided_task.json()["decisions"][0]["evidence_ids"] == ["observation-workbench"]

    engine.dispose()


def test_work_item_pagination_filters_project_roles_before_limit(
    tmp_path: Path,
) -> None:
    settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite://",
        local_object_store_path=tmp_path / "objects",
        allow_insecure_dev_auth=True,
        audit_signing_key="workbench-pagination-audit-key-at-least-32-bytes",
    )
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    app = create_app(
        settings,
        engine=engine,
        object_store=LocalObjectStore(tmp_path / "objects"),
    )
    owner = _headers("pagination-owner", "ESTIMATOR", "REVIEWER")

    with TestClient(app) as client:
        project_id = _create_project(
            client,
            headers=owner,
            code="WB-PAGINATION",
        )
        narrowed = client.post(
            f"/v1/projects/{project_id}/members",
            headers=owner,
            json={
                "principal_id": "pagination-owner",
                "roles": ["ESTIMATOR"],
                "access_level": "OWNER",
                "reason": "Limit project duty to estimating while retaining identity roles",
            },
        )
        assert narrowed.status_code == 200, narrowed.text

        now = datetime.now(UTC)
        with create_session_factory(engine).begin() as session:
            session.add_all(
                [
                    ApprovalTaskRow(
                        id=f"approval-task-ineligible-{index}",
                        project_id=project_id,
                        task_type="REVIEW_ONLY",
                        entity_type="boq_line",
                        entity_id=f"line-ineligible-{index}",
                        assigned_role="REVIEWER",
                        status="PENDING",
                        required=True,
                        payload={"created_by": "another-user"},
                        created_at=now + timedelta(minutes=10 + index),
                        updated_at=now + timedelta(minutes=10 + index),
                    )
                    for index in range(5)
                ]
            )
            session.add(
                ApprovalTaskRow(
                    id="approval-task-eligible-after-role-filter",
                    project_id=project_id,
                    task_type="ESTIMATOR_REVIEW",
                    entity_type="boq_line",
                    entity_id="line-eligible",
                    assigned_role="ESTIMATOR",
                    status="PENDING",
                    required=True,
                    payload={"created_by": "another-user"},
                    created_at=now,
                    updated_at=now,
                )
            )

        work_items = client.get(
            "/v1/work-items",
            headers=owner,
            params={"limit": 1},
        )
        assert work_items.status_code == 200, work_items.text
        assert [item["task_id"] for item in work_items.json()["items"]] == [
            "approval-task-eligible-after-role-filter"
        ]
        assert work_items.json()["next_cursor"] is None

        portfolio = client.get("/v1/projects", headers=owner)
        assert portfolio.status_code == 200, portfolio.text
        assert portfolio.json()["items"][0]["access"]["roles"] == ["ESTIMATOR"]

    engine.dispose()
