from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from tenderguard.application.load_qualification import (
    LoadQualificationService,
    RequestMeasurement,
)
from tenderguard.application.recovery_verification import RecoveryVerificationService
from tenderguard.domain.common import content_hash
from tenderguard.domain.operational_qualification import (
    LoadEndpoint,
    LoadProfile,
    LoadSlo,
    QualificationFinding,
    QualificationResultEnvelope,
    RecoveryProfile,
)

TEST_BUILD_REFERENCE = "git:" + "1" * 40
PRODUCTION_BUILD_REFERENCE = "sha256:" + "a" * 64


def _slo(*, minimum_completed_requests: int = 1) -> LoadSlo:
    return LoadSlo(
        minimum_success_ratio=Decimal("1"),
        maximum_p95_ms=Decimal("1000"),
        maximum_p99_ms=Decimal("2000"),
        minimum_requests_per_second=Decimal("0.001"),
        minimum_completed_requests=minimum_completed_requests,
    )


def _load_profile(**updates: object) -> LoadProfile:
    values: dict[str, object] = {
        "schema_version": "tenderguard.load-profile/v1",
        "target_environment": "test",
        "expected_application_build_reference": TEST_BUILD_REFERENCE,
        "base_url": "http://127.0.0.1:8099",
        "duration_seconds": 1,
        "concurrency": 2,
        "maximum_requests": 10,
        "request_timeout_seconds": 5,
        "maximum_response_bytes": 1024,
        "auth_mode": "NONE",
        "auth_token_environment_variable": None,
        "allow_production_target": False,
        "production_change_reference": None,
        "endpoints": (
            LoadEndpoint(
                name="health",
                method="GET",
                path="/health",
                weight=1,
                expected_statuses=(200,),
                slo=_slo(),
            ),
        ),
        "overall_slo": _slo(),
    }
    values.update(updates)
    return LoadProfile.model_validate(values)


def test_recovery_profile_rejects_same_environment_and_production_waiver() -> None:
    base = {
        "schema_version": "tenderguard.recovery-profile/v1",
        "source_environment": "production",
        "restore_environment": "staging",
        "expected_application_build_reference": PRODUCTION_BUILD_REFERENCE,
        "maximum_rpo_seconds": 300,
        "maximum_rto_seconds": 3600,
        "require_worm": True,
        "require_external_audit_anchor": True,
        "require_oidc_configuration": True,
        "require_export_signing_configuration": True,
        "require_integration_signing_configuration": True,
        "required_adapter_qualification_ids": ("adapter-1",),
        "required_golden_snapshot_ids": ("snapshot-1",),
    }
    RecoveryProfile.model_validate(base)

    with pytest.raises(ValidationError, match="isolated"):
        RecoveryProfile.model_validate({**base, "restore_environment": "production"})
    with pytest.raises(ValidationError, match="cannot waive"):
        RecoveryProfile.model_validate({**base, "require_worm": False})


def test_load_profile_rejects_mutation_unsafe_origin_and_implicit_production() -> None:
    with pytest.raises(ValidationError):
        _load_profile(
            endpoints=(
                {
                    "name": "mutation",
                    "method": "POST",
                    "path": "/v1/projects",
                    "weight": 1,
                    "expected_statuses": (201,),
                    "slo": _slo(),
                },
            )
        )
    with pytest.raises(ValidationError, match="HTTPS origin"):
        _load_profile(base_url="https://user:password@example.test/path")
    with pytest.raises(ValidationError, match="Production load target"):
        _load_profile(target_environment="production")
    with pytest.raises(ValidationError, match="Floating-point"):
        LoadSlo.model_validate(
            {
                "minimum_success_ratio": 0.99,
                "maximum_p95_ms": "1000",
                "maximum_p99_ms": "2000",
                "minimum_requests_per_second": "1",
                "minimum_completed_requests": 1,
            }
        )


