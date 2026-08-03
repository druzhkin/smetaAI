from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from tenderguard.application.boq_market_research import (
    BoqMarketLineResult,
    BoqMarketSourceObservation,
    PreparedBoqMarketResearchPackage,
    verify_boq_market_research_package,
)
from tenderguard.domain.common import content_hash, ensure_utc, utc_now
from tenderguard.domain.models import DomainModel
from tenderguard.domain.technical_literals import (
    TECHNICAL_LITERAL_ALGORITHM_VERSION,
    TechnicalLiteralComparison,
    compare_technical_literals,
)
from tenderguard.integrations.public_market import (
    PublicMarketOfferCandidate,
    PublicMarketPageRequest,
)

BOQ_MARKET_ASSESSMENT_SCHEMA = "boq-market-technical-assessment/v2"
BOQ_MARKET_ASSESSMENT_REPORT_SCHEMA = "boq-market-assessment-report/v1"
DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class BoqMarketCandidateAssessment(DomainModel):
    source_request: PublicMarketPageRequest
    candidate: PublicMarketOfferCandidate
    candidate_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    literal_comparison: TechnicalLiteralComparison
    commercial_gaps: tuple[str, ...] = Field(min_length=1)
    status: Literal["BLOCKED"] = "BLOCKED"
    ready_for_normalization: bool = False

    @model_validator(mode="after")
    def assessment_is_reproducible_and_blocked(self) -> BoqMarketCandidateAssessment:
        if self.candidate_content_hash != content_hash(self.candidate):
            raise ValueError("Market candidate assessment content hash does not reproduce")
        if (
            self.literal_comparison.source_text != self.candidate.source_item_name
            or self.literal_comparison.establishes_technical_equivalence
        ):
            raise ValueError("Market candidate literal comparison differs from its source item")
        if self.commercial_gaps != _commercial_gaps(self.candidate):
            raise ValueError("Market candidate commercial gaps do not reproduce")
        if self.ready_for_normalization:
            raise ValueError("Diagnostic market assessment cannot normalize a price")
        return self


class BoqMarketLineAssessment(DomainModel):
    candidate_id: str = Field(pattern=r"^boq-candidate-[0-9a-f]{24}$")
    row_number: int = Field(ge=1, le=1_048_576)
    boq_description: str = Field(min_length=1, max_length=20_000)
    boq_unit: str = Field(min_length=1, max_length=100)
    source_observation_count: int = Field(ge=0, le=10)
    source_pages_without_offer_count: int = Field(ge=0, le=10)
    source_error_count: int = Field(ge=0, le=10)
    structured_finding_count: int = Field(ge=0, le=2000)
    source_observations: tuple[BoqMarketSourceObservation, ...] = Field(max_length=10)
    candidate_assessments: tuple[BoqMarketCandidateAssessment, ...] = Field(max_length=2000)
    status: Literal["BLOCKED"] = "BLOCKED"
    ready_for_nomenclature: bool = False
    ready_for_normalization: bool = False
    blockers: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def line_is_internally_consistent_and_blocked(self) -> BoqMarketLineAssessment:
        if self.ready_for_nomenclature or self.ready_for_normalization:
            raise ValueError("Diagnostic market line cannot enter a governed pricing workflow")
        expected_assessments = _candidate_assessments(
            sources=self.source_observations,
            boq_description=self.boq_description,
        )
        if self.candidate_assessments != expected_assessments:
            raise ValueError("Market line candidate assessments do not reproduce from sources")
        identities = tuple(
            (item.source_request.source_uri, item.candidate_content_hash)
            for item in self.candidate_assessments
        )
        if len(identities) != len(set(identities)):
            raise ValueError("Market line assessment contains duplicate source candidates")
        if any(
            item.literal_comparison.boq_text != self.boq_description
            for item in self.candidate_assessments
        ):
            raise ValueError("Market line literal comparison differs from its BoQ description")
        expected_observation_count = len(self.source_observations)
        expected_without_offer = sum(
            source.page_result is not None and not source.page_result.candidates
            for source in self.source_observations
        )
        expected_errors = sum(
            source.acquisition_error_code is not None for source in self.source_observations
        )
        expected_findings = sum(
            len(source.page_result.extraction_findings)
            for source in self.source_observations
            if source.page_result is not None
        )
        if (
            self.source_observation_count,
            self.source_pages_without_offer_count,
            self.source_error_count,
            self.structured_finding_count,
        ) != (
            expected_observation_count,
            expected_without_offer,
            expected_errors,
            expected_findings,
        ):
            raise ValueError("Market line source counters do not reproduce")
        expected = _line_blockers(
            candidate_assessments=self.candidate_assessments,
            source_observation_count=self.source_observation_count,
            source_pages_without_offer_count=self.source_pages_without_offer_count,
            source_error_count=self.source_error_count,
            structured_finding_count=self.structured_finding_count,
        )
        if self.blockers != expected:
            raise ValueError("Market line assessment blockers do not reproduce")
        return self


