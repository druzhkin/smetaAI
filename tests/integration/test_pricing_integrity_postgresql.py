from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tenderguard.config import get_settings


def _require_postgresql() -> str:
    database_url = get_settings().database_url
    if not database_url.startswith("postgresql"):
        pytest.skip("PostgreSQL pricing guards require a PostgreSQL test database")
    return database_url


def _assert_rejected(
    engine_url: str,
    statement: str,
    parameters: dict[str, str],
    expected_message: str,
) -> None:
    engine = create_engine(engine_url)
    with pytest.raises(DBAPIError) as exc_info, engine.begin() as connection:
        connection.execute(text(statement), parameters)
    engine.dispose()
    assert expected_message in str(exc_info.value)


def test_postgresql_pricing_evidence_guards_enforce_only_allowed_transitions() -> None:
    database_url = _require_postgresql()
    suffix = uuid4().hex
    project_id = f"project-{suffix}"
    policy_id = f"policy-{suffix}"
    quote_id = f"quote-{suffix}"
    normalized_id = f"normalized-{suffix}"
    decision_id = f"decision-{suffix}"
    engine = create_engine(database_url)

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO controlled_versions (
                    id, kind, version_label, content_hash, status, payload,
                    approved_by, approved_at
                ) VALUES (
                    :policy_id, 'PRICE_POLICY', :version_label, :content_hash,
                    'APPROVED', CAST('{}' AS json), 'ci-reviewer', now()
                )
                """
            ),
            {
                "policy_id": policy_id,
                "version_label": f"ci-{suffix}",
                "content_hash": "a" * 64,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO projects (
                    id, organization_id, code, name, state, blocked_resume_state,
                    current_document_set_revision_id, row_version, created_at, updated_at
                ) VALUES (
                    :project_id, 'ci-org', :code, 'Pricing guard CI', 'DRAFT',
                    NULL, NULL, 1, now(), now()
                )
                """
            ),
            {"project_id": project_id, "code": f"PRICE-GUARD-{suffix}"},
        )
        connection.execute(
            text(
                """
                INSERT INTO price_quotes (
                    id, project_id, item_id, status, quote_date, valid_until,
                    amount, currency, payload, created_at, updated_at,
                    source_observation_id
                ) VALUES (
                    :quote_id, :project_id, 'ci-item', 'UNNORMALIZED',
                    current_date, current_date + 30, 1250.00, 'RUB',
                    CAST('{}' AS json), now(), now(), NULL
                )
                """
            ),
            {"quote_id": quote_id, "project_id": project_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO normalized_prices (
                    id, quote_id, amount_per_unit, currency, formula_hash,
                    payload, created_at
                ) VALUES (
                    :normalized_id, :quote_id, 1250.00, 'RUB',
                    :formula_hash, CAST('{}' AS json), now()
                )
                """
            ),
            {
                "normalized_id": normalized_id,
                "quote_id": quote_id,
                "formula_hash": "b" * 64,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO price_decisions (
                    id, project_id, item_id, status, amount_per_unit, currency,
                    unit, policy_version_id, derived_observation_id,
                    supersedes_decision_id, is_current, payload, created_at
                ) VALUES (
                    :decision_id, :project_id, 'ci-item', 'VERIFIED', 1250.00,
                    'RUB', 'm', :policy_id, NULL, NULL, true,
                    CAST('{}' AS json), now()
                )
                """
            ),
            {
                "decision_id": decision_id,
                "project_id": project_id,
                "policy_id": policy_id,
            },
        )
    engine.dispose()

    _assert_rejected(
        database_url,
        "UPDATE price_quotes SET amount=1300 WHERE id=:quote_id",
        {"quote_id": quote_id},
        "price quote inputs are immutable",
    )
    _assert_rejected(
        database_url,
        "UPDATE normalized_prices SET amount_per_unit=1300 WHERE id=:normalized_id",
        {"normalized_id": normalized_id},
        "immutable TenderGuard record cannot be changed",
    )
    _assert_rejected(
        database_url,
        "UPDATE price_decisions SET amount_per_unit=1300 WHERE id=:decision_id",
        {"decision_id": decision_id},
        "price decision inputs and outputs are immutable",
    )

    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE price_quotes
                SET status='NORMALIZED', updated_at=now()
                WHERE id=:quote_id
                """
            ),
            {"quote_id": quote_id},
        )
        connection.execute(
            text(
                """
                UPDATE price_decisions
                SET is_current=false
                WHERE id=:decision_id
                """
            ),
            {"decision_id": decision_id},
        )
        quote = connection.execute(
            text("SELECT status, amount FROM price_quotes WHERE id=:quote_id"),
            {"quote_id": quote_id},
        ).one()
        decision = connection.execute(
            text(
                """
                SELECT is_current, amount_per_unit
                FROM price_decisions
                WHERE id=:decision_id
                """
            ),
            {"decision_id": decision_id},
        ).one()
    engine.dispose()

    assert quote.status == "NORMALIZED"
    assert quote.amount == 1250
    assert decision.is_current is False
    assert decision.amount_per_unit == 1250
