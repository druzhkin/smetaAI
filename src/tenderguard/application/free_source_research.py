from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field, field_validator, model_validator

from tenderguard.domain.boq_spreadsheet import BoqRowCandidate, BoqXlsxExtractionResult
from tenderguard.domain.common import content_hash, ensure_utc, utc_now
from tenderguard.domain.models import DomainModel
from tenderguard.integrations.fgiscs_public import (
    FgisCsKsrSearchAcquisition,
    FgisCsKsrSearchResult,
    FgisCsPublicApiError,
    FgisCsRawHttpExchange,
    replay_fgiscs_ksr_search_acquisition,
)

BOQ_FREE_SOURCE_PROFILE_SCHEMA = "boq-free-source-research-profile/v1"
BOQ_FREE_SOURCE_RESULT_SCHEMA = "boq-free-source-research-result/v1"

CostNature = Literal["WORK", "MATERIAL", "LOGISTICS"]
AcquireKsrSearch = Callable[[str], FgisCsKsrSearchAcquisition]


class BoqResearchEvidenceReference(DomainModel):
    label: str = Field(min_length=1, max_length=500)
    object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_locator: str = Field(min_length=1, max_length=4000)

    @field_validator("label", "source_locator")
    @classmethod
    def literals_are_bounded(cls, value: str) -> str:
        if value != value.strip() or any(character in value for character in "\r\n\x00"):
            raise ValueError("Research evidence references must be exact single-line literals")
        return value