class BoqMarketTechnicalAssessmentPackage(DomainModel):
    schema_version: str = BOQ_MARKET_ASSESSMENT_SCHEMA
    algorithm_version: str = TECHNICAL_LITERAL_ALGORITHM_VERSION
    source_market_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_market_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    project_code: str = Field(min_length=1, max_length=200)
    workbook_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    completed_at: datetime
    lines: tuple[BoqMarketLineAssessment, ...] = Field(min_length=1)
    status: Literal["BLOCKED"] = "BLOCKED"
    ready_for_nomenclature: bool = False
    ready_for_normalization: bool = False
    global_blockers: tuple[str, ...] = Field(min_length=1)

    @field_validator("completed_at")
    @classmethod
    def completed_at_is_aware(cls, value: datetime) -> datetime:
        normalized = ensure_utc(value)
        if normalized is None or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Market assessment timestamp must include a timezone")
        return normalized

    @model_validator(mode="after")
    def package_is_complete_and_fail_closed(self) -> BoqMarketTechnicalAssessmentPackage:
        if self.schema_version != BOQ_MARKET_ASSESSMENT_SCHEMA:
            raise ValueError("Unsupported BoQ market assessment schema")
        if self.algorithm_version != TECHNICAL_LITERAL_ALGORITHM_VERSION:
            raise ValueError("Unsupported BoQ market assessment algorithm")
        if self.ready_for_nomenclature or self.ready_for_normalization:
            raise ValueError("Diagnostic market assessment cannot release downstream data")
        line_ids = tuple(item.candidate_id for item in self.lines)
        if len(line_ids) != len(set(line_ids)):
            raise ValueError("Market assessment package contains duplicate BoQ lines")
        if self.global_blockers != _global_blockers(self.lines):
            raise ValueError("Market assessment global blockers do not reproduce")
        return self


class BoqMarketAssessmentArtifact(DomainModel):
    filename: str = Field(pattern=r"^[^/\\\x00]{1,200}\.docx$")
    media_type: Literal[
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ] = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)


class BoqMarketAssessmentReportManifest(DomainModel):
    schema_version: str = BOQ_MARKET_ASSESSMENT_REPORT_SCHEMA
    project_code: str = Field(min_length=1, max_length=200)
    source_market_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_assessment_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_assessment_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_at: datetime
    status: Literal["BLOCKED"] = "BLOCKED"
    final_estimate_available: bool = False
    artifacts: tuple[BoqMarketAssessmentArtifact, ...] = Field(min_length=1, max_length=1)

    @field_validator("generated_at")
    @classmethod
    def generated_at_is_aware(cls, value: datetime) -> datetime:
        normalized = ensure_utc(value)
        if normalized is None or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Market assessment report timestamp must include a timezone")
        return normalized

    @model_validator(mode="after")
    def report_is_exact_and_fail_closed(self) -> BoqMarketAssessmentReportManifest:
        if self.schema_version != BOQ_MARKET_ASSESSMENT_REPORT_SCHEMA:
            raise ValueError("Unsupported market assessment report schema")
        if self.final_estimate_available:
            raise ValueError("Diagnostic market assessment report cannot expose an estimate")
        if len({item.filename for item in self.artifacts}) != 1:
            raise ValueError("Market assessment report must contain one DOCX")
        return self


