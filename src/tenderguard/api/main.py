from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi import (
    Path as ApiPath,
)
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.staticfiles import StaticFiles

from tenderguard import __version__
from tenderguard.application.actuals import ActualsService
from tenderguard.application.approvals import ApprovalService
from tenderguard.application.audit_integrity import AuditIntegrityService
from tenderguard.application.boq import BoqService
from tenderguard.application.business_qualification import (
    BusinessQualificationService,
)
from tenderguard.application.calculations import CalculationService
from tenderguard.application.commercial_costs import CommercialCostService
from tenderguard.application.contracts import ContractService
from tenderguard.application.evidence import EvidenceService
from tenderguard.application.exports import ExportIntegrityError, ExportPackageService
from tenderguard.application.governance import GovernanceService
from tenderguard.application.idempotency import (
    IdempotentAPIRoute,
    mutation_transaction,
    request_scoped_actor,
    request_scoped_session,
)
from tenderguard.application.integrations import IntegrationInboxService
from tenderguard.application.lineage import LineageService
from tenderguard.application.passport import PassportService
from tenderguard.application.pricing import PricingService
from tenderguard.application.production_qualification import (
    ProductionGateEvidenceService,
)
from tenderguard.application.projects import (
    OptimisticLockError,
    ProjectMembershipView,
    ProjectNotFoundError,
    ProjectService,
    ProjectView,
)
from tenderguard.application.quarantine import QuarantineService
from tenderguard.application.rate_limits import (
    DistributedRateLimiter,
    RateLimitDecision,
    request_rate_limit_category,
)
from tenderguard.application.risks import RiskService
from tenderguard.application.scenarios import ScenarioService
from tenderguard.application.workbench import ProjectReadService, ProjectRecordSection
from tenderguard.config import Settings, get_settings
from tenderguard.domain.common import utc_now
from tenderguard.domain.enums import ActorRole, ApprovalState, ContractTermKind
from tenderguard.domain.exports import load_signing_material
from tenderguard.domain.integration import (
    load_integration_signing_material,
    validate_integration_public_key,
)
from tenderguard.infrastructure.auth import Actor, Authenticator
from tenderguard.infrastructure.database import (
    CURRENT_SCHEMA_REVISION,
    create_database_engine,
    create_session_factory,
)
from tenderguard.infrastructure.object_store import (
    ObjectStore,
    build_object_store,
    build_quarantine_store,
)
from tenderguard.infrastructure.orm import AdapterQualificationRow
from tenderguard.observability import RequestLoggingMiddleware, configure_logging

from .schemas import (
    AcknowledgeIntegrationInboxRequest,
    ActivateAdapterQualificationRequest,
    ActualComparisonResponse,
    ActualRecordResponse,
    AdapterQualificationResponse,
    ApplyQuantityManualChangeRequest,
    ApprovalDecisionResponse,
    ApprovalPlanResponse,
    ApproveCalibrationRequest,
    ApproveControlledVersionRequest,
    AssessNomenclatureRequest,
    AuditAnchorReceiptResponse,
    AuditAnchorStatusResponse,
    AuditCheckpointResponse,
    BindControlledVersionRequest,
    BoqAuthoringContextResponse,
    BoqLineResponse,
    BoqLineReviewResponse,
    BuildApprovalPlanRequest,
    BusinessQualificationCampaignDetailResponse,
    BusinessQualificationCampaignResponse,
    BusinessQualificationEvaluationResponse,
    CalculateRiskReserveRequest,
    CalculateScenarioRequest,
    CalculationContextResponse,
    CalculationExecutionRequest,
    CalculationExecutionResponse,
    CalibrationApprovalResponse,
    ClaimIntegrationInboxRequest,
    CommercialCostModelResponse,
    CommercialCostProposalResponse,
    CompareActualRequest,
    ConfirmDocumentSetRequest,
    ConflictResolutionResponse,
    ConflictReviewResponse,
    ContractContextResponse,
    ContractTermDecisionResponse,
    ContractTermResponse,
    ContractTermValidationResponse,
    ContractValidationResponse,
    ControlledVersionResponse,
    CreateAuditCheckpointRequest,
    CreateBoqLineRequest,
    CreateBusinessQualificationCampaignRequest,
    CreateControlledVersionRequest,
    CreateProjectRequest,
    CurrentCalculationExecutionRequest,
    DecideApprovalRequest,
    DecideContractTermRequest,
    DecideManualEvidenceRequest,
    DecidePassportFactRequest,
    DecideRiskItemRequest,
    DocumentSetResponse,
    EvaluateItemPriceRequest,
    ExportArtifactResponse,
    ExportVerificationResponse,
    FinalizeAnalogueRequest,
    FinalizeCommercialCostModelRequest,
    FinalizeContractCostImpactRequest,
    GenerateExportRequest,
    GrantProjectMembershipRequest,
    IntegrationInboxClaimResponse,
    IntegrationInboxMessageResponse,
    IntegrationInboxProcessingResponse,
    IntegrationInboxReceiptResponse,
    IntegrationInboxSettlementResponse,
    ManualEvidenceContextResponse,
    ManualEvidenceDecisionResponse,
    ManualEvidenceReviewResponse,
    NomenclatureContextResponse,
    NomenclatureMatchResponse,
    NomenclatureReviewContextResponse,
    NormalizedPriceResponse,
    NormalizePriceRequest,
    ObservationResponse,
    PassportContextResponse,
    PassportFactDecisionResponse,
    PassportFactResponse,
    PassportFactVerificationResponse,
    PassportValidationResponse,
    PrepareQualificationReferenceRequest,
    PriceDecisionResponse,
    PriceItemContextResponse,
    PriceQuoteCandidateResponse,
    PriceQuoteResponse,
    ProductionGateEvidencePackageDetailResponse,
    ProductionGateEvidencePackageResponse,
    ProjectMembershipResponse,
    ProjectPortfolioResponse,
    ProjectRecordPageResponse,
    ProjectWorkbenchResponse,
    ProposeAnalogueRequest,
    ProposeCommercialCostModelRequest,
    ProposeContractCostImpactRequest,
    ProposeQuantityManualChangeRequest,
    QualificationActionRequest,
    QualificationReferenceEvidenceResponse,
    QuantityChangeContextResponse,
    QuantityExecutionResponse,
    QuantityManualChangeResponse,
    QuarantinedUploadResponse,
    ReadinessResponse,
    ReceiveIntegrationMessageRequest,
    ReconcileObservationsRequest,
    ReconciliationContextResponse,
    ReconciliationResponse,
    RecordActualRequest,
    RecordMalwareScanResultRequest,
    RecordObservationRequest,
    RecordPriceQuoteFromObservationRequest,
    RecordPriceQuoteRequest,
    RecordQuantityRequest,
    RegisterAuditAnchorReceiptRequest,
    RejectIntegrationInboxRequest,
    ReleaseAttemptResponse,
    ReleaseGateResponse,
    ReleaseRequest,
    ReplayIntegrationMessageRequest,
    ReplayOutboxResponse,
    RequeueDocumentProcessingRequest,
    ResolveConflictRequest,
    ReviewProductionGateEvidenceRequest,
    ReviewQualificationDiscrepancyRequest,
    RevokeProductionGateEvidenceRequest,
    RevokeProjectMembershipRequest,
    RiskCalculationResponse,
    RiskContextResponse,
    RiskItemDecisionResponse,
    RiskItemResponse,
    RunScopeRequest,
    RuntimeConfigResponse,
    ScenarioContextResponse,
    ScenarioExecutionResponse,
    ScopeRunResponse,
    SnapshotLineageResponse,
    SubmitContractTermRequest,
    SubmitPassportFactRequest,
    SubmitProductionGateEvidenceRequest,
    SubmitRiskItemRequest,
    TransitionRequest,
    ValidateContractRequest,
    ValidatePassportRequest,
    VerifyActualRequest,
    VerifyBoqLineRequest,
    VerifyContractTermRequest,
    VerifyPassportFactRequest,
    VerifyQualificationReferenceRequest,
    VerifyRiskItemRequest,
    WorkItemDetailResponse,
    WorkItemPageResponse,
)
from .security import RequestSizeLimitMiddleware, SecurityHeadersMiddleware


def _operator_ui_dist(settings: Settings) -> Path | None:
    if not settings.operator_ui_enabled:
        return None
    candidates = (
        (settings.operator_ui_dist_path.resolve(),)
        if settings.operator_ui_dist_path is not None
        else (
            Path(__file__).resolve().parents[1] / "web_dist",
            Path.cwd() / "web" / "dist",
        )
    )
    for candidate in candidates:
        if candidate.is_dir() and (candidate / "index.html").is_file():
            return candidate
    if settings.app_env in {"staging", "production"}:
        raise RuntimeError("Operator UI is enabled but its built assets are unavailable")
    return None


def _oidc_connect_origins(settings: Settings) -> tuple[str, ...]:
    origins: set[str] = set()
    for value in (settings.oidc_issuer, settings.oidc_jwks_url):
        if value is None:
            continue
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            origins.add(f"{parsed.scheme}://{parsed.netloc}")
    return tuple(sorted(origins))


