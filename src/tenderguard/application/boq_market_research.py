from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from tenderguard.application.free_source_research import (
    PreparedBoqFreeSourceResearch,
    verify_boq_free_source_research_package,
)
from tenderguard.domain.common import content_hash, ensure_utc, utc_now
from tenderguard.domain.models import DomainModel
from tenderguard.integrations.public_market import (
    PublicMarketPageAcquisition,
    PublicMarketPageError,
    PublicMarketPageRequest,
    PublicMarketPageResult,
    PublicMarketRawHttpExchange,
    replay_public_market_page_acquisition,
    replay_public_market_page_failure,
)

BOQ_MARKET_PROFILE_SCHEMA = "boq-market-research-profile/v1"
BOQ_MARKET_PACKAGE_SCHEMA = "boq-market-research-package/v1"

MarketSelectionDecision = Literal["DIAGNOSTIC_URLS_SELECTED", "NO_PUBLIC_SOURCE_SELECTED"]
AcquirePublicMarketPage = Callable[[PublicMarketPageRequest], PublicMarketPageAcquisition]


class BoqMarketLineSelection(DomainModel):
    candidate_id: str = Field(pattern=r"^boq-candidate-[0-9a-f]{24}$")
    decision: MarketSelectionDecision
    sources: tuple[PublicMarketPageRequest, ...] = Field(default=(), max_length=10)

    @model_validator(mode="after")
    def decision_matches_sources(self) -> BoqMarketLineSelection:
        uris = tuple(item.source_uri for item in self.sources)
        if len(uris) != len(set(uris)):
            raise ValueError("Market line selection contains duplicate source URIs")
        if self.decision == "DIAGNOSTIC_URLS_SELECTED" and not self.sources:
            raise ValueError("Selected market research decision requires source URLs")
        if self.decision == "NO_PUBLIC_SOURCE_SELECTED" and self.sources:
            raise ValueError("No-source market decision cannot contain source URLs")
        return self


class BoqMarketResearchProfile(DomainModel):
    schema_version: str = BOQ_MARKET_PROFILE_SCHEMA
    profile_version_id: str = Field(min_length=1, max_length=64)
    expected_research_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    line_selections: tuple[BoqMarketLineSelection, ...] = Field(
        min_length=1,
        max_length=10_000,
    )

    @field_validator("profile_version_id")
    @classmethod
    def profile_version_is_exact(cls, value: str) -> str:
        if value != value.strip() or any(character in value for character in "\r\n\x00"):
            raise ValueError("Market profile version must be an exact single-line literal")
        return value

    @model_validator(mode="after")
    def profile_is_complete_and_unique(self) -> BoqMarketResearchProfile:
        if self.schema_version != BOQ_MARKET_PROFILE_SCHEMA:
            raise ValueError("Unsupported BoQ market research profile schema")
        candidate_ids = tuple(item.candidate_id for item in self.line_selections)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("Market research profile contains duplicate BoQ candidates")
        source_uris = tuple(
            source.source_uri for selection in self.line_selections for source in selection.sources
        )
        if len(source_uris) != len(set(source_uris)):
            raise ValueError("Market research profile reuses one URL across BoQ lines")
        return self


class BoqMarketSourceObservation(DomainModel):
    request: PublicMarketPageRequest
    page_result: PublicMarketPageResult | None
    raw_sequence: int | None = Field(default=None, ge=1, le=100_000)
    response_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    response_size_bytes: int | None = Field(default=None, gt=0)
    response_status_code: int | None = Field(default=None, ge=100, le=599)
    response_media_type: str | None = Field(default=None, min_length=1, max_length=200)
    response_charset: str | None = Field(default=None, min_length=1, max_length=100)
    acquisition_error_code: str | None = Field(default=None, min_length=1, max_length=200)
    acquisition_error_retryable: bool = False
    status: Literal["UNVERIFIED", "BLOCKED"]

    @model_validator(mode="after")
    def observation_is_complete(self) -> BoqMarketSourceObservation:
        response_identity = (
            self.raw_sequence,
            self.response_sha256,
            self.response_size_bytes,
            self.response_status_code,
            self.response_media_type,
        )
        if self.page_result is not None:
            if self.acquisition_error_code is not None or self.acquisition_error_retryable:
                raise ValueError("Successful market observation contains an acquisition error")
            if any(value is None for value in response_identity):
                raise ValueError("Successful market observation lacks retained response identity")
            if (
                self.page_result.source_uri != self.request.source_uri
                or self.page_result.source_type is not self.request.source_type
                or self.page_result.display_name != self.request.display_name
                or self.page_result.response_sha256 != self.response_sha256
                or self.page_result.response_size_bytes != self.response_size_bytes
                or self.page_result.response_media_type != self.response_media_type
                or self.page_result.status != self.status
            ):
                raise ValueError("Market observation result differs from its request or response")
        else:
            if self.acquisition_error_code is None or self.status != "BLOCKED":
                raise ValueError("Failed market observation requires an explicit blocked error")
            if self.raw_sequence is None:
                if any(value is not None for value in response_identity[1:]):
                    raise ValueError("Market transport failure contains incomplete raw identity")
            elif any(value is None for value in response_identity[1:]):
                raise ValueError("Market HTTP failure contains incomplete raw identity")
        return self


