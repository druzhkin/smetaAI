from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi import (
    Path as ApiPath,
)
from fastapi.responses import JSONResponse
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware

from tenderguard.application.actuals import ActualsService
from tenderguard.application.approvals import ApprovalService
from tenderguard.application.audit_integrity import AuditIntegrityService
from tenderguard.application.boq import BoqService
from tenderguard.application.calculations import CalculationService
from tenderguard.application.contracts import ContractService
from tenderguard.application.evidence import EvidenceService
from tenderguard.application.exports import ExportIntegrityError, ExportPackageService
from tenderguard.application.governance import GovernanceService
from tenderguard.application.lineage import LineageService
from tenderguard.application.passport import PassportService
from tenderguard.application.pricing import PricingService
from tenderguard.application.projects import (
    OptimisticLockError,
    ProjectMembershipView,
    ProjectNotFoundError,
    ProjectService,
    ProjectView,
)
from tenderguard.application.quarantine import QuarantineService
from tenderguard.application.risks import RiskService
from tenderguard.application.scenarios import ScenarioService
from tenderguard.config import Settings, get_settings
from tenderguard.domain.common import utc_now
from tenderguard.domain.enums import ActorRole
from tenderguard.domain.exports import load_signing_material
from tenderguard.domain.release import evaluate_bid_release
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
    ActivateAdapterQualificationRequest,
    ActualComparisonResponse,
    ActualRecordResponse,
    AdapterQualificationResponse,
    ApprovalDecisionResponse,
    ApprovalPlanResponse,
    ApproveCalibrationRequest,
    ApproveControlledVersionRequest,
    AssessNomenclatureRequest,
    AuditAnchorReceiptResponse,
    AuditAnchorStatusResponse,
    AuditCheckpointResponse,
    BindControlledVersionRequest,
    BoqLineResponse,
    BuildApprovalPlanRequest,
    CalculateRiskReserveRequest,
    CalculateScenarioRequest,
    CalculationExecutionRequest,
    CalculationExecutionResponse,
    CalibrationApprovalResponse,
    CompareActualRequest,
    ConfirmDocumentSetRequest,
    ConflictResolutionResponse,
    ContractTermResponse,
    ContractTermValidationResponse,
    ContractValidationResponse,
    ControlledVersionResponse,
    CreateAuditCheckpointRequest,
    CreateBoqLineRequest,
    CreateControlledVersionRequest,
    CreateProjectRequest,
    DecideApprovalRequest,
    EvaluateItemPriceRequest,
    ExportArtifactResponse,
    ExportVerificationResponse,
    FinalizeAnalogueRequest,
    FinalizeContractCostImpactRequest,
    GenerateExportRequest,
    GrantProjectMembershipRequest,
    NomenclatureMatchResponse,
    NormalizedPriceResponse,
    NormalizePriceRequest,
    ObservationResponse,
    PassportFactResponse,
    PassportFactVerificationResponse,
    PassportValidationResponse,
    PriceDecisionResponse,
    PriceQuoteResponse,
    ProjectMembershipResponse,
    ProposeAnalogueRequest,
    ProposeContractCostImpactRequest,
    QuantityExecutionResponse,
    QuarantinedUploadResponse,
    ReadinessResponse,
    ReconcileObservationsRequest,
    ReconciliationResponse,
    RecordActualRequest,
    RecordMalwareScanResultRequest,
    RecordObservationRequest,
    RecordPriceQuoteRequest,
    RecordQuantityRequest,
    RegisterAuditAnchorReceiptRequest,
    ReleaseAttemptResponse,
    ReleaseGateResponse,
    ReleaseRequest,
    RequeueDocumentProcessingRequest,
    ResolveConflictRequest,
    RevokeProjectMembershipRequest,
    RiskCalculationResponse,
    RiskItemResponse,
    RunScopeRequest,
    ScenarioExecutionResponse,
    ScopeRunResponse,
    SnapshotLineageResponse,
    SubmitContractTermRequest,
    SubmitPassportFactRequest,
    SubmitRiskItemRequest,
    TransitionRequest,
    ValidateContractRequest,
    ValidatePassportRequest,
    VerifyActualRequest,
    VerifyBoqLineRequest,
    VerifyContractTermRequest,
    VerifyPassportFactRequest,
    VerifyRiskItemRequest,
)
from .security import RequestSizeLimitMiddleware, SecurityHeadersMiddleware


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
        path_suffix_limits={
            "/scan-results": resolved_settings.max_scan_report_bytes + 64 * 1024,
        },
    )
    application.add_middleware(SecurityHeadersMiddleware)
    application.add_middleware(RequestLoggingMiddleware)
    application.state.settings = resolved_settings
    application.state.engine = resolved_engine
    application.state.session_factory = session_factory
    application.state.object_store = resolved_store
    application.state.quarantine_store = resolved_quarantine_store
    application.state.authenticator = authenticator

    def get_session() -> Iterator[Session]:
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    def get_actor(
        authorization: Annotated[str | None, Header()] = None,
        x_dev_actor: Annotated[str | None, Header()] = None,
        x_dev_organization: Annotated[str | None, Header()] = None,
        x_dev_roles: Annotated[str | None, Header()] = None,
    ) -> Actor:
        return authenticator.authenticate(
            authorization=authorization,
            dev_actor=x_dev_actor,
            dev_organization=x_dev_organization,
            dev_roles=x_dev_roles,
        )

    def service(session: Session) -> ProjectService:
        return ProjectService(
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
        ready = bool(
            database_ok
            and schema_current
            and store_ok
            and store_worm_ok
            and quarantine_store_ok
            and auth_configured
            and normative_engine_qualified
            and audit_anchor_valid
            and malware_scanner_qualified
            and document_processor_qualified
            and export_signing_configured
        )
        if not ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessResponse(
            ready=ready,
            database=database_ok,
            schema_current=schema_current,
            object_store=store_ok,
            object_store_worm=store_worm_ok,
            quarantine_store=quarantine_store_ok,
            authentication_configured=bool(auth_configured),
            audit_anchor_valid=audit_anchor_valid,
            normative_engine_qualified=normative_engine_qualified,
            malware_scanner_qualified=malware_scanner_qualified,
            document_processor_qualified=document_processor_qualified,
            export_signing_configured=export_signing_configured,
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
        with session.begin():
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
        with session.begin():
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
        with session.begin():
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
        with session.begin():
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
        with session.begin():
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
            with session.begin():
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
        with session.begin():
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
        with session.begin():
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
        with session.begin():
            return service(session).confirm_document_set(
                actor=actor,
                project_id=project_id,
                candidate_id=payload.candidate_document_set_revision_id,
                request_id=request.state.request_id,
                reason=payload.reason,
            )

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
        with session.begin():
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
        context = service(session).evaluate_release(actor=actor, project_id=project_id)
        return ReleaseGateResponse(decision=evaluate_bid_release(context))

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
        with session.begin():
            project, decision = service(session).attempt_bid_release(
                actor=actor,
                project_id=project_id,
                expected_row_version=payload.expected_row_version,
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
        with session.begin():
            project, decision = service(session).attempt_internal_release(
                actor=actor,
                project_id=project_id,
                expected_row_version=payload.expected_row_version,
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
        with session.begin():
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
        with session.begin():
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
        with session.begin():
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
        with session.begin():
            governance_service(session).bind_to_project(
                actor=actor,
                project_id=project_id,
                version_id=payload.version_id,
                purpose=payload.purpose,
                request_id=request.state.request_id,
                reason=payload.reason,
            )

    @application.post(
        "/v1/projects/{project_id}/evidence/observations",
        response_model=ObservationResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def record_observation(
        project_id: str,
        payload: RecordObservationRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ObservationResponse:
        with session.begin():
            observation = evidence_service(session).record_observation(
                actor=actor,
                project_id=project_id,
                draft=payload.draft,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return ObservationResponse.model_validate(observation.model_dump())

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
        with session.begin():
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
        project_id: str,
        conflict_id: str,
        payload: ResolveConflictRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ConflictResolutionResponse:
        with session.begin():
            result = evidence_service(session).resolve_conflict(
                actor=actor,
                project_id=project_id,
                conflict_id=conflict_id,
                command=payload,
                request_id=request.state.request_id,
            )
        return ConflictResolutionResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/passport/facts",
        response_model=PassportFactResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def submit_passport_fact(
        project_id: str,
        payload: SubmitPassportFactRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> PassportFactResponse:
        with session.begin():
            fact = passport_service(session).submit_fact(
                actor=actor,
                project_id=project_id,
                draft=payload.draft,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return PassportFactResponse.model_validate(fact.model_dump())

    @application.post(
        "/v1/projects/{project_id}/passport/facts/{fact_id}/verify",
        response_model=PassportFactVerificationResponse,
    )
    def verify_passport_fact(
        project_id: str,
        fact_id: str,
        payload: VerifyPassportFactRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> PassportFactVerificationResponse:
        with session.begin():
            fact, validation = passport_service(session).verify_fact(
                actor=actor,
                project_id=project_id,
                fact_id=fact_id,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return PassportFactVerificationResponse(fact=fact, validation=validation)

    @application.post(
        "/v1/projects/{project_id}/passport/validate",
        response_model=PassportValidationResponse,
    )
    def validate_project_passport(
        project_id: str,
        payload: ValidatePassportRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> PassportValidationResponse:
        with session.begin():
            validation = passport_service(session).validate_current(
                actor=actor,
                project_id=project_id,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return PassportValidationResponse.model_validate(validation.model_dump())

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
        with session.begin():
            line = boq_service(session).create_line(
                actor=actor,
                project_id=project_id,
                draft=payload.draft,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return BoqLineResponse.model_validate(line.model_dump())

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
        with session.begin():
            line = boq_service(session).verify_line(
                actor=actor,
                project_id=project_id,
                line_id=line_id,
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
        with session.begin():
            result = boq_service(session).record_quantity(
                actor=actor,
                project_id=project_id,
                line_id=line_id,
                submission=payload.submission,
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
        with session.begin():
            result = boq_service(session).run_scope_completeness(
                actor=actor,
                project_id=project_id,
                wbs_node_id=payload.wbs_node_id,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return ScopeRunResponse.model_validate(result.model_dump())

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
        with session.begin():
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
        with session.begin():
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
        with session.begin():
            result = pricing_service(session).finalize_analogue(
                actor=actor,
                project_id=project_id,
                match_id=match_id,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return NomenclatureMatchResponse.model_validate(result.model_dump())

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
        with session.begin():
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
        with session.begin():
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
        with session.begin():
            result = pricing_service(session).evaluate_item_price(
                actor=actor,
                project_id=project_id,
                item_id=item_id,
                as_of=payload.as_of,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return PriceDecisionResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/contract/terms",
        response_model=ContractTermResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def submit_contract_term(
        project_id: str,
        payload: SubmitContractTermRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ContractTermResponse:
        with session.begin():
            result = contract_service(session).submit_term(
                actor=actor,
                project_id=project_id,
                draft=payload.draft,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return ContractTermResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/contract/terms/{term_id}/verify",
        response_model=ContractTermValidationResponse,
    )
    def verify_contract_term(
        project_id: str,
        term_id: str,
        payload: VerifyContractTermRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ContractTermValidationResponse:
        with session.begin():
            term, validation = contract_service(session).verify_term(
                actor=actor,
                project_id=project_id,
                term_id=term_id,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return ContractTermValidationResponse(term=term, validation=validation)

    @application.post(
        "/v1/projects/{project_id}/contract/terms/{term_id}/cost-impact-proposals",
        response_model=ContractTermResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def propose_contract_cost_impact(
        project_id: str,
        term_id: str,
        payload: ProposeContractCostImpactRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ContractTermResponse:
        with session.begin():
            result = contract_service(session).propose_cost_impact(
                actor=actor,
                project_id=project_id,
                term_id=term_id,
                command=payload.command,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return ContractTermResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/contract/terms/{term_id}/cost-impact/finalize",
        response_model=ContractTermValidationResponse,
    )
    def finalize_contract_cost_impact(
        project_id: str,
        term_id: str,
        payload: FinalizeContractCostImpactRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ContractTermValidationResponse:
        with session.begin():
            term, validation = contract_service(session).finalize_cost_impact(
                actor=actor,
                project_id=project_id,
                term_id=term_id,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return ContractTermValidationResponse(term=term, validation=validation)

    @application.post(
        "/v1/projects/{project_id}/contract/validate",
        response_model=ContractValidationResponse,
    )
    def validate_contract(
        project_id: str,
        payload: ValidateContractRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ContractValidationResponse:
        with session.begin():
            result = contract_service(session).validate_current(
                actor=actor,
                project_id=project_id,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return ContractValidationResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/risks",
        response_model=RiskItemResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def submit_risk_item(
        project_id: str,
        payload: SubmitRiskItemRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> RiskItemResponse:
        with session.begin():
            result = risk_service(session).submit_risk(
                actor=actor,
                project_id=project_id,
                draft=payload.draft,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return RiskItemResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/risks/{risk_item_id}/verify",
        response_model=RiskItemResponse,
    )
    def verify_risk_item(
        project_id: str,
        risk_item_id: str,
        payload: VerifyRiskItemRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> RiskItemResponse:
        with session.begin():
            result = risk_service(session).verify_risk(
                actor=actor,
                project_id=project_id,
                risk_item_id=risk_item_id,
                request_id=request.state.request_id,
                reason=payload.reason,
            )
        return RiskItemResponse.model_validate(result.model_dump())

    @application.post(
        "/v1/projects/{project_id}/risks/calculate",
        response_model=RiskCalculationResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def calculate_risk_reserve(
        project_id: str,
        payload: CalculateRiskReserveRequest,
        request: Request,
        actor: Annotated[Actor, Depends(get_actor)],
        session: Annotated[Session, Depends(get_session)],
    ) -> RiskCalculationResponse:
        with session.begin():
            result = risk_service(session).calculate_reserve(
                actor=actor,
                project_id=project_id,
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
        with session.begin():
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
        with session.begin():
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
        with session.begin():
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
        with session.begin():
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
        with session.begin():
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
        with session.begin():
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
        with session.begin():
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
        with session.begin():
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
        with session.begin():
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

    return application


app = create_app()
