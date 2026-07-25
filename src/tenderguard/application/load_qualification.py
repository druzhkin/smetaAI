from __future__ import annotations

import asyncio
import json
import os
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext

import httpx

from tenderguard.application.operational_qualification import build_result_envelope
from tenderguard.domain.common import utc_now
from tenderguard.domain.operational_qualification import (
    LoadEndpoint,
    LoadProfile,
    LoadSlo,
    QualificationFinding,
    QualificationResultEnvelope,
)


@dataclass(frozen=True, slots=True)
class RequestMeasurement:
    endpoint_name: str
    status_code: int | None
    latency_ns: int
    passed: bool
    error_code: str | None


class LoadQualificationService:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.transport = transport

    def run(
        self,
        *,
        profile_version_id: str,
        profile_content_hash: str,
        profile: LoadProfile,
    ) -> QualificationResultEnvelope:
        token = self._authentication_token(profile)
        started_at = utc_now()
        measurements, elapsed_ns = asyncio.run(self._run_requests(profile=profile, token=token))
        completed_at = utc_now()
        findings: list[QualificationFinding] = []
        evidence_metrics: dict[str, object] = {}
        self._evaluate_scope(
            scope_name="overall",
            measurements=measurements,
            elapsed_ns=elapsed_ns,
            slo=profile.overall_slo,
            findings=findings,
            evidence_metrics=evidence_metrics,
        )
        by_endpoint: dict[str, list[RequestMeasurement]] = defaultdict(list)
        for measurement in measurements:
            by_endpoint[measurement.endpoint_name].append(measurement)
        for endpoint in profile.endpoints:
            self._evaluate_scope(
                scope_name=endpoint.name,
                measurements=by_endpoint[endpoint.name],
                elapsed_ns=elapsed_ns,
                slo=endpoint.slo,
                findings=findings,
                evidence_metrics=evidence_metrics,
            )
        status = (
            "TECHNICAL_VERIFICATION_PASSED"
            if findings and all(finding.passed for finding in findings)
            else "FAILED"
        )
        evidence: dict[str, object] = {
            "target_environment": profile.target_environment,
            "expected_application_build_reference": (profile.expected_application_build_reference),
            "base_url_origin": profile.base_url,
            "duration_seconds": profile.duration_seconds,
            "concurrency": profile.concurrency,
            "maximum_requests": profile.maximum_requests,
            "request_timeout_seconds": profile.request_timeout_seconds,
            "maximum_response_bytes": profile.maximum_response_bytes,
            "production_change_reference": profile.production_change_reference,
            "elapsed_ns": elapsed_ns,
            "metrics": evidence_metrics,
            "independent_reviewer_signoff_required": True,
        }
        return build_result_envelope(
            qualification_type="LOAD",
            status=status,
            profile_version_id=profile_version_id,
            profile_content_hash=profile_content_hash,
            started_at=started_at,
            completed_at=completed_at,
            findings=tuple(findings),
            evidence=evidence,
        )

    async def _run_requests(
        self,
        *,
        profile: LoadProfile,
        token: str | None,
    ) -> tuple[list[RequestMeasurement], int]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "TenderGuard-Qualification/1",
        }
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        schedule = tuple(endpoint for endpoint in profile.endpoints for _ in range(endpoint.weight))
        measurements: list[RequestMeasurement] = []
        next_request = 0
        lock = asyncio.Lock()

        async with httpx.AsyncClient(
            base_url=profile.base_url,
            headers=headers,
            timeout=httpx.Timeout(profile.request_timeout_seconds),
            follow_redirects=False,
            transport=self.transport,
            limits=httpx.Limits(
                max_connections=profile.concurrency,
                max_keepalive_connections=profile.concurrency,
            ),
        ) as client:
            await self._verify_target_runtime(
                client=client,
                profile=profile,
            )
            start_ns = time.perf_counter_ns()
            deadline_ns = start_ns + profile.duration_seconds * 1_000_000_000

            async def worker() -> None:
                nonlocal next_request
                while True:
                    async with lock:
                        now_ns = time.perf_counter_ns()
                        if next_request >= profile.maximum_requests or now_ns >= deadline_ns:
                            return
                        request_index = next_request
                        next_request += 1
                    endpoint = schedule[request_index % len(schedule)]
                    measurement = await self._request(
                        client=client,
                        endpoint=endpoint,
                        maximum_response_bytes=profile.maximum_response_bytes,
                    )
                    measurements.append(measurement)

            await asyncio.gather(*(worker() for _ in range(profile.concurrency)))
        elapsed_ns = max(1, time.perf_counter_ns() - start_ns)
        return measurements, elapsed_ns

    @staticmethod
    async def _verify_target_runtime(
        *,
        client: httpx.AsyncClient,
        profile: LoadProfile,
    ) -> None:
        payload = bytearray()
        try:
            async with client.stream("GET", "/v1/runtime-config") as response:
                if response.status_code != 200:
                    raise ValueError("Load target runtime-config preflight failed")
                async for chunk in response.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > profile.maximum_response_bytes:
                        raise ValueError("Load target runtime-config exceeds response limit")
        except httpx.HTTPError as error:
            raise ValueError("Load target runtime-config is unavailable") from error
        try:
            runtime = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Load target runtime-config is not valid JSON") from error
        if (
            not isinstance(runtime, dict)
            or runtime.get("environment") != profile.target_environment
            or runtime.get("application_build_reference")
            != profile.expected_application_build_reference
        ):
            raise ValueError("Load target environment/build binding does not match the profile")

    @staticmethod
    async def _request(
        *,
        client: httpx.AsyncClient,
        endpoint: LoadEndpoint,
        maximum_response_bytes: int,
    ) -> RequestMeasurement:
        started_ns = time.perf_counter_ns()
        status_code: int | None = None
        error_code: str | None = None
        passed = False
        try:
            async with client.stream(endpoint.method, endpoint.path) as response:
                status_code = response.status_code
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > maximum_response_bytes:
                        error_code = "RESPONSE_TOO_LARGE"
                        break
                if error_code is None:
                    passed = status_code in endpoint.expected_statuses
                    if not passed:
                        error_code = "UNEXPECTED_STATUS"
        except httpx.TimeoutException:
            error_code = "TIMEOUT"
        except httpx.HTTPError:
            error_code = "HTTP_TRANSPORT_ERROR"
        except Exception:
            error_code = "UNEXPECTED_CLIENT_ERROR"
        return RequestMeasurement(
            endpoint_name=endpoint.name,
            status_code=status_code,
            latency_ns=max(0, time.perf_counter_ns() - started_ns),
            passed=passed,
            error_code=error_code,
        )

    @staticmethod
    def _authentication_token(profile: LoadProfile) -> str | None:
        if profile.auth_mode == "NONE":
            return None
        assert profile.auth_token_environment_variable is not None
        token = os.environ.get(profile.auth_token_environment_variable)
        if token is None or not token or token != token.strip() or "\r" in token or "\n" in token:
            raise ValueError("Load authentication token environment variable is absent or invalid")
        return token

    @classmethod
    def _evaluate_scope(
        cls,
        *,
        scope_name: str,
        measurements: list[RequestMeasurement],
        elapsed_ns: int,
        slo: LoadSlo,
        findings: list[QualificationFinding],
        evidence_metrics: dict[str, object],
    ) -> None:
        metrics = cls._metrics(measurements, elapsed_ns)
        evidence_metrics[scope_name] = metrics
        completed_requests = len(measurements)
        successful_requests = sum(measurement.passed for measurement in measurements)
        sorted_latencies = sorted(measurement.latency_ns for measurement in measurements)
        with localcontext() as context:
            context.prec = 50
            exact_success_ratio = (
                Decimal(successful_requests) / Decimal(completed_requests)
                if completed_requests
                else Decimal("0")
            )
            exact_p95_ms = Decimal(cls._percentile(sorted_latencies, 95)) / Decimal(1_000_000)
            exact_p99_ms = Decimal(cls._percentile(sorted_latencies, 99)) / Decimal(1_000_000)
            exact_requests_per_second = (
                Decimal(completed_requests) * Decimal(1_000_000_000) / Decimal(max(1, elapsed_ns))
            )
        checks = (
            (
                "MINIMUM_COMPLETED_REQUESTS",
                completed_requests >= slo.minimum_completed_requests,
                str(completed_requests),
                str(slo.minimum_completed_requests),
            ),
            (
                "SUCCESS_RATIO",
                exact_success_ratio >= slo.minimum_success_ratio,
                format(exact_success_ratio, "f"),
                str(slo.minimum_success_ratio),
            ),
            (
                "P95_LATENCY",
                exact_p95_ms <= slo.maximum_p95_ms,
                format(exact_p95_ms, "f"),
                str(slo.maximum_p95_ms),
            ),
            (
                "P99_LATENCY",
                exact_p99_ms <= slo.maximum_p99_ms,
                format(exact_p99_ms, "f"),
                str(slo.maximum_p99_ms),
            ),
            (
                "REQUEST_RATE",
                exact_requests_per_second >= slo.minimum_requests_per_second,
                format(exact_requests_per_second, "f"),
                str(slo.minimum_requests_per_second),
            ),
        )
        for metric_name, passed, actual, threshold in checks:
            findings.append(
                QualificationFinding(
                    code=f"LOAD_{scope_name.upper()}_{metric_name}",
                    passed=passed,
                    message=(
                        f"Load metric {metric_name} passed for {scope_name}"
                        if passed
                        else f"Load metric {metric_name} failed for {scope_name}"
                    ),
                    details={
                        "scope": scope_name,
                        "actual": actual,
                        "approved_threshold": threshold,
                    },
                )
            )

    @staticmethod
    def _metrics(
        measurements: list[RequestMeasurement],
        elapsed_ns: int,
    ) -> dict[str, object]:
        completed = len(measurements)
        successful = sum(measurement.passed for measurement in measurements)
        latencies = sorted(measurement.latency_ns for measurement in measurements)
        with localcontext() as context:
            context.prec = 50
            success_ratio = (
                Decimal(successful) / Decimal(completed) if completed else Decimal("0")
            ).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
            requests_per_second = (
                Decimal(completed) * Decimal(1_000_000_000) / Decimal(max(1, elapsed_ns))
            ).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
        status_counts = Counter(
            str(measurement.status_code) if measurement.status_code is not None else "NO_STATUS"
            for measurement in measurements
        )
        error_counts = Counter(
            measurement.error_code
            for measurement in measurements
            if measurement.error_code is not None
        )
        return {
            "completed_requests": completed,
            "successful_requests": successful,
            "success_ratio": str(success_ratio),
            "requests_per_second": str(requests_per_second),
            "p50_ms": cls_decimal_milliseconds(LoadQualificationService._percentile(latencies, 50)),
            "p95_ms": cls_decimal_milliseconds(LoadQualificationService._percentile(latencies, 95)),
            "p99_ms": cls_decimal_milliseconds(LoadQualificationService._percentile(latencies, 99)),
            "status_counts": dict(sorted(status_counts.items())),
            "error_counts": dict(sorted(error_counts.items())),
        }

    @staticmethod
    def _percentile(sorted_values: list[int], percentile: int) -> int:
        if not sorted_values:
            return 0
        rank = max(1, (len(sorted_values) * percentile + 99) // 100)
        return sorted_values[rank - 1]


def cls_decimal_milliseconds(nanoseconds: int) -> str:
    with localcontext() as context:
        context.prec = 50
        return str(
            (Decimal(nanoseconds) / Decimal(1_000_000)).quantize(
                Decimal("0.001"),
                rounding=ROUND_HALF_UP,
            )
        )
