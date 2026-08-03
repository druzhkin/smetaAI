from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

from pydantic import Field, field_validator, model_validator

from tenderguard.domain.models import DomainModel
from tenderguard.integrations.fgiscs_public import (
    FgisCsMaterialAcquisition,
    FgisCsMaterialLookupRequest,
    FgisCsMaterialLookupResult,
    FgisCsRawHttpExchange,
    replay_fgiscs_material_acquisition,
)

FGIS_CS_DIAGNOSTIC_PACKAGE_SCHEMA = "fgiscs-diagnostic-material-package/v1"


class FgisCsDiagnosticRawResponse(DomainModel):
    sequence: int = Field(ge=1, le=4)
    file_name: str = Field(pattern=r"^raw/[0-9]{2}-[0-9a-f]{64}\.json$")
    request_uri: str = Field(pattern=r"^https://fgiscs\.minstroyrf\.ru/api/")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    media_type: Literal["application/json"] = "application/json"


class FgisCsDiagnosticMaterialPackage(DomainModel):
    schema_version: str = FGIS_CS_DIAGNOSTIC_PACKAGE_SCHEMA
    status: Literal["UNVERIFIED"] = "UNVERIFIED"
    request: FgisCsMaterialLookupRequest
    result: FgisCsMaterialLookupResult
    raw_responses: tuple[FgisCsDiagnosticRawResponse, ...] = Field(
        min_length=4,
        max_length=4,
    )
    ready_for_pricing: bool = False
    blockers: tuple[str, ...] = (
        "DIAGNOSTIC_ACQUISITION_NOT_GOVERNED",
        "APPROVED_FGIS_MAPPING_REQUIRED",
        "COMMERCIAL_BASIS_NOT_ESTABLISHED",
        "BID_RELEASE_NOT_APPROVED",
    )

    @field_validator("blockers")
    @classmethod
    def blockers_are_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values or len(values) != len(set(values)):
            raise ValueError("FGIS CS diagnostic blockers must be present and unique")
        return values

    @model_validator(mode="after")
    def package_is_complete_and_fail_closed(self) -> FgisCsDiagnosticMaterialPackage:
        if self.schema_version != FGIS_CS_DIAGNOSTIC_PACKAGE_SCHEMA:
            raise ValueError("Unsupported FGIS CS diagnostic package schema")
        if self.ready_for_pricing or self.result.ready_for_pricing:
            raise ValueError("Diagnostic FGIS CS acquisition cannot release a price")
        required_blockers = {
            "DIAGNOSTIC_ACQUISITION_NOT_GOVERNED",
            "APPROVED_FGIS_MAPPING_REQUIRED",
            "COMMERCIAL_BASIS_NOT_ESTABLISHED",
            "BID_RELEASE_NOT_APPROVED",
        }
        if not required_blockers.issubset(self.blockers):
            raise ValueError("FGIS CS diagnostic package is missing mandatory blockers")
        if self.result.price is None and "FGIS_PRICE_NOT_PUBLISHED" not in self.blockers:
            raise ValueError("Missing FGIS CS price requires an explicit blocker")
        if tuple(item.sequence for item in self.raw_responses) != (1, 2, 3, 4):
            raise ValueError("FGIS CS diagnostic response sequence is incomplete")
        file_names = tuple(item.file_name for item in self.raw_responses)
        request_uris = tuple(item.request_uri for item in self.raw_responses)
        if len(file_names) != len(set(file_names)) or len(request_uris) != len(set(request_uris)):
            raise ValueError("FGIS CS diagnostic response identities must be unique")
        if self.request.resource_code != self.result.requested_resource_code:
            raise ValueError("FGIS CS diagnostic request code differs from its result")
        if self.request.subject_name != self.result.subject.name:
            raise ValueError("FGIS CS diagnostic subject differs from its result")
        if self.request.period_name != self.result.period.name:
            raise ValueError("FGIS CS diagnostic period differs from its result")
        if (
            self.request.price_zone_name is not None
            and self.request.price_zone_name != self.result.price_zone.name
        ):
            raise ValueError("FGIS CS diagnostic price zone differs from its result")
        if self.raw_responses[-1].sha256 != self.result.response_sha256:
            raise ValueError("FGIS CS diagnostic price response differs from its result")
        return self


@dataclass(frozen=True)
class PreparedFgisCsDiagnosticMaterialPackage:
    manifest: FgisCsDiagnosticMaterialPackage
    raw_files: tuple[tuple[str, bytes], ...]

    def __post_init__(self) -> None:
        expected = tuple(item.file_name for item in self.manifest.raw_responses)
        actual = tuple(item[0] for item in self.raw_files)
        if actual != expected:
            raise ValueError("Prepared FGIS CS diagnostic files are incomplete or reordered")
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
                raise ValueError("Prepared FGIS CS diagnostic raw response differs")


def prepare_fgiscs_diagnostic_material_package(
    acquisition: FgisCsMaterialAcquisition,
) -> PreparedFgisCsDiagnosticMaterialPackage:
    replay_fgiscs_material_acquisition(acquisition)
    references: list[FgisCsDiagnosticRawResponse] = []
    raw_files: list[tuple[str, bytes]] = []
    for sequence, exchange in enumerate(acquisition.exchanges, start=1):
        object_hash = exchange.response_sha256
        file_name = f"raw/{sequence:02d}-{object_hash}.json"
        references.append(
            FgisCsDiagnosticRawResponse(
                sequence=sequence,
                file_name=file_name,
                request_uri=exchange.request_uri,
                sha256=object_hash,
                size_bytes=len(exchange.response_body),
            )
        )
        raw_files.append((file_name, exchange.response_body))
    manifest = FgisCsDiagnosticMaterialPackage(
        request=acquisition.request,
        result=acquisition.result,
        raw_responses=tuple(references),
        blockers=tuple(
            dict.fromkeys(
                (
                    "DIAGNOSTIC_ACQUISITION_NOT_GOVERNED",
                    *acquisition.result.pricing_blockers,
                    *(("FGIS_PRICE_NOT_PUBLISHED",) if acquisition.result.price is None else ()),
                    "BID_RELEASE_NOT_APPROVED",
                )
            )
        ),
    )
    return PreparedFgisCsDiagnosticMaterialPackage(
        manifest=manifest,
        raw_files=tuple(raw_files),
    )


def verify_fgiscs_diagnostic_material_package(
    manifest: FgisCsDiagnosticMaterialPackage,
    raw_files: tuple[tuple[str, bytes], ...],
) -> FgisCsDiagnosticMaterialPackage:
    prepared = PreparedFgisCsDiagnosticMaterialPackage(
        manifest=manifest,
        raw_files=raw_files,
    )
    acquisition = FgisCsMaterialAcquisition(
        request=manifest.request,
        result=manifest.result,
        exchanges=tuple(
            FgisCsRawHttpExchange(
                request_uri=reference.request_uri,
                response_body=content,
            )
            for reference, (_, content) in zip(
                manifest.raw_responses,
                prepared.raw_files,
                strict=True,
            )
        ),
    )
    replay_fgiscs_material_acquisition(acquisition)
    return manifest
