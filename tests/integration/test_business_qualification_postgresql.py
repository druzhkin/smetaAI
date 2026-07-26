from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tenderguard.config import get_settings


def _require_postgresql() -> str:
    database_url = get_settings().database_url
    if not database_url.startswith("postgresql"):
        pytest.skip("Business qualification evidence guards require PostgreSQL")
    return database_url


def _assert_rejected(
    database_url: str,
    statement: str,
    parameters: dict[str, str],
    message: str,
) -> None:
    engine = create_engine(database_url)
    with pytest.raises(DBAPIError) as error_info, engine.begin() as connection:
        connection.execute(text(statement), parameters)
    engine.dispose()
    assert message in str(error_info.value)


def test_postgresql_business_qualification_evidence_is_append_only() -> None:
    database_url = _require_postgresql()
    suffix = uuid4().hex
    profile_id = f"profile-{suffix}"
    dataset_id = f"dataset-{suffix}"
    project_id = f"project-{suffix}"
    run_id = f"run-{suffix}"
    snapshot_id = f"snapshot-{suffix}"
    campaign_id = f"campaign-{suffix}"
    case_id = f"case-{suffix}"
    reference_id = f"reference-{suffix}"
    evaluation_id = f"evaluation-{suffix}"
    discrepancy_id = f"discrepancy-{suffix}"
    review_id = f"review-{suffix}"
    approval_id = f"approval-{suffix}"
    result_hash = ("e" * 32) + suffix
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO controlled_versions (
                    id, kind, version_label, content_hash, status, payload,
                    approved_by, approved_at
                ) VALUES
                    (
                        :profile_id, 'business_qualification_profile',
                        :profile_label, :profile_hash, 'DRAFT',
                        CAST('{}' AS json), NULL, NULL
                    ),
                    (
                        :dataset_id, 'business_qualification_dataset',
                        :dataset_label, :dataset_hash, 'DRAFT',
                        CAST('{}' AS json), NULL, NULL
                    )
                """
            ),
            {
                "profile_id": profile_id,
                "profile_label": f"profile-{suffix}",
                "profile_hash": "a" * 64,
                "dataset_id": dataset_id,
                "dataset_label": f"dataset-{suffix}",
                "dataset_hash": "b" * 64,
            },
        )
        connection.execute(
            text(
                """
                UPDATE controlled_versions
                SET status = 'APPROVED',
                    approved_by = 'owner-b',
                    approved_at = now()
                WHERE id IN (:profile_id, :dataset_id)
                """
            ),
            {"profile_id": profile_id, "dataset_id": dataset_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO projects (
                    id, organization_id, code, name, state, blocked_resume_state,
                    current_document_set_revision_id, row_version, created_at, updated_at
                ) VALUES (
                    :project_id, 'qualification-ci', :code, 'Qualification guard CI',
                    'DRAFT', NULL, NULL, 1, now(), now()
                )
                """
            ),
            {"project_id": project_id, "code": f"QUAL-{suffix}"},
        )
        connection.execute(
            text(
                """
                INSERT INTO calculation_runs (
                    id, project_id, engine_version, status, currency,
                    grand_total, payload, created_at
                ) VALUES (
                    :run_id, :project_id, 'engine-ci', 'VALIDATED', 'RUB',
                    100, CAST('{}' AS json), now()
                )
                """
            ),
            {"run_id": run_id, "project_id": project_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO calculation_snapshots (
                    id, project_id, calculation_run_id, document_set_revision_id,
                    input_hash, output_hash, snapshot_hash, fixed, object_key,
                    created_by, created_at
                ) VALUES (
                    :snapshot_id, :project_id, :run_id, 'document-set-ci',
                    :input_hash, :output_hash, :snapshot_hash, true,
                    :object_key, 'system-estimator', now()
                )
                """
            ),
            {
                "snapshot_id": snapshot_id,
                "project_id": project_id,
                "run_id": run_id,
                "input_hash": "c" * 64,
                "output_hash": "d" * 64,
                "snapshot_hash": result_hash,
                "object_key": f"objects/{result_hash}",
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO business_qualification_campaigns (
                    id, organization_id, profile_version_id, dataset_version_id,
                    profile_hash, dataset_hash, application_build_reference,
                    status, input_hash, payload, created_by, locked_at,
                    evaluated_by, evaluated_at, finalized_by, finalized_at,
                    result_hash
                ) VALUES (
                    :campaign_id, 'qualification-ci', :profile_id, :dataset_id,
                    :profile_hash, :dataset_hash, :build_reference,
                    'INPUTS_LOCKED', :input_hash, CAST('{}' AS json),
                    'campaign-creator', now(), NULL, NULL, NULL, NULL, NULL
                )
                """
            ),
            {
                "campaign_id": campaign_id,
                "profile_id": profile_id,
                "dataset_id": dataset_id,
                "profile_hash": "a" * 64,
                "dataset_hash": "b" * 64,
                "build_reference": "git:" + ("f" * 40),
                "input_hash": suffix + ("0" * 32),
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO business_qualification_cases (
                    id, campaign_id, case_key, mode, project_id, snapshot_id,
                    snapshot_hash, prediction_total, currency, prediction_hash,
                    stratum, created_at
                ) VALUES (
                    :case_id, :campaign_id, :case_key, 'HISTORICAL',
                    :project_id, :snapshot_id, :snapshot_hash, 100, 'RUB',
                    :prediction_hash, 'ci', now()
                )
                """
            ),
            {
                "case_id": case_id,
                "campaign_id": campaign_id,
                "case_key": f"case-{suffix}",
                "project_id": project_id,
                "snapshot_id": snapshot_id,
                "snapshot_hash": result_hash,
                "prediction_hash": "1" * 64,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO business_qualification_references (
                    id, campaign_id, case_id, reference_kind,
                    source_entity_type, source_entity_id, reference_total,
                    currency, evidence_hash, independence_domain,
                    professional_estimator_id, performed_at, payload,
                    registered_by, registered_at
                ) VALUES (
                    :reference_id, :campaign_id, :case_id, 'VERIFIED_ACTUAL',
                    'ACTUAL_RECORD', :source_id, 101, 'RUB', :evidence_hash,
                    'VERIFIED_PROJECT_ACTUAL', NULL, now(),
                    CAST('{}' AS json), 'campaign-creator', now()
                )
                """
            ),
            {
                "reference_id": reference_id,
                "campaign_id": campaign_id,
                "case_id": case_id,
                "source_id": f"actual-{suffix}",
                "evidence_hash": "2" * 64,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO business_qualification_evaluations (
                    id, campaign_id, metrics_passed, result_hash, payload,
                    evaluated_by, evaluated_at
                ) VALUES (
                    :evaluation_id, :campaign_id, true, :result_hash,
                    CAST('{}' AS json), 'evaluator', now()
                )
                """
            ),
            {
                "evaluation_id": evaluation_id,
                "campaign_id": campaign_id,
                "result_hash": result_hash,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO business_qualification_discrepancies (
                    id, campaign_id, evaluation_id, case_id, absolute_error,
                    exact_ratio_numerator, exact_ratio_denominator, payload,
                    created_at
                ) VALUES (
                    :discrepancy_id, :campaign_id, :evaluation_id, :case_id,
                    1, '1', '101', CAST('{}' AS json), now()
                )
                """
            ),
            {
                "discrepancy_id": discrepancy_id,
                "campaign_id": campaign_id,
                "evaluation_id": evaluation_id,
                "case_id": case_id,
            },
        )
        connection.execute(
            text(
                """
                UPDATE business_qualification_campaigns
                SET status='EXPERT_REVIEW', evaluated_by='evaluator',
                    evaluated_at=now(), result_hash=:result_hash
                WHERE id=:campaign_id
                """
            ),
            {"campaign_id": campaign_id, "result_hash": result_hash},
        )
        connection.execute(
            text(
                """
                INSERT INTO business_qualification_discrepancy_reviews (
                    id, discrepancy_id, decision, reason_code, root_cause,
                    corrective_action, evidence_hash, evidence_observation_ids,
                    reviewed_by, reviewed_at
                ) VALUES (
                    :review_id, :discrepancy_id, 'ACCEPTED', 'CI_VARIANCE',
                    'CI root cause', 'CI corrective action', :evidence_hash,
                    CAST('["observation-ci"]' AS json), 'reviewer', now()
                )
                """
            ),
            {
                "review_id": review_id,
                "discrepancy_id": discrepancy_id,
                "evidence_hash": "3" * 64,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO business_qualification_approvals (
                    id, campaign_id, evaluation_id, package_hash, reason,
                    approved_by, approved_at
                ) VALUES (
                    :approval_id, :campaign_id, :evaluation_id, :package_hash,
                    'CI approval', 'owner-c', now()
                )
                """
            ),
            {
                "approval_id": approval_id,
                "campaign_id": campaign_id,
                "evaluation_id": evaluation_id,
                "package_hash": "4" * 64,
            },
        )
        connection.execute(
            text(
                """
                UPDATE business_qualification_campaigns
                SET status='PASSED', finalized_by='owner-c', finalized_at=now()
                WHERE id=:campaign_id
                """
            ),
            {"campaign_id": campaign_id},
        )
    engine.dispose()

    for table_name, row_id in (
        ("business_qualification_cases", case_id),
        ("business_qualification_references", reference_id),
        ("business_qualification_evaluations", evaluation_id),
        ("business_qualification_discrepancies", discrepancy_id),
        ("business_qualification_discrepancy_reviews", review_id),
        ("business_qualification_approvals", approval_id),
    ):
        _assert_rejected(
            database_url,
            f"DELETE FROM {table_name} WHERE id=:id",
            {"id": row_id},
            "immutable TenderGuard record cannot be changed",
        )
    _assert_rejected(
        database_url,
        ("UPDATE business_qualification_campaigns SET profile_hash=:value WHERE id=:id"),
        {"id": campaign_id, "value": "9" * 64},
        "business qualification campaign basis is immutable",
    )
