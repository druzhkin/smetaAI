from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import BinaryIO, Protocol

from pydantic import Field

from tenderguard.domain.enums import EvidenceMethod, PriceEvidenceClass
from tenderguard.domain.models import DomainModel, Observation, PriceQuote
from tenderguard.domain.quarantine import MalwareScanResult


class AdapterQualification(DomainModel):
    adapter_name: str
    adapter_version: str
    qualification_id: str
    approved_by: str
    approved_at: datetime
    valid_until: date | None = None
    test_evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ConnectorHealth(DomainModel):
    connector_name: str
    healthy: bool
    checked_at: datetime
    source_as_of: datetime | None = None
    message: str


class MalwareScanRequest(DomainModel):
    upload_id: str
    project_id: str
    object_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    original_filename: str


class MalwareScanner(Protocol):
    qualification: AdapterQualification

    def scan(self, request: MalwareScanRequest, content: BinaryIO) -> MalwareScanResult: ...

    def health(self) -> ConnectorHealth: ...


class ExtractionRequest(DomainModel):
    project_id: str
    document_revision_id: str
    object_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    requested_fields: frozenset[str]


class ExtractionResult(DomainModel):
    adapter_qualification_id: str
    method: EvidenceMethod
    observations: tuple[Observation, ...]
    completed_at: datetime


class DocumentExtractor(Protocol):
    qualification: AdapterQualification

    def extract(self, request: ExtractionRequest, content: BinaryIO) -> ExtractionResult: ...

    def health(self) -> ConnectorHealth: ...


class NormativeCalculationRequest(DomainModel):
    request_id: str
    project_id: str
    work_or_resource_code: str
    description: str
    quantity: Decimal
    unit: str
    region: str
    price_period: date
    normative_basis_version: str
    calculation_method: str
    technology_attributes: dict[str, str]
    requested_coefficients: dict[str, Decimal]


class NormativeResourceComponent(DomainModel):
    resource_code: str
    category: str
    quantity: Decimal
    unit: str
    rate: Decimal
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    source_reference: str


class NormativeCalculationResult(DomainModel):
    adapter_qualification_id: str
    engine_name: str
    engine_version: str
    normative_basis_version: str
    calculation_method: str
    region: str
    price_period: date
    work_or_resource_code: str
    unit: str
    applied_coefficients: dict[str, Decimal]
    resource_components: tuple[NormativeResourceComponent, ...]
    total: Decimal
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    calculation_artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    calculated_at: datetime


class NormativeEstimatingEngine(Protocol):
    qualification: AdapterQualification

    def calculate(self, request: NormativeCalculationRequest) -> NormativeCalculationResult: ...

    def health(self) -> ConnectorHealth: ...


class NormativeEngineUnavailable(RuntimeError):
    pass


class UnavailableNormativeEstimatingEngine:
    def calculate(self, request: NormativeCalculationRequest) -> NormativeCalculationResult:
        raise NormativeEngineUnavailable(
            "No qualified normative estimating engine is configured; "
            f"request {request.request_id} is blocked"
        )

    def health(self) -> ConnectorHealth:
        raise NormativeEngineUnavailable("No qualified normative estimating engine")


class PriceSearchRequest(DomainModel):
    project_id: str
    item_id: str
    technical_attributes: dict[str, str]
    region: str
    required_on: date
    party_quantity: Decimal
    unit: str
    evidence_class: PriceEvidenceClass


class MarketPriceSource(Protocol):
    qualification: AdapterQualification

    def search(self, request: PriceSearchRequest) -> tuple[PriceQuote, ...]: ...

    def health(self) -> ConnectorHealth: ...


class RfqRequest(DomainModel):
    rfq_id: str
    project_id: str
    item_ids: tuple[str, ...]
    technical_requirements_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    due_at: datetime


class RfqGateway(Protocol):
    qualification: AdapterQualification

    def issue(self, request: RfqRequest) -> str: ...

    def collect(self, external_rfq_id: str) -> tuple[PriceQuote, ...]: ...


class ExportRequest(DomainModel):
    project_id: str
    snapshot_id: str
    format: str
    template_version_id: str


class ExportArtifact(DomainModel):
    object_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str
    filename: str
    generated_at: datetime


class SnapshotExporter(Protocol):
    qualification: AdapterQualification

    def export(self, request: ExportRequest) -> ExportArtifact: ...