def create_app(
    settings: Settings | None = None,
    *,
    engine: Engine | None = None,
    object_store: ObjectStore | None = None,
    quarantine_store: ObjectStore | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    resolved_engine = engine or create_database_engine(resolved_settings)
    session_factory = create_session_factory(resolved_engine)
    resolved_store = object_store or build_object_store(resolved_settings)
    resolved_quarantine_store = quarantine_store or build_quarantine_store(resolved_settings)
    operator_ui_dist = _operator_ui_dist(resolved_settings)
    authenticator = Authenticator(resolved_settings)
    configure_logging()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        resolved_engine.dispose()

    application = FastAPI(
        title="TenderGuard API",
        version="0.1.0",
        description=(
            "Fail-closed evidence, validation, approval, and audit control plane "
            "for industrial tender costing."
        ),
        lifespan=lifespan,
        docs_url=None if resolved_settings.app_env in {"staging", "production"} else "/docs",
        redoc_url=None if resolved_settings.app_env in {"staging", "production"} else "/redoc",
        openapi_url=(
            None if resolved_settings.app_env in {"staging", "production"} else "/openapi.json"
        ),
    )
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=resolved_settings.trusted_hosts,
    )
    application.add_middleware(
        RequestSizeLimitMiddleware,
        max_bytes=resolved_settings.max_upload_bytes + 1024 * 1024,
        max_non_multipart_bytes=resolved_settings.max_api_request_bytes,
        path_suffix_limits={
            "/scan-results": resolved_settings.max_scan_report_bytes + 64 * 1024,
        },
    )
    application.add_middleware(
        SecurityHeadersMiddleware,
        connect_sources=_oidc_connect_origins(resolved_settings),
        include_hsts=resolved_settings.app_env in {"staging", "production"},
    )
    application.add_middleware(RequestLoggingMiddleware)
    application.router.route_class = IdempotentAPIRoute
    application.state.settings = resolved_settings
    application.state.engine = resolved_engine
    application.state.session_factory = session_factory
    application.state.object_store = resolved_store
    application.state.quarantine_store = resolved_quarantine_store
    application.state.authenticator = authenticator

    def rate_limit_headers(decision: RateLimitDecision) -> dict[str, str]:
        return {
            "RateLimit-Limit": (
                f"actor={decision.actor_limit}, organization={decision.organization_limit}"
            ),
            "RateLimit-Remaining": str(decision.remaining),
            "RateLimit-Reset": str(decision.reset_epoch_seconds),
            "X-RateLimit-Category": decision.category,
        }

    def enforce_rate_limit(request: Request, actor: Actor) -> None:
        if not resolved_settings.rate_limit_enabled or getattr(
            request.state, "rate_limit_checked", False
        ):
            return
        limiter_session = session_factory()
        try:
            with limiter_session.begin():
                decision = DistributedRateLimiter(
                    session=limiter_session,
                    settings=resolved_settings,
                ).consume(
                    actor=actor,
                    category=request_rate_limit_category(
                        method=request.method,
                        path=request.url.path,
                        content_type=request.headers.get("content-type"),
                    ),
                )
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Distributed request quota is unavailable",
            ) from error
        finally:
            limiter_session.close()
        request.state.rate_limit_checked = True
        request.state.rate_limit_decision = decision
        if not decision.allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Distributed actor or organization request quota exceeded",
                headers={
                    **rate_limit_headers(decision),
                    "Retry-After": str(decision.retry_after_seconds),
                },
            )

    application.state.enforce_rate_limit = enforce_rate_limit

    def get_session(request: Request) -> Iterator[Session]:
        shared_session = request_scoped_session(request)
        if shared_session is not None:
            yield shared_session
            return
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    def get_actor(
        request: Request,
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
        x_dev_actor: Annotated[str | None, Header()] = None,
        x_dev_organization: Annotated[str | None, Header()] = None,
        x_dev_roles: Annotated[str | None, Header()] = None,
    ) -> Actor:
        shared_actor = request_scoped_actor(request)
        actor = shared_actor or authenticator.authenticate(
            authorization=authorization,
            dev_actor=x_dev_actor,
            dev_organization=x_dev_organization,
            dev_roles=x_dev_roles,
        )
        enforce_rate_limit(request, actor)
        decision = getattr(request.state, "rate_limit_decision", None)
        if isinstance(decision, RateLimitDecision):
            response.headers.update(rate_limit_headers(decision))
        return actor

    def service(session: Session) -> ProjectService:
        return ProjectService(
            session=session,
            settings=resolved_settings,
            object_store=resolved_store,
        )

    def read_service(session: Session) -> ProjectReadService:
        return ProjectReadService(
            session=session,
            settings=resolved_settings,
            object_store=resolved_store,
        )

    def governance_service(session: Session) -> GovernanceService:
        return GovernanceService(
            session=session,
            settings=resolved_settings,
            object_store=resolved_store,
        )

    def business_qualification_service(
        session: Session,
    ) -> BusinessQualificationService:
        return BusinessQualificationService(
            session=session,
            settings=resolved_settings,
            object_store=resolved_store,
        )

    def production_gate_evidence_service(
        session: Session,
    ) -> ProductionGateEvidenceService:
        return ProductionGateEvidenceService(
            session=session,
            settings=resolved_settings,
            object_store=resolved_store,
        )

    def quarantine_service(session: Session) -> QuarantineService:
        return QuarantineService(
            session=session,
            settings=resolved_settings,
            evidence_store=resolved_store,
            quarantine_store=resolved_quarantine_store,
        )

    def evidence_service(session: Session) -> EvidenceService:
        return EvidenceService(
            session=session,
            settings=resolved_settings,
            object_store=resolved_store,
        )

    def approval_service(session: Session) -> ApprovalService:
        return ApprovalService(
            session=session,
            settings=resolved_settings,
            object_store=resolved_store,
        )

    def lineage_service(session: Session) -> LineageService:
        return LineageService(
            session=session,
            settings=resolved_settings,
            object_store=resolved_store,
        )

    def boq_service(session: Session) -> BoqService:
        return BoqService(
            session=session,
            settings=resolved_settings,
            object_store=resolved_store,
        )

    def passport_service(session: Session) -> PassportService:
        return PassportService(
            session=session,
            settings=resolved_settings,
            object_store=resolved_store,
        )

    def pricing_service(session: Session) -> PricingService:
        return PricingService(
            session=session,
            settings=resolved_settings,
            object_store=resolved_store,
        )

    def contract_service(session: Session) -> ContractService:
        return ContractService(
            session=session,
            settings=resolved_settings,
            object_store=resolved_store,
        )

    def commercial_cost_service(session: Session) -> CommercialCostService:
        return CommercialCostService(
            session=session,
            settings=resolved_settings,
            object_store=resolved_store,
        )

    def integration_inbox_service(session: Session) -> IntegrationInboxService:
        return IntegrationInboxService(
            session=session,
            settings=resolved_settings,
            object_store=resolved_store,
        )

    def risk_service(session: Session) -> RiskService:
        return RiskService(
            session=session,
            settings=resolved_settings,
            object_store=resolved_store,
        )

    def actuals_service(session: Session) -> ActualsService:
        return ActualsService(
            session=session,
            settings=resolved_settings,
            object_store=resolved_store,
        )

    def scenario_service(session: Session) -> ScenarioService:
        return ScenarioService(
            session=session,
            settings=resolved_settings,
            object_store=resolved_store,
        )

    def export_service(session: Session) -> ExportPackageService:
        return ExportPackageService(
            session=session,
            settings=resolved_settings,
            object_store=resolved_store,
        )

    def audit_integrity_service(session: Session) -> AuditIntegrityService:
        return AuditIntegrityService(
            session=session,
            settings=resolved_settings,
            object_store=resolved_store,
        )

    @application.exception_handler(ProjectNotFoundError)
    async def project_not_found(_: Request, error: ProjectNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": f"Project not found: {error}"},
        )

    @application.exception_handler(LookupError)
    async def governed_record_not_found(_: Request, error: LookupError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": f"Governed record not found: {error}"},
        )

    @application.exception_handler(OptimisticLockError)
    async def optimistic_lock(_: Request, error: OptimisticLockError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": str(error)},
        )

    @application.exception_handler(ExportIntegrityError)
    async def export_integrity_error(_: Request, error: ExportIntegrityError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": str(error)},
        )

    @application.exception_handler(IntegrityError)
    async def integrity_error(_: Request, __: IntegrityError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": "A uniqueness or referential-integrity rule was violated"},
        )

    @application.exception_handler(ValueError)
    async def invalid_domain_input(_: Request, error: ValueError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": str(error)},
        )

    @application.get("/health/live")
    def liveness() -> dict[str, str]:
        return {"status": "alive"}

    @application.get("/v1/runtime-config", response_model=RuntimeConfigResponse)
    def runtime_config() -> RuntimeConfigResponse:
        authentication_mode: Literal["OIDC", "DEVELOPMENT", "UNAVAILABLE"]
        if resolved_settings.allow_insecure_dev_auth:
            authentication_mode = "DEVELOPMENT"
        elif all(
            (
                resolved_settings.oidc_issuer,
                resolved_settings.oidc_audience,
                resolved_settings.oidc_jwks_url,
                resolved_settings.oidc_web_client_id,
            )
        ):
            authentication_mode = "OIDC"
        else:
            authentication_mode = "UNAVAILABLE"
        return RuntimeConfigResponse(
            environment=resolved_settings.app_env,
            authentication_mode=authentication_mode,
            oidc_authority=resolved_settings.oidc_issuer,
            oidc_client_id=resolved_settings.oidc_web_client_id,
            oidc_scope=resolved_settings.oidc_web_scope,
            api_base_path="/v1",
            application_version=__version__,
            application_build_reference=resolved_settings.application_build_reference,
            max_upload_bytes=resolved_settings.max_upload_bytes,
        )

    @application.get("/health/ready", response_model=ReadinessResponse)
    def readiness(
        response: Response,
        session: Annotated[Session, Depends(get_session)],
    ) -> ReadinessResponse:
        database_ok = False
        schema_current = False
        store_ok = False
        store_worm_ok = False
        quarantine_store_ok = False
        audit_anchor_valid = False
        notes: list[str] = []
        try:
            session.execute(text("SELECT 1"))
            database_ok = True
        except Exception:
            notes.append("database check failed")
        if database_ok:
            try:
                database_revision = session.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one_or_none()
                schema_current = database_revision == CURRENT_SCHEMA_REVISION
                if not schema_current:
                    notes.append("database schema is not at the application migration head")
            except Exception:
                notes.append("database migration state is unavailable")
        try:
            store_ok = resolved_store.healthcheck()
        except Exception:
            notes.append("object-store check failed")
        if resolved_settings.worm_policy_configured:
            assert resolved_settings.s3_required_object_lock_mode is not None
            assert resolved_settings.s3_minimum_retention_days is not None
            try:
                store_worm_ok = resolved_store.retention_status().satisfies(
                    required_mode=resolved_settings.s3_required_object_lock_mode,
                    minimum_days=resolved_settings.s3_minimum_retention_days,
                )
            except Exception:
                notes.append("evidence-store WORM retention check failed")
        if not store_worm_ok:
            notes.append("evidence-store WORM retention policy is not satisfied")
        try:
            quarantine_store_ok = resolved_quarantine_store.healthcheck()
        except Exception:
            notes.append("quarantine-store check failed")
        auth_configured = resolved_settings.allow_insecure_dev_auth or all(
            (
                resolved_settings.oidc_issuer,
                resolved_settings.oidc_audience,
                resolved_settings.oidc_jwks_url,
            )
        )
        if not auth_configured:
            notes.append("OIDC is not configured")
        operator_ui_ready = (
            not resolved_settings.operator_ui_enabled or operator_ui_dist is not None
        )
        if not operator_ui_ready:
            notes.append("operator UI build is unavailable")
        idempotency_enforced = resolved_settings.require_idempotency_keys
        if not idempotency_enforced:
            notes.append("persisted idempotency keys are not enforced")
        normative_engine_qualified = False
        if schema_current:
            try:
                normative_engine_qualified = service(session).normative_engine_qualified()
            except Exception:
                notes.append("normative qualification check failed")
        if not normative_engine_qualified:
            notes.append("bid release remains blocked: normative engine is not qualified")
        if schema_current:
            try:
                anchor_status = audit_integrity_service(session).anchor_status()
                audit_anchor_valid = anchor_status.valid
                notes.extend(anchor_status.reasons)
            except Exception:
                notes.append("external audit anchor validation failed")
        now = utc_now()
        malware_scanner_qualification = None
        if schema_current and resolved_settings.malware_scanner_configured:
            try:
                malware_scanner_qualification = session.scalar(
                    select(AdapterQualificationRow).where(
                        AdapterQualificationRow.id
                        == resolved_settings.malware_scanner_qualification_id,
                        AdapterQualificationRow.adapter_name
                        == resolved_settings.malware_scanner_adapter,
                        AdapterQualificationRow.status == "APPROVED",
                        (
                            (AdapterQualificationRow.valid_until.is_(None))
                            | (AdapterQualificationRow.valid_until >= now.date())
                        ),
                    )
                )
            except Exception:
                notes.append("malware scanner qualification check failed")
        malware_scanner_qualified = bool(
            malware_scanner_qualification
            and "MALWARE_SCAN" in malware_scanner_qualification.payload.get("supported_methods", [])
            and isinstance(
                malware_scanner_qualification.payload.get("service_actor_id"),
                str,
            )
            and malware_scanner_qualification.payload.get("service_actor_id")
        )
        if not malware_scanner_qualified:
            notes.append("qualified malware scanner is unavailable")
        document_processor_qualification = None
        if schema_current and resolved_settings.document_processor_configured:
            try:
                document_processor_qualification = session.scalar(
                    select(AdapterQualificationRow).where(
                        AdapterQualificationRow.id
                        == resolved_settings.document_processor_qualification_id,
                        AdapterQualificationRow.adapter_name
                        == resolved_settings.document_processor_adapter,
                        AdapterQualificationRow.status == "APPROVED",
                        (
                            (AdapterQualificationRow.valid_until.is_(None))
                            | (AdapterQualificationRow.valid_until >= now.date())
                        ),
                    )
                )
            except Exception:
                notes.append("document processor qualification check failed")
        document_processor_qualified = bool(
            resolved_settings.document_worker_actor_id
            and document_processor_qualification
            and document_processor_qualification.payload.get("service_actor_id")
            == resolved_settings.document_worker_actor_id
            and "DOCUMENT_INTAKE"
            in document_processor_qualification.payload.get("supported_methods", [])
        )
        if not document_processor_qualified:
            notes.append("qualified isolated document processor is unavailable")
        export_signing_configured = False
        if resolved_settings.export_signing_configured:
            assert resolved_settings.export_signing_key_id is not None
            assert resolved_settings.export_signing_private_key_b64 is not None
            try:
                load_signing_material(
                    key_id=resolved_settings.export_signing_key_id,
                    private_key_b64=(
                        resolved_settings.export_signing_private_key_b64.get_secret_value()
                    ),
                )
                export_signing_configured = True
            except ValueError:
                notes.append("Ed25519 export signing key configuration is invalid")
        else:
            notes.append("Ed25519 export signing key is not configured")
        integration_signing_configured = False
        if (
            resolved_settings.integration_signing_configured
            and resolved_settings.integration_receiver_id
        ):
            assert resolved_settings.integration_signing_key_id is not None
            assert resolved_settings.integration_signing_private_key_b64 is not None
            try:
                load_integration_signing_material(
                    key_id=resolved_settings.integration_signing_key_id,
                    private_key_b64=(
                        resolved_settings.integration_signing_private_key_b64.get_secret_value()
                    ),
                )
                integration_signing_configured = True
            except ValueError:
                notes.append("Ed25519 integration signing key configuration is invalid")
        else:
            notes.append(
                "Ed25519 integration signing key or receipt receiver identity is not configured"
            )
        integration_connectors_qualified = False
        distributed_rate_limiting = bool(
            schema_current and resolved_settings.distributed_rate_limit_configured
        )
        if not distributed_rate_limiting:
            notes.append("distributed actor/organization request quotas are unavailable")
        build_identified = bool(resolved_settings.application_build_reference)
        if not build_identified:
            notes.append("immutable application build reference is not configured")
        if schema_current and resolved_settings.integration_operator_organization_id:
            try:
                qualifications = tuple(
                    session.scalars(
                        select(AdapterQualificationRow).where(
                            AdapterQualificationRow.status == "APPROVED",
                            (
                                (AdapterQualificationRow.valid_until.is_(None))
                                | (AdapterQualificationRow.valid_until >= now.date())
                            ),
                        )
                    )
                )
                outbound = False
                inbound = False
                handler = False
                for qualification in qualifications:
                    if (
                        qualification.payload.get("organization_id")
                        != resolved_settings.integration_operator_organization_id
                    ):
                        continue
                    methods = qualification.payload.get("supported_methods")
                    if not isinstance(methods, list):
                        continue
                    inbound_topics = qualification.payload.get("inbound_topics")
                    outbound_topics = qualification.payload.get("outbound_topics")
                    has_service_actor = _readiness_string(
                        qualification.payload.get("service_actor_id"),
                        128,
                    )
                    if "INTEGRATION_OUTBOUND_DELIVERY" in methods:
                        receipt_key = qualification.payload.get("receipt_public_key_b64")
                        if (
                            _readiness_topics(outbound_topics)
                            and _readiness_string(
                                qualification.payload.get("receipt_signing_key_id"),
                                200,
                            )
                            and isinstance(receipt_key, str)
                            and _readiness_string(
                                qualification.payload.get("receiver_id"),
                                200,
                            )
                            and has_service_actor
                        ):
                            validate_integration_public_key(receipt_key)
                            outbound = True
                    if "INTEGRATION_INBOUND_SOURCE" in methods:
                        inbound_key = qualification.payload.get("inbound_signing_public_key_b64")
                        if (
                            _readiness_topics(inbound_topics)
                            and _readiness_string(
                                qualification.payload.get("inbound_signing_key_id"),
                                200,
                            )
                            and isinstance(inbound_key, str)
                            and has_service_actor
                        ):
                            validate_integration_public_key(inbound_key)
                            inbound = True
                    if (
                        "INTEGRATION_INBOX_HANDLER" in methods
                        and _readiness_topics(inbound_topics)
                        and has_service_actor
                    ):
                        handler = True
                integration_connectors_qualified = outbound and inbound and handler
            except Exception:
                notes.append("integration connector qualification check failed")
        if not integration_connectors_qualified:
            notes.append("qualified integration delivery/source/handler set is unavailable")
        ready = bool(
            build_identified
            and database_ok
            and schema_current
            and store_ok
            and store_worm_ok
            and quarantine_store_ok
            and operator_ui_ready
            and auth_configured
            and idempotency_enforced
            and normative_engine_qualified
            and audit_anchor_valid
            and malware_scanner_qualified
            and document_processor_qualified
            and export_signing_configured
            and integration_signing_configured
            and integration_connectors_qualified
            and distributed_rate_limiting
        )
        if not ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessResponse(
            ready=ready,
            build_identified=build_identified,
            database=database_ok,
            schema_current=schema_current,
            object_store=store_ok,
            object_store_worm=store_worm_ok,
            quarantine_store=quarantine_store_ok,
            operator_ui=operator_ui_ready,
            authentication_configured=bool(auth_configured),
            idempotency_enforced=idempotency_enforced,
            audit_anchor_valid=audit_anchor_valid,
            normative_engine_qualified=normative_engine_qualified,
            malware_scanner_qualified=malware_scanner_qualified,
            document_processor_qualified=document_processor_qualified,
            export_signing_configured=export_signing_configured,
            integration_signing_configured=integration_signing_configured,
            integration_connectors_qualified=integration_connectors_qualified,
            distributed_rate_limiting=distributed_rate_limiting,
            notes=tuple(notes),
        )

    @application.post(
        "/v1/audit/checkpoints",
        response_model=AuditCheckpointResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_audit_checkpoint(
        payload: CreateAuditCheckpointRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> AuditCheckpointResponse:
        with mutation_transaction(session):
            checkpoint = audit_integrity_service(session).create_checkpoint(
                actor=actor,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return AuditCheckpointResponse.model_validate(checkpoint)

    @application.post(
        "/v1/audit/checkpoints/{checkpoint_id}/receipts",
        response_model=AuditAnchorReceiptResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def register_audit_anchor_receipt(
        checkpoint_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
        payload: RegisterAuditAnchorReceiptRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> AuditAnchorReceiptResponse:
        with mutation_transaction(session):
            receipt = audit_integrity_service(session).register_receipt(
                actor=actor,
                checkpoint_id=checkpoint_id,
                anchored_at=payload.anchored_at,
                external_reference=payload.external_reference,
                signature_b64=payload.signature_b64,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return AuditAnchorReceiptResponse.model_validate(receipt)

    @application.get(
        "/v1/audit/anchor-status",
        response_model=AuditAnchorStatusResponse,
    )
    def get_audit_anchor_status(
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> AuditAnchorStatusResponse:
        integrity_service = audit_integrity_service(session)
        integrity_service.require_operator(actor)
        return AuditAnchorStatusResponse.model_validate(integrity_service.anchor_status())

    @application.post(
        "/v1/integrations/inbox",
        response_model=IntegrationInboxReceiptResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def receive_integration_message(
        payload: ReceiveIntegrationMessageRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> IntegrationInboxReceiptResponse:
        with mutation_transaction(session):
            result = integration_inbox_service(session).receive(
                actor=actor,
                source_qualification_id=payload.source_qualification_id,
                envelope=payload.envelope,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return IntegrationInboxReceiptResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/integrations/inbox/claims",
        response_model=IntegrationInboxClaimResponse,
    )
    def claim_integration_message(
        payload: ClaimIntegrationInboxRequest,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> IntegrationInboxClaimResponse:
        with mutation_transaction(session):
            claim = integration_inbox_service(session).claim_next(
                actor=actor,
                handler_qualification_id=payload.handler_qualification_id,
                topics=payload.topics,
                worker_id=payload.worker_id,
            )
        return IntegrationInboxClaimResponse(claim=claim)

    @application.post(
        "/v1/integrations/inbox/processings/{processing_id}/acknowledge",
        response_model=IntegrationInboxSettlementResponse,
    )
    def acknowledge_integration_message(
        processing_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        payload: AcknowledgeIntegrationInboxRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> IntegrationInboxSettlementResponse:
        if payload.claim.processing_id != processing_id:
            raise ValueError("Inbox claim does not match the processing path")
        with mutation_transaction(session):
            result = integration_inbox_service(session).acknowledge(
                actor=actor,
                claim=payload.claim,
                result_reference=payload.result_reference,
                result_hash=payload.result_hash,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return IntegrationInboxSettlementResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/integrations/inbox/processings/{processing_id}/reject",
        response_model=IntegrationInboxSettlementResponse,
    )
    def reject_integration_message(
        processing_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        payload: RejectIntegrationInboxRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> IntegrationInboxSettlementResponse:
        if payload.claim.processing_id != processing_id:
            raise ValueError("Inbox claim does not match the processing path")
        with mutation_transaction(session):
            result = integration_inbox_service(session).reject(
                actor=actor,
                claim=payload.claim,
                error_code=payload.error_code,
                force_dead_letter=payload.force_dead_letter,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return IntegrationInboxSettlementResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/integrations/inbox/processings/{processing_id}/replay",
        response_model=IntegrationInboxProcessingResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def replay_integration_message(
        processing_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        payload: ReplayIntegrationMessageRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> IntegrationInboxProcessingResponse:
        with mutation_transaction(session):
            result = integration_inbox_service(session).replay_dead_letter(
                actor=actor,
                processing_id=processing_id,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return IntegrationInboxProcessingResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/integrations/outbox/{outbox_event_id}/replay",
        response_model=ReplayOutboxResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def replay_integration_outbox_event(
        outbox_event_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        payload: ReplayIntegrationMessageRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ReplayOutboxResponse:
        with mutation_transaction(session):
            replay_event_id = integration_inbox_service(session).replay_outbox_dead_letter(
                actor=actor,
                outbox_event_id=outbox_event_id,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return ReplayOutboxResponse(replay_outbox_event_id=replay_event_id)

    @application.get(
        "/v1/integrations/inbox/{message_id}",
        response_model=IntegrationInboxMessageResponse,
    )
    def get_integration_message(
        message_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> IntegrationInboxMessageResponse:
        result = integration_inbox_service(session).get(
            actor=actor,
            message_id=message_id,
        )
        return IntegrationInboxMessageResponse.model_validate(result.model_dump())

    @application.get(
        "/v1/projects",
        response_model=ProjectPortfolioResponse,
    )
    def list_projects(
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
        query: Annotated[str | None, Query(max_length=200)] = None,
        states: Annotated[list[ApprovalState] | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: Annotated[str | None, Query(max_length=1000)] = None,
    ) -> ProjectPortfolioResponse:
        result = read_service(session).list_projects(
            actor=actor,
            query=query,
            states=frozenset(states or ()),
            limit=limit,
            cursor=cursor,
        )
        return ProjectPortfolioResponse.model_validate(result.model_dump())

    @application.get(
        "/v1/work-items",
        response_model=WorkItemPageResponse,
    )
    def list_work_items(
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
        statuses: Annotated[list[str] | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: Annotated[str | None, Query(max_length=1000)] = None,
    ) -> WorkItemPageResponse:
        result = read_service(session).list_work_items(
            actor=actor,
            statuses=frozenset(statuses or ("PENDING",)),
            limit=limit,
            cursor=cursor,
        )
        return WorkItemPageResponse.model_validate(result.model_dump())

    @application.get(
        "/v1/work-items/{task_id}",
        response_model=WorkItemDetailResponse,
    )
    def get_work_item(
        task_id: str,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> WorkItemDetailResponse:
        result = read_service(session).get_work_item(actor=actor, task_id=task_id)
        return WorkItemDetailResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects",
        response_model=ProjectView,
        status_code=status.HTTP_201_CREATED,
    )
    def create_project(
        payload: CreateProjectRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ProjectView:
        with mutation_transaction(session):
            return service(session).create_project(
                actor=actor,
                code=payload.code,
                name=payload.name,
                request_id=request.state.request_id,
                reason=payload.reason,
            )

    @application.get("/v1/projects/{project_id}", response_model=ProjectView)
    def get_project(
        project_id: str,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ProjectView:
        return service(session).project_view(actor=actor, project_id=project_id)

    @application.get(
        "/v1/projects/{project_id}/workbench",
        response_model=ProjectWorkbenchResponse,
    )
    def get_project_workbench(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ProjectWorkbenchResponse:
        result = read_service(session).workbench(
            actor=actor,
            project_id=project_id,
        )
        return ProjectWorkbenchResponse.model_validate(result.model_dump())

    @application.get(
        "/v1/projects/{project_id}/records",
        response_model=ProjectRecordPageResponse,
    )
    def list_project_records(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        section: Annotated[ProjectRecordSection, Query()],
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: Annotated[str | None, Query(max_length=1000)] = None,
        current_only: bool = False,
        query: Annotated[str | None, Query(max_length=200)] = None,
        statuses: Annotated[list[str] | None, Query()] = None,
    ) -> ProjectRecordPageResponse:
        result = read_service(session).records(
            actor=actor,
            project_id=project_id,
            section=section,
            limit=limit,
            cursor=cursor,
            current_only=current_only,
            query=query,
            statuses=frozenset(statuses or ()),
        )
        return ProjectRecordPageResponse.model_validate(result.model_dump())

    @application.get(
        "/v1/projects/{project_id}/members",
        response_model=list[ProjectMembershipView],
    )
    def list_project_members(
        project_id: str,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> tuple[ProjectMembershipView, ...]:
        return service(session).list_project_memberships(
            actor=actor,
            project_id=project_id,
        )

    @application.post(
        "/v1/projects/{project_id}/members",
        response_model=ProjectMembershipResponse,
    )
    def grant_project_member(
        project_id: str,
        payload: GrantProjectMembershipRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ProjectMembershipView:
        with mutation_transaction(session):
            return service(session).grant_project_membership(
                actor=actor,
                project_id=project_id,
                principal_id=payload.principal_id,
                roles=payload.roles,
                access_level=payload.access_level,
                request_id=request.state.request_id,
                reason=payload.reason,
            )

    @application.post(
        "/v1/projects/{project_id}/members/{principal_id}/revoke",
        response_model=ProjectMembershipResponse,
    )
    def revoke_project_member(
        project_id: str,
        principal_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
        payload: RevokeProjectMembershipRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ProjectMembershipView:
        with mutation_transaction(session):
            return service(session).revoke_project_membership(
                actor=actor,
                project_id=project_id,
                principal_id=principal_id,
                request_id=request.state.request_id,
                reason=payload.reason,
            )

    @application.post(
        "/v1/projects/{project_id}/documents",
        response_model=QuarantinedUploadResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def upload_document(
        project_id: str,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
        logical_key: Annotated[str, Form(min_length=1, max_length=300)],
        title: Annotated[str, Form(min_length=1, max_length=1000)],
        document_type: Annotated[str, Form(min_length=1, max_length=100)],
        revision_label: Annotated[str, Form(min_length=1, max_length=100)],
        reason: Annotated[str, Form(min_length=1, max_length=2000)],
        upload: Annotated[UploadFile, File()],
        critical: Annotated[bool, Form()] = False,
        make_candidate_current: Annotated[bool, Form()] = True,
    ) -> QuarantinedUploadResponse:
        try:
            upload.file.seek(0)
            with mutation_transaction(session):
                result = quarantine_service(session).receive(
                    actor=actor,
                    project_id=project_id,
                    logical_key=logical_key,
                    title=title,
                    document_type=document_type,
                    critical=critical,
                    revision_label=revision_label,
                    filename=upload.filename or "unnamed",
                    media_type=upload.content_type or "application/octet-stream",
                    stream=upload.file,
                    request_id=request.state.request_id,
                    reason=reason,
                    make_candidate_current=make_candidate_current,
                )
        except ValueError as error:
            if "exceeds configured limit" in str(error):
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="Upload exceeds configured size limit",
                ) from error
            raise
        return QuarantinedUploadResponse.model_validate(result.model_dump())

    @application.get(
        "/v1/projects/{project_id}/document-uploads/{upload_id}",
        response_model=QuarantinedUploadResponse,
    )
    def get_document_upload(
        project_id: str,
        upload_id: str,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> QuarantinedUploadResponse:
        result = quarantine_service(session).get(
            actor=actor,
            project_id=project_id,
            upload_id=upload_id,
        )
        return QuarantinedUploadResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/document-uploads/{upload_id}/scan-results",
        response_model=QuarantinedUploadResponse,
    )
    def record_document_scan_result(
        project_id: str,
        upload_id: str,
        payload: RecordMalwareScanResultRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> QuarantinedUploadResponse:
        with mutation_transaction(session):
            result = quarantine_service(session).record_scan_result(
                actor=actor,
                project_id=project_id,
                upload_id=upload_id,
                result=payload.result,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return QuarantinedUploadResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/document-uploads/{upload_id}/requeue-processing",
        response_model=QuarantinedUploadResponse,
    )
    def requeue_document_processing(
        project_id: str,
        upload_id: str,
        payload: RequeueDocumentProcessingRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> QuarantinedUploadResponse:
        with mutation_transaction(session):
            result = quarantine_service(session).requeue_processing(
                actor=actor,
                project_id=project_id,
                upload_id=upload_id,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return QuarantinedUploadResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/document-set/confirm",
        response_model=ProjectView,
    )
    def confirm_document_set(
        project_id: str,
        payload: ConfirmDocumentSetRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ProjectView:
        with mutation_transaction(session):
            return service(session).confirm_document_set(
                actor=actor,
                project_id=project_id,
                candidate_id=payload.candidate_document_set_revision_id,
                request_id=request.state.request_id,
                reason=payload.reason,
            )

    @application.get(
        "/v1/projects/{project_id}/document-sets/{document_set_id}",
        response_model=DocumentSetResponse,
    )
    def get_document_set(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        document_set_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> DocumentSetResponse:
        result = service(session).document_set_view(
            actor=actor,
            project_id=project_id,
            document_set_id=document_set_id,
        )
        return DocumentSetResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/transitions",
        response_model=ProjectView,
    )
    def transition(
        project_id: str,
        payload: TransitionRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ProjectView:
        with mutation_transaction(session):
            return service(session).transition(
                actor=actor,
                project_id=project_id,
                to_state=payload.to_state,
                expected_row_version=payload.expected_row_version,
                request_id=request.state.request_id,
                reason=payload.reason,
            )

    @application.get(
        "/v1/projects/{project_id}/release-gates",
        response_model=ReleaseGateResponse,
    )
    def release_gates(
        project_id: str,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ReleaseGateResponse:
        actor.require_any(
            ActorRole.ESTIMATOR,
            ActorRole.REVIEWER,
            ActorRole.APPROVER,
            ActorRole.AUDITOR,
        )
        project, bid, bid_hash, internal, internal_hash = service(session).evaluate_release_gates(
            actor=actor,
            project_id=project_id,
        )
        return ReleaseGateResponse(
            project=project,
            decision=bid,
            gate_hash=bid_hash,
            internal_decision=internal,
            internal_gate_hash=internal_hash,
        )

    @application.post(
        "/v1/projects/{project_id}/release/bid",
        response_model=ReleaseAttemptResponse,
    )
    def release_bid(
        project_id: str,
        payload: ReleaseRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ReleaseAttemptResponse:
        with mutation_transaction(session):
            project, decision = service(session).attempt_bid_release(
                actor=actor,
                project_id=project_id,
                expected_row_version=payload.expected_row_version,
                expected_gate_hash=payload.gate_hash,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return ReleaseAttemptResponse(project=project, decision=decision)

    @application.post(
        "/v1/projects/{project_id}/release/internal",
        response_model=ReleaseAttemptResponse,
    )
    def release_internal(
        project_id: str,
        payload: ReleaseRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ReleaseAttemptResponse:
        with mutation_transaction(session):
            project, decision = service(session).attempt_internal_release(
                actor=actor,
                project_id=project_id,
                expected_row_version=payload.expected_row_version,
                expected_gate_hash=payload.gate_hash,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return ReleaseAttemptResponse(project=project, decision=decision)

    @application.post(
        "/v1/governance/versions",
        response_model=ControlledVersionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_controlled_version(
        payload: CreateControlledVersionRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ControlledVersionResponse:
        with mutation_transaction(session):
            version = governance_service(session).create_version(
                actor=actor,
                kind=payload.kind,
                version_label=payload.version_label,
                payload=payload.payload,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return ControlledVersionResponse.model_validate(version.model_dump())

    @application.post(
        "/v1/governance/versions/{version_id}/approve",
        response_model=ControlledVersionResponse,
    )
    def approve_controlled_version(
        version_id: str,
        payload: ApproveControlledVersionRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ControlledVersionResponse:
        with mutation_transaction(session):
            version = governance_service(session).approve_version(
                actor=actor,
                version_id=version_id,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return ControlledVersionResponse.model_validate(version.model_dump())

    @application.post(
        "/v1/governance/versions/{version_id}/activate-adapter-qualification",
        response_model=AdapterQualificationResponse,
    )
    def activate_adapter_qualification(
        version_id: str,
        payload: ActivateAdapterQualificationRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> AdapterQualificationResponse:
        with mutation_transaction(session):
            qualification = governance_service(session).activate_adapter_qualification(
                actor=actor,
                version_id=version_id,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return AdapterQualificationResponse(
            qualification_id=qualification.id,
            adapter_name=qualification.adapter_name,
            adapter_version=qualification.adapter_version,
            status=qualification.status,
            valid_until=(
                qualification.valid_until.isoformat() if qualification.valid_until else None
            ),
            test_evidence_hash=qualification.test_evidence_hash,
            approved_by=qualification.approved_by,
        )

    @application.post(
        "/v1/projects/{project_id}/controlled-versions/bind",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def bind_controlled_version(
        project_id: str,
        payload: BindControlledVersionRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> None:
        with mutation_transaction(session):
            governance_service(session).bind_to_project(
                actor=actor,
                project_id=project_id,
                version_id=payload.version_id,
                purpose=payload.purpose,
                request_id=request.state.request_id,
                reason=payload.reason,
            )

    @application.post(
        "/v1/qualification/business/campaigns",
        response_model=BusinessQualificationCampaignResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_business_qualification_campaign(
        payload: CreateBusinessQualificationCampaignRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> BusinessQualificationCampaignResponse:
        with mutation_transaction(session):
            campaign = business_qualification_service(session).create_campaign(
                actor=actor,
                profile_version_id=payload.profile_version_id,
                profile_content_hash=payload.profile_content_hash,
                dataset_version_id=payload.dataset_version_id,
                dataset_content_hash=payload.dataset_content_hash,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return BusinessQualificationCampaignResponse.model_validate(campaign.model_dump())

    @application.get(
        "/v1/qualification/business/campaigns/{campaign_id}",
        response_model=BusinessQualificationCampaignDetailResponse,
    )
    def get_business_qualification_campaign(
        campaign_id: str,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> BusinessQualificationCampaignDetailResponse:
        campaign = business_qualification_service(session).get_campaign_detail(
            actor=actor,
            campaign_id=campaign_id,
        )
        return BusinessQualificationCampaignDetailResponse.model_validate(campaign.model_dump())

    @application.post(
        "/v1/qualification/business/campaigns/{campaign_id}/cases/{case_id}/references/prepare",
        response_model=QualificationReferenceEvidenceResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def prepare_business_qualification_reference(
        campaign_id: str,
        case_id: str,
        payload: PrepareQualificationReferenceRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> QualificationReferenceEvidenceResponse:
        with mutation_transaction(session):
            evidence = business_qualification_service(session).prepare_reference_evidence(
                actor=actor,
                campaign_id=campaign_id,
                case_id=case_id,
                draft=payload.draft,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return QualificationReferenceEvidenceResponse.model_validate(evidence.model_dump())

    @application.post(
        "/v1/qualification/business/campaigns/{campaign_id}/cases/{case_id}/references/verify",
        response_model=BusinessQualificationCampaignResponse,
    )
    def verify_business_qualification_reference(
        campaign_id: str,
        case_id: str,
        payload: VerifyQualificationReferenceRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> BusinessQualificationCampaignResponse:
        with mutation_transaction(session):
            campaign = business_qualification_service(session).verify_and_register_reference(
                actor=actor,
                campaign_id=campaign_id,
                case_id=case_id,
                prepared_observation_id=payload.prepared_observation_id,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return BusinessQualificationCampaignResponse.model_validate(campaign.model_dump())

    @application.post(
        "/v1/qualification/business/campaigns/{campaign_id}/evaluate",
        response_model=BusinessQualificationEvaluationResponse,
    )
    def evaluate_business_qualification_campaign(
        campaign_id: str,
        payload: QualificationActionRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> BusinessQualificationEvaluationResponse:
        with mutation_transaction(session):
            evaluation = business_qualification_service(session).evaluate(
                actor=actor,
                campaign_id=campaign_id,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return BusinessQualificationEvaluationResponse.model_validate(evaluation.model_dump())

    @application.post(
        "/v1/qualification/business/campaigns/{campaign_id}/discrepancies/{discrepancy_id}/review",
        response_model=BusinessQualificationCampaignResponse,
    )
    def review_business_qualification_discrepancy(
        campaign_id: str,
        discrepancy_id: str,
        payload: ReviewQualificationDiscrepancyRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> BusinessQualificationCampaignResponse:
        with mutation_transaction(session):
            campaign = business_qualification_service(session).review_discrepancy(
                actor=actor,
                campaign_id=campaign_id,
                discrepancy_id=discrepancy_id,
                command=payload.command,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return BusinessQualificationCampaignResponse.model_validate(campaign.model_dump())

    @application.post(
        "/v1/qualification/business/campaigns/{campaign_id}/approve",
        response_model=BusinessQualificationCampaignResponse,
    )
    def approve_business_qualification_campaign(
        campaign_id: str,
        payload: QualificationActionRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> BusinessQualificationCampaignResponse:
        with mutation_transaction(session):
            campaign = business_qualification_service(session).approve_campaign(
                actor=actor,
                campaign_id=campaign_id,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return BusinessQualificationCampaignResponse.model_validate(campaign.model_dump())

    @application.post(
        "/v1/qualification/production-evidence/packages",
        response_model=ProductionGateEvidencePackageResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def submit_production_gate_evidence(
        payload: SubmitProductionGateEvidenceRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ProductionGateEvidencePackageResponse:
        with mutation_transaction(session):
            package = production_gate_evidence_service(session).submit_package(
                actor=actor,
                submission=payload.submission,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return ProductionGateEvidencePackageResponse.model_validate(package.model_dump())

    @application.get(
        "/v1/qualification/production-evidence/packages/{package_id}",
        response_model=ProductionGateEvidencePackageDetailResponse,
    )
    def get_production_gate_evidence(
        package_id: str,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ProductionGateEvidencePackageDetailResponse:
        package = production_gate_evidence_service(session).get_package(
            actor=actor,
            package_id=package_id,
        )
        return ProductionGateEvidencePackageDetailResponse.model_validate(package.model_dump())

    @application.post(
        "/v1/qualification/production-evidence/packages/{package_id}/review",
        response_model=ProductionGateEvidencePackageResponse,
    )
    def review_production_gate_evidence(
        package_id: str,
        payload: ReviewProductionGateEvidenceRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ProductionGateEvidencePackageResponse:
        with mutation_transaction(session):
            package = production_gate_evidence_service(session).review_package(
                actor=actor,
                package_id=package_id,
                command=payload.command,
                request_id=request.state.request_id,
            )
        return ProductionGateEvidencePackageResponse.model_validate(package.model_dump())

    @application.post(
        "/v1/qualification/production-evidence/packages/{package_id}/revoke",
        response_model=ProductionGateEvidencePackageResponse,
    )
    def revoke_production_gate_evidence(
        package_id: str,
        payload: RevokeProductionGateEvidenceRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ProductionGateEvidencePackageResponse:
        with mutation_transaction(session):
            package = production_gate_evidence_service(session).revoke_package(
                actor=actor,
                package_id=package_id,
                command=payload.command,
                request_id=request.state.request_id,
            )
        return ProductionGateEvidencePackageResponse.model_validate(package.model_dump())

    @application.post(
        "/v1/projects/{project_id}/evidence/observations",
        response_model=ObservationResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def record_observation(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        payload: RecordObservationRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ObservationResponse:
        with mutation_transaction(session):
            observation = evidence_service(session).record_observation(
                actor=actor,
                project_id=project_id,
                draft=payload.draft,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return ObservationResponse.model_validate(observation.model_dump())

    @application.get(
        "/v1/projects/{project_id}/evidence/manual/context",
        response_model=ManualEvidenceContextResponse,
    )
    def get_manual_evidence_context(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ManualEvidenceContextResponse:
        context = evidence_service(session).manual_evidence_context(
            actor=actor,
            project_id=project_id,
        )
        return ManualEvidenceContextResponse.model_validate(context.model_dump())

    @application.get(
        "/v1/projects/{project_id}/evidence/observations/{observation_id}/manual-review",
        response_model=ManualEvidenceReviewResponse,
    )
    def get_manual_evidence_review(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        observation_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ManualEvidenceReviewResponse:
        review = evidence_service(session).manual_evidence_review(
            actor=actor,
            project_id=project_id,
            observation_id=observation_id,
        )
        return ManualEvidenceReviewResponse.model_validate(review.model_dump())

    @application.post(
        "/v1/projects/{project_id}/evidence/observations/{observation_id}/manual-review/decision",
        response_model=ManualEvidenceDecisionResponse,
    )
    def decide_manual_evidence(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        observation_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        payload: DecideManualEvidenceRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ManualEvidenceDecisionResponse:
        with mutation_transaction(session):
            result = evidence_service(session).decide_manual_evidence(
                actor=actor,
                project_id=project_id,
                observation_id=observation_id,
                command=payload,
                request_id=request.state.request_id,
            )
        return ManualEvidenceDecisionResponse.model_validate(result.model_dump())

    @application.get(
        "/v1/projects/{project_id}/evidence/reconciliation-context",
        response_model=ReconciliationContextResponse,
    )
    def get_reconciliation_context(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
        field_name: Annotated[str | None, Query(max_length=300)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> ReconciliationContextResponse:
        context = evidence_service(session).reconciliation_context(
            actor=actor,
            project_id=project_id,
            field_name=field_name,
            limit=limit,
        )
        return ReconciliationContextResponse.model_validate(context.model_dump())

    @application.post(
        "/v1/projects/{project_id}/evidence/reconcile",
        response_model=ReconciliationResponse,
    )
    def reconcile_observations(
        project_id: str,
        payload: ReconcileObservationsRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ReconciliationResponse:
        with mutation_transaction(session):
            outcome = evidence_service(session).reconcile(
                actor=actor,
                project_id=project_id,
                observation_ids=payload.observation_ids,
                reconciliation_version_id=payload.reconciliation_version_id,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return ReconciliationResponse.model_validate(outcome.model_dump())

    @application.post(
        "/v1/projects/{project_id}/evidence/conflicts/{conflict_id}/resolve",
        response_model=ConflictResolutionResponse,
    )
    def resolve_evidence_conflict(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        conflict_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        payload: ResolveConflictRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ConflictResolutionResponse:
        with mutation_transaction(session):
            result = evidence_service(session).resolve_conflict(
                actor=actor,
                project_id=project_id,
                conflict_id=conflict_id,
                command=payload,
                request_id=request.state.request_id,
            )
        return ConflictResolutionResponse.model_validate(result.model_dump())

    @application.get(
        "/v1/projects/{project_id}/evidence/conflicts/{conflict_id}",
        response_model=ConflictReviewResponse,
    )
    def get_evidence_conflict(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        conflict_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ConflictReviewResponse:
        result = evidence_service(session).conflict_review(
            actor=actor,
            project_id=project_id,
            conflict_id=conflict_id,
        )
        return ConflictReviewResponse.model_validate(result.model_dump())

    @application.get(
        "/v1/projects/{project_id}/passport/context",
        response_model=PassportContextResponse,
    )
    def get_passport_context(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
        field_name: Annotated[
            str | None,
            Query(min_length=1, max_length=200),
        ] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> PassportContextResponse:
        context = passport_service(session).context(
            actor=actor,
            project_id=project_id,
            selected_field_name=field_name,
            limit=limit,
        )
        return PassportContextResponse.model_validate(context.model_dump())

    @application.post(
        "/v1/projects/{project_id}/passport/facts",
        response_model=PassportFactResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def submit_passport_fact(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        payload: SubmitPassportFactRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> PassportFactResponse:
        with mutation_transaction(session):
            fact = passport_service(session).submit_fact(
                actor=actor,
                project_id=project_id,
                draft=payload.draft,
                expected_document_set_revision_id=(payload.expected_document_set_revision_id),
                requirements_version_id=payload.requirements_version_id,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return PassportFactResponse.model_validate(fact.model_dump())

    @application.post(
        "/v1/projects/{project_id}/passport/facts/{fact_id}/verify",
        response_model=PassportFactVerificationResponse,
    )
    def verify_passport_fact(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        fact_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        payload: VerifyPassportFactRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> PassportFactVerificationResponse:
        with mutation_transaction(session):
            fact, validation = passport_service(session).verify_fact(
                actor=actor,
                project_id=project_id,
                fact_id=fact_id,
                expected_fact_updated_at=payload.expected_fact_updated_at,
                expected_task_updated_at=payload.expected_task_updated_at,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return PassportFactVerificationResponse(fact=fact, validation=validation)

    @application.post(
        "/v1/projects/{project_id}/passport/facts/{fact_id}/decision",
        response_model=PassportFactDecisionResponse,
    )
    def decide_passport_fact(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        fact_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        payload: DecidePassportFactRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> PassportFactDecisionResponse:
        with mutation_transaction(session):
            result = passport_service(session).decide_fact(
                actor=actor,
                project_id=project_id,
                fact_id=fact_id,
                command=payload,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return PassportFactDecisionResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/passport/validate",
        response_model=PassportValidationResponse,
    )
    def validate_project_passport(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        payload: ValidatePassportRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> PassportValidationResponse:
        with mutation_transaction(session):
            validation = passport_service(session).validate_current(
                actor=actor,
                project_id=project_id,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return PassportValidationResponse.model_validate(validation.model_dump())

    @application.get(
        "/v1/projects/{project_id}/boq/authoring-context",
        response_model=BoqAuthoringContextResponse,
    )
    def get_boq_authoring_context(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
        evidence_field_name: Annotated[str, Query(min_length=1, max_length=300)] = ("boq_line"),
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> BoqAuthoringContextResponse:
        context = boq_service(session).authoring_context(
            actor=actor,
            project_id=project_id,
            evidence_field_name=evidence_field_name,
            limit=limit,
        )
        return BoqAuthoringContextResponse.model_validate(context.model_dump())

    @application.post(
        "/v1/projects/{project_id}/boq/lines",
        response_model=BoqLineResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_boq_line(
        project_id: str,
        payload: CreateBoqLineRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> BoqLineResponse:
        with mutation_transaction(session):
            line = boq_service(session).create_line(
                actor=actor,
                project_id=project_id,
                draft=payload.draft,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return BoqLineResponse.model_validate(line.model_dump())

    @application.get(
        "/v1/projects/{project_id}/boq/lines/{line_id}/review",
        response_model=BoqLineReviewResponse,
    )
    def get_boq_line_review(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        line_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> BoqLineReviewResponse:
        review = boq_service(session).line_review(
            actor=actor,
            project_id=project_id,
            line_id=line_id,
        )
        return BoqLineReviewResponse.model_validate(review.model_dump())

    @application.post(
        "/v1/projects/{project_id}/boq/lines/{line_id}/verify",
        response_model=BoqLineResponse,
    )
    def verify_boq_line(
        project_id: str,
        line_id: str,
        payload: VerifyBoqLineRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> BoqLineResponse:
        with mutation_transaction(session):
            line = boq_service(session).verify_line(
                actor=actor,
                project_id=project_id,
                line_id=line_id,
                expected_line_updated_at=payload.expected_line_updated_at,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return BoqLineResponse.model_validate(line.model_dump())

    @application.post(
        "/v1/projects/{project_id}/boq/lines/{line_id}/quantities",
        response_model=QuantityExecutionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def record_quantity(
        project_id: str,
        line_id: str,
        payload: RecordQuantityRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> QuantityExecutionResponse:
        with mutation_transaction(session):
            result = boq_service(session).record_quantity(
                actor=actor,
                project_id=project_id,
                line_id=line_id,
                submission=payload.submission,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return QuantityExecutionResponse.model_validate(result.model_dump())

    @application.get(
        "/v1/projects/{project_id}/boq/lines/{line_id}/quantity-change-context",
        response_model=QuantityChangeContextResponse,
    )
    def get_quantity_change_context(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        line_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> QuantityChangeContextResponse:
        result = boq_service(session).quantity_change_context(
            actor=actor,
            project_id=project_id,
            line_id=line_id,
        )
        return QuantityChangeContextResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/boq/lines/{line_id}/quantity-change-proposals",
        response_model=QuantityManualChangeResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def propose_quantity_manual_change(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        line_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        payload: ProposeQuantityManualChangeRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> QuantityManualChangeResponse:
        with mutation_transaction(session):
            result = boq_service(session).propose_quantity_manual_change(
                actor=actor,
                project_id=project_id,
                line_id=line_id,
                submission=payload.submission,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return QuantityManualChangeResponse.model_validate(result.model_dump())

    @application.get(
        "/v1/projects/{project_id}/manual-changes/{change_id}",
        response_model=QuantityManualChangeResponse,
    )
    def get_quantity_manual_change(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        change_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> QuantityManualChangeResponse:
        result = boq_service(session).quantity_manual_change_review(
            actor=actor,
            project_id=project_id,
            change_id=change_id,
        )
        return QuantityManualChangeResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/manual-changes/{change_id}/apply",
        response_model=QuantityExecutionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def apply_quantity_manual_change(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        change_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        payload: ApplyQuantityManualChangeRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> QuantityExecutionResponse:
        with mutation_transaction(session):
            result = boq_service(session).apply_quantity_manual_change(
                actor=actor,
                project_id=project_id,
                change_id=change_id,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return QuantityExecutionResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/boq/scope-evaluations",
        response_model=ScopeRunResponse,
    )
    def run_scope_completeness(
        project_id: str,
        payload: RunScopeRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ScopeRunResponse:
        with mutation_transaction(session):
            result = boq_service(session).run_scope_completeness(
                actor=actor,
                project_id=project_id,
                wbs_node_id=payload.wbs_node_id,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return ScopeRunResponse.model_validate(result.model_dump())

    @application.get(
        "/v1/projects/{project_id}/nomenclature/context",
        response_model=NomenclatureContextResponse,
    )
    def get_nomenclature_context(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
        catalog_query: Annotated[str | None, Query(max_length=200)] = None,
        evidence_field_name: Annotated[str, Query(min_length=1, max_length=300)] = (
            "technical_attributes"
        ),
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> NomenclatureContextResponse:
        context = pricing_service(session).nomenclature_context(
            actor=actor,
            project_id=project_id,
            catalog_query=catalog_query,
            evidence_field_name=evidence_field_name,
            limit=limit,
        )
        return NomenclatureContextResponse.model_validate(context.model_dump())

    @application.get(
        "/v1/projects/{project_id}/nomenclature/{match_id}/review",
        response_model=NomenclatureReviewContextResponse,
    )
    def get_nomenclature_review_context(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        match_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> NomenclatureReviewContextResponse:
        context = pricing_service(session).nomenclature_review_context(
            actor=actor,
            project_id=project_id,
            match_id=match_id,
        )
        return NomenclatureReviewContextResponse.model_validate(context.model_dump())

    @application.post(
        "/v1/projects/{project_id}/nomenclature/assessments",
        response_model=NomenclatureMatchResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def assess_nomenclature(
        project_id: str,
        payload: AssessNomenclatureRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> NomenclatureMatchResponse:
        with mutation_transaction(session):
            result = pricing_service(session).assess_nomenclature(
                actor=actor,
                project_id=project_id,
                draft=payload.draft,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return NomenclatureMatchResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/nomenclature/{match_id}/analogue-proposals",
        response_model=NomenclatureMatchResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def propose_nomenclature_analogue(
        project_id: str,
        match_id: str,
        payload: ProposeAnalogueRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> NomenclatureMatchResponse:
        with mutation_transaction(session):
            result = pricing_service(session).propose_analogue(
                actor=actor,
                project_id=project_id,
                match_id=match_id,
                command=payload.command,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return NomenclatureMatchResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/nomenclature/{match_id}/finalize",
        response_model=NomenclatureMatchResponse,
    )
    def finalize_nomenclature_analogue(
        project_id: str,
        match_id: str,
        payload: FinalizeAnalogueRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> NomenclatureMatchResponse:
        with mutation_transaction(session):
            result = pricing_service(session).finalize_analogue(
                actor=actor,
                project_id=project_id,
                match_id=match_id,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return NomenclatureMatchResponse.model_validate(result.model_dump())

    @application.get(
        "/v1/projects/{project_id}/pricing/items/{item_id}/context",
        response_model=PriceItemContextResponse,
    )
    def get_price_item_context(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        item_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> PriceItemContextResponse:
        result = pricing_service(session).price_item_context(
            actor=actor,
            project_id=project_id,
            item_id=item_id,
        )
        return PriceItemContextResponse.model_validate(result.model_dump())

    @application.get(
        "/v1/projects/{project_id}/pricing/items/{item_id}/"
        "quote-candidates/{source_observation_id}",
        response_model=PriceQuoteCandidateResponse,
    )
    def get_price_quote_candidate(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        item_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
        source_observation_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> PriceQuoteCandidateResponse:
        result = pricing_service(session).price_quote_candidate(
            actor=actor,
            project_id=project_id,
            item_id=item_id,
            source_observation_id=source_observation_id,
        )
        return PriceQuoteCandidateResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/pricing/items/{item_id}/quotes/from-observation",
        response_model=PriceQuoteResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def record_price_quote_from_observation(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        item_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
        payload: RecordPriceQuoteFromObservationRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> PriceQuoteResponse:
        with mutation_transaction(session):
            result = pricing_service(session).record_quote_from_observation(
                actor=actor,
                project_id=project_id,
                item_id=item_id,
                source_observation_id=payload.source_observation_id,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return PriceQuoteResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/pricing/quotes",
        response_model=PriceQuoteResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def record_price_quote(
        project_id: str,
        payload: RecordPriceQuoteRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> PriceQuoteResponse:
        with mutation_transaction(session):
            result = pricing_service(session).record_quote(
                actor=actor,
                project_id=project_id,
                draft=payload.draft,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return PriceQuoteResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/pricing/normalize",
        response_model=NormalizedPriceResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def normalize_price(
        project_id: str,
        payload: NormalizePriceRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> NormalizedPriceResponse:
        with mutation_transaction(session):
            result = pricing_service(session).normalize_price(
                actor=actor,
                project_id=project_id,
                command=payload.command,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return NormalizedPriceResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/pricing/items/{item_id}/evaluate",
        response_model=PriceDecisionResponse,
    )
    def evaluate_item_price(
        project_id: str,
        item_id: str,
        payload: EvaluateItemPriceRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> PriceDecisionResponse:
        with mutation_transaction(session):
            result = pricing_service(session).evaluate_item_price(
                actor=actor,
                project_id=project_id,
                item_id=item_id,
                as_of=payload.as_of,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return PriceDecisionResponse.model_validate(result.model_dump())

    @application.get(
        "/v1/projects/{project_id}/contract/context",
        response_model=ContractContextResponse,
    )
    def get_contract_context(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
        kind: ContractTermKind | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> ContractContextResponse:
        result = contract_service(session).context(
            actor=actor,
            project_id=project_id,
            selected_kind=kind,
            limit=limit,
        )
        return ContractContextResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/contract/terms",
        response_model=ContractTermResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def submit_contract_term(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        payload: SubmitContractTermRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ContractTermResponse:
        with mutation_transaction(session):
            result = contract_service(session).submit_term(
                actor=actor,
                project_id=project_id,
                draft=payload.draft,
                expected_document_set_revision_id=(payload.expected_document_set_revision_id),
                rules_version_id=payload.rules_version_id,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return ContractTermResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/contract/terms/{term_id}/verify",
        response_model=ContractTermValidationResponse,
    )
    def verify_contract_term(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        term_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        payload: VerifyContractTermRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ContractTermValidationResponse:
        with mutation_transaction(session):
            term, validation = contract_service(session).verify_term(
                actor=actor,
                project_id=project_id,
                term_id=term_id,
                expected_term_updated_at=payload.expected_term_updated_at,
                expected_task_updated_at=payload.expected_task_updated_at,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return ContractTermValidationResponse(term=term, validation=validation)

    @application.post(
        "/v1/projects/{project_id}/contract/terms/{term_id}/decision",
        response_model=ContractTermDecisionResponse,
    )
    def decide_contract_term(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        term_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        payload: DecideContractTermRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ContractTermDecisionResponse:
        with mutation_transaction(session):
            result = contract_service(session).decide_term(
                actor=actor,
                project_id=project_id,
                term_id=term_id,
                command=payload,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return ContractTermDecisionResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/contract/terms/{term_id}/cost-impact-proposals",
        response_model=ContractTermResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def propose_contract_cost_impact(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        term_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        payload: ProposeContractCostImpactRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ContractTermResponse:
        with mutation_transaction(session):
            result = contract_service(session).propose_cost_impact(
                actor=actor,
                project_id=project_id,
                term_id=term_id,
                command=payload.command,
                expected_term_updated_at=payload.expected_term_updated_at,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return ContractTermResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/contract/terms/{term_id}/cost-impact/finalize",
        response_model=ContractTermValidationResponse,
    )
    def finalize_contract_cost_impact(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        term_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        payload: FinalizeContractCostImpactRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ContractTermValidationResponse:
        with mutation_transaction(session):
            term, validation = contract_service(session).finalize_cost_impact(
                actor=actor,
                project_id=project_id,
                term_id=term_id,
                expected_term_updated_at=payload.expected_term_updated_at,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return ContractTermValidationResponse(term=term, validation=validation)

    @application.post(
        "/v1/projects/{project_id}/contract/validate",
        response_model=ContractValidationResponse,
    )
    def validate_contract(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        payload: ValidateContractRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ContractValidationResponse:
        with mutation_transaction(session):
            result = contract_service(session).validate_current(
                actor=actor,
                project_id=project_id,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return ContractValidationResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/commercial-costs/models",
        response_model=CommercialCostProposalResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def propose_commercial_cost_model(
        project_id: str,
        payload: ProposeCommercialCostModelRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> CommercialCostProposalResponse:
        with mutation_transaction(session):
            result = commercial_cost_service(session).propose(
                actor=actor,
                project_id=project_id,
                model=payload.model,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return CommercialCostProposalResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/commercial-costs/models/{model_id}/finalize",
        response_model=CommercialCostModelResponse,
    )
    def finalize_commercial_cost_model(
        project_id: str,
        model_id: str,
        payload: FinalizeCommercialCostModelRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> CommercialCostModelResponse:
        with mutation_transaction(session):
            result = commercial_cost_service(session).finalize(
                actor=actor,
                project_id=project_id,
                model_id=model_id,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return CommercialCostModelResponse.model_validate(result.model_dump())

    @application.get(
        "/v1/projects/{project_id}/commercial-costs/models/{model_id}",
        response_model=CommercialCostModelResponse,
    )
    def get_commercial_cost_model(
        project_id: str,
        model_id: str,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> CommercialCostModelResponse:
        result = commercial_cost_service(session).get(
            actor=actor,
            project_id=project_id,
            model_id=model_id,
        )
        return CommercialCostModelResponse.model_validate(result.model_dump())

    @application.get(
        "/v1/projects/{project_id}/risks/context",
        response_model=RiskContextResponse,
    )
    def get_risk_context(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
        risk_key: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> RiskContextResponse:
        result = risk_service(session).context(
            actor=actor,
            project_id=project_id,
            selected_risk_key=risk_key,
            limit=limit,
        )
        return RiskContextResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/risks",
        response_model=RiskItemResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def submit_risk_item(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        payload: SubmitRiskItemRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> RiskItemResponse:
        with mutation_transaction(session):
            result = risk_service(session).submit_risk(
                actor=actor,
                project_id=project_id,
                draft=payload.draft,
                expected_document_set_revision_id=(
                    payload.expected_document_set_revision_id
                ),
                risk_model_version_id=payload.risk_model_version_id,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return RiskItemResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/risks/{risk_item_id}/verify",
        response_model=RiskItemResponse,
    )
    def verify_risk_item(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        risk_item_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        payload: VerifyRiskItemRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> RiskItemResponse:
        with mutation_transaction(session):
            result = risk_service(session).verify_risk(
                actor=actor,
                project_id=project_id,
                risk_item_id=risk_item_id,
                expected_risk_updated_at=payload.expected_risk_updated_at,
                expected_task_updated_at=payload.expected_task_updated_at,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return RiskItemResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/risks/{risk_item_id}/decision",
        response_model=RiskItemDecisionResponse,
    )
    def decide_risk_item(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        risk_item_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        payload: DecideRiskItemRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> RiskItemDecisionResponse:
        with mutation_transaction(session):
            result = risk_service(session).decide_risk(
                actor=actor,
                project_id=project_id,
                risk_item_id=risk_item_id,
                command=payload.command,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return RiskItemDecisionResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/risks/calculate",
        response_model=RiskCalculationResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def calculate_risk_reserve(
        project_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
        payload: CalculateRiskReserveRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> RiskCalculationResponse:
        with mutation_transaction(session):
            result = risk_service(session).calculate_reserve(
                actor=actor,
                project_id=project_id,
                expected_document_set_revision_id=(
                    payload.expected_document_set_revision_id
                ),
                risk_model_version_id=payload.risk_model_version_id,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return RiskCalculationResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/actuals",
        response_model=ActualRecordResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def record_actual(
        project_id: str,
        payload: RecordActualRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ActualRecordResponse:
        with mutation_transaction(session):
            result = actuals_service(session).record_actual(
                actor=actor,
                project_id=project_id,
                draft=payload.draft,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return ActualRecordResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/actuals/{actual_id}/verify",
        response_model=ActualRecordResponse,
    )
    def verify_actual(
        project_id: str,
        actual_id: str,
        payload: VerifyActualRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ActualRecordResponse:
        with mutation_transaction(session):
            result = actuals_service(session).verify_actual(
                actor=actor,
                project_id=project_id,
                actual_id=actual_id,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return ActualRecordResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/actuals/{actual_id}/compare",
        response_model=ActualComparisonResponse,
    )
    def compare_actual(
        project_id: str,
        actual_id: str,
        payload: CompareActualRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ActualComparisonResponse:
        with mutation_transaction(session):
            result = actuals_service(session).compare_to_forecast(
                actor=actor,
                project_id=project_id,
                actual_id=actual_id,
                command=payload.command,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return ActualComparisonResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/calibration/{example_id}/approve",
        response_model=CalibrationApprovalResponse,
    )
    def approve_calibration_example(
        project_id: str,
        example_id: str,
        payload: ApproveCalibrationRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> CalibrationApprovalResponse:
        with mutation_transaction(session):
            example = actuals_service(session).approve_calibration_example(
                actor=actor,
                project_id=project_id,
                example_id=example_id,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return CalibrationApprovalResponse(example_id=example.example_id, approved=True)

    @application.post(
        "/v1/projects/{project_id}/approvals/plan",
        response_model=ApprovalPlanResponse,
    )
    def build_approval_plan(
        project_id: str,
        payload: BuildApprovalPlanRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ApprovalPlanResponse:
        with mutation_transaction(session):
            result = approval_service(session).plan(
                actor=actor,
                project_id=project_id,
                subjects=payload.subjects,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return ApprovalPlanResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/approvals/{task_id}/decision",
        response_model=ApprovalDecisionResponse,
    )
    def decide_approval(
        project_id: str,
        task_id: str,
        payload: DecideApprovalRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ApprovalDecisionResponse:
        with mutation_transaction(session):
            decision = approval_service(session).decide(
                actor=actor,
                project_id=project_id,
                task_id=task_id,
                command=payload,
                request_id=request.state.request_id,
            )
        return ApprovalDecisionResponse.model_validate(decision.model_dump())

    @application.post(
        "/v1/projects/{project_id}/calculations",
        response_model=CalculationExecutionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def execute_calculation(
        project_id: str,
        payload: CalculationExecutionRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> CalculationExecutionResponse:
        with mutation_transaction(session):
            result = CalculationService(
                session=session,
                settings=resolved_settings,
                object_store=resolved_store,
            ).execute(
                actor=actor,
                project_id=project_id,
                expected_row_version=payload.expected_row_version,
                inputs=payload.inputs,
                policy=payload.policy,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return CalculationExecutionResponse.model_validate(result.model_dump())

    @application.get(
        "/v1/projects/{project_id}/calculation-context",
        response_model=CalculationContextResponse,
    )
    def calculation_context(
        project_id: str,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> CalculationContextResponse:
        context = CalculationService(
            session=session,
            settings=resolved_settings,
            object_store=resolved_store,
        ).context(
            actor=actor,
            project_id=project_id,
        )
        return CalculationContextResponse.model_validate(context.model_dump())

    @application.post(
        "/v1/projects/{project_id}/calculations/current",
        response_model=CalculationExecutionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def execute_current_calculation(
        project_id: str,
        payload: CurrentCalculationExecutionRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> CalculationExecutionResponse:
        with mutation_transaction(session):
            result = CalculationService(
                session=session,
                settings=resolved_settings,
                object_store=resolved_store,
            ).execute_current(
                actor=actor,
                project_id=project_id,
                expected_row_version=payload.expected_row_version,
                candidate_hash=payload.candidate_hash,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return CalculationExecutionResponse.model_validate(result.model_dump())

    @application.get(
        "/v1/projects/{project_id}/scenarios/context",
        response_model=ScenarioContextResponse,
    )
    def get_scenario_context(
        project_id: str,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
        snapshot_id: Annotated[str | None, Query(max_length=64)] = None,
    ) -> ScenarioContextResponse:
        result = scenario_service(session).context(
            actor=actor,
            project_id=project_id,
            snapshot_id=snapshot_id,
        )
        return ScenarioContextResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/scenarios/calculate",
        response_model=ScenarioExecutionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def execute_scenario(
        project_id: str,
        payload: CalculateScenarioRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ScenarioExecutionResponse:
        with mutation_transaction(session):
            result = scenario_service(session).execute(
                actor=actor,
                project_id=project_id,
                command=payload.command,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return ScenarioExecutionResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/exports",
        response_model=ExportArtifactResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def generate_export(
        project_id: str,
        payload: GenerateExportRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ExportArtifactResponse:
        with mutation_transaction(session):
            artifact = export_service(session).generate(
                actor=actor,
                project_id=project_id,
                snapshot_id=payload.snapshot_id,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return ExportArtifactResponse.model_validate(artifact.model_dump())

    @application.get(
        "/v1/projects/{project_id}/exports/{artifact_id}",
        response_model=ExportArtifactResponse,
    )
    def get_export(
        project_id: str,
        artifact_id: str,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ExportArtifactResponse:
        artifact = export_service(session).get(
            actor=actor,
            project_id=project_id,
            artifact_id=artifact_id,
        )
        return ExportArtifactResponse.model_validate(artifact.model_dump())

    @application.get(
        "/v1/projects/{project_id}/exports/{artifact_id}/verify",
        response_model=ExportVerificationResponse,
    )
    def verify_export(
        project_id: str,
        artifact_id: str,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ExportVerificationResponse:
        verification = export_service(session).verify(
            actor=actor,
            project_id=project_id,
            artifact_id=artifact_id,
        )
        return ExportVerificationResponse.model_validate(verification.model_dump())

    @application.get(
        "/v1/projects/{project_id}/exports/{artifact_id}/content",
        response_class=Response,
    )
    def download_export(
        project_id: str,
        artifact_id: str,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> Response:
        artifact, content = export_service(session).content(
            actor=actor,
            project_id=project_id,
            artifact_id=artifact_id,
        )
        return Response(
            content=content,
            media_type=artifact.media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{artifact.filename}"',
                "ETag": f'"{artifact.object_hash}"',
            },
        )

    @application.get(
        "/v1/projects/{project_id}/snapshots/{snapshot_id}/lineage",
        response_model=SnapshotLineageResponse,
    )
    def snapshot_lineage(
        project_id: str,
        snapshot_id: str,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> SnapshotLineageResponse:
        lineage = lineage_service(session).snapshot_lineage(
            actor=actor,
            project_id=project_id,
            snapshot_id=snapshot_id,
        )
        return SnapshotLineageResponse.model_validate(lineage.model_dump())

    if operator_ui_dist is not None:
        asset_directory = operator_ui_dist / "assets"
        if not asset_directory.is_dir():
            raise RuntimeError("Operator UI assets directory is unavailable")
        application.mount(
            "/assets",
            StaticFiles(directory=asset_directory),
            name="operator-ui-assets",
        )

        def ui_index() -> FileResponse:
            return FileResponse(operator_ui_dist / "index.html")

        application.add_api_route(
            "/",
            ui_index,
            methods=["GET"],
            include_in_schema=False,
            response_class=FileResponse,
        )
        for route in (
            "/auth/callback",
            "/auth/signout-callback",
            "/tasks",
            "/tasks/{ui_path:path}",
            "/projects/{ui_path:path}",
        ):
            application.add_api_route(
                route,
                ui_index,
                methods=["GET"],
                include_in_schema=False,
                response_class=FileResponse,
            )
        favicon = operator_ui_dist / "favicon.svg"
        if favicon.is_file():

            def ui_favicon() -> FileResponse:
                return FileResponse(favicon, media_type="image/svg+xml")

            application.add_api_route(
                "/favicon.svg",
                ui_favicon,
                methods=["GET"],
                include_in_schema=False,
                response_class=FileResponse,
            )

    return application


def _readiness_string(value: object, max_length: int) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value) <= max_length
    )


def _readiness_topics(value: object) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and len(value) == len(set(value))
        and all(
            isinstance(item, str)
            and _readiness_string(item, 200)
            and (item[0].islower() or item[0].isdigit())
            and all(
                character.islower() or character.isdigit() or character in "._-"
                for character in item
            )
            for item in value
        )
    )


app = create_app()
