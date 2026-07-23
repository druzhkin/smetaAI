from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tenderguard.application.actuals import (
    ActualComparisonResult,
    ActualRecordDraft,
    ActualRecordView,
    CompareActualCommand,
)
from tenderguard.application.approvals import (
    ApprovalDecisionCommand,
    ApprovalDecisionResult,
    ApprovalPlanResult,
)
from tenderguard.application.boq import (
    BoqLineDraft,
    BoqLineView,
    QuantityExecutionResult,
    QuantitySubmission,
    ScopeRunResult,
)
from tenderguard.application.calculations import CalculationExecutionResult
from tenderguard.application.contracts import (
    ContractCostImpactCommand,
    ContractTermDraft,
    ContractTermView,
    ContractValidationResult,
)
from tenderguard.application.evidence import (
    ConflictResolutionCommand,
    ConflictResolutionResult,
    ObservationDraft,
    ReconciliationOutcome,
)
from tenderguard.application.exports import (
    ExportArtifactView,
    ExportVerificationResult,
)
from tenderguard.application.lineage import SnapshotLineage
from tenderguard.application.passport import (
    PassportFactDraft,
    PassportFactView,
    PassportValidationResult,
)
from tenderguard.application.pricing import (
    AnalogueProposalCommand,
    NomenclatureAssessmentDraft,
    NomenclatureMatchView,
    NormalizedPriceView,
    NormalizePriceCommand,
    PriceDecisionView,
    PriceQuoteDraft,
    PriceQuoteView,
)
from tenderguard.application.projects import ProjectView
from tenderguard.application.risks import (
    RiskCalculationView,
    RiskItemDraft,
    RiskItemView,
)
from tenderguard.application.scenarios import (
    ScenarioExecutionCommand,
    ScenarioExecutionResult,
)
from tenderguard.domain.approvals import ApprovalSubject
from tenderguard.domain.calculation import AtomicCostInput, CalculationPolicy
from tenderguard.domain.enums import ApprovalState
from tenderguard.domain.models import ControlledVersion, GateDecision, Observation
from tenderguard.infrastructure.intake import IntakeManifest


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateProjectRequest(ApiModel):
    code: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=2000)


class TransitionRequest(ApiModel):
    to_state: ApprovalState
    expected_row_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2000)


class ConfirmDocumentSetRequest(ApiModel):
    candidate_document_set_revision_id: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=2000)


class ReleaseRequest(ApiModel):
    expected_row_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2000)


class DocumentUploadResponse(ApiModel):
    document_id: str
    document_revision_id: str
    candidate_document_set_revision_id: str
    manifest: IntakeManifest
    project_state: ApprovalState


class ReleaseGateResponse(ApiModel):
    decision: GateDecision


class ReleaseAttemptResponse(ApiModel):
    project: ProjectView
    decision: GateDecision


class CreateControlledVersionRequest(ApiModel):
    kind: str = Field(min_length=1, max_length=100)
    version_label: str = Field(min_length=1, max_length=200)
    payload: dict[str, Any]
    reason: str = Field(min_length=1, max_length=2000)


class ApproveControlledVersionRequest(ApiModel):
    reason: str = Field(min_length=1, max_length=2000)


class BindControlledVersionRequest(ApiModel):
    version_id: str = Field(min_length=1)
    purpose: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=2000)


class ActivateAdapterQualificationRequest(ApiModel):
    reason: str = Field(min_length=1, max_length=2000)


class AdapterQualificationResponse(ApiModel):
    qualification_id: str
    adapter_name: str
    adapter_version: str
    status: str
    valid_until: str | None
    test_evidence_hash: str
    approved_by: str


class RecordObservationRequest(ApiModel):
    draft: ObservationDraft
    reason: str = Field(min_length=1, max_length=2000)


class ObservationResponse(Observation):
    pass


class ReconcileObservationsRequest(ApiModel):
    observation_ids: tuple[str, ...] = Field(min_length=2)
    reconciliation_version_id: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=2000)


class ReconciliationResponse(ReconciliationOutcome):
    pass


class ResolveConflictRequest(ConflictResolutionCommand):
    pass


class ConflictResolutionResponse(ConflictResolutionResult):
    pass


class SubmitPassportFactRequest(ApiModel):
    draft: PassportFactDraft
    reason: str = Field(min_length=1, max_length=2000)


class VerifyPassportFactRequest(ApiModel):
    reason: str = Field(min_length=1, max_length=2000)


class ValidatePassportRequest(ApiModel):
    reason: str = Field(min_length=1, max_length=2000)


class PassportFactResponse(PassportFactView):
    pass


class PassportFactVerificationResponse(ApiModel):
    fact: PassportFactView
    validation: PassportValidationResult


class PassportValidationResponse(PassportValidationResult):
    pass


class CreateBoqLineRequest(ApiModel):
    draft: BoqLineDraft
    reason: str = Field(min_length=1, max_length=2000)


class VerifyBoqLineRequest(ApiModel):
    reason: str = Field(min_length=1, max_length=2000)


