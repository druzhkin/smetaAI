from __future__ import annotations

from datetime import datetime

from tenderguard.domain.enums import MatchClass
from tenderguard.domain.models import NomenclatureMatch


def _normal(value: str) -> str:
    return " ".join(value.casefold().split())


def assess_exact_match(
    *,
    match_id: str,
    source_item_id: str,
    canonical_item_id: str,
    required_critical_attributes: frozenset[str],
    source_attributes: dict[str, str],
    canonical_attributes: dict[str, str],
) -> NomenclatureMatch:
    missing = {
        attribute
        for attribute in required_critical_attributes
        if not source_attributes.get(attribute) or not canonical_attributes.get(attribute)
    }
    mismatched = {
        attribute
        for attribute in required_critical_attributes - missing
        if _normal(source_attributes[attribute]) != _normal(canonical_attributes[attribute])
    }
    if missing:
        match_class = MatchClass.INSUFFICIENT_DATA
    elif mismatched:
        match_class = MatchClass.TECHNICALLY_UNACCEPTABLE
    else:
        match_class = MatchClass.EXACT
    return NomenclatureMatch(
        match_id=match_id,
        source_item_id=source_item_id,
        canonical_item_id=canonical_item_id,
        match_class=match_class,
        required_critical_attributes=required_critical_attributes,
        source_attributes=source_attributes,
        canonical_attributes=canonical_attributes,
        missing_attributes=frozenset(missing),
        mismatched_attributes=frozenset(mismatched),
    )


def approve_analogue(
    match: NomenclatureMatch,
    *,
    analogue_class: MatchClass,
    equivalence_rule_version_id: str,
    verified_by: str,
    verified_at: datetime,
) -> NomenclatureMatch:
    if analogue_class not in {
        MatchClass.FUNCTIONAL_ANALOGUE,
        MatchClass.CONDITIONALLY_ACCEPTABLE_ANALOGUE,
    }:
        raise ValueError("Only an explicit analogue class can be approved")
    if not equivalence_rule_version_id:
        raise ValueError("A versioned equivalence rule is required")
    if match.missing_attributes:
        raise ValueError("An analogue cannot be approved with missing critical attributes")
    return match.model_copy(
        update={
            "match_class": analogue_class,
            "verified_by": verified_by,
            "verified_at": verified_at,
        }
    )
