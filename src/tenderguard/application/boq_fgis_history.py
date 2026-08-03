from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import Field, field_validator, model_validator

from tenderguard.application.free_source_research import (
    PreparedBoqFreeSourceResearch,
    verify_boq_free_source_research_package,
)
from tenderguard.domain.common import content_hash
from tenderguard.domain.models import DomainModel
from tenderguard.integrations.fgiscs_public import (
    FgisCsMaterialHistoryAcquisition,
    FgisCsMaterialHistoryRequest,
    FgisCsMaterialHistoryResult,
    FgisCsRawHttpExchange,
    replay_fgiscs_material_history_acquisition,
)

BOQ_FGIS_HISTORY_PROFILE_SCHEMA = "boq-fgis-history-profile/v1"
BOQ_FGIS_HISTORY_PACKAGE_SCHEMA = "boq-fgis-history-package/v1"

SelectionDecision = Literal[
    "DIAGNOSTIC_CANDIDATES_SELECTED",
    "NO_SUITABLE_CANDIDATE_RETRIEVED",
]
AcquireMaterialHistory = Callable[
    [FgisCsMaterialHistoryRequest],
    FgisCsMaterialHistoryAcquisition,
]


class BoqFgisHistoryLineSelection(DomainModel):
    candidate_id: str = Field(pattern=r"^boq-candidate-[0-9a-f]{24}$")
    decision: SelectionDecision
    resource_codes: tuple[str, ...] = Field(default=(), max_length=10)

    @field_validator("resource_codes")
    @classmethod
    def codes_are_exact_and_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("FGIS history selection contains duplicate resource codes")
        if any(
            not value
            or len(value) > 200
            or value != value.strip()
            or any(character in value for character in "\r\n\x00")
            for value in values
        ):
            raise ValueError("FGIS history resource codes must be exact single-line literals")
        return values

    @model_validator(mode="after")
    def decision_matches_codes(self) -> BoqFgisHistoryLineSelection:
        if self.decision == "DIAGNOSTIC_CANDIDATES_SELECTED" and not self.resource_codes:
            raise ValueError("Selected FGIS history candidates require resource codes")
        if self.decision == "NO_SUITABLE_CANDIDATE_RETRIEVED" and self.resource_codes:
            raise ValueError("No-candidate FGIS history decision cannot contain resource codes")
        return self


