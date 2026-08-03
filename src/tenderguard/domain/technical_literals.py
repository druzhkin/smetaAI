# ruff: noqa: RUF001

from __future__ import annotations

import re
import unicodedata
from typing import Literal

from pydantic import Field, model_validator

from tenderguard.domain.models import DomainModel

TECHNICAL_LITERAL_ALGORITHM_VERSION = "technical-literal-extraction/v1"

TechnicalLiteralKind = Literal[
    "CLASS_MARKER",
    "DESIGNATION",
    "DIMENSION",
    "MEASUREMENT",
    "QUOTED_PHRASE",
]
LiteralNameRelation = Literal[
    "EXACT_LITERAL_NAME",
    "ALL_EXTRACTED_BOQ_LITERALS_PRESENT",
    "PARTIAL_LITERAL_OVERLAP",
    "NO_LITERAL_OVERLAP",
    "NO_BOQ_LITERALS",
]

_LETTER = "A-Za-zА-Яа-яЁё"
_UNIT = r"(?:мм(?:²|2|³|3)?|см(?:²|2|³|3)?|м(?:²|2|³|3)?|кВ|В|кН/м(?:²|2))"
_PATTERNS: tuple[tuple[TechnicalLiteralKind, re.Pattern[str]], ...] = (
    (
        "QUOTED_PHRASE",
        re.compile(r"«[^»]{2,200}»|\"[^\"\r\n]{2,200}\""),
    ),
    (
        "DESIGNATION",
        re.compile(
            rf"(?<!\w)[{_LETTER}]{{1,20}}\s+\d+\s*[xх×]\s*\d+"
            rf"(?:\s*/\s*\d+)?(?:\([A-Za-zА-Яа-яЁё0-9]+\))?",
        ),
    ),
    (
        "DESIGNATION",
        re.compile(
            rf"(?<!\w)\d+[{_LETTER}]{{1,12}}(?:\s*-\s*\d+)+"
            rf"(?:\s*/\s*\d+)?(?:\([A-Za-zА-Яа-яЁё0-9]+\))?",
        ),
    ),
    (
        "DIMENSION",
        re.compile(
            rf"(?<![\w.,])\d+(?:[.,]\d+)?\s*[xх×]\s*\d+(?:[.,]\d+)?"
            rf"(?:\s*[xх×]\s*\d+(?:[.,]\d+)?)?\s*{_UNIT}(?!\w)",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "DESIGNATION",
        re.compile(
            r"(?<!\w)(?:DN|SDR|SN|PN|D|F|T)\s*[-:]?\s*\d+(?:[.,]\d+)?(?!\w)",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "DESIGNATION",
        re.compile(
            r"(?<!\w)(?:[A-ZА-ЯЁ]{2,12}|[A-ZА-ЯЁ][a-zа-яё]+[A-ZА-ЯЁ][a-zа-яё]+)"
            r"\s*[-:]?\s*\d+(?:\s*[-/]\s*\d+)*(?!\w)",
        ),
    ),
    (
        "CLASS_MARKER",
        re.compile(
            r"(?<!\w)(?:[IVX]+|\d+)\s+(?:класс(?:а|у|ом|е)?|class)(?!\w)",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "MEASUREMENT",
        re.compile(
            rf"(?<![\w.,])\d+(?:[.,]\d+)?\s*{_UNIT}(?!\w)",
            flags=re.IGNORECASE,
        ),
    ),
)


class TechnicalLiteral(DomainModel):
    kind: TechnicalLiteralKind
    raw_literal: str = Field(min_length=1, max_length=500)
    normalized_literal: str = Field(min_length=1, max_length=500)
    start_offset: int = Field(ge=0, le=20_000)
    end_offset: int = Field(gt=0, le=20_000)

    @model_validator(mode="after")
    def literal_is_reproducible(self) -> TechnicalLiteral:
        if self.end_offset <= self.start_offset:
            raise ValueError("Technical literal offsets are invalid")
        if self.normalized_literal != _normalize_literal(self.raw_literal, self.kind):
            raise ValueError("Technical literal normalization does not reproduce")
        return self

    @property
    def identity(self) -> str:
        return f"{self.kind}:{self.normalized_literal}"


class TechnicalLiteralComparison(DomainModel):
    algorithm_version: str = TECHNICAL_LITERAL_ALGORITHM_VERSION
    boq_text: str = Field(min_length=1, max_length=20_000)
    source_text: str = Field(min_length=1, max_length=20_000)
    boq_literals: tuple[TechnicalLiteral, ...]
    source_literals: tuple[TechnicalLiteral, ...]
    matched_literal_identities: tuple[str, ...]
    boq_only_literal_identities: tuple[str, ...]
    source_only_literal_identities: tuple[str, ...]
    name_relation: LiteralNameRelation
    establishes_technical_equivalence: bool = False

    @model_validator(mode="after")
    def comparison_is_reproducible_and_non_conclusive(self) -> TechnicalLiteralComparison:
        if self.algorithm_version != TECHNICAL_LITERAL_ALGORITHM_VERSION:
            raise ValueError("Unsupported technical literal algorithm")
        if self.establishes_technical_equivalence:
            raise ValueError("Literal comparison cannot establish technical equivalence")
        expected_boq = extract_technical_literals(self.boq_text)
        expected_source = extract_technical_literals(self.source_text)
        if self.boq_literals != expected_boq or self.source_literals != expected_source:
            raise ValueError("Technical literal evidence does not reproduce from source text")
        expected = _comparison_fields(
            boq_text=self.boq_text,
            source_text=self.source_text,
            boq_literals=expected_boq,
            source_literals=expected_source,
        )
        if (
            self.matched_literal_identities,
            self.boq_only_literal_identities,
            self.source_only_literal_identities,
            self.name_relation,
        ) != expected:
            raise ValueError("Technical literal comparison does not reproduce")
        return self


def extract_technical_literals(value: str) -> tuple[TechnicalLiteral, ...]:
    if not value or len(value) > 20_000:
        raise ValueError("Technical literal source text is missing or too large")
    findings: list[TechnicalLiteral] = []
    identities: set[tuple[TechnicalLiteralKind, str]] = set()
    for kind, pattern in _PATTERNS:
        for match in pattern.finditer(value):
            raw_literal = match.group(0)
            normalized = _normalize_literal(raw_literal, kind)
            identity = (kind, normalized)
            if identity in identities:
                continue
            identities.add(identity)
            findings.append(
                TechnicalLiteral(
                    kind=kind,
                    raw_literal=raw_literal,
                    normalized_literal=normalized,
                    start_offset=match.start(),
                    end_offset=match.end(),
                )
            )
    return tuple(
        sorted(
            findings,
            key=lambda item: (item.start_offset, item.end_offset, item.kind),
        )
    )


def compare_technical_literals(*, boq_text: str, source_text: str) -> TechnicalLiteralComparison:
    boq_literals = extract_technical_literals(boq_text)
    source_literals = extract_technical_literals(source_text)
    matched, boq_only, source_only, relation = _comparison_fields(
        boq_text=boq_text,
        source_text=source_text,
        boq_literals=boq_literals,
        source_literals=source_literals,
    )
    return TechnicalLiteralComparison(
        boq_text=boq_text,
        source_text=source_text,
        boq_literals=boq_literals,
        source_literals=source_literals,
        matched_literal_identities=matched,
        boq_only_literal_identities=boq_only,
        source_only_literal_identities=source_only,
        name_relation=relation,
    )


def _comparison_fields(
    *,
    boq_text: str,
    source_text: str,
    boq_literals: tuple[TechnicalLiteral, ...],
    source_literals: tuple[TechnicalLiteral, ...],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], LiteralNameRelation]:
    boq_identities = {item.identity for item in boq_literals}
    source_identities = {item.identity for item in source_literals}
    matched = tuple(sorted(boq_identities & source_identities))
    boq_only = tuple(sorted(boq_identities - source_identities))
    source_only = tuple(sorted(source_identities - boq_identities))
    if _normalize_name(boq_text) == _normalize_name(source_text):
        relation: LiteralNameRelation = "EXACT_LITERAL_NAME"
    elif not boq_identities:
        relation = "NO_BOQ_LITERALS"
    elif not boq_only:
        relation = "ALL_EXTRACTED_BOQ_LITERALS_PRESENT"
    elif matched:
        relation = "PARTIAL_LITERAL_OVERLAP"
    else:
        relation = "NO_LITERAL_OVERLAP"
    return matched, boq_only, source_only, relation


def _normalize_literal(value: str, kind: TechnicalLiteralKind) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"(?<=\d),(?=\d)", ".", normalized)
    normalized = normalized.replace("×", "x")
    normalized = re.sub(r"(?<=\d)х(?=\d)", "x", normalized)
    if kind == "QUOTED_PHRASE":
        normalized = normalized.strip("«»\"")
        return " ".join(normalized.split())
    normalized = re.sub(r"\s+", "", normalized)
    return normalized


def _normalize_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"(?<=\d),(?=\d)", ".", normalized)
    normalized = normalized.replace("×", "x")
    normalized = re.sub(r"(?<=\d)х(?=\d)", "x", normalized)
    return " ".join(normalized.split())
