from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from tenderguard.domain.common import content_hash
from tenderguard.domain.models import DomainModel

RECOVERY_PROFILE_SCHEMA_VERSION = "tenderguard.recovery-profile/v1"
RECOVERY_EXERCISE_SCHEMA_VERSION = "tenderguard.recovery-exercise/v1"
LOAD_PROFILE_SCHEMA_VERSION = "tenderguard.load-profile/v1"
QUALIFICATION_RESULT_SCHEMA_VERSION = "tenderguard.qualification-result/v1"
IMMUTABLE_BUILD_REFERENCE_PATTERN = r"^(?:sha256:[0-9a-f]{64}|git:[0-9a-f]{40}(?:[0-9a-f]{24})?)$"


def _reject_floats(value: Any) -> Any:
    if isinstance(value, float):
        raise ValueError("Floating-point values are forbidden in qualification evidence")
    if isinstance(value, dict):
        for item in value.values():
            _reject_floats(item)
    elif isinstance(value, list | tuple | set | frozenset):
        for item in value:
            _reject_floats(item)
    return value


class RecoveryProfile(DomainModel):
    schema_version: Literal["tenderguard.recovery-profile/v1"]
    source_environment: str = Field(min_length=1, max_length=100)
    restore_environment: str = Field(min_length=1, max_length=100)
    expected_application_build_reference: str = Field(pattern=IMMUTABLE_BUILD_REFERENCE_PATTERN)
    maximum_rpo_seconds: int = Field(gt=0)
    maximum_rto_seconds: int = Field(gt=0)
    require_worm: bool
    require_external_audit_anchor: bool
    require_oidc_configuration: bool
    require_export_signing_configuration: bool
    require_integration_signing_configuration: bool
    required_adapter_qualification_ids: tuple[str, ...] = Field(min_length=1)
    required_golden_snapshot_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator(
        "source_environment",
        "restore_environment",
        "expected_application_build_reference",
    )
    @classmethod
    def normalized_environment(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("Environment names must not contain surrounding whitespace")
        return value

    @field_validator(
        "required_adapter_qualification_ids",
        "required_golden_snapshot_ids",
    )
    @classmethod
    def unique_required_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("Required qualification IDs must be unique")
        if any(not item.strip() or item != item.strip() for item in value):
            raise ValueError("Required qualification IDs must be normalized strings")
        return value

    @model_validator(mode="after")
    def restore_is_isolated(self) -> RecoveryProfile:
        if self.source_environment == self.restore_environment:
            raise ValueError("Recovery verification must target an isolated environment")
        if self.source_environment.casefold() == "production" and not all(
            (
                self.require_worm,
                self.require_external_audit_anchor,
                self.require_oidc_configuration,
                self.require_export_signing_configuration,
                self.require_integration_signing_configuration,
            )
        ):
            raise ValueError("Production recovery profile cannot waive required security controls")
        return self


class RecoveryExerciseManifest(DomainModel):
    schema_version: Literal["tenderguard.recovery-exercise/v1"]
    exercise_id: str = Field(min_length=1, max_length=200)
    profile_version_id: str = Field(min_length=1, max_length=64)
    profile_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_environment: str = Field(min_length=1, max_length=100)
    restore_environment: str = Field(min_length=1, max_length=100)
    incident_at: datetime
    restored_database_point_at: datetime
    restoration_started_at: datetime
    database_backup_reference: str = Field(min_length=1, max_length=1000)
    object_store_backup_reference: str = Field(min_length=1, max_length=1000)
    identity_binding_evidence_reference: str = Field(min_length=1, max_length=1000)
    connector_binding_evidence_reference: str = Field(min_length=1, max_length=1000)
    secrets_manager_evidence_reference: str = Field(min_length=1, max_length=1000)
    executed_by: str = Field(min_length=1, max_length=200)
    change_reference: str = Field(min_length=1, max_length=500)

    @field_validator(
        "exercise_id",
        "source_environment",
        "restore_environment",
        "database_backup_reference",
        "object_store_backup_reference",
        "identity_binding_evidence_reference",
        "connector_binding_evidence_reference",
        "secrets_manager_evidence_reference",
        "executed_by",
        "change_reference",
    )
    @classmethod
    def normalized_text(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("Recovery exercise text must not contain surrounding whitespace")
        return value

    @field_validator(
        "incident_at",
        "restored_database_point_at",
        "restoration_started_at",
    )
    @classmethod
    def timezone_is_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Recovery exercise timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def timestamps_are_consistent(self) -> RecoveryExerciseManifest:
        if self.restored_database_point_at > self.incident_at:
            raise ValueError("Restored database point cannot be later than the incident")
        if self.restoration_started_at < self.incident_at:
            raise ValueError("Restoration cannot start before the declared incident")
        if self.source_environment == self.restore_environment:
            raise ValueError("Recovery exercise must use an isolated restore environment")
        return self


class LoadSlo(DomainModel):
    minimum_success_ratio: Decimal = Field(gt=0, le=1)
    maximum_p95_ms: Decimal = Field(gt=0)
    maximum_p99_ms: Decimal = Field(gt=0)
    minimum_requests_per_second: Decimal = Field(gt=0)
    minimum_completed_requests: int = Field(gt=0)

    @model_validator(mode="before")
    @classmethod
    def floating_point_is_forbidden(cls, value: Any) -> Any:
        return _reject_floats(value)

    @model_validator(mode="after")
    def percentiles_are_ordered(self) -> LoadSlo:
        if self.maximum_p99_ms < self.maximum_p95_ms:
            raise ValueError("Maximum p99 latency must not be below maximum p95 latency")
        return self


class LoadEndpoint(DomainModel):
    name: str = Field(min_length=1, max_length=100)
    method: Literal["GET", "HEAD"]
    path: str = Field(min_length=1, max_length=2000)
    weight: int = Field(gt=0, le=10_000)
    expected_statuses: tuple[int, ...] = Field(min_length=1)
    slo: LoadSlo

    @field_validator("name", "path")
    @classmethod
    def normalized_text(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("Load endpoint fields must not contain surrounding whitespace")
        return value

    @field_validator("path")
    @classmethod
    def safe_relative_path(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            not value.startswith("/")
            or value.startswith("//")
            or parsed.scheme
            or parsed.netloc
            or parsed.fragment
            or any(segment == ".." for segment in parsed.path.split("/"))
        ):
            raise ValueError("Load endpoint path must be a safe origin-relative URL")
        return value

    @field_validator("expected_statuses")
    @classmethod
    def valid_statuses(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if len(value) != len(set(value)) or any(item < 100 or item > 599 for item in value):
            raise ValueError("Expected HTTP status codes are invalid")
        return tuple(sorted(value))


class LoadProfile(DomainModel):
    schema_version: Literal["tenderguard.load-profile/v1"]
    target_environment: str = Field(min_length=1, max_length=100)
    expected_application_build_reference: str = Field(pattern=IMMUTABLE_BUILD_REFERENCE_PATTERN)
    base_url: str = Field(min_length=1, max_length=2000)
    duration_seconds: int = Field(gt=0, le=86_400)
    concurrency: int = Field(gt=0, le=2_000)
    maximum_requests: int = Field(gt=0, le=1_000_000_000)
    request_timeout_seconds: int = Field(gt=0, le=600)
    maximum_response_bytes: int = Field(gt=0, le=100 * 1024 * 1024)
    auth_mode: Literal["NONE", "BEARER_ENV"]
    auth_token_environment_variable: str | None = Field(
        default=None,
        pattern=r"^[A-Z][A-Z0-9_]{0,127}$",
    )
    allow_production_target: bool
    production_change_reference: str | None = Field(default=None, max_length=500)
    endpoints: tuple[LoadEndpoint, ...] = Field(min_length=1, max_length=1_000)
    overall_slo: LoadSlo

    @field_validator(
        "target_environment",
        "expected_application_build_reference",
        "base_url",
    )
    @classmethod
    def normalized_text(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("Load profile fields must not contain surrounding whitespace")
        return value

    @model_validator(mode="after")
    def target_and_auth_are_safe(self) -> LoadProfile:
        parsed = urlsplit(self.base_url)
        host = parsed.hostname
        loopback = host in {"localhost", "127.0.0.1", "::1"}
        if (
            not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or (parsed.scheme != "https" and not (loopback and parsed.scheme == "http"))
        ):
            raise ValueError(
                "Load base URL must be an HTTPS origin without credentials, query, or path"
            )
        if self.auth_mode == "BEARER_ENV" and not self.auth_token_environment_variable:
            raise ValueError("Bearer load profile requires an authentication environment variable")
        if self.auth_mode == "NONE" and self.auth_token_environment_variable is not None:
            raise ValueError("Unauthenticated load profile cannot name a token variable")
        production = self.target_environment.casefold() == "production"
        if production and (
            not self.allow_production_target
            or not self.production_change_reference
            or self.production_change_reference != self.production_change_reference.strip()
        ):
            raise ValueError(
                "Production load target requires explicit permission and a change reference"
            )
        if not production and (
            self.allow_production_target or self.production_change_reference is not None
        ):
            raise ValueError("Non-production profile cannot carry production authorization fields")
        names = [endpoint.name for endpoint in self.endpoints]
        if len(names) != len(set(names)):
            raise ValueError("Load endpoint names must be unique")
        if sum(endpoint.weight for endpoint in self.endpoints) > 100_000:
            raise ValueError("Load endpoint schedule exceeds the safe runner limit")
        if self.overall_slo.minimum_completed_requests > self.maximum_requests:
            raise ValueError("Overall minimum requests exceeds the profile request cap")
        for endpoint in self.endpoints:
            if endpoint.slo.minimum_completed_requests > self.maximum_requests:
                raise ValueError(
                    f"Endpoint {endpoint.name} minimum requests exceeds the profile cap"
                )
        return self


class QualificationFinding(DomainModel):
    code: str = Field(min_length=1, max_length=100)
    passed: bool
    message: str = Field(min_length=1, max_length=2000)
    details: dict[str, str | int | bool | None] = Field(default_factory=dict)


class QualificationResultEnvelope(DomainModel):
    schema_version: Literal["tenderguard.qualification-result/v1"]
    qualification_type: Literal["RECOVERY", "LOAD"]
    status: Literal["TECHNICAL_VERIFICATION_PASSED", "FAILED", "BLOCKED"]
    profile_version_id: str
    profile_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    started_at: datetime
    completed_at: datetime
    findings: tuple[QualificationFinding, ...]
    evidence: dict[str, object]
    result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="before")
    @classmethod
    def floating_point_is_forbidden(cls, value: Any) -> Any:
        return _reject_floats(value)

    @model_validator(mode="after")
    def result_hash_verifies(self) -> QualificationResultEnvelope:
        body = self.model_dump(mode="python", exclude={"result_hash"})
        if self.result_hash != content_hash(body):
            raise ValueError("Qualification result hash does not verify")
        return self
