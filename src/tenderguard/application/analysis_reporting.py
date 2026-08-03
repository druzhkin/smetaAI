from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from tenderguard.application.pricing import (
    BoqPriceMatrixRowView,
    BoqPriceMatrixView,
    BoqSourcePriceView,
)
from tenderguard.domain.boq_spreadsheet import BoqXlsxExtractionResult
from tenderguard.domain.common import content_hash
from tenderguard.domain.enums import ApprovalState
from tenderguard.domain.models import DomainModel

BOQ_ANALYSIS_SCHEMA_VERSION = "tenderguard.boq-analysis/v1"
BOQ_ANALYSIS_MANIFEST_SCHEMA_VERSION = "tenderguard.boq-analysis-manifest/v1"

SourceGroup = Literal["WON_TENDER", "FGIS_CS", "MARKET", "OTHER"]


class BoqAnalysisSource(DomainModel):
    source_group: SourceGroup
    quote_id: str
    normalized_price_id: str | None = None
    source_type: str
    evidence_class: str
    display_name: str
    source_item_name: str
    source_record_id: str
    source_uri: str | None = None
    source_locator: str
    source_document_revision_id: str
    source_observation_id: str
    source_origin_id: str
    observed_at: datetime
    quote_date: date
    valid_until: date | None = None
    available: bool | None = None
    lead_time_days: int | None = Field(default=None, ge=0)
    raw_amount: Decimal = Field(gt=0, max_digits=38, decimal_places=12)
    raw_currency: str
    raw_unit: str
    normalized_amount_per_unit: Decimal | None = Field(
        default=None,
        gt=0,
        max_digits=38,
        decimal_places=12,
    )
    normalized_currency: str | None = None
    normalized_unit: str | None = None
    normalization_formula_hash: str | None = None
    normalization_policy_version_id: str | None = None
    technical_attributes: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalized_fields_are_complete(self) -> BoqAnalysisSource:
        normalized = (
            self.normalized_price_id,
            self.normalized_amount_per_unit,
            self.normalized_currency,
            self.normalized_unit,
            self.normalization_formula_hash,
            self.normalization_policy_version_id,
        )
        if any(value is not None for value in normalized) and not all(
            value is not None for value in normalized
        ):
            raise ValueError("Normalized analysis-source fields must be all present or all absent")
        return self


class BoqAnalysisNameMatch(DomainModel):
    match_id: str
    status: str
    match_class: str
    boq_item_name: str
    source_item_id: str
    canonical_item_id: str | None
    source_attributes: dict[str, str]
    canonical_attributes: dict[str, str]
    mismatched_attributes: tuple[str, ...]
    missing_attributes: tuple[str, ...]
    catalog_version_id: str
    assessment_method: str | None = None


class BoqAnalysisRow(DomainModel):
    row_id: str
    boq_line_id: str
    line_key: str
    wbs_node_id: str
    work_code: str
    boq_item_name: str
    boq_unit: str
    quantity: Decimal | None = Field(
        default=None,
        gt=0,
        max_digits=38,
        decimal_places=12,
    )
    quantity_status: str
    item_id: str
    cost_category: str | None = None
    basis_kind: str | None = None
    row_status: Literal["VERIFIED", "BLOCKED"]
    blockers: tuple[str, ...]
    name_match: BoqAnalysisNameMatch | None = None
    sources: tuple[BoqAnalysisSource, ...] = ()
    proposed_amount_per_unit: Decimal | None = Field(
        default=None,
        gt=0,
        max_digits=38,
        decimal_places=12,
    )
    proposed_currency: str | None = None
    proposed_unit: str | None = None
    price_decision_id: str | None = None
    price_as_of: date | None = None
    selection_method: str | None = None
    normalized_price_ids: tuple[str, ...] = ()
    rationale: tuple[str, ...]
    line_amount: Decimal | None = Field(
        default=None,
        gt=0,
        max_digits=38,
        decimal_places=12,
    )

    @model_validator(mode="after")
    def row_is_fail_closed(self) -> BoqAnalysisRow:
        financial = (
            self.proposed_amount_per_unit,
            self.proposed_currency,
            self.proposed_unit,
            self.price_decision_id,
            self.price_as_of,
            self.selection_method,
        )
        if self.row_status == "BLOCKED":
            if any(value is not None for value in financial) or self.line_amount is not None:
                raise ValueError("A blocked analysis row cannot expose a proposed price or total")
            if not self.blockers:
                raise ValueError("A blocked analysis row must explain its blockers")
        else:
            if self.blockers:
                raise ValueError("A verified analysis row cannot retain blockers")
            if any(value is None for value in financial):
                raise ValueError("A verified analysis row requires the complete price decision")
            if self.quantity is None or self.line_amount is None:
                raise ValueError("A verified analysis row requires quantity and line amount")
        return self