class BoqLineResponse(BoqLineView):
    pass


class RecordQuantityRequest(ApiModel):
    submission: QuantitySubmission
    reason: str = Field(min_length=1, max_length=2000)


class QuantityExecutionResponse(QuantityExecutionResult):
    pass


class RunScopeRequest(ApiModel):
    wbs_node_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=2000)


class ScopeRunResponse(ScopeRunResult):
    pass


class AssessNomenclatureRequest(ApiModel):
    draft: NomenclatureAssessmentDraft
    reason: str = Field(min_length=1, max_length=2000)


class ProposeAnalogueRequest(ApiModel):
    command: AnalogueProposalCommand
    reason: str = Field(min_length=1, max_length=2000)


class FinalizeAnalogueRequest(ApiModel):
    reason: str = Field(min_length=1, max_length=2000)


class NomenclatureMatchResponse(NomenclatureMatchView):
    pass


class RecordPriceQuoteRequest(ApiModel):
    draft: PriceQuoteDraft
    reason: str = Field(min_length=1, max_length=2000)


class PriceQuoteResponse(PriceQuoteView):
    pass


class NormalizePriceRequest(ApiModel):
    command: NormalizePriceCommand
    reason: str = Field(min_length=1, max_length=2000)


class NormalizedPriceResponse(NormalizedPriceView):
    pass


class EvaluateItemPriceRequest(ApiModel):
    as_of: date
    reason: str = Field(min_length=1, max_length=2000)


class PriceDecisionResponse(PriceDecisionView):
    pass


class SubmitContractTermRequest(ApiModel):
    draft: ContractTermDraft
    reason: str = Field(min_length=1, max_length=2000)


class VerifyContractTermRequest(ApiModel):
    reason: str = Field(min_length=1, max_length=2000)


class ProposeContractCostImpactRequest(ApiModel):
    command: ContractCostImpactCommand
    reason: str = Field(min_length=1, max_length=2000)


class FinalizeContractCostImpactRequest(ApiModel):
    reason: str = Field(min_length=1, max_length=2000)


class ValidateContractRequest(ApiModel):
    reason: str = Field(min_length=1, max_length=2000)


class ContractTermResponse(ContractTermView):
    pass


class ContractTermValidationResponse(ApiModel):
    term: ContractTermView
    validation: ContractValidationResult


class ContractValidationResponse(ContractValidationResult):
    pass


class SubmitRiskItemRequest(ApiModel):
    draft: RiskItemDraft
    reason: str = Field(min_length=1, max_length=2000)


class VerifyRiskItemRequest(ApiModel):
    reason: str = Field(min_length=1, max_length=2000)


class CalculateRiskReserveRequest(ApiModel):
    reason: str = Field(min_length=1, max_length=2000)


class RiskItemResponse(RiskItemView):
    pass


class RiskCalculationResponse(RiskCalculationView):
    pass


class RecordActualRequest(ApiModel):
    draft: ActualRecordDraft
    reason: str = Field(min_length=1, max_length=2000)


class VerifyActualRequest(ApiModel):
    reason: str = Field(min_length=1, max_length=2000)


class CompareActualRequest(ApiModel):
    command: CompareActualCommand
    reason: str = Field(min_length=1, max_length=2000)


class ApproveCalibrationRequest(ApiModel):
    reason: str = Field(min_length=1, max_length=2000)


class ActualRecordResponse(ActualRecordView):
    pass


class ActualComparisonResponse(ActualComparisonResult):
    pass


class CalibrationApprovalResponse(ApiModel):
    example_id: str
    approved: bool


class BuildApprovalPlanRequest(ApiModel):
    subjects: tuple[ApprovalSubject, ...] = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=2000)


class ApprovalPlanResponse(ApprovalPlanResult):
    pass


class DecideApprovalRequest(ApprovalDecisionCommand):
    pass


class ApprovalDecisionResponse(ApprovalDecisionResult):
    pass


class SnapshotLineageResponse(SnapshotLineage):
    pass


class CalculationExecutionRequest(ApiModel):
    expected_row_version: int = Field(ge=1)
    inputs: tuple[AtomicCostInput, ...] = Field(min_length=1)
    policy: CalculationPolicy
    reason: str = Field(min_length=1, max_length=2000)


class ControlledVersionResponse(ControlledVersion):
    pass


class CalculationExecutionResponse(CalculationExecutionResult):
    pass


class CalculateScenarioRequest(ApiModel):
    command: ScenarioExecutionCommand
    reason: str = Field(min_length=1, max_length=2000)


class ScenarioExecutionResponse(ScenarioExecutionResult):
    pass


class GenerateExportRequest(ApiModel):
    snapshot_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=2000)


class ExportArtifactResponse(ExportArtifactView):
    pass


class ExportVerificationResponse(ExportVerificationResult):
    pass


class ReadinessResponse(ApiModel):
    ready: bool
    database: bool
    object_store: bool
    authentication_configured: bool
    normative_engine_qualified: bool
    export_signing_configured: bool
    notes: tuple[str, ...]