def test_load_metrics_use_nearest_rank_and_exact_decimal_rates() -> None:
    measurements = [
        RequestMeasurement("health", 200, latency, True, None)
        for latency in (1_000_000, 2_000_000, 3_000_000, 4_000_000)
    ]

    metrics = LoadQualificationService._metrics(
        measurements,
        elapsed_ns=2_000_000_000,
    )

    assert metrics["p50_ms"] == "2.000"
    assert metrics["p95_ms"] == "4.000"
    assert metrics["p99_ms"] == "4.000"
    assert metrics["success_ratio"] == "1.000000"
    assert metrics["requests_per_second"] == "2.000"


def test_recovery_objective_measurement_does_not_truncate_fractional_seconds() -> None:
    started = datetime(2026, 7, 24, tzinfo=UTC)

    measured = RecoveryVerificationService._seconds_between(
        started,
        started + timedelta(seconds=60, microseconds=1),
    )

    assert measured == Decimal("60.000001")
    assert measured > 60


def test_load_slo_decision_uses_unrounded_measurement() -> None:
    findings: list[QualificationFinding] = []
    metrics: dict[str, object] = {}

    LoadQualificationService._evaluate_scope(
        scope_name="boundary",
        measurements=[
            RequestMeasurement(
                "boundary",
                200,
                1_000_000_001,
                True,
                None,
            )
        ],
        elapsed_ns=1_000_000_000,
        slo=_slo(),
        findings=findings,
        evidence_metrics=metrics,
    )

    p95 = next(finding for finding in findings if finding.code.endswith("P95_LATENCY"))
    assert metrics["boundary"]["p95_ms"] == "1000.000"
    assert p95.details["actual"] == "1000.000001"
    assert p95.passed is False


def test_load_runner_emits_tamper_evident_result_without_credentials() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        if request.url.path == "/v1/runtime-config":
            return httpx.Response(
                200,
                json={
                    "environment": "test",
                    "application_build_reference": TEST_BUILD_REFERENCE,
                },
            )
        assert request.url.path == "/health"
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"status": "ok"})

    result = LoadQualificationService(transport=httpx.MockTransport(handler)).run(
        profile_version_id="version-load-1",
        profile_content_hash="a" * 64,
        profile=_load_profile(),
    )
    result_body = result.model_dump(mode="python")
    result_hash = str(result_body.pop("result_hash"))

    assert result.status == "TECHNICAL_VERIFICATION_PASSED"
    assert result.evidence["independent_reviewer_signoff_required"] is True
    assert result_hash == content_hash(result_body)
    with pytest.raises(ValidationError, match="result hash"):
        QualificationResultEnvelope.model_validate(
            {
                **result.model_dump(mode="python"),
                "status": "FAILED",
            }
        )


def test_load_runner_fails_closed_on_bounded_response_violation() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/runtime-config":
            return httpx.Response(
                200,
                json={
                    "environment": "test",
                    "application_build_reference": TEST_BUILD_REFERENCE,
                },
            )
        return httpx.Response(200, content=b"x" * 2048)

    result = LoadQualificationService(transport=httpx.MockTransport(handler)).run(
        profile_version_id="version-load-1",
        profile_content_hash="b" * 64,
        profile=_load_profile(),
    )

    assert result.status == "FAILED"
    metrics = result.evidence["metrics"]
    assert isinstance(metrics, dict)
    assert metrics["overall"]["error_counts"] == {"RESPONSE_TOO_LARGE": 10}


def test_load_runner_blocks_before_workload_on_target_build_mismatch() -> None:
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "environment": "test",
                "application_build_reference": "git:" + "2" * 40,
            },
        )

    with pytest.raises(ValueError, match="environment/build binding"):
        LoadQualificationService(transport=httpx.MockTransport(handler)).run(
            profile_version_id="version-load-1",
            profile_content_hash="c" * 64,
            profile=_load_profile(),
        )

    assert paths == ["/v1/runtime-config"]