class BoqMarketLineResult(DomainModel):
    candidate_id: str = Field(pattern=r"^boq-candidate-[0-9a-f]{24}$")
    row_number: int = Field(ge=1, le=1_048_576)
    boq_description: str = Field(min_length=1, max_length=20_000)
    boq_unit: str = Field(min_length=1, max_length=100)
    market_query: str = Field(min_length=1, max_length=2000)
    decision: MarketSelectionDecision
    sources: tuple[BoqMarketSourceObservation, ...] = Field(max_length=10)
    offer_candidate_count: int = Field(ge=0, le=2000)
    source_error_count: int = Field(ge=0, le=10)
    structured_finding_count: int = Field(ge=0, le=2000)
    status: Literal["BLOCKED"] = "BLOCKED"
    pricing_blockers: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def line_is_recalculated_and_blocked(self) -> BoqMarketLineResult:
        expected_offers = sum(
            len(item.page_result.candidates)
            for item in self.sources
            if item.page_result is not None
        )
        expected_errors = sum(item.acquisition_error_code is not None for item in self.sources)
        expected_findings = sum(
            len(item.page_result.extraction_findings)
            for item in self.sources
            if item.page_result is not None
        )
        if (
            self.offer_candidate_count != expected_offers
            or self.source_error_count != expected_errors
            or self.structured_finding_count != expected_findings
        ):
            raise ValueError("Market line counters differ from retained source observations")
        if self.decision == "NO_PUBLIC_SOURCE_SELECTED" and self.sources:
            raise ValueError("No-source market line contains source observations")
        if self.decision == "DIAGNOSTIC_URLS_SELECTED" and not self.sources:
            raise ValueError("Selected market line contains no source observations")
        if self.pricing_blockers != _line_blockers(
            decision=self.decision,
            offer_count=expected_offers,
            error_count=expected_errors,
            finding_count=expected_findings,
        ):
            raise ValueError("Market line blockers contradict retained source observations")
        return self


class BoqMarketRawResponse(DomainModel):
    sequence: int = Field(ge=1, le=100_000)
    file_name: str = Field(pattern=r"^raw/[0-9]{5}-[0-9a-f]{64}\.bin$")
    request_uri: str = Field(min_length=1, max_length=2000)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    status_code: int = Field(ge=100, le=599)
    media_type: str = Field(min_length=1, max_length=200)
    charset: str | None = Field(default=None, min_length=1, max_length=100)