class BoqAnalysisReport(DomainModel):
    schema_version: str = BOQ_ANALYSIS_SCHEMA_VERSION
    project_id: str
    project_code: str
    source_kind: Literal["PRICE_MATRIX", "XLSX_EXTRACTION"]
    source_reference: str
    source_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_at: datetime
    release_state: str
    rows: tuple[BoqAnalysisRow, ...]
    blocked_row_count: int = Field(ge=0)
    global_blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    calculation_snapshot_id: str | None = None
    final_total: Decimal | None = Field(
        default=None,
        gt=0,
        max_digits=38,
        decimal_places=12,
    )
    final_currency: str | None = None
    analysis_status: Literal["APPROVED_FOR_BID", "BLOCKED"]

    @model_validator(mode="after")
    def report_is_fail_closed(self) -> BoqAnalysisReport:
        if self.schema_version != BOQ_ANALYSIS_SCHEMA_VERSION:
            raise ValueError("Unsupported BoQ analysis schema")
        actual_blocked = sum(row.row_status == "BLOCKED" for row in self.rows)
        if self.blocked_row_count != actual_blocked:
            raise ValueError("BoQ analysis blocked-row count is inconsistent")
        approved = self.analysis_status == "APPROVED_FOR_BID"
        if approved:
            if (
                self.release_state != ApprovalState.APPROVED_FOR_BID.value
                or actual_blocked
                or self.global_blockers
                or self.calculation_snapshot_id is None
                or self.final_total is None
                or self.final_currency is None
            ):
                raise ValueError("Approved analysis lacks a released fixed calculation")
        elif self.final_total is not None or self.final_currency is not None:
            raise ValueError("A blocked analysis cannot expose a final total")
        return self


class BoqAnalysisArtifactEntry(DomainModel):
    filename: str
    media_type: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)


class BoqAnalysisArtifactManifest(DomainModel):
    schema_version: str = BOQ_ANALYSIS_MANIFEST_SCHEMA_VERSION
    project_id: str
    project_code: str
    report_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    analysis_status: Literal["APPROVED_FOR_BID", "BLOCKED"]
    release_state: str
    generated_at: datetime
    artifacts: tuple[BoqAnalysisArtifactEntry, ...]

    @model_validator(mode="after")
    def manifest_has_exact_artifacts(self) -> BoqAnalysisArtifactManifest:
        media_types = {artifact.media_type for artifact in self.artifacts}
        if media_types != {
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }:
            raise ValueError("BoQ analysis manifest must contain one XLSX and one DOCX")
        if len(self.artifacts) != 2 or len({item.filename for item in self.artifacts}) != 2:
            raise ValueError("BoQ analysis manifest artifact identities are invalid")
        return self


