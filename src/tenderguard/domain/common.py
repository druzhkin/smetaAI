from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("Naive datetimes are forbidden in canonical records")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "model_dump"):
        return _canonical(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, set | frozenset):
        return sorted((_canonical(item) for item in value), key=repr)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_canonical(item) for item in value]
    if isinstance(value, float):
        raise TypeError("Floating point values are forbidden in auditable records")
    return value


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        _canonical(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_data(value: Any) -> Any:
    """Return the exact JSON-compatible representation used for hashing."""

    return _canonical(value)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def signed_hash(value: Any, secret: bytes) -> str:
    if not secret:
        raise ValueError("Audit signing key must not be empty")
    return hmac.new(secret, canonical_json(value), hashlib.sha256).hexdigest()
