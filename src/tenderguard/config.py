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
    max_upload_bytes: int = Field(default=500 * 1024 * 1024, gt=0)
    max_archive_depth: int = Field(default=3, ge=0, le=10)
    max_archive_files: int = Field(default=10_000, gt=0)
    max_archive_unpacked_bytes: int = Field(default=2 * 1024 * 1024 * 1024, gt=0)
    max_archive_compression_ratio: int = Field(default=200, gt=0)

    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    allow_insecure_dev_auth: bool = False
    audit_signing_key: SecretStr = SecretStr(DEVELOPMENT_AUDIT_SIGNING_KEY)
    trusted_hosts: list[str] = ["localhost", "127.0.0.1", "testserver"]

    s3_endpoint_url: str | None = None
    s3_bucket: str | None = None
    s3_access_key: SecretStr | None = None
    s3_secret_key: SecretStr | None = None

    normative_adapter: str | None = None
    normative_adapter_qualification_id: str | None = None

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
            if not self.trusted_hosts or "*" in self.trusted_hosts:
                problems.append("trusted hosts are empty or contain a wildcard")
            if self.object_store_backend != "s3":
                problems.append("versioned S3-compatible object storage is required")
            if not self.database_url.startswith("postgresql"):
                problems.append("PostgreSQL is required")
            if not all(
                (
                    self.s3_bucket,
                    self.s3_access_key,
                    self.s3_secret_key,
                )
            ):
                problems.append("S3 credentials/bucket are incomplete")
            if problems:
                raise ValueError("Unsafe production configuration: " + "; ".join(problems))
        if self.allow_insecure_dev_auth and self.app_env not in {"development", "test"}:
            raise ValueError("Insecure authentication is allowed only in development/test")
        return self

    @property
    def normative_adapter_configured(self) -> bool:
        return bool(self.normative_adapter and self.normative_adapter_qualification_id)


@lru_cache
def get_settings() -> Settings:
    return Settings()
