from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEVELOPMENT_AUDIT_SIGNING_KEY = "development-only-not-for-production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["development", "test", "staging", "production"] = "development"
    database_url: str = "sqlite+pysqlite:///./var/tenderguard.db"
    object_store_backend: Literal["local", "s3"] = "local"
    local_object_store_path: Path = Path("./var/objects")
    local_quarantine_store_path: Path = Path("./var/quarantine")
    max_upload_bytes: int = Field(default=500 * 1024 * 1024, gt=0)
    max_parser_spool_memory_bytes: int = Field(default=8 * 1024 * 1024, gt=0)
    max_scan_report_bytes: int = Field(default=1024 * 1024, gt=0)
    max_archive_depth: int = Field(default=3, ge=0, le=10)
    max_archive_files: int = Field(default=10_000, gt=0)
    max_archive_unpacked_bytes: int = Field(default=2 * 1024 * 1024 * 1024, gt=0)
    max_archive_compression_ratio: int = Field(default=200, gt=0)

    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    allow_insecure_dev_auth: bool = False
    audit_signing_key: SecretStr = SecretStr(DEVELOPMENT_AUDIT_SIGNING_KEY)
    export_signing_key_id: str | None = None
    export_signing_private_key_b64: SecretStr | None = None
    trusted_hosts: list[str] = ["localhost", "127.0.0.1", "testserver"]

    s3_endpoint_url: str | None = None
    s3_bucket: str | None = None
    s3_quarantine_bucket: str | None = None
    s3_access_key: SecretStr | None = None
    s3_secret_key: SecretStr | None = None

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

    @model_validator(mode="after")
    def production_is_fail_closed(self) -> Settings:
        if self.app_env in {"staging", "production"}:
            problems: list[str] = []
            if self.allow_insecure_dev_auth:
                problems.append("insecure development authentication is enabled")
            if not all((self.oidc_issuer, self.oidc_audience, self.oidc_jwks_url)):
                problems.append("OIDC configuration is incomplete")
            if len(self.audit_signing_key.get_secret_value().encode("utf-8")) < 32:
                problems.append("audit signing key is shorter than 32 bytes")
            if self.audit_signing_key.get_secret_value() == DEVELOPMENT_AUDIT_SIGNING_KEY:
                problems.append("development audit signing key is still configured")
            if not self.export_signing_key_id or not self.export_signing_private_key_b64:
                problems.append("Ed25519 export signing key configuration is incomplete")
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
        if self.document_job_timeout_seconds >= self.document_job_lease_seconds:
            raise ValueError("Document job timeout must be shorter than its lease")
        if self.document_job_retry_max_seconds < self.document_job_retry_base_seconds:
            raise ValueError("Document job retry maximum must be at least the base delay")
        return self

    @property
    def normative_adapter_configured(self) -> bool:
        return bool(self.normative_adapter and self.normative_adapter_qualification_id)

    @property
    def export_signing_configured(self) -> bool:
        return bool(self.export_signing_key_id and self.export_signing_private_key_b64)

    @property
    def malware_scanner_configured(self) -> bool:
        return bool(self.malware_scanner_adapter and self.malware_scanner_qualification_id)

    @property
    def document_processor_configured(self) -> bool:
        return bool(self.document_processor_adapter and self.document_processor_qualification_id)


@lru_cache
def get_settings() -> Settings:
    return Settings()