class BoqMarketResearchPackage(DomainModel):
    schema_version: str = BOQ_MARKET_PACKAGE_SCHEMA
    profile_version_id: str = Field(min_length=1, max_length=64)
    profile_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile: BoqMarketResearchProfile
    research_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    research_result_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    project_code: str = Field(min_length=1, max_length=200)
    workbook_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    completed_at: datetime
    line_results: tuple[BoqMarketLineResult, ...] = Field(min_length=1)
    raw_responses: tuple[BoqMarketRawResponse, ...]
    status: Literal["BLOCKED"] = "BLOCKED"
    ready_for_pricing: bool = False
    global_blockers: tuple[str, ...] = Field(min_length=1)

    @field_validator("completed_at")
    @classmethod
    def completed_at_is_aware(cls, value: datetime) -> datetime:
        normalized = ensure_utc(value)
        if normalized is None or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Market package timestamp must include a timezone")
        return normalized

    @model_validator(mode="after")
    def package_is_complete_and_fail_closed(self) -> BoqMarketResearchPackage:
        if self.schema_version != BOQ_MARKET_PACKAGE_SCHEMA:
            raise ValueError("Unsupported BoQ market research package schema")
        if self.ready_for_pricing:
            raise ValueError("Diagnostic market research cannot release a price")
        if (
            self.profile_version_id != self.profile.profile_version_id
            or self.profile_content_hash != content_hash(self.profile)
        ):
            raise ValueError("Market package profile binding does not reproduce")
        if self.research_manifest_sha256 != self.profile.expected_research_manifest_sha256:
            raise ValueError("Market package research binding differs from its profile")
        line_ids = tuple(item.candidate_id for item in self.line_results)
        if len(line_ids) != len(set(line_ids)):
            raise ValueError("Market package contains duplicate BoQ lines")
        selections = {item.candidate_id: item for item in self.profile.line_selections}
        if set(line_ids) != set(selections):
            raise ValueError("Market package lines differ from its profile")
        source_observations: list[BoqMarketSourceObservation] = []
        for line in self.line_results:
            selection = selections[line.candidate_id]
            if line.decision != selection.decision or tuple(
                item.request for item in line.sources
            ) != selection.sources:
                raise ValueError("Market package line sources differ from its profile")
            source_observations.extend(line.sources)
        expected_sequences = tuple(range(1, len(self.raw_responses) + 1))
        if tuple(item.sequence for item in self.raw_responses) != expected_sequences:
            raise ValueError("Market package raw response sequence is incomplete")
        referenced_sequences = tuple(
            item.raw_sequence for item in source_observations if item.raw_sequence is not None
        )
        if referenced_sequences != expected_sequences:
            raise ValueError("Market observations do not reference every raw response in order")
        file_names = tuple(item.file_name for item in self.raw_responses)
        request_uris = tuple(item.request_uri for item in self.raw_responses)
        if len(file_names) != len(set(file_names)) or len(request_uris) != len(set(request_uris)):
            raise ValueError("Market raw response identities must be unique")
        raw_by_sequence = {item.sequence: item for item in self.raw_responses}
        for observation in source_observations:
            if observation.raw_sequence is None:
                continue
            raw = raw_by_sequence[observation.raw_sequence]
            if (
                raw.request_uri != observation.request.source_uri
                or raw.sha256 != observation.response_sha256
                or raw.size_bytes != observation.response_size_bytes
                or raw.status_code != observation.response_status_code
                or raw.media_type != observation.response_media_type
                or raw.charset != observation.response_charset
            ):
                raise ValueError("Market raw response differs from its source observation")
        if self.global_blockers != _global_blockers(self.line_results):
            raise ValueError("Market package global blockers contradict its lines")
        return self


@dataclass(frozen=True)
class PreparedBoqMarketResearchPackage:
    manifest: BoqMarketResearchPackage
    raw_files: tuple[tuple[str, bytes], ...]

    def __post_init__(self) -> None:
        expected_names = tuple(item.file_name for item in self.manifest.raw_responses)
        actual_names = tuple(item[0] for item in self.raw_files)
        if actual_names != expected_names:
            raise ValueError("Prepared market research files are incomplete or reordered")
        for reference, (_, content) in zip(
            self.manifest.raw_responses,
            self.raw_files,
            strict=True,
        ):
            if (
                not content
                or len(content) != reference.size_bytes
                or hashlib.sha256(content).hexdigest() != reference.sha256
            ):
                raise ValueError("Prepared market research raw response differs")