class BoqFgisHistoryProfile(DomainModel):
    schema_version: str = BOQ_FGIS_HISTORY_PROFILE_SCHEMA
    profile_version_id: str = Field(min_length=1, max_length=64)
    expected_research_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    subject_name: str = Field(min_length=1, max_length=500)
    price_zone_name: str | None = Field(default=None, min_length=1, max_length=500)
    line_selections: tuple[BoqFgisHistoryLineSelection, ...] = Field(
        min_length=1,
        max_length=10_000,
    )

    @field_validator("profile_version_id", "subject_name", "price_zone_name")
    @classmethod
    def literals_are_exact(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value != value.strip() or any(character in value for character in "\r\n\x00"):
            raise ValueError("FGIS history profile literals must be exact single-line values")
        return value

    @model_validator(mode="after")
    def profile_is_unique(self) -> BoqFgisHistoryProfile:
        if self.schema_version != BOQ_FGIS_HISTORY_PROFILE_SCHEMA:
            raise ValueError("Unsupported FGIS history profile schema")
        identities = tuple(item.candidate_id for item in self.line_selections)
        if len(identities) != len(set(identities)):
            raise ValueError("FGIS history profile contains duplicate BoQ candidates")
        return self


class BoqFgisHistoryLineResult(DomainModel):
    candidate_id: str = Field(pattern=r"^boq-candidate-[0-9a-f]{24}$")
    row_number: int = Field(ge=1, le=1_048_576)
    boq_description: str = Field(min_length=1, max_length=20_000)
    boq_unit: str = Field(min_length=1, max_length=100)
    decision: SelectionDecision
    resource_codes: tuple[str, ...] = Field(default=(), max_length=10)
    published_observation_count: int = Field(ge=0, le=10_000)
    source_error_observation_count: int = Field(ge=0, le=10_000)
    status: Literal["BLOCKED"] = "BLOCKED"
    pricing_blockers: tuple[str, ...] = Field(min_length=1)

    @field_validator("resource_codes")
    @classmethod
    def result_codes_are_exact_and_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("FGIS history line result contains duplicate resource codes")
        if any(
            not value
            or len(value) > 200
            or value != value.strip()
            or any(character in value for character in "\r\n\x00")
            for value in values
        ):
            raise ValueError("FGIS history line result codes must be exact literals")
        return values

    @model_validator(mode="after")
    def line_remains_blocked(self) -> BoqFgisHistoryLineResult:
        required = {
            "APPROVED_FGIS_MAPPING_REQUIRED",
            "PROJECT_PRICE_PERIOD_NOT_SELECTED",
            "PRICE_NORMALIZATION_REQUIRED",
            "BID_RELEASE_NOT_APPROVED",
        }
        if not required.issubset(self.pricing_blockers):
            raise ValueError("FGIS history line is missing mandatory pricing blockers")
        if self.decision == "NO_SUITABLE_CANDIDATE_RETRIEVED":
            if self.resource_codes or self.published_observation_count:
                raise ValueError("No-candidate FGIS history line contains observations")
            if "FGIS_KSR_SUITABLE_CANDIDATE_NOT_SELECTED" not in self.pricing_blockers:
                raise ValueError("No-candidate FGIS history line requires an explicit blocker")
        elif not self.resource_codes:
            raise ValueError("FGIS history line selection contains no resource codes")
        elif self.published_observation_count == 0:
            if "FGIS_PRICE_NOT_PUBLISHED_FOR_RETRIEVED_PERIODS" not in self.pricing_blockers:
                raise ValueError("FGIS history line without prices requires an explicit blocker")
        elif "FGIS_PRICE_NOT_PUBLISHED_FOR_RETRIEVED_PERIODS" in self.pricing_blockers:
            raise ValueError("FGIS history line with prices contains a contradictory blocker")
        if self.source_error_observation_count:
            if "FGIS_HISTORY_SOURCE_ERRORS_PRESENT" not in self.pricing_blockers:
                raise ValueError("FGIS history line with source errors requires a blocker")
        elif "FGIS_HISTORY_SOURCE_ERRORS_PRESENT" in self.pricing_blockers:
            raise ValueError("FGIS history line contains a contradictory source-error blocker")
        return self


class BoqFgisHistoryRawResponse(DomainModel):
    sequence: int = Field(ge=1, le=10_003)
    file_name: str = Field(pattern=r"^raw/[0-9]{5}-[0-9a-f]{64}\.bin$")
    request_uri: str = Field(pattern=r"^https://fgiscs\.minstroyrf\.ru/api/")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    status_code: int = Field(ge=100, le=599)
    media_type: str = Field(min_length=1, max_length=200)

    @field_validator("media_type")
    @classmethod
    def media_type_is_exact(cls, value: str) -> str:
        if value != value.strip() or any(character in value for character in "\r\n\x00"):
            raise ValueError("FGIS history raw media type must be an exact single-line literal")
        return value


class BoqFgisHistoryPackage(DomainModel):
    schema_version: str = BOQ_FGIS_HISTORY_PACKAGE_SCHEMA
    profile_version_id: str = Field(min_length=1, max_length=64)
    profile_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile: BoqFgisHistoryProfile
    research_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    research_result_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    project_code: str = Field(min_length=1, max_length=200)
    workbook_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    line_results: tuple[BoqFgisHistoryLineResult, ...] = Field(min_length=1)
    history: FgisCsMaterialHistoryResult
    raw_responses: tuple[BoqFgisHistoryRawResponse, ...] = Field(min_length=4)
    status: Literal["BLOCKED"] = "BLOCKED"
    ready_for_pricing: bool = False
    global_blockers: tuple[str, ...] = (
        "DIAGNOSTIC_FGIS_HISTORY_NOT_GOVERNED",
        "APPROVED_FGIS_MAPPING_REQUIRED",
        "PROJECT_PRICE_PERIOD_NOT_SELECTED",
        "COMMERCIAL_BASIS_NOT_ESTABLISHED",
        "PRICE_NORMALIZATION_REQUIRED",
        "INDEPENDENT_VALIDATION_REQUIRED",
        "BID_RELEASE_NOT_APPROVED",
    )

    @model_validator(mode="after")
    def package_is_complete_and_fail_closed(self) -> BoqFgisHistoryPackage:
        if self.schema_version != BOQ_FGIS_HISTORY_PACKAGE_SCHEMA:
            raise ValueError("Unsupported FGIS history package schema")
        if self.ready_for_pricing or self.history.ready_for_pricing:
            raise ValueError("Diagnostic FGIS history cannot release a price")
        if (
            self.profile_version_id != self.profile.profile_version_id
            or self.profile_content_hash != content_hash(self.profile)
        ):
            raise ValueError("FGIS history package profile binding does not reproduce")
        if self.research_manifest_sha256 != self.profile.expected_research_manifest_sha256:
            raise ValueError("FGIS history package research binding differs from its profile")
        line_ids = tuple(item.candidate_id for item in self.line_results)
        if len(line_ids) != len(set(line_ids)):
            raise ValueError("FGIS history package contains duplicate BoQ lines")
        profile_selections = {item.candidate_id: item for item in self.profile.line_selections}
        if set(line_ids) != set(profile_selections):
            raise ValueError("FGIS history package lines differ from its profile")
        for line in self.line_results:
            selection = profile_selections[line.candidate_id]
            if (
                line.decision != selection.decision
                or line.resource_codes != selection.resource_codes
            ):
                raise ValueError("FGIS history package line selection differs from its profile")
        if self.history.subject.name != self.profile.subject_name:
            raise ValueError("FGIS history package subject differs from its profile")
        if (
            self.profile.price_zone_name is not None
            and self.history.price_zone.name != self.profile.price_zone_name
        ):
            raise ValueError("FGIS history package price zone differs from its profile")
        selected_codes = tuple(code for line in self.line_results for code in line.resource_codes)
        if len(selected_codes) != len(set(selected_codes)):
            raise ValueError("FGIS history package maps one resource code to multiple BoQ lines")
        if selected_codes != self.history.resource_codes:
            raise ValueError("FGIS history package codes differ from the retrieved grid")
        for line in self.line_results:
            expected_published = sum(
                1
                for observation in self.history.observations
                if observation.requested_resource_code in line.resource_codes
                and observation.price is not None
            )
            expected_source_errors = sum(
                1
                for observation in self.history.observations
                if observation.requested_resource_code in line.resource_codes
                and observation.acquisition_error_code is not None
            )
            if (
                line.published_observation_count != expected_published
                or line.source_error_observation_count != expected_source_errors
            ):
                raise ValueError("FGIS history line counters differ from the retrieved grid")
        expected_sequences = tuple(range(1, len(self.raw_responses) + 1))
        if tuple(item.sequence for item in self.raw_responses) != expected_sequences:
            raise ValueError("FGIS history raw response sequence is incomplete")
        if len(self.raw_responses) != 3 + len(self.history.observations):
            raise ValueError("FGIS history raw response count differs from the grid")
        file_names = tuple(item.file_name for item in self.raw_responses)
        request_uris = tuple(item.request_uri for item in self.raw_responses)
        if len(file_names) != len(set(file_names)) or len(request_uris) != len(set(request_uris)):
            raise ValueError("FGIS history raw response identities must be unique")
        for reference, observation in zip(
            self.raw_responses[3:],
            self.history.observations,
            strict=True,
        ):
            if (
                reference.request_uri != observation.api_request_uri
                or reference.sha256 != observation.response_sha256
                or reference.status_code != observation.response_status_code
                or reference.media_type != observation.response_media_type
            ):
                raise ValueError("FGIS history raw response differs from its observation")
        required_global = {
            "DIAGNOSTIC_FGIS_HISTORY_NOT_GOVERNED",
            "APPROVED_FGIS_MAPPING_REQUIRED",
            "PROJECT_PRICE_PERIOD_NOT_SELECTED",
            "PRICE_NORMALIZATION_REQUIRED",
            "BID_RELEASE_NOT_APPROVED",
        }
        if not required_global.issubset(self.global_blockers):
            raise ValueError("FGIS history package is missing mandatory blockers")
        return self


@dataclass(frozen=True)
class PreparedBoqFgisHistoryPackage:
    manifest: BoqFgisHistoryPackage
    raw_files: tuple[tuple[str, bytes], ...]

    def __post_init__(self) -> None:
        expected_names = tuple(item.file_name for item in self.manifest.raw_responses)
        actual_names = tuple(item[0] for item in self.raw_files)
        if actual_names != expected_names:
            raise ValueError("Prepared FGIS history files are incomplete or reordered")
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
                raise ValueError("Prepared FGIS history raw response differs")


def run_boq_fgis_history_research(
    *,
    research: PreparedBoqFreeSourceResearch,
    research_manifest_sha256: str,
    profile: BoqFgisHistoryProfile,
    acquire_material_history: AcquireMaterialHistory,
) -> PreparedBoqFgisHistoryPackage:
    verify_boq_free_source_research_package(research)
    if research_manifest_sha256 != profile.expected_research_manifest_sha256:
        raise ValueError("FGIS history profile is not bound to the research manifest")
    material_lines = tuple(line for line in research.result.lines if line.cost_nature == "MATERIAL")
    line_by_id = {line.candidate_id: line for line in material_lines}
    selections = {item.candidate_id: item for item in profile.line_selections}
    if set(line_by_id) != set(selections):
        raise ValueError("FGIS history profile must classify every material BoQ line")
    selected_codes: list[str] = []
    line_results_seed: list[tuple[BoqFgisHistoryLineSelection, int, str, str]] = []
    for line in material_lines:
        selection = selections[line.candidate_id]
        candidates = (
            {item.resource_code for item in line.fgis_search_result.candidates}
            if line.fgis_search_result is not None
            else set()
        )
        if not set(selection.resource_codes).issubset(candidates):
            raise ValueError("FGIS history profile selects a code absent from retained KSR data")
        selected_codes.extend(selection.resource_codes)
        line_results_seed.append((selection, line.row_number, line.boq_description, line.boq_unit))
    if not selected_codes:
        raise ValueError("FGIS history profile selects no retrievable resource codes")
    if len(selected_codes) != len(set(selected_codes)):
        raise ValueError("FGIS history profile maps one resource code to multiple BoQ lines")

    request = FgisCsMaterialHistoryRequest(
        subject_name=profile.subject_name,
        price_zone_name=profile.price_zone_name,
        resource_codes=tuple(selected_codes),
    )
    acquisition = acquire_material_history(request)
    replay_fgiscs_material_history_acquisition(acquisition)
    raw_references: list[BoqFgisHistoryRawResponse] = []
    raw_files: list[tuple[str, bytes]] = []
    for sequence, exchange in enumerate(acquisition.exchanges, start=1):
        object_hash = exchange.response_sha256
        file_name = f"raw/{sequence:05d}-{object_hash}.bin"
        raw_references.append(
            BoqFgisHistoryRawResponse(
                sequence=sequence,
                file_name=file_name,
                request_uri=exchange.request_uri,
                sha256=object_hash,
                size_bytes=len(exchange.response_body),
                status_code=exchange.status_code,
                media_type=exchange.media_type,
            )
        )
        raw_files.append((file_name, exchange.response_body))

    line_results: list[BoqFgisHistoryLineResult] = []
    for selection, row_number, description, unit in line_results_seed:
        published_count = sum(
            1
            for observation in acquisition.result.observations
            if observation.requested_resource_code in selection.resource_codes
            and observation.price is not None
        )
        source_error_count = sum(
            1
            for observation in acquisition.result.observations
            if observation.requested_resource_code in selection.resource_codes
            and observation.acquisition_error_code is not None
        )
        blockers = [
            "APPROVED_FGIS_MAPPING_REQUIRED",
            "PROJECT_PRICE_PERIOD_NOT_SELECTED",
            "PRICE_NORMALIZATION_REQUIRED",
            "INDEPENDENT_VALIDATION_REQUIRED",
            "BID_RELEASE_NOT_APPROVED",
        ]
        if not selection.resource_codes:
            blockers.insert(0, "FGIS_KSR_SUITABLE_CANDIDATE_NOT_SELECTED")
        elif published_count == 0:
            blockers.insert(0, "FGIS_PRICE_NOT_PUBLISHED_FOR_RETRIEVED_PERIODS")
        if source_error_count:
            blockers.insert(0, "FGIS_HISTORY_SOURCE_ERRORS_PRESENT")
        line_results.append(
            BoqFgisHistoryLineResult(
                candidate_id=selection.candidate_id,
                row_number=row_number,
                boq_description=description,
                boq_unit=unit,
                decision=selection.decision,
                resource_codes=selection.resource_codes,
                published_observation_count=published_count,
                source_error_observation_count=source_error_count,
                pricing_blockers=tuple(blockers),
            )
        )

    manifest = BoqFgisHistoryPackage(
        profile_version_id=profile.profile_version_id,
        profile_content_hash=content_hash(profile),
        profile=profile,
        research_manifest_sha256=research_manifest_sha256,
        research_result_content_hash=content_hash(research.result),
        project_code=research.result.project_code,
        workbook_sha256=research.result.workbook_sha256,
        line_results=tuple(line_results),
        history=acquisition.result,
        raw_responses=tuple(raw_references),
    )
    return PreparedBoqFgisHistoryPackage(
        manifest=manifest,
        raw_files=tuple(raw_files),
    )


def verify_boq_fgis_history_package(
    prepared: PreparedBoqFgisHistoryPackage,
) -> BoqFgisHistoryPackage:
    acquisition = FgisCsMaterialHistoryAcquisition(
        request=FgisCsMaterialHistoryRequest(
            subject_name=prepared.manifest.profile.subject_name,
            price_zone_name=prepared.manifest.profile.price_zone_name,
            resource_codes=prepared.manifest.history.resource_codes,
        ),
        result=prepared.manifest.history,
        exchanges=tuple(
            FgisCsRawHttpExchange(
                request_uri=reference.request_uri,
                response_body=content,
                status_code=reference.status_code,
                media_type=reference.media_type,
            )
            for reference, (_, content) in zip(
                prepared.manifest.raw_responses,
                prepared.raw_files,
                strict=True,
            )
        ),
    )
    replay_fgiscs_material_history_acquisition(acquisition)
    return prepared.manifest