def build_boq_market_technical_assessment(
    *,
    market: PreparedBoqMarketResearchPackage,
    source_market_manifest_sha256: str,
    completed_at: datetime | None = None,
) -> BoqMarketTechnicalAssessmentPackage:
    market_manifest = verify_boq_market_research_package(market)
    lines = tuple(_assess_line(line) for line in market_manifest.line_results)
    return BoqMarketTechnicalAssessmentPackage(
        source_market_manifest_sha256=source_market_manifest_sha256,
        source_market_content_hash=content_hash(market_manifest),
        project_code=market_manifest.project_code,
        workbook_sha256=market_manifest.workbook_sha256,
        completed_at=completed_at or utc_now(),
        lines=lines,
        global_blockers=_global_blockers(lines),
    )


def verify_boq_market_technical_assessment(
    *,
    market: PreparedBoqMarketResearchPackage,
    source_market_manifest_sha256: str,
    assessment: BoqMarketTechnicalAssessmentPackage,
) -> BoqMarketTechnicalAssessmentPackage:
    expected = build_boq_market_technical_assessment(
        market=market,
        source_market_manifest_sha256=source_market_manifest_sha256,
        completed_at=assessment.completed_at,
    )
    if expected != assessment:
        raise ValueError("Market technical assessment does not reproduce from retained evidence")
    return assessment


def _assess_line(line: BoqMarketLineResult) -> BoqMarketLineAssessment:
    assessments = _candidate_assessments(
        sources=line.sources,
        boq_description=line.boq_description,
    )
    without_offer = sum(
        source.page_result is not None and not source.page_result.candidates
        for source in line.sources
    )
    blockers = _line_blockers(
        candidate_assessments=assessments,
        source_observation_count=len(line.sources),
        source_pages_without_offer_count=without_offer,
        source_error_count=line.source_error_count,
        structured_finding_count=line.structured_finding_count,
    )
    return BoqMarketLineAssessment(
        candidate_id=line.candidate_id,
        row_number=line.row_number,
        boq_description=line.boq_description,
        boq_unit=line.boq_unit,
        source_observation_count=len(line.sources),
        source_pages_without_offer_count=without_offer,
        source_error_count=line.source_error_count,
        structured_finding_count=line.structured_finding_count,
        source_observations=line.sources,
        candidate_assessments=assessments,
        blockers=blockers,
    )


def _candidate_assessments(
    *,
    sources: tuple[BoqMarketSourceObservation, ...],
    boq_description: str,
) -> tuple[BoqMarketCandidateAssessment, ...]:
    return tuple(
        BoqMarketCandidateAssessment(
            source_request=source.request,
            candidate=candidate,
            candidate_content_hash=content_hash(candidate),
            literal_comparison=compare_technical_literals(
                boq_text=boq_description,
                source_text=candidate.source_item_name,
            ),
            commercial_gaps=_commercial_gaps(candidate),
        )
        for source in sources
        if source.page_result is not None
        for candidate in source.page_result.candidates
    )