def build_analysis_from_price_matrix(
    *,
    matrix: BoqPriceMatrixView,
    project_code: str,
    release_state: str,
    calculation_snapshot_id: str | None = None,
    final_total: Decimal | None = None,
    final_currency: str | None = None,
    global_blockers: Iterable[str] = (),
) -> BoqAnalysisReport:
    rows = tuple(_analysis_row(row) for row in matrix.rows)
    blockers = list(dict.fromkeys(global_blockers))
    blockers.append("GOVERNED_RELEASE_EXPORT_NOT_IMPLEMENTED")
    if any(row.row_status == "BLOCKED" for row in rows):
        blockers.append("PRICE_MATRIX_ROWS_BLOCKED")
    if calculation_snapshot_id is None:
        blockers.append("CALCULATION_SNAPSHOT_MISSING")
    if release_state != ApprovalState.APPROVED_FOR_BID.value:
        blockers.append("BID_RELEASE_NOT_APPROVED")
    blockers = list(dict.fromkeys(blockers))
    can_release = False
    if not can_release:
        final_total = None
        final_currency = None
    return BoqAnalysisReport(
        project_id=matrix.project_id,
        project_code=_bounded_literal(project_code, "project code"),
        source_kind="PRICE_MATRIX",
        source_reference="governed BoQ price matrix",
        source_content_hash=content_hash(matrix),
        generated_at=matrix.generated_at,
        release_state=release_state,
        rows=rows,
        blocked_row_count=sum(row.row_status == "BLOCKED" for row in rows),
        global_blockers=tuple(blockers),
        warnings=(matrix.release_warning,),
        calculation_snapshot_id=calculation_snapshot_id,
        final_total=final_total,
        final_currency=final_currency,
        analysis_status="APPROVED_FOR_BID" if can_release else "BLOCKED",
    )


def build_analysis_from_extraction(
    *,
    extraction: BoqXlsxExtractionResult,
    project_id: str,
    project_code: str,
) -> BoqAnalysisReport:
    global_blockers = tuple(
        dict.fromkeys(
            (
                *extraction.global_blockers,
                *extraction.workflow_blockers,
                "PRICE_MATRIX_NOT_AVAILABLE",
                "CALCULATION_SNAPSHOT_MISSING",
                "BID_RELEASE_NOT_APPROVED",
            )
        )
    )
    rows = tuple(
        BoqAnalysisRow(
            row_id=candidate.provisional_candidate_id,
            boq_line_id=candidate.provisional_candidate_id,
            line_key=candidate.source_position_id or f"XLSX-ROW-{candidate.row_number}",
            wbs_node_id="UNMAPPED",
            work_code="UNMAPPED",
            boq_item_name=candidate.description or "NOT_EXTRACTED",
            boq_unit=candidate.unit or "NOT_EXTRACTED",
            quantity=candidate.quantity,
            quantity_status="UNVERIFIED" if candidate.quantity is not None else "MISSING",
            item_id=candidate.provisional_candidate_id,
            row_status="BLOCKED",
            blockers=tuple(dict.fromkeys((*global_blockers, *candidate.blockers))),
            rationale=(
                "Строка извлечена из XLSX как непроверенное свидетельство.",
                "Цены и итог удерживаются до управляемого сопоставления и расчета.",
            ),
        )
        for candidate in extraction.candidates
    )
    return BoqAnalysisReport(
        project_id=_bounded_literal(project_id, "project ID"),
        project_code=_bounded_literal(project_code, "project code"),
        source_kind="XLSX_EXTRACTION",
        source_reference=extraction.archive_path,
        source_content_hash=extraction.workbook_object_sha256,
        generated_at=extraction.extracted_at,
        release_state=ApprovalState.BLOCKED.value,
        rows=rows,
        blocked_row_count=len(rows),
        global_blockers=global_blockers,
        warnings=(
            "Диагностический отчет не подтверждает номенклатуру, цены или полноту проекта.",
        ),
        analysis_status="BLOCKED",
    )


def analysis_report_hash(report: BoqAnalysisReport) -> str:
    return content_hash(report)


