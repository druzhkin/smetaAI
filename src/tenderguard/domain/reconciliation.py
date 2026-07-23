from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from tenderguard.domain.common import canonical_json, content_hash
from tenderguard.domain.enums import VerificationStatus
from tenderguard.domain.models import Conflict, Observation


def _comparable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return value.normalize()
    if isinstance(value, str):
        return " ".join(value.casefold().split())
    if isinstance(value, Mapping) or (
        isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray)
    ):
        return canonical_json(value)
    return value


def reconcile_observations(
    *,
    conflict_namespace: str,
    observations: Sequence[Observation],
) -> tuple[Any | None, Conflict | None]:
    """Reconcile only exact independent agreement.

    Confidence and source priority never resolve a disagreement automatically.
    """

    if len(observations) < 2:
        return None, None
    field_names = {item.field_name for item in observations}
    if len(field_names) != 1:
        raise ValueError("Only observations of the same field can be reconciled")
    methods = {(item.method, item.method_version) for item in observations}
    if len(methods) < 2:
        raise ValueError("Independent extraction requires distinct method/version pairs")
    units = {item.unit for item in observations}
    values = {_comparable(item.value) for item in observations}
    if len(units) == 1 and len(values) == 1:
        return observations[0].value, None

    identity = {
        "namespace": conflict_namespace,
        "field": observations[0].field_name,
        "observations": sorted(item.observation_id for item in observations),
    }
    conflict = Conflict(
        conflict_id=f"conflict-{content_hash(identity)[:24]}",
        field_name=observations[0].field_name,
        observation_ids=tuple(sorted(item.observation_id for item in observations)),
        reason="Independent extraction observations disagree in value or unit",
        status=VerificationStatus.CONFLICT,
    )
    return None, conflict