def _commercial_gaps(candidate: PublicMarketOfferCandidate) -> tuple[str, ...]:
    gaps: list[str] = []
    if candidate.unit_code is None and candidate.unit_text is None:
        gaps.append("STRUCTURED_SOURCE_UNIT_MISSING")
    if candidate.availability_literal is None:
        gaps.append("STRUCTURED_AVAILABILITY_MISSING")
    if candidate.price_valid_until_literal is None:
        gaps.append("STRUCTURED_PRICE_VALIDITY_MISSING")
    gaps.extend(
        (
            "PACKAGE_QUANTITY_NOT_STRUCTURED",
            "VAT_BASIS_NOT_STRUCTURED",
            "SOURCE_REGION_NOT_STRUCTURED",
            "DELIVERY_BASIS_NOT_STRUCTURED",
            "UNLOADING_BASIS_NOT_STRUCTURED",
            "PAYMENT_TERMS_NOT_STRUCTURED",
            "BOQ_UNIT_MAPPING_NOT_APPROVED",
            "APPROVED_NORMALIZATION_POLICY_REQUIRED",
        )
    )
    return tuple(gaps)


def _line_blockers(
    *,
    candidate_assessments: tuple[BoqMarketCandidateAssessment, ...],
    source_observation_count: int,
    source_pages_without_offer_count: int,
    source_error_count: int,
    structured_finding_count: int,
) -> tuple[str, ...]:
    blockers: list[str] = []
    if source_observation_count == 0:
        blockers.append("MARKET_PUBLIC_SOURCE_NOT_SELECTED")
    if not candidate_assessments:
        blockers.append("STRUCTURED_MARKET_OFFER_NOT_FOUND")
    if source_pages_without_offer_count:
        blockers.append("MARKET_PAGES_WITHOUT_STRUCTURED_OFFER")
    if source_error_count:
        blockers.append("MARKET_SOURCE_ERRORS_PRESENT")
    if structured_finding_count:
        blockers.append("MARKET_STRUCTURED_DATA_FINDINGS_PRESENT")
    if any(item.literal_comparison.boq_only_literal_identities for item in candidate_assessments):
        blockers.append("BOQ_LITERAL_REQUIREMENTS_NOT_PRESENT_IN_SOURCE")
    if any(
        item.literal_comparison.source_only_literal_identities
        for item in candidate_assessments
    ):
        blockers.append("SOURCE_VARIANT_LITERALS_REQUIRE_CLASSIFICATION")
    blockers.extend(
        (
            "APPROVED_TECHNICAL_ATTRIBUTE_SCHEMA_REQUIRED",
            "TECHNICAL_EQUIVALENCE_NOT_ESTABLISHED",
            "COMMERCIAL_BASIS_INCOMPLETE",
            "PRICE_NORMALIZATION_REQUIRED",
            "INDEPENDENT_VALIDATION_REQUIRED",
            "BID_RELEASE_NOT_APPROVED",
        )
    )
    return tuple(blockers)


def _global_blockers(lines: tuple[BoqMarketLineAssessment, ...]) -> tuple[str, ...]:
    blockers: list[str] = []
    if any(not line.candidate_assessments for line in lines):
        blockers.append("MARKET_OFFER_COVERAGE_INCOMPLETE")
    if any("BOQ_LITERAL_REQUIREMENTS_NOT_PRESENT_IN_SOURCE" in line.blockers for line in lines):
        blockers.append("BOQ_LITERAL_GAPS_PRESENT")
    if any(line.source_error_count for line in lines):
        blockers.append("MARKET_SOURCE_ERRORS_PRESENT")
    if any(line.structured_finding_count for line in lines):
        blockers.append("MARKET_STRUCTURED_DATA_FINDINGS_PRESENT")
    blockers.extend(
        (
            "DIAGNOSTIC_LITERAL_ASSESSMENT_NOT_GOVERNED",
            "APPROVED_TECHNICAL_ATTRIBUTE_SCHEMA_REQUIRED",
            "TECHNICAL_EQUIVALENCE_NOT_ESTABLISHED",
            "COMMERCIAL_BASIS_INCOMPLETE",
            "PRICE_NORMALIZATION_REQUIRED",
            "INDEPENDENT_VALIDATION_REQUIRED",
            "BID_RELEASE_NOT_APPROVED",
        )
    )
    return tuple(blockers)