def run_boq_market_research(
    *,
    research: PreparedBoqFreeSourceResearch,
    research_manifest_sha256: str,
    profile: BoqMarketResearchProfile,
    acquire_page: AcquirePublicMarketPage,
    completed_at: datetime | None = None,
) -> PreparedBoqMarketResearchPackage:
    verify_boq_free_source_research_package(research)
    if research_manifest_sha256 != profile.expected_research_manifest_sha256:
        raise ValueError("Market research profile is not bound to the research manifest")
    material_lines = tuple(line for line in research.result.lines if line.cost_nature == "MATERIAL")
    line_by_id = {line.candidate_id: line for line in material_lines}
    selections = {item.candidate_id: item for item in profile.line_selections}
    if set(line_by_id) != set(selections):
        raise ValueError("Market profile must classify every material BoQ line")

    raw_references: list[BoqMarketRawResponse] = []
    raw_files: list[tuple[str, bytes]] = []
    line_results: list[BoqMarketLineResult] = []
    next_sequence = 1
    for line in material_lines:
        selection = selections[line.candidate_id]
        source_results: list[BoqMarketSourceObservation] = []
        for request in selection.sources:
            try:
                acquisition = acquire_page(request)
                replay_public_market_page_acquisition(acquisition)
            except PublicMarketPageError as error:
                raw_sequence = None
                response_fields: dict[str, object] = {}
                if error.exchange is not None:
                    raw_sequence = next_sequence
                    next_sequence += 1
                    raw_reference, raw_file = _retain_exchange(
                        sequence=raw_sequence,
                        exchange=error.exchange,
                    )
                    raw_references.append(raw_reference)
                    raw_files.append(raw_file)
                    response_fields = _response_fields(error.exchange)
                source_results.append(
                    BoqMarketSourceObservation(
                        request=request,
                        page_result=None,
                        raw_sequence=raw_sequence,
                        acquisition_error_code=error.code,
                        acquisition_error_retryable=error.retryable,
                        status="BLOCKED",
                        **response_fields,
                    )
                )
                continue
            raw_sequence = next_sequence
            next_sequence += 1
            raw_reference, raw_file = _retain_exchange(
                sequence=raw_sequence,
                exchange=acquisition.exchange,
            )
            raw_references.append(raw_reference)
            raw_files.append(raw_file)
            source_results.append(
                BoqMarketSourceObservation(
                    request=request,
                    page_result=acquisition.result,
                    raw_sequence=raw_sequence,
                    status=acquisition.result.status,
                    **_response_fields(acquisition.exchange),
                )
            )
        offer_count = sum(
            len(item.page_result.candidates)
            for item in source_results
            if item.page_result is not None
        )
        error_count = sum(item.acquisition_error_code is not None for item in source_results)
        finding_count = sum(
            len(item.page_result.extraction_findings)
            for item in source_results
            if item.page_result is not None
        )
        assert line.market_query is not None
        line_results.append(
            BoqMarketLineResult(
                candidate_id=line.candidate_id,
                row_number=line.row_number,
                boq_description=line.boq_description,
                boq_unit=line.boq_unit,
                market_query=line.market_query,
                decision=selection.decision,
                sources=tuple(source_results),
                offer_candidate_count=offer_count,
                source_error_count=error_count,
                structured_finding_count=finding_count,
                pricing_blockers=_line_blockers(
                    decision=selection.decision,
                    offer_count=offer_count,
                    error_count=error_count,
                    finding_count=finding_count,
                ),
            )
        )
    line_tuple = tuple(line_results)
    manifest = BoqMarketResearchPackage(
        profile_version_id=profile.profile_version_id,
        profile_content_hash=content_hash(profile),
        profile=profile,
        research_manifest_sha256=research_manifest_sha256,
        research_result_content_hash=content_hash(research.result),
        project_code=research.result.project_code,
        workbook_sha256=research.result.workbook_sha256,
        completed_at=completed_at or utc_now(),
        line_results=line_tuple,
        raw_responses=tuple(raw_references),
        global_blockers=_global_blockers(line_tuple),
    )
    return PreparedBoqMarketResearchPackage(
        manifest=manifest,
        raw_files=tuple(raw_files),
    )


