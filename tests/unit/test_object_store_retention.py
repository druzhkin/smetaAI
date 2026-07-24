import pytest
from botocore.exceptions import ClientError

from tenderguard.infrastructure.object_store import (
    ObjectStoreRetentionStatus,
    S3ObjectStore,
)


class _RetentionClient:
    def __init__(
        self,
        *,
        versioning: dict[str, str],
        object_lock: dict[str, object] | None,
    ) -> None:
        self.versioning = versioning
        self.object_lock = object_lock

    def get_bucket_versioning(self, *, Bucket: str) -> dict[str, str]:
        assert Bucket == "evidence"
        return self.versioning

    def get_object_lock_configuration(self, *, Bucket: str) -> dict[str, object]:
        assert Bucket == "evidence"
        if self.object_lock is None:
            raise ClientError(
                {"Error": {"Code": "ObjectLockConfigurationNotFoundError"}},
                "GetObjectLockConfiguration",
            )
        return self.object_lock


class _PolicyTestStore(S3ObjectStore):
    def __init__(self, status: ObjectStoreRetentionStatus) -> None:
        self.required_object_lock_mode = "GOVERNANCE"
        self.minimum_retention_days = 365
        self._status = status

    def retention_status(self) -> ObjectStoreRetentionStatus:
        return self._status


def _store_with_client(client: _RetentionClient) -> S3ObjectStore:
    store = object.__new__(S3ObjectStore)
    store.bucket = "evidence"
    store.client = client  # type: ignore[assignment]
    return store


def test_compliance_retention_satisfies_governance_requirement() -> None:
    status = ObjectStoreRetentionStatus(
        versioning_enabled=True,
        object_lock_enabled=True,
        default_retention_mode="COMPLIANCE",
        default_retention_days=730,
    )

    assert status.satisfies(required_mode="GOVERNANCE", minimum_days=365)
    assert not status.satisfies(required_mode="COMPLIANCE", minimum_days=731)


def test_s3_retention_status_reads_bucket_default_policy() -> None:
    store = _store_with_client(
        _RetentionClient(
            versioning={"Status": "Enabled"},
            object_lock={
                "ObjectLockConfiguration": {
                    "ObjectLockEnabled": "Enabled",
                    "Rule": {
                        "DefaultRetention": {
                            "Mode": "COMPLIANCE",
                            "Years": 2,
                        }
                    },
                }
            },
        )
    )

    status = store.retention_status()
    assert status.versioning_enabled
    assert status.object_lock_enabled
    assert status.default_retention_mode == "COMPLIANCE"
    assert status.default_retention_days == 730


def test_missing_object_lock_configuration_fails_policy_enforcement() -> None:
    status = _store_with_client(
        _RetentionClient(
            versioning={"Status": "Enabled"},
            object_lock=None,
        )
    ).retention_status()
    store = _PolicyTestStore(status)

    assert not status.object_lock_enabled
    with pytest.raises(RuntimeError, match="WORM policy is not satisfied"):
        store._require_worm()
