from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from tenderguard.domain.audit_anchor import validate_anchor_public_key

DEVELOPMENT_AUDIT_SIGNING_KEY = "development-only-not-for-production"
DEVELOPMENT_AUDIT_SIGNING_KEY_ID = "legacy"
APPLICATION_BUILD_REFERENCE_PATTERN = re.compile(
    r"^(?:sha256:[0-9a-f]{64}|git:[0-9a-f]{40}(?:[0-9a-f]{24})?)$"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["development", "test", "staging", "production"] = "development"
    application_build_reference: str | None = None
    database_url: str = "sqlite+pysqlite:///./var/tenderguard.db"
    object_store_backend: Literal["local", "s3"] = "local"
    local_object_store_path: Path = Path("./var/objects")
    local_quarantine_store_path: Path = Path("./var/quarantine")
    max_upload_bytes: int = Field(default=500 * 1024 * 1024, gt=0)
    max_parser_spool_memory_bytes: int = Field(default=8 * 1024 * 1024, gt=0)
    max_scan_report_bytes: int = Field(default=1024 * 1024, gt=0)
    max_api_request_bytes: int = Field(default=2 * 1024 * 1024, gt=0)
    max_archive_depth: int = Field(default=3, ge=0, le=10)
    max_archive_files: int = Field(default=10_000, gt=0)
    max_archive_unpacked_bytes: int = Field(default=2 * 1024 * 1024 * 1024, gt=0)
    max_archive_compression_ratio: int = Field(default=200, gt=0)

    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    oidc_web_client_id: str | None = None
    oidc_web_scope: str = "openid profile email"
    operator_ui_enabled: bool = True
    operator_ui_dist_path: Path | None = None
    allow_insecure_dev_auth: bool = False
    audit_signing_key: SecretStr = SecretStr(DEVELOPMENT_AUDIT_SIGNING_KEY)
    audit_signing_key_id: str = DEVELOPMENT_AUDIT_SIGNING_KEY_ID
    audit_verification_keys: dict[str, SecretStr] = Field(default_factory=dict)
    audit_anchor_provider_id: str | None = None
    audit_anchor_provider_key_id: str | None = None
    audit_anchor_public_key_b64: str | None = None
    audit_anchor_max_age_seconds: int | None = Field(default=None, ge=60)
    audit_operator_organization_id: str | None = None
    require_idempotency_keys: bool = False
    export_signing_key_id: str | None = None
    export_signing_private_key_b64: SecretStr | None = None
    integration_signing_key_id: str | None = None
    integration_signing_private_key_b64: SecretStr | None = None
    integration_receiver_id: str | None = None
    integration_operator_organization_id: str | None = None
    trusted_hosts: list[str] = ["localhost", "127.0.0.1", "testserver"]

    s3_endpoint_url: str | None = None
    s3_bucket: str | None = None
    s3_quarantine_bucket: str | None = None
    s3_access_key: SecretStr | None = None
    s3_secret_key: SecretStr | None = None
    s3_required_object_lock_mode: Literal["GOVERNANCE", "COMPLIANCE"] | None = None
    s3_minimum_retention_days: int | None = Field(default=None, ge=1)

    normative_adapter: str | None = None
    normative_adapter_qualification_id: str | None = None
    malware_scanner_adapter: str | None = None
    malware_scanner_qualification_id: str | None = None
    document_processor_adapter: str | None = None
    document_processor_qualification_id: str | None = None
    document_worker_actor_id: str | None = None
    document_job_lease_seconds: int = Field(default=900, ge=30, le=86_400)
    document_job_timeout_seconds: int = Field(default=840, ge=1, le=86_399)
    document_job_max_attempts: int = Field(default=3, ge=1, le=100)
    document_job_retry_base_seconds: int = Field(default=30, ge=1, le=86_400)
    document_job_retry_max_seconds: int = Field(default=900, ge=1, le=604_800)
    integration_job_lease_seconds: int = Field(default=300, ge=30, le=86_400)
    integration_job_timeout_seconds: int = Field(default=240, ge=1, le=86_399)
    integration_job_max_attempts: int = Field(default=5, ge=1, le=100)
    integration_job_retry_base_seconds: int = Field(default=30, ge=1, le=86_400)
    integration_job_retry_max_seconds: int = Field(default=3600, ge=1, le=604_800)
    integration_max_event_bytes: int = Field(default=2 * 1024 * 1024, gt=0)
    integration_max_response_bytes: int = Field(default=128 * 1024, gt=0)
    integration_http_timeout_seconds: int = Field(default=60, ge=1, le=600)
    integration_inbound_max_message_age_seconds: int = Field(
        default=86_400,
        ge=60,
        le=2_592_000,
    )
    integration_inbound_max_future_skew_seconds: int = Field(
        default=300,
        ge=0,
        le=86_400,
    )

    @model_validator(mode="after")
    def production_is_fail_closed(self) -> Settings:
        if (
            not self.audit_signing_key_id.strip()
            or self.audit_signing_key_id != self.audit_signing_key_id.strip()
            or len(self.audit_signing_key_id) > 200
        ):
            raise ValueError("Audit signing key ID is invalid")
        for key_id, key in self.audit_verification_keys.items():
            if not key_id.strip() or key_id != key_id.strip() or len(key_id) > 200:
                raise ValueError("An audit verification key ID is invalid")
            if not key.get_secret_value():
                raise ValueError("An audit verification key is empty")
        for field_name, value, max_length in (
            ("application build reference", self.application_build_reference, 200),
            ("audit anchor provider ID", self.audit_anchor_provider_id, 200),
            ("audit anchor provider key ID", self.audit_anchor_provider_key_id, 200),
            ("audit operator organization ID", self.audit_operator_organization_id, 64),
            (
                "integration operator organization ID",
                self.integration_operator_organization_id,
                64,
            ),
            ("integration receiver ID", self.integration_receiver_id, 200),
            ("OIDC web client ID", self.oidc_web_client_id, 200),
        ):
            if value is not None and (
                not value.strip() or value != value.strip() or len(value) > max_length
            ):
                raise ValueError(f"{field_name} is invalid")
        if (
            self.application_build_reference is not None
            and APPLICATION_BUILD_REFERENCE_PATTERN.fullmatch(self.application_build_reference)
            is None
        ):
            raise ValueError(
                "Application build reference must be an immutable SHA-256 or Git digest"
            )
        current_verification_key = self.audit_verification_keys.get(self.audit_signing_key_id)
        if (
            current_verification_key is not None
            and current_verification_key.get_secret_value()
            != self.audit_signing_key.get_secret_value()
        ):
            raise ValueError("Current audit verification key differs from the signing key")
        if self.audit_anchor_public_key_b64 is not None:
            validate_anchor_public_key(self.audit_anchor_public_key_b64)
        if self.app_env in {"staging", "production"}:
            problems: list[str] = []
            if self.allow_insecure_dev_auth:
                problems.append("insecure development authentication is enabled")
            if not self.application_build_reference:
                problems.append("immutable application build reference is not configured")
            if not all((self.oidc_issuer, self.oidc_audience, self.oidc_jwks_url)):
                problems.append("OIDC configuration is incomplete")
            for label, value in (
                ("OIDC issuer", self.oidc_issuer),
                ("OIDC JWKS URL", self.oidc_jwks_url),
            ):
                if value is None:
                    continue
                parsed = urlsplit(value)
                if (
                    parsed.scheme != "https"
                    or not parsed.hostname
                    or parsed.username is not None
                    or parsed.password is not None
                    or parsed.fragment
                ):
                    problems.append(f"{label} must be an HTTPS URL without credentials")
            if self.operator_ui_enabled and not self.oidc_web_client_id:
                problems.append("OIDC web client ID is not configured")
            if len(self.audit_signing_key.get_secret_value().encode("utf-8")) < 32:
                problems.append("audit signing key is shorter than 32 bytes")
            if self.audit_signing_key.get_secret_value() == DEVELOPMENT_AUDIT_SIGNING_KEY:
                problems.append("development audit signing key is still configured")
            if self.audit_signing_key_id == DEVELOPMENT_AUDIT_SIGNING_KEY_ID:
                problems.append("legacy audit signing key ID is still configured")
            if not self.audit_anchor_configured:
                problems.append("external audit anchor configuration is incomplete")
            if not self.audit_operator_organization_id:
                problems.append("audit operator organization is not configured")
            if not self.require_idempotency_keys:
                problems.append("persisted idempotency keys are not required")
            if not self.export_signing_key_id or not self.export_signing_private_key_b64:
                problems.append("Ed25519 export signing key configuration is incomplete")
            if not self.integration_signing_configured:
                problems.append("Ed25519 integration signing key configuration is incomplete")
            if not self.integration_receiver_id:
                problems.append("integration receipt receiver ID is not configured")
            if not self.integration_operator_organization_id:
                problems.append("integration operator organization is not configured")
            if not self.trusted_hosts or "*" in self.trusted_hosts:
                problems.append("trusted hosts are empty or contain a wildcard")
            if self.object_store_backend != "s3":
                problems.append("versioned S3-compatible object storage is required")
            if not self.database_url.startswith("postgresql"):
                problems.append("PostgreSQL is required")
            if not all(
                (
                    self.s3_bucket,
                    self.s3_quarantine_bucket,
                    self.s3_access_key,
                    self.s3_secret_key,
                )
            ):
                problems.append("S3 credentials/evidence/quarantine buckets are incomplete")
            if self.s3_bucket and self.s3_bucket == self.s3_quarantine_bucket:
                problems.append("quarantine and evidence must use different S3 buckets")
            if not self.worm_policy_configured:
                problems.append("S3 object-lock retention policy is not configured")
            if not self.malware_scanner_configured:
                problems.append("qualified malware scanner binding is incomplete")
            if not self.document_processor_configured:
                problems.append("qualified isolated document processor binding is incomplete")
            if not self.document_worker_actor_id:
                problems.append("isolated document worker actor is not configured")
            if problems:
                raise ValueError("Unsafe production configuration: " + "; ".join(problems))
        if self.allow_insecure_dev_auth and self.app_env not in {"development", "test"}:
            raise ValueError("Insecure authentication is allowed only in development/test")
        scopes = self.oidc_web_scope.split()
        if (
            not scopes
            or "openid" not in scopes
            or len(scopes) != len(set(scopes))
            or any(
                not scope or len(scope) > 100 or any(character.isspace() for character in scope)
                for scope in scopes
            )
        ):
            raise ValueError("OIDC web scope must contain unique scopes including openid")
        if self.document_job_timeout_seconds >= self.document_job_lease_seconds:
            raise ValueError("Document job timeout must be shorter than its lease")
        if self.document_job_retry_max_seconds < self.document_job_retry_base_seconds:
            raise ValueError("Document job retry maximum must be at least the base delay")
        if self.integration_job_timeout_seconds >= self.integration_job_lease_seconds:
            raise ValueError("Integration job timeout must be shorter than its lease")
        if self.integration_http_timeout_seconds > self.integration_job_timeout_seconds:
            raise ValueError("Integration HTTP timeout must not exceed the job timeout")
        if self.integration_job_retry_max_seconds < self.integration_job_retry_base_seconds:
            raise ValueError("Integration job retry maximum must be at least the base delay")
        return self

    @property
    def normative_adapter_configured(self) -> bool:
        return bool(self.normative_adapter and self.normative_adapter_qualification_id)

    @property
    def export_signing_configured(self) -> bool:
        return bool(self.export_signing_key_id and self.export_signing_private_key_b64)

    @property
    def integration_signing_configured(self) -> bool:
        return bool(self.integration_signing_key_id and self.integration_signing_private_key_b64)

    @property
    def audit_verification_keyring(self) -> dict[str, bytes]:
        keyring = {
            key_id: secret.get_secret_value().encode("utf-8")
            for key_id, secret in self.audit_verification_keys.items()
        }
        keyring[self.audit_signing_key_id] = self.audit_signing_key.get_secret_value().encode(
            "utf-8"
        )
        return keyring

    @property
    def audit_anchor_configured(self) -> bool:
        return bool(
            self.audit_anchor_provider_id
            and self.audit_anchor_provider_key_id
            and self.audit_anchor_public_key_b64
            and self.audit_anchor_max_age_seconds
        )

    @property
    def worm_policy_configured(self) -> bool:
        return bool(self.s3_required_object_lock_mode and self.s3_minimum_retention_days)

    @property
    def malware_scanner_configured(self) -> bool:
        return bool(self.malware_scanner_adapter and self.malware_scanner_qualification_id)

    @property
    def document_processor_configured(self) -> bool:
        return bool(self.document_processor_adapter and self.document_processor_qualification_id)


@lru_cache
def get_settings() -> Settings:
    return Settings()
