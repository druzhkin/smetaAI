from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field, field_validator, model_validator

from tenderguard.domain.common import ensure_utc
from tenderguard.domain.enums import VerificationStatus
from tenderguard.domain.models import DomainModel

BOQ_XLSX_PROFILE_SCHEMA = "boq-xlsx-profile/v1"
BOQ_XLSX_EXTRACTION_SCHEMA = "boq-xlsx-extraction/v1"
BOQ_ROW_VALUE_SCHEMA = "boq-row-candidate/v1"


class BoqXlsxColumn(DomainModel):
    column: int = Field(ge=1, le=16_384)
    header: str = Field(min_length=1, max_length=500)

    @field_validator("header")
    @classmethod
    def header_is_exact_literal(cls, value: str) -> str:
        if value != value.strip() or "\x00" in value:
            raise ValueError("BoQ header must be an exact bounded literal")
        return value


class BoqXlsxProfile(DomainModel):
    schema_version: str = BOQ_XLSX_PROFILE_SCHEMA
    profile_version_id: str = Field(min_length=1, max_length=64)
    worksheet_name: str = Field(min_length=1, max_length=500)
    header_row: int = Field(ge=1, le=1_048_576)
    data_start_row: int = Field(ge=1, le=1_048_576)
    data_end_row: int = Field(ge=1, le=1_048_576)
    position_id: BoqXlsxColumn
    description: BoqXlsxColumn
    unit: BoqXlsxColumn
    quantity: BoqXlsxColumn
    specification: BoqXlsxColumn | None = None
    reference: BoqXlsxColumn | None = None
    position_id_pattern: str = Field(min_length=1, max_length=500)
    section_row_patterns: tuple[str, ...] = ()
    allowed_units: tuple[str, ...] = Field(min_length=1, max_length=500)
    quantity_decimal_separator: Literal[".", ","]
    allow_quantity_formulas: bool = False
    expected_workbook_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @field_validator("worksheet_name")
    @classmethod
    def worksheet_name_is_exact_literal(cls, value: str) -> str:
        if value != value.strip() or any(character in value for character in "\r\n\x00"):
            raise ValueError("Worksheet name must be an exact single-line literal")
        return value

    @field_validator("allowed_units")
    @classmethod
    def units_are_unique_exact_literals(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("Allowed BoQ units must be unique")
        for value in values:
            if (
                not value
                or value != value.strip()
                or len(value) > 100
                or any(character in value for character in "\r\n\x00")
            ):
                raise ValueError("Allowed BoQ units must be exact single-line literals")
        return values

    @field_validator("position_id_pattern")
    @classmethod
    def position_pattern_is_valid(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as error:
            raise ValueError("Position ID pattern is invalid") from error
        return value

    @field_validator("section_row_patterns")
    @classmethod
    def section_patterns_are_valid(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("Section row patterns must be unique")
        for value in values:
            if not value or len(value) > 500:
                raise ValueError("Section row pattern is empty or too long")
            try:
                re.compile(value)
            except re.error as error:
                raise ValueError("Section row pattern is invalid") from error
        return values

    @model_validator(mode="after")
    def profile_is_bounded_and_unambiguous(self) -> BoqXlsxProfile:
        if self.schema_version != BOQ_XLSX_PROFILE_SCHEMA:
            raise ValueError("Unsupported BoQ XLSX profile schema")
        if self.header_row >= self.data_start_row or self.data_start_row > self.data_end_row:
            raise ValueError("BoQ header and data row bounds are inconsistent")
        if self.data_end_row - self.data_start_row + 1 > 100_000:
            raise ValueError("BoQ profile exceeds the bounded row limit")
        columns = (
            self.position_id,
            self.description,
            self.unit,
            self.quantity,
            self.specification,
            self.reference,
        )
        indexes = tuple(item.column for item in columns if item is not None)
        if len(indexes) != len(set(indexes)):
            raise ValueError("BoQ semantic columns must be unique")
        return self


class BoqCellEvidence(DomainModel):
    coordinate: str = Field(pattern=r"^[A-Z]{1,3}[1-9][0-9]{0,6}$")
    value_kind: Literal["EMPTY", "TEXT", "NUMBER", "FORMULA", "BOOLEAN", "OTHER"]
    source_literal: str | None = Field(default=None, max_length=20_000)
    formula: str | None = Field(default=None, max_length=20_000)
    cached_literal: str | None = Field(default=None, max_length=20_000)

    @model_validator(mode="after")
    def formula_fields_are_consistent(self) -> BoqCellEvidence:
        if self.value_kind == "FORMULA":
            if not self.formula or self.source_literal is not None:
                raise ValueError("Formula cell evidence is incomplete")
        elif self.formula is not None or self.cached_literal is not None:
            raise ValueError("Non-formula cell evidence cannot contain formula data")
        return self


class BoqRowCandidate(DomainModel):
    provisional_candidate_id: str = Field(pattern=r"^boq-candidate-[0-9a-f]{24}$")
    worksheet_name: str = Field(min_length=1, max_length=500)
    row_number: int = Field(ge=1, le=1_048_576)
    source_position_id: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=20_000)
    specification: str | None = Field(default=None, max_length=20_000)
    source_reference: str | None = Field(default=None, max_length=20_000)
    unit: str | None = Field(default=None, max_length=100)
    quantity: Decimal | None = Field(
        default=None,
        gt=0,
        max_digits=38,
        decimal_places=12,
    )
    cells: dict[str, BoqCellEvidence]
    blockers: tuple[str, ...] = ()
    status: VerificationStatus = VerificationStatus.UNVERIFIED

    @model_validator(mode="after")
    def candidate_is_never_verified_by_parsing(self) -> BoqRowCandidate:
        if self.status is not VerificationStatus.UNVERIFIED:
            raise ValueError("Spreadsheet parsing cannot verify a BoQ row")
        if not self.cells:
            raise ValueError("BoQ row candidate has no cell provenance")
        return self


class ImportedBoqRowValue(DomainModel):
    """Persisted, source-faithful value produced by the governed XLSX adapter."""

    schema_version: str = BOQ_ROW_VALUE_SCHEMA
    source_item_id: str = Field(pattern=r"^boq-source-[0-9a-f]{24}$")
    source_position_id: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=1, max_length=20_000)
    specification: str | None = Field(default=None, max_length=20_000)
    source_reference: str | None = Field(default=None, max_length=20_000)
    unit: str = Field(min_length=1, max_length=100)
    quantity: Decimal = Field(gt=0, max_digits=38, decimal_places=12)
    cells: dict[str, BoqCellEvidence]
    worksheet_name: str = Field(min_length=1, max_length=500)
    row_number: int = Field(ge=1, le=1_048_576)
    archive_path: str = Field(min_length=1, max_length=4000)
    workbook_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workbook_profile_version_id: str = Field(min_length=1, max_length=64)
    workbook_profile_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def persisted_row_is_complete(self) -> ImportedBoqRowValue:
        if self.schema_version != BOQ_ROW_VALUE_SCHEMA:
            raise ValueError("Unsupported persisted BoQ row schema")
        if not self.cells:
            raise ValueError("Persisted BoQ row has no cell provenance")
        if self.source_position_id != self.source_position_id.strip():
            raise ValueError("Persisted BoQ position ID must be normalized")
        if self.unit != self.unit.strip():
            raise ValueError("Persisted BoQ unit must be normalized")
        return self


class BoqXlsxExtractionResult(DomainModel):
    schema_version: str = BOQ_XLSX_EXTRACTION_SCHEMA
    status: Literal["UNVERIFIED", "BLOCKED"]
    profile_version_id: str
    profile_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    root_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workbook_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    archive_path: str = Field(min_length=1, max_length=4000)
    worksheet_name: str = Field(min_length=1, max_length=500)
    candidates: tuple[BoqRowCandidate, ...]
    global_blockers: tuple[str, ...] = ()
    extracted_at: datetime
    parser_name: str = "tenderguard-boq-xlsx"
    parser_version: str = "1.0.0"
    ready_for_boq: bool = False
    workflow_blockers: tuple[str, ...] = (
        "CONTROLLED_IMPORT_WORKFLOW_REQUIRED",
        "INDEPENDENT_ROW_REVIEW_REQUIRED",
    )

    @field_validator("extracted_at")
    @classmethod
    def extracted_at_is_aware(cls, value: datetime) -> datetime:
        normalized = ensure_utc(value)
        if normalized is None or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("BoQ extraction timestamp must include a timezone")
        return normalized

    @model_validator(mode="after")
    def extraction_is_fail_closed(self) -> BoqXlsxExtractionResult:
        if self.schema_version != BOQ_XLSX_EXTRACTION_SCHEMA:
            raise ValueError("Unsupported BoQ XLSX extraction schema")
        has_blockers = bool(self.global_blockers) or any(
            candidate.blockers for candidate in self.candidates
        )
        expected_status = "BLOCKED" if has_blockers else "UNVERIFIED"
        if self.status != expected_status:
            raise ValueError("BoQ extraction status contradicts its blockers")
        if self.ready_for_boq or not self.workflow_blockers:
            raise ValueError("Raw spreadsheet extraction cannot create a verified BoQ")
        return self
