from __future__ import annotations

import re
from datetime import datetime

from tenderguard.domain.enums import MatchClass
from tenderguard.domain.models import DomainModel, NomenclatureMatch


class CatalogRetrievalEvidence(DomainModel):
    exact_identifier_mentioned: bool
    matched_terms: tuple[str, ...]
    matched_critical_attributes: tuple[str, ...]


_LEXEME_PATTERN = re.compile(r"\d+(?:\.\d+)?|[^\W\d_]+", flags=re.UNICODE)


def _ordered_lexemes(value: str) -> tuple[str, ...]:
    return tuple(
        term
        for term in _LEXEME_PATTERN.findall(value.casefold().replace(",", "."))
        if len(term) > 1 or term.isdecimal()
    )


def _lexemes(value: str) -> frozenset[str]:
    return frozenset(_ordered_lexemes(value))


def _contains_sequence(source: tuple[str, ...], candidate: tuple[str, ...]) -> bool:
    return bool(candidate) and any(
        source[index : index + len(candidate)] == candidate
        for index in range(len(source) - len(candidate) + 1)
    )


def _critical_value_mentioned(
    source: tuple[str, ...],
    *,
    attribute: str,
    value: str,
) -> bool:
    value_terms = _ordered_lexemes(value)
    if not _contains_sequence(source, value_terms):
        return False
    if any(not re.fullmatch(r"\d+(?:\.\d+)?", term) for term in value_terms):
        return True
    return _contains_sequence(source, _ordered_lexemes(attribute))


def catalog_retrieval_evidence(
    *,
    source_name: str,
    canonical_item_id: str,
    attributes: dict[str, str],
    critical_attributes: tuple[str, ...],
) -> CatalogRetrievalEvidence:
    """Explain deterministic lexical retrieval without claiming equivalence."""

    ordered_source_terms = _ordered_lexemes(source_name)
    source_terms = frozenset(ordered_source_terms)
    ordered_identifier_terms = _ordered_lexemes(canonical_item_id)
    identifier_terms = frozenset(ordered_identifier_terms)
    attribute_terms = frozenset().union(
        *(_lexemes(key) | _lexemes(value) for key, value in attributes.items())
    )
    matched_critical_attributes = tuple(
        sorted(
            attribute
            for attribute in critical_attributes
            if _critical_value_mentioned(
                ordered_source_terms,
                attribute=attribute,
                value=attributes[attribute],
            )
        )
    )
    return CatalogRetrievalEvidence(
        exact_identifier_mentioned=_contains_sequence(
            ordered_source_terms,
            ordered_identifier_terms,
        ),
        matched_terms=tuple(sorted(source_terms & (identifier_terms | attribute_terms))),
        matched_critical_attributes=matched_critical_attributes,
    )


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