class BoqFreeSourceLineRule(DomainModel):
    candidate_id: str = Field(pattern=r"^boq-candidate-[0-9a-f]{24}$")
    cost_nature: CostNature
    fgis_ksr_query: str | None = Field(default=None, min_length=1, max_length=1000)

    @field_validator("fgis_ksr_query")
    @classmethod
    def queries_and_literals_are_exact(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value != value.strip() or any(character in value for character in "\r\n\x00"):
            raise ValueError("Research literals and queries must be exact single-line values")
        return value

    @model_validator(mode="after")
    def only_materials_have_ksr_queries(self) -> BoqFreeSourceLineRule:
        if self.cost_nature == "MATERIAL" and self.fgis_ksr_query is None:
            raise ValueError("Material research rules require an exact FGIS KSR query")
        if self.cost_nature != "MATERIAL" and self.fgis_ksr_query is not None:
            raise ValueError("Work and logistics rules cannot issue an FGIS material query")
        return self


class BoqFreeSourceResearchProfile(DomainModel):
    schema_version: str = BOQ_FREE_SOURCE_PROFILE_SCHEMA
    profile_version_id: str = Field(min_length=1, max_length=64)
    project_code: str = Field(min_length=1, max_length=200)
    subject_name: str = Field(min_length=1, max_length=500)
    expected_extraction_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_workbook_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_evidence: tuple[BoqResearchEvidenceReference, ...] = Field(
        min_length=1,
        max_length=50,
    )
    line_rules: tuple[BoqFreeSourceLineRule, ...] = Field(min_length=1, max_length=10_000)

    @field_validator("project_code", "subject_name")
    @classmethod
    def profile_literals_are_exact(cls, value: str) -> str:
        if value != value.strip() or any(character in value for character in "\r\n\x00"):
            raise ValueError("Research profile literals must be exact single-line values")
        return value

    @model_validator(mode="after")
    def profile_is_complete_and_unique(self) -> BoqFreeSourceResearchProfile:
        if self.schema_version != BOQ_FREE_SOURCE_PROFILE_SCHEMA:
            raise ValueError("Unsupported free-source research profile schema")
        candidate_ids = tuple(rule.candidate_id for rule in self.line_rules)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("Research profile contains duplicate candidate identities")
        evidence_ids = tuple(item.object_sha256 for item in self.context_evidence)
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("Research profile contains duplicate evidence objects")
        return self


class BoqFreeSourceRawArtifact(DomainModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    media_type: Literal["application/json"] = "application/json"
    source_uris: tuple[str, ...] = Field(min_length=1, max_length=100)

    @field_validator("source_uris")
    @classmethod
    def source_uris_are_unique_fgis_urls(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("Raw research artifact contains duplicate source URIs")
        if any(not value.startswith("https://fgiscs.minstroyrf.ru/api/") for value in values):
            raise ValueError("Raw research artifact URI is outside the FGIS CS API origin")
        return values


class BoqFreeSourceResearchLine(DomainModel):
    candidate_id: str = Field(pattern=r"^boq-candidate-[0-9a-f]{24}$")
    row_number: int = Field(ge=1, le=1_048_576)
    source_position_id: str | None = Field(default=None, max_length=500)
    boq_description: str = Field(min_length=1, max_length=20_000)
    boq_unit: str = Field(min_length=1, max_length=100)
    quantity: Decimal | None = Field(default=None, gt=0, max_digits=38, decimal_places=12)
    cost_nature: CostNature
    fgis_ksr_query: str | None = Field(default=None, min_length=1, max_length=1000)
    market_query: str = Field(min_length=1, max_length=2000)
    eis_query: str = Field(min_length=1, max_length=2000)
    fgis_search_result: FgisCsKsrSearchResult | None = None
    raw_response_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    raw_response_size_bytes: int | None = Field(default=None, gt=0)
    literal_name_unit_candidate_ids: tuple[str, ...] = ()
    acquisition_error_code: str | None = Field(default=None, min_length=1, max_length=200)
    acquisition_error_retryable: bool = False
    status: Literal["UNVERIFIED", "BLOCKED"]
    pricing_blockers: tuple[str, ...] = Field(min_length=1)

    @field_validator("fgis_ksr_query", "market_query", "eis_query")
    @classmethod
    def external_queries_are_single_line(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value != value.strip() or any(character in value for character in "\r\n\x00"):
            raise ValueError("External research queries must be exact single-line values")
        return value

    @model_validator(mode="after")
    def line_is_fail_closed(self) -> BoqFreeSourceResearchLine:
        retained = (
            self.fgis_search_result,
            self.raw_response_sha256,
            self.raw_response_size_bytes,
        )
        if self.cost_nature != "MATERIAL":
            if self.fgis_ksr_query is not None or any(value is not None for value in retained):
                raise ValueError("Non-material research line contains FGIS material evidence")
            if self.acquisition_error_code is not None:
                raise ValueError("Non-material research line contains an acquisition error")
            if self.literal_name_unit_candidate_ids:
                raise ValueError("Non-material research line contains FGIS candidate identities")
        elif self.fgis_search_result is not None:
            if not all(value is not None for value in retained):
                raise ValueError(
                    "FGIS research line does not retain its complete response identity"
                )
            if self.acquisition_error_code is not None or self.acquisition_error_retryable:
                raise ValueError(
                    "Successful FGIS research line cannot contain an acquisition error"
                )
            if self.status != "UNVERIFIED":
                raise ValueError("Retrieved FGIS research remains unverified")
            if self.fgis_search_result.query != self.fgis_ksr_query:
                raise ValueError("FGIS research query differs from the retained result")
            if self.fgis_search_result.response_sha256 != self.raw_response_sha256:
                raise ValueError("FGIS research result differs from the retained response hash")
            candidate_ids = {item.source_record_id for item in self.fgis_search_result.candidates}
            if len(self.literal_name_unit_candidate_ids) != len(
                set(self.literal_name_unit_candidate_ids)
            ) or not set(self.literal_name_unit_candidate_ids).issubset(candidate_ids):
                raise ValueError("FGIS literal candidate identities are invalid")
            expected_retrieval_blocker = (
                "FGIS_KSR_CANDIDATES_NOT_FOUND"
                if not self.fgis_search_result.candidates
                else (
                    "FGIS_KSR_EXACT_LITERAL_CANDIDATE_NOT_FOUND"
                    if not self.literal_name_unit_candidate_ids
                    else (
                        "FGIS_KSR_EXACT_LITERAL_CANDIDATE_AMBIGUOUS"
                        if len(self.literal_name_unit_candidate_ids) > 1
                        else None
                    )
                )
            )
            retrieval_blockers = {
                blocker for blocker in self.pricing_blockers if blocker.startswith("FGIS_KSR_")
            }
            if retrieval_blockers != (
                {expected_retrieval_blocker} if expected_retrieval_blocker is not None else set()
            ):
                raise ValueError("FGIS retrieval blockers contradict the retained candidates")
        else:
            if any(value is not None for value in retained[1:]):
                raise ValueError("Failed FGIS research line retains incomplete response identity")
            if self.acquisition_error_code is None or self.status != "BLOCKED":
                raise ValueError("Missing FGIS research requires an explicit blocked error")
            if self.literal_name_unit_candidate_ids:
                raise ValueError("Failed FGIS research line contains candidate identities")
        return self


class BoqFreeSourceResearchResult(DomainModel):
    schema_version: str = BOQ_FREE_SOURCE_RESULT_SCHEMA
    profile_version_id: str = Field(min_length=1, max_length=64)
    profile_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    extraction_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    workbook_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    project_code: str = Field(min_length=1, max_length=200)
    subject_name: str = Field(min_length=1, max_length=500)
    context_evidence: tuple[BoqResearchEvidenceReference, ...] = Field(
        min_length=1,
        max_length=50,
    )
    completed_at: datetime
    status: Literal["UNVERIFIED", "BLOCKED"]
    lines: tuple[BoqFreeSourceResearchLine, ...] = Field(min_length=1)
    raw_artifacts: tuple[BoqFreeSourceRawArtifact, ...]
    global_blockers: tuple[str, ...] = Field(min_length=1)
    ready_for_pricing: bool = False

    @field_validator("profile_version_id", "project_code", "subject_name")
    @classmethod
    def result_literals_are_exact(cls, value: str) -> str:
        if value != value.strip() or any(character in value for character in "\r\n\x00"):
            raise ValueError("Research result literals must be exact single-line values")
        return value

    @field_validator("completed_at")
    @classmethod
    def completed_at_is_aware(cls, value: datetime) -> datetime:
        normalized = ensure_utc(value)
        if normalized is None or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Research completion timestamp must include a timezone")
        return normalized

    @model_validator(mode="after")
    def result_is_complete_and_fail_closed(self) -> BoqFreeSourceResearchResult:
        if self.schema_version != BOQ_FREE_SOURCE_RESULT_SCHEMA:
            raise ValueError("Unsupported free-source research result schema")
        if self.ready_for_pricing:
            raise ValueError("Free-source research cannot release a price")
        candidate_ids = tuple(line.candidate_id for line in self.lines)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("Free-source research result contains duplicate BoQ lines")
        expected_status = (
            "BLOCKED" if any(line.status == "BLOCKED" for line in self.lines) else "UNVERIFIED"
        )
        if self.status != expected_status:
            raise ValueError("Free-source research status contradicts its line results")
        if self.global_blockers != _global_blockers(self.lines):
            raise ValueError("Free-source research global blockers contradict its lines")
        artifacts = {artifact.sha256: artifact for artifact in self.raw_artifacts}
        if len(artifacts) != len(self.raw_artifacts):
            raise ValueError("Free-source research contains duplicate raw artifacts")
        referenced = {
            line.raw_response_sha256 for line in self.lines if line.raw_response_sha256 is not None
        }
        if referenced != set(artifacts):
            raise ValueError("Free-source research raw artifact set is incomplete")
        for line in self.lines:
            if line.raw_response_sha256 is None:
                continue
            artifact = artifacts[line.raw_response_sha256]
            if artifact.size_bytes != line.raw_response_size_bytes:
                raise ValueError("Free-source research raw artifact size differs from its line")
            if (
                line.fgis_search_result is None
                or line.fgis_search_result.api_request_uri not in artifact.source_uris
            ):
                raise ValueError("Free-source research artifact URI differs from its line")
        return self


@dataclass(frozen=True)
class PreparedBoqFreeSourceResearch:
    result: BoqFreeSourceResearchResult
    raw_responses: tuple[tuple[str, bytes], ...]

    def __post_init__(self) -> None:
        identities = tuple(item[0] for item in self.raw_responses)
        if len(identities) != len(set(identities)):
            raise ValueError("Prepared free-source research contains duplicate raw objects")
        if set(identities) != {artifact.sha256 for artifact in self.result.raw_artifacts}:
            raise ValueError("Prepared free-source research raw objects are incomplete")
        for object_hash, content in self.raw_responses:
            if not content or hashlib.sha256(content).hexdigest() != object_hash:
                raise ValueError("Prepared free-source research raw object hash differs")


def run_boq_free_source_research(
    *,
    extraction: BoqXlsxExtractionResult,
    profile: BoqFreeSourceResearchProfile,
    acquire_ksr_search: AcquireKsrSearch,
    completed_at: datetime | None = None,
) -> PreparedBoqFreeSourceResearch:
    extraction_hash = content_hash(extraction)
    if extraction_hash != profile.expected_extraction_content_hash:
        raise ValueError("Research profile is not bound to the supplied extraction")
    if extraction.workbook_object_sha256 != profile.expected_workbook_sha256:
        raise ValueError("Research profile is not bound to the supplied workbook")
    candidates = {
        candidate.provisional_candidate_id: candidate for candidate in extraction.candidates
    }
    if len(candidates) != len(extraction.candidates):
        raise ValueError("BoQ extraction contains duplicate candidate identities")
    rules = {rule.candidate_id: rule for rule in profile.line_rules}
    if set(candidates) != set(rules):
        raise ValueError(
            "Research profile must classify every extracted BoQ candidate exactly once"
        )

    raw_content: dict[str, bytes] = {}
    raw_uris: dict[str, set[str]] = {}
    lines: list[BoqFreeSourceResearchLine] = []
    stop_after_retryable_failure = False
    for candidate in extraction.candidates:
        rule = rules[candidate.provisional_candidate_id]
        _require_candidate_is_researchable(candidate)
        assert candidate.description is not None
        assert candidate.unit is not None
        description = candidate.description
        unit = candidate.unit
        search_query = _single_line_search_query(description)
        if rule.cost_nature != "MATERIAL":
            lines.append(
                _non_material_line(
                    candidate,
                    rule,
                    description=description,
                    unit=unit,
                    search_query=search_query,
                )
            )
            continue
        if stop_after_retryable_failure:
            lines.append(
                _failed_material_line(
                    candidate,
                    rule,
                    description=description,
                    unit=unit,
                    search_query=search_query,
                    code="FGIS_QUERY_NOT_ATTEMPTED_AFTER_RETRYABLE_FAILURE",
                    retryable=True,
                )
            )
            continue
        assert rule.fgis_ksr_query is not None
        try:
            acquisition = acquire_ksr_search(rule.fgis_ksr_query)
            replay_fgiscs_ksr_search_acquisition(acquisition)
        except FgisCsPublicApiError as error:
            lines.append(
                _failed_material_line(
                    candidate,
                    rule,
                    description=description,
                    unit=unit,
                    search_query=search_query,
                    code=error.code,
                    retryable=error.retryable,
                )
            )
            stop_after_retryable_failure = error.retryable
            continue
        response_hash = acquisition.exchange.response_sha256
        existing = raw_content.get(response_hash)
        if existing is not None and existing != acquisition.exchange.response_body:
            raise RuntimeError("FGIS responses collided on SHA-256 identity")
        raw_content[response_hash] = acquisition.exchange.response_body
        raw_uris.setdefault(response_hash, set()).add(acquisition.exchange.request_uri)
        exact_ids = tuple(
            item.source_record_id
            for item in acquisition.result.candidates
            if item.source_item_name == description and item.unit == unit
        )
        blockers = list(_pricing_blockers(rule.cost_nature))
        if not acquisition.result.candidates:
            blockers.append("FGIS_KSR_CANDIDATES_NOT_FOUND")
        elif not exact_ids:
            blockers.append("FGIS_KSR_EXACT_LITERAL_CANDIDATE_NOT_FOUND")
        elif len(exact_ids) > 1:
            blockers.append("FGIS_KSR_EXACT_LITERAL_CANDIDATE_AMBIGUOUS")
        lines.append(
            BoqFreeSourceResearchLine(
                candidate_id=candidate.provisional_candidate_id,
                row_number=candidate.row_number,
                source_position_id=candidate.source_position_id,
                boq_description=description,
                boq_unit=unit,
                quantity=candidate.quantity,
                cost_nature=rule.cost_nature,
                fgis_ksr_query=rule.fgis_ksr_query,
                market_query=search_query,
                eis_query=search_query,
                fgis_search_result=acquisition.result,
                raw_response_sha256=response_hash,
                raw_response_size_bytes=len(acquisition.exchange.response_body),
                literal_name_unit_candidate_ids=exact_ids,
                status="UNVERIFIED",
                pricing_blockers=tuple(dict.fromkeys(blockers)),
            )
        )

    artifacts = tuple(
        BoqFreeSourceRawArtifact(
            sha256=object_hash,
            size_bytes=len(raw_content[object_hash]),
            source_uris=tuple(sorted(raw_uris[object_hash])),
        )
        for object_hash in sorted(raw_content)
    )
    line_tuple = tuple(lines)
    result = BoqFreeSourceResearchResult(
        profile_version_id=profile.profile_version_id,
        profile_content_hash=content_hash(profile),
        extraction_content_hash=extraction_hash,
        workbook_sha256=extraction.workbook_object_sha256,
        project_code=profile.project_code,
        subject_name=profile.subject_name,
        context_evidence=profile.context_evidence,
        completed_at=completed_at or utc_now(),
        status="BLOCKED" if any(line.status == "BLOCKED" for line in line_tuple) else "UNVERIFIED",
        lines=line_tuple,
        raw_artifacts=artifacts,
        global_blockers=_global_blockers(line_tuple),
    )
    return PreparedBoqFreeSourceResearch(
        result=result,
        raw_responses=tuple(
            (object_hash, raw_content[object_hash]) for object_hash in sorted(raw_content)
        ),
    )


def verify_boq_free_source_research_package(
    prepared: PreparedBoqFreeSourceResearch,
) -> BoqFreeSourceResearchResult:
    raw_by_hash = dict(prepared.raw_responses)
    for line in prepared.result.lines:
        if line.fgis_search_result is None:
            continue
        assert line.fgis_ksr_query is not None
        assert line.raw_response_sha256 is not None
        replay_fgiscs_ksr_search_acquisition(
            FgisCsKsrSearchAcquisition(
                query=line.fgis_ksr_query,
                result=line.fgis_search_result,
                exchange=FgisCsRawHttpExchange(
                    request_uri=line.fgis_search_result.api_request_uri,
                    response_body=raw_by_hash[line.raw_response_sha256],
                ),
            )
        )
    return prepared.result


def _require_candidate_is_researchable(candidate: BoqRowCandidate) -> None:
    if not candidate.description or not candidate.unit:
        raise ValueError("Free-source research requires an extracted description and unit")


def _single_line_search_query(description: str) -> str:
    query = " ".join(description.split())
    if not query or len(query) > 2000:
        raise ValueError("BoQ description cannot be represented as a bounded search query")
    return query


def _non_material_line(
    candidate: BoqRowCandidate,
    rule: BoqFreeSourceLineRule,
    *,
    description: str,
    unit: str,
    search_query: str,
) -> BoqFreeSourceResearchLine:
    return BoqFreeSourceResearchLine(
        candidate_id=candidate.provisional_candidate_id,
        row_number=candidate.row_number,
        source_position_id=candidate.source_position_id,
        boq_description=description,
        boq_unit=unit,
        quantity=candidate.quantity,
        cost_nature=rule.cost_nature,
        market_query=search_query,
        eis_query=search_query,
        status="UNVERIFIED",
        pricing_blockers=_pricing_blockers(rule.cost_nature),
    )


def _failed_material_line(
    candidate: BoqRowCandidate,
    rule: BoqFreeSourceLineRule,
    *,
    description: str,
    unit: str,
    search_query: str,
    code: str,
    retryable: bool,
) -> BoqFreeSourceResearchLine:
    return BoqFreeSourceResearchLine(
        candidate_id=candidate.provisional_candidate_id,
        row_number=candidate.row_number,
        source_position_id=candidate.source_position_id,
        boq_description=description,
        boq_unit=unit,
        quantity=candidate.quantity,
        cost_nature=rule.cost_nature,
        fgis_ksr_query=rule.fgis_ksr_query,
        market_query=search_query,
        eis_query=search_query,
        acquisition_error_code=code,
        acquisition_error_retryable=retryable,
        status="BLOCKED",
        pricing_blockers=tuple(dict.fromkeys((*_pricing_blockers(rule.cost_nature), code))),
    )


def _pricing_blockers(cost_nature: CostNature) -> tuple[str, ...]:
    common = (
        "WON_TENDER_LINE_PRICE_REQUIRED",
        "MARKET_SOURCE_ACQUISITION_REQUIRED",
        "PRICE_NORMALIZATION_REQUIRED",
        "INDEPENDENT_VALIDATION_REQUIRED",
        "BID_RELEASE_NOT_APPROVED",
    )
    if cost_nature == "MATERIAL":
        return (
            "APPROVED_FGIS_MAPPING_REQUIRED",
            "FGIS_PRICE_PERIOD_REQUIRED",
            "FGIS_PRICE_ACQUISITION_REQUIRED",
            *common,
        )
    if cost_nature == "WORK":
        return ("APPROVED_NORMATIVE_ENGINE_REQUIRED", *common)
    return ("APPROVED_LOGISTICS_METHOD_REQUIRED", *common)


def _global_blockers(
    lines: tuple[BoqFreeSourceResearchLine, ...],
) -> tuple[str, ...]:
    blockers = [
        "DIAGNOSTIC_RESEARCH_NOT_GOVERNED",
        "MARKET_CONNECTOR_NOT_IMPLEMENTED",
        "EIS_CONNECTOR_NOT_IMPLEMENTED",
        "PRICE_NORMALIZATION_REQUIRED",
        "INDEPENDENT_VALIDATION_REQUIRED",
        "BID_RELEASE_NOT_APPROVED",
    ]
    if any(line.cost_nature == "MATERIAL" for line in lines):
        blockers.extend(
            (
                "APPROVED_FGIS_MAPPING_REQUIRED",
                "FGIS_PRICE_PERIOD_REQUIRED",
                "FGIS_PRICE_ACQUISITION_REQUIRED",
            )
        )
    if any(line.cost_nature == "WORK" for line in lines):
        blockers.append("APPROVED_NORMATIVE_ENGINE_REQUIRED")
    if any(line.cost_nature == "LOGISTICS" for line in lines):
        blockers.append("APPROVED_LOGISTICS_METHOD_REQUIRED")
    return tuple(dict.fromkeys(blockers))
