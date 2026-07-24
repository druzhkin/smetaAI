from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Protocol, cast

import boto3
from botocore.exceptions import ClientError
from pydantic import BaseModel, ConfigDict, Field

from tenderguard.config import Settings


class StoredObject(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    object_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    object_key: str
    size_bytes: int = Field(ge=0)


class ObjectStoreRetentionStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    versioning_enabled: bool
    object_lock_enabled: bool
    default_retention_mode: str | None = None
    default_retention_days: int | None = Field(default=None, ge=1)

    def satisfies(self, *, required_mode: str, minimum_days: int) -> bool:
        mode_satisfies = self.default_retention_mode == required_mode or (
            required_mode == "GOVERNANCE" and self.default_retention_mode == "COMPLIANCE"
        )
        return bool(
            self.versioning_enabled
            and self.object_lock_enabled
            and mode_satisfies
            and self.default_retention_days is not None
            and self.default_retention_days >= minimum_days
        )


class ObjectStore(Protocol):
    def put(self, stream: BinaryIO, *, max_bytes: int | None = None) -> StoredObject: ...

    def open(self, object_hash: str) -> AbstractContextManager[BinaryIO]: ...

    def healthcheck(self) -> bool: ...

    def retention_status(self) -> ObjectStoreRetentionStatus: ...


class LocalObjectStore:
    """Content-addressed local store for development and tests only."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, object_hash: str) -> Path:
        if len(object_hash) != 64 or any(char not in "0123456789abcdef" for char in object_hash):
            raise ValueError("Invalid SHA-256 object hash")
        path = (self.root / object_hash[:2] / object_hash).resolve()
        if self.root not in path.parents:
            raise ValueError("Object path escaped the configured store")
        return path

    def put(self, stream: BinaryIO, *, max_bytes: int | None = None) -> StoredObject:
        digest = hashlib.sha256()
        size = 0
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as temporary:
                temporary_path = Path(temporary.name)
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if max_bytes is not None and size > max_bytes:
                        raise ValueError(f"File exceeds configured limit of {max_bytes} bytes")
                    digest.update(chunk)
                    temporary.write(chunk)
                temporary.flush()
                os.fsync(temporary.fileno())
            object_hash = digest.hexdigest()
            destination = self._path_for(object_hash)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                temporary_path.unlink(missing_ok=True)
            else:
                os.replace(temporary_path, destination)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return StoredObject(
            object_hash=object_hash,
            object_key=str(destination.relative_to(self.root)).replace("\\", "/"),
            size_bytes=size,
        )

    @contextmanager
    def open(self, object_hash: str) -> Iterator[BinaryIO]:
        with self._path_for(object_hash).open("rb") as stream:
            _verify_stream_hash(stream, object_hash)
            yield stream

    def healthcheck(self) -> bool:
        return self.root.is_dir() and os.access(self.root, os.R_OK | os.W_OK)

    def retention_status(self) -> ObjectStoreRetentionStatus:
        return ObjectStoreRetentionStatus(
            versioning_enabled=False,
            object_lock_enabled=False,
        )


class S3ObjectStore:
    def __init__(self, settings: Settings, *, bucket: str | None = None) -> None:
        resolved_bucket = bucket or settings.s3_bucket
        if not (resolved_bucket and settings.s3_access_key and settings.s3_secret_key):
            raise ValueError("S3 object store configuration is incomplete")
        self.bucket = resolved_bucket
        self.spool_memory_bytes = settings.max_parser_spool_memory_bytes
        self.required_object_lock_mode = settings.s3_required_object_lock_mode
        self.minimum_retention_days = settings.s3_minimum_retention_days
        self.enforce_worm = bool(
            settings.app_env in {"staging", "production"} and resolved_bucket == settings.s3_bucket
        )
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key.get_secret_value(),
            aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
        )

    @staticmethod
    def _key(object_hash: str) -> str:
        if len(object_hash) != 64 or any(char not in "0123456789abcdef" for char in object_hash):
            raise ValueError("Invalid SHA-256 object hash")
        return f"objects/{object_hash[:2]}/{object_hash}"

    def put(self, stream: BinaryIO, *, max_bytes: int | None = None) -> StoredObject:
        if self.enforce_worm:
            self._require_worm()
        digest = hashlib.sha256()
        size = 0
        with tempfile.SpooledTemporaryFile(max_size=self.spool_memory_bytes) as spool:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if max_bytes is not None and size > max_bytes:
                    raise ValueError(f"File exceeds configured limit of {max_bytes} bytes")
                digest.update(chunk)
                spool.write(chunk)
            object_hash = digest.hexdigest()
            key = self._key(object_hash)
            try:
                self.client.head_object(Bucket=self.bucket, Key=key)
            except self.client.exceptions.ClientError as error:
                status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                if status != 404:
                    raise
                spool.seek(0)
                self.client.upload_fileobj(
                    spool,
                    self.bucket,
                    key,
                    ExtraArgs={
                        "Metadata": {"sha256": object_hash},
                    },
                )
        return StoredObject(object_hash=object_hash, object_key=key, size_bytes=size)

    @contextmanager
    def open(self, object_hash: str) -> Iterator[BinaryIO]:
        key = self._key(object_hash)
        with tempfile.SpooledTemporaryFile(max_size=self.spool_memory_bytes) as spool:
            self.client.download_fileobj(self.bucket, key, spool)
            spool.seek(0)
            _verify_stream_hash(cast(BinaryIO, spool), object_hash)
            yield cast(BinaryIO, spool)

    def healthcheck(self) -> bool:
        self.client.head_bucket(Bucket=self.bucket)
        return True

    def retention_status(self) -> ObjectStoreRetentionStatus:
        versioning = self.client.get_bucket_versioning(Bucket=self.bucket)
        try:
            lock_response = cast(
                dict[str, Any],
                self.client.get_object_lock_configuration(Bucket=self.bucket),
            )
        except ClientError as error:
            error_code = error.response.get("Error", {}).get("Code")
            if error_code not in {
                "ObjectLockConfigurationNotFoundError",
                "InvalidRequest",
                "NoSuchObjectLockConfiguration",
            }:
                raise
            lock_response = {}
        configuration = lock_response.get("ObjectLockConfiguration", {})
        retention = configuration.get("Rule", {}).get("DefaultRetention", {})
        days = retention.get("Days")
        years = retention.get("Years")
        retention_days: int | None = None
        if isinstance(days, int) and days > 0:
            retention_days = days
        elif isinstance(years, int) and years > 0:
            retention_days = years * 365
        return ObjectStoreRetentionStatus(
            versioning_enabled=versioning.get("Status") == "Enabled",
            object_lock_enabled=configuration.get("ObjectLockEnabled") == "Enabled",
            default_retention_mode=retention.get("Mode"),
            default_retention_days=retention_days,
        )

    def _require_worm(self) -> None:
        if self.required_object_lock_mode is None or self.minimum_retention_days is None:
            raise RuntimeError("Evidence-store WORM policy is not configured")
        status = self.retention_status()
        if not status.satisfies(
            required_mode=self.required_object_lock_mode,
            minimum_days=self.minimum_retention_days,
        ):
            raise RuntimeError("Evidence-store WORM policy is not satisfied")


def build_object_store(settings: Settings) -> ObjectStore:
    if settings.object_store_backend == "s3":
        return S3ObjectStore(settings)
    return LocalObjectStore(settings.local_object_store_path)


def build_quarantine_store(settings: Settings) -> ObjectStore:
    if settings.object_store_backend == "s3":
        return S3ObjectStore(settings, bucket=settings.s3_quarantine_bucket)
    return LocalObjectStore(settings.local_quarantine_store_path)


def copy_limited(source: BinaryIO, destination: BinaryIO, max_bytes: int) -> int:
    copied = 0
    while chunk := source.read(1024 * 1024):
        copied += len(chunk)
        if copied > max_bytes:
            raise ValueError(f"File exceeds configured limit of {max_bytes} bytes")
        destination.write(chunk)
    return copied


def _verify_stream_hash(stream: BinaryIO, expected_hash: str) -> None:
    digest = hashlib.sha256()
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
    if digest.hexdigest() != expected_hash:
        raise RuntimeError("Content-addressed object fails SHA-256 verification")
    stream.seek(0)