def verify_boq_market_research_package(
    prepared: PreparedBoqMarketResearchPackage,
) -> BoqMarketResearchPackage:
    content_by_sequence = {
        reference.sequence: content
        for reference, (_, content) in zip(
            prepared.manifest.raw_responses,
            prepared.raw_files,
            strict=True,
        )
    }
    raw_by_sequence = {item.sequence: item for item in prepared.manifest.raw_responses}
    for line in prepared.manifest.line_results:
        for observation in line.sources:
            if observation.raw_sequence is None:
                continue
            reference = raw_by_sequence[observation.raw_sequence]
            exchange = PublicMarketRawHttpExchange(
                request_uri=reference.request_uri,
                response_body=content_by_sequence[reference.sequence],
                status_code=reference.status_code,
                media_type=reference.media_type,
                charset=reference.charset,
            )
            if observation.page_result is not None:
                replay_public_market_page_acquisition(
                    PublicMarketPageAcquisition(
                        request=observation.request,
                        result=observation.page_result,
                        exchange=exchange,
                    )
                )
            else:
                assert observation.acquisition_error_code is not None
                replay_public_market_page_failure(
                    request=observation.request,
                    exchange=exchange,
                    expected_error_code=observation.acquisition_error_code,
                )
    return prepared.manifest


def _retain_exchange(
    *,
    sequence: int,
    exchange: PublicMarketRawHttpExchange,
) -> tuple[BoqMarketRawResponse, tuple[str, bytes]]:
    object_hash = exchange.response_sha256
    file_name = f"raw/{sequence:05d}-{object_hash}.bin"
    return (
        BoqMarketRawResponse(
            sequence=sequence,
            file_name=file_name,
            request_uri=exchange.request_uri,
            sha256=object_hash,
            size_bytes=len(exchange.response_body),
            status_code=exchange.status_code,
            media_type=exchange.media_type,
            charset=exchange.charset,
        ),
        (file_name, exchange.response_body),
    )


def _response_fields(exchange: PublicMarketRawHttpExchange) -> dict[str, object]:
    return {
        "response_sha256": exchange.response_sha256,
        "response_size_bytes": len(exchange.response_body),
        "response_status_code": exchange.status_code,
        "response_media_type": exchange.media_type,
        "response_charset": exchange.charset,
    }


def _line_blockers(
    *,
    decision: MarketSelectionDecision,
    offer_count: int,
    error_count: int,
    finding_count: int,
) -> tuple[str, ...]:
    blockers: list[str] = []
    if decision == "NO_PUBLIC_SOURCE_SELECTED":
        blockers.append("MARKET_PUBLIC_SOURCE_NOT_SELECTED")
    elif offer_count == 0:
        blockers.append("STRUCTURED_MARKET_OFFER_NOT_FOUND")
    if error_count:
        blockers.append("MARKET_SOURCE_ERRORS_PRESENT")
    if finding_count:
        blockers.append("MARKET_STRUCTURED_DATA_FINDINGS_PRESENT")
    blockers.extend(
        (
            "DIAGNOSTIC_MARKET_RESEARCH_NOT_GOVERNED",
            "TECHNICAL_EQUIVALENCE_NOT_ESTABLISHED",
            "MARKET_UNIT_MAPPING_REQUIRED",
            "VAT_BASIS_UNKNOWN",
            "DELIVERY_BASIS_UNKNOWN",
            "UNLOADING_BASIS_UNKNOWN",
            "PAYMENT_TERMS_UNKNOWN",
            "PRICE_VALIDITY_NOT_ESTABLISHED",
            "PRICE_NORMALIZATION_REQUIRED",
            "INDEPENDENT_VALIDATION_REQUIRED",
            "BID_RELEASE_NOT_APPROVED",
        )
    )
    return tuple(blockers)


def _global_blockers(lines: tuple[BoqMarketLineResult, ...]) -> tuple[str, ...]:
    blockers: list[str] = []
    if any(line.offer_candidate_count == 0 for line in lines):
        blockers.append("MARKET_OFFER_COVERAGE_INCOMPLETE")
    if any(line.source_error_count for line in lines):
        blockers.append("MARKET_SOURCE_ERRORS_PRESENT")
    if any(line.structured_finding_count for line in lines):
        blockers.append("MARKET_STRUCTURED_DATA_FINDINGS_PRESENT")
    blockers.extend(
        (
            "DIAGNOSTIC_MARKET_RESEARCH_NOT_GOVERNED",
            "TECHNICAL_EQUIVALENCE_NOT_ESTABLISHED",
            "MARKET_UNIT_MAPPING_REQUIRED",
            "COMMERCIAL_BASIS_INCOMPLETE",
            "PRICE_NORMALIZATION_REQUIRED",
            "INDEPENDENT_VALIDATION_REQUIRED",
            "BID_RELEASE_NOT_APPROVED",
        )
    )
    return tuple(blockers)