def _analysis_row(row: BoqPriceMatrixRowView) -> BoqAnalysisRow:
    sources = (
        *_analysis_sources("WON_TENDER", row.won_tender_prices),
        *_analysis_sources("FGIS_CS", row.fgis_cs_prices),
        *_analysis_sources("MARKET", row.market_prices),
        *_analysis_sources("OTHER", row.other_prices),
    )
    match = (
        None
        if row.name_match is None
        else BoqAnalysisNameMatch(
            match_id=row.name_match.match_id,
            status=row.name_match.status,
            match_class=row.name_match.match_class.value,
            boq_item_name=row.name_match.boq_item_name,
            source_item_id=row.name_match.source_item_id,
            canonical_item_id=row.name_match.canonical_item_id,
            source_attributes=row.name_match.source_attributes,
            canonical_attributes=row.name_match.canonical_attributes,
            mismatched_attributes=row.name_match.mismatched_attributes,
            missing_attributes=row.name_match.missing_attributes,
            catalog_version_id=row.name_match.catalog_version_id,
            assessment_method=row.name_match.assessment_method,
        )
    )
    verified = row.row_status == "VERIFIED"
    proposed = row.proposed_price
    line_amount = (
        proposed.amount_per_unit * row.quantity
        if verified and proposed.amount_per_unit is not None and row.quantity is not None
        else None
    )
    return BoqAnalysisRow(
        row_id=row.row_id,
        boq_line_id=row.boq_line_id,
        line_key=row.line_key,
        wbs_node_id=row.wbs_node_id,
        work_code=row.work_code,
        boq_item_name=row.boq_item_name,
        boq_unit=row.boq_unit,
        quantity=row.quantity,
        quantity_status=row.quantity_status,
        item_id=row.item_id,
        cost_category=row.cost_category,
        basis_kind=row.basis_kind,
        row_status=row.row_status,
        blockers=row.blockers,
        name_match=match,
        sources=sources,
        proposed_amount_per_unit=proposed.amount_per_unit if verified else None,
        proposed_currency=proposed.currency if verified else None,
        proposed_unit=proposed.unit if verified else None,
        price_decision_id=proposed.decision_id if verified else None,
        price_as_of=proposed.as_of if verified else None,
        selection_method=proposed.selection_method if verified else None,
        normalized_price_ids=proposed.normalized_price_ids if verified else (),
        rationale=proposed.rationale,
        line_amount=line_amount,
    )


def _analysis_sources(
    source_group: SourceGroup,
    prices: tuple[BoqSourcePriceView, ...],
) -> tuple[BoqAnalysisSource, ...]:
    results: list[BoqAnalysisSource] = []
    for price in prices:
        normalized = price.normalized_prices or (None,)
        for normalized_price in normalized:
            results.append(
                BoqAnalysisSource(
                    source_group=source_group,
                    quote_id=price.quote_id,
                    normalized_price_id=(
                        normalized_price.normalized_price_id
                        if normalized_price is not None
                        else None
                    ),
                    source_type=price.source_reference.source_type.value,
                    evidence_class=price.evidence_class.value,
                    display_name=price.source_reference.display_name,
                    source_item_name=price.source_reference.source_item_name,
                    source_record_id=price.source_reference.source_record_id,
                    source_uri=price.source_reference.source_uri,
                    source_locator=price.source_locator,
                    source_document_revision_id=price.source_document_revision_id,
                    source_observation_id=price.source_observation_id,
                    source_origin_id=price.source_origin_id,
                    observed_at=price.observed_at,
                    quote_date=price.quote_date,
                    valid_until=price.valid_until,
                    available=price.available,
                    lead_time_days=price.lead_time_days,
                    raw_amount=price.raw_amount,
                    raw_currency=price.raw_currency,
                    raw_unit=price.raw_unit,
                    normalized_amount_per_unit=(
                        normalized_price.amount_per_unit
                        if normalized_price is not None
                        else None
                    ),
                    normalized_currency=(
                        normalized_price.currency if normalized_price is not None else None
                    ),
                    normalized_unit=(
                        normalized_price.unit if normalized_price is not None else None
                    ),
                    normalization_formula_hash=(
                        normalized_price.formula_hash if normalized_price is not None else None
                    ),
                    normalization_policy_version_id=(
                        normalized_price.policy_version_id
                        if normalized_price is not None
                        else None
                    ),
                    technical_attributes=price.technical_attributes,
                )
            )
    return tuple(results)


def _bounded_literal(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 200 or any(ch in normalized for ch in "\r\n\x00"):
        raise ValueError(f"Invalid {label}")
    return normalized
