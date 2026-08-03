from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text

from tenderguard.application.audit_integrity import AuditIntegrityService
from tenderguard.application.load_qualification import LoadQualificationService
from tenderguard.application.operational_qualification import (
    build_result_envelope,
    load_approved_profile,
    read_json_object,
    write_result_exclusive,
)
from tenderguard.application.projects import ProjectService
from tenderguard.application.recovery_verification import RecoveryVerificationService
from tenderguard.config import get_settings
from tenderguard.domain.common import utc_now
from tenderguard.domain.jobs import DispatchDisposition
from tenderguard.domain.operational_qualification import (
    LoadProfile,
    QualificationFinding,
    QualificationResultEnvelope,
    RecoveryExerciseManifest,
    RecoveryProfile,
)
from tenderguard.infrastructure.database import (
    CURRENT_SCHEMA_REVISION,
    create_database_engine,
    create_session_factory,
)
from tenderguard.infrastructure.object_store import (
    build_object_store,
    build_quarantine_store,
)
from tenderguard.integrations.fgiscs_public import (
    FgisCsMaterialLookupRequest,
    FgisCsPublicApi,
    FgisCsPublicApiError,
)


def doctor() -> int:
    settings = get_settings()
    checks: dict[str, bool | str] = {
        "environment": settings.app_env,
        "build_identified": bool(settings.application_build_reference),
        "database": False,
        "schema_current": False,
        "object_store": False,
        "object_store_worm": False,
        "quarantine_store": False,
        "audit_anchor_valid": False,
        "idempotency_enforced": settings.require_idempotency_keys,
        "oidc_configured": bool(
            settings.allow_insecure_dev_auth
            or (settings.oidc_issuer and settings.oidc_audience and settings.oidc_jwks_url)
        ),
        "normative_adapter_configured": settings.normative_adapter_configured,
        "normative_engine_qualified": False,
    }
    engine = create_database_engine(settings)
    object_store = None
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one_or_none()
        checks["database"] = True
        checks["schema_current"] = revision == CURRENT_SCHEMA_REVISION
    except Exception as error:
        checks["database_error"] = type(error).__name__
    try:
        object_store = build_object_store(settings)
        checks["object_store"] = object_store.healthcheck()
        if settings.worm_policy_configured:
            assert settings.s3_required_object_lock_mode is not None
            assert settings.s3_minimum_retention_days is not None
            checks["object_store_worm"] = object_store.retention_status().satisfies(
                required_mode=settings.s3_required_object_lock_mode,
                minimum_days=settings.s3_minimum_retention_days,
            )
    except Exception as error:
        checks["object_store_error"] = type(error).__name__
    try:
        checks["quarantine_store"] = build_quarantine_store(settings).healthcheck()
    except Exception as error:
        checks["quarantine_store_error"] = type(error).__name__
    if checks["database"] is True and checks["schema_current"] is True and object_store is not None:
        try:
            with create_session_factory(engine)() as session:
                project_service = ProjectService(
                    session=session,
                    settings=settings,
                    object_store=object_store,
                )
                checks["normative_engine_qualified"] = project_service.normative_engine_qualified()
                anchor_status = AuditIntegrityService(
                    session=session,
                    settings=settings,
                    object_store=object_store,
                ).anchor_status()
                checks["audit_anchor_valid"] = anchor_status.valid
                if anchor_status.reasons:
                    checks["audit_anchor_reasons"] = "; ".join(anchor_status.reasons)
        except Exception as error:
            checks["governed_readiness_error"] = type(error).__name__
    engine.dispose()
    print(json.dumps(checks, ensure_ascii=False, indent=2, sort_keys=True))
    return (
        0
        if all(
            checks[key] is True
            for key in (
                "database",
                "build_identified",
                "schema_current",
                "object_store",
                "object_store_worm",
                "quarantine_store",
                "audit_anchor_valid",
                "idempotency_enforced",
                "oidc_configured",
                "normative_engine_qualified",
            )
        )
        else 1
    )


def process_quarantined_upload(upload_id: str) -> int:
    try:
        from tenderguard.application.document_jobs import DocumentIntakeDispatcher
    except ModuleNotFoundError as error:
        if error.name not in {"PIL", "openpyxl", "pypdf"}:
            raise
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "detail": (
                        "document-worker parser dependencies are not installed; "
                        "use the document-worker image"
                    ),
                },
                sort_keys=True,
            )
        )
        return 2

    settings = get_settings()
    if not settings.document_worker_actor_id:
        print(
            json.dumps(
                {"status": "BLOCKED", "detail": "document worker actor is not configured"},
                sort_keys=True,
            )
        )
        return 2
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)
    evidence_store = build_object_store(settings)
    quarantine_store = build_quarantine_store(settings)
    try:
        result = DocumentIntakeDispatcher(
            session_factory=factory,
            settings=settings,
            evidence_store=evidence_store,
            quarantine_store=quarantine_store,
        ).dispatch_next(
            worker_id=f"document-worker-instance-{uuid4()}",
            upload_id=upload_id,
        )
        print(result.model_dump_json(indent=2))
        return 0 if result.disposition is DispatchDisposition.PROCESSED else 2
    finally:
        engine.dispose()


def dispatch_document_intake(max_events: int) -> int:
    try:
        from tenderguard.application.document_jobs import DocumentIntakeDispatcher
    except ModuleNotFoundError as error:
        if error.name not in {"PIL", "openpyxl", "pypdf"}:
            raise
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "detail": (
                        "document-worker parser dependencies are not installed; "
                        "use the document-worker image"
                    ),
                },
                sort_keys=True,
            )
        )
        return 2
    if max_events < 1 or max_events > 10_000:
        print(
            json.dumps(
                {"status": "BLOCKED", "detail": "max-events must be between 1 and 10000"},
                sort_keys=True,
            )
        )
        return 2
    settings = get_settings()
    if not settings.document_worker_actor_id:
        print(
            json.dumps(
                {"status": "BLOCKED", "detail": "document worker actor is not configured"},
                sort_keys=True,
            )
        )
        return 2
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)
    dispatcher = DocumentIntakeDispatcher(
        session_factory=factory,
        settings=settings,
        evidence_store=build_object_store(settings),
        quarantine_store=build_quarantine_store(settings),
    )
    worker_instance_id = f"document-worker-instance-{uuid4()}"
    counts = {item.value: 0 for item in DispatchDisposition}
    exit_code = 0
    try:
        for _ in range(max_events):
            result = dispatcher.dispatch_next(
                worker_id=worker_instance_id,
            )
            counts[result.disposition.value] += 1
            if result.disposition is DispatchDisposition.IDLE:
                break
            if result.disposition in {
                DispatchDisposition.RETRY_SCHEDULED,
                DispatchDisposition.DEAD_LETTERED,
            }:
                exit_code = 2
        print(
            json.dumps(
                {
                    "status": "COMPLETED" if exit_code == 0 else "ATTENTION_REQUIRED",
                    "counts": counts,
                },
                sort_keys=True,
            )
        )
        return exit_code
    finally:
        engine.dispose()


def dispatch_final_rework(max_events: int) -> int:
    from tenderguard.application.automation_rework import (
        AutomationDispatchDisposition,
        AutomationReworkDispatcher,
    )

    if max_events < 1 or max_events > 10_000:
        print(
            json.dumps(
                {"status": "BLOCKED", "detail": "max-events must be between 1 and 10000"},
                sort_keys=True,
            )
        )
        return 2
    settings = get_settings()
    if not settings.automation_rework_configured:
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "detail": "automatic rework worker binding is not configured",
                },
                sort_keys=True,
            )
        )
        return 2
    engine = create_database_engine(settings)
    dispatcher = AutomationReworkDispatcher(
        session_factory=create_session_factory(engine),
        settings=settings,
        object_store=build_object_store(settings),
    )
    worker_instance_id = f"automation-rework-worker-{uuid4()}"
    counts = {item.value: 0 for item in AutomationDispatchDisposition}
    exit_code = 0
    try:
        for _ in range(max_events):
            try:
                result = dispatcher.dispatch_next(worker_id=worker_instance_id)
            except ValueError as error:
                print(
                    json.dumps(
                        {"status": "BLOCKED", "detail": str(error)[:2000]},
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
                return 2
            counts[result.disposition.value] += 1
            if result.disposition is AutomationDispatchDisposition.IDLE:
                break
            if result.disposition in {
                AutomationDispatchDisposition.BLOCKED,
                AutomationDispatchDisposition.RETRY_SCHEDULED,
                AutomationDispatchDisposition.DEAD_LETTERED,
            }:
                exit_code = 2
        print(
            json.dumps(
                {
                    "status": ("COMMANDS_QUEUED" if exit_code == 0 else "ATTENTION_REQUIRED"),
                    "counts": counts,
                },
                sort_keys=True,
            )
        )
        return exit_code
    finally:
        engine.dispose()


def probe_fgiscs_ksr(query: str) -> int:
    try:
        result = FgisCsPublicApi().search_ksr(query)
    except (FgisCsPublicApiError, ValueError) as error:
        _emit_fgiscs_probe_error(error)
        return 2
    print(result.model_dump_json(indent=2))
    return 0


def probe_fgiscs_material(
    *,
    subject_name: str,
    price_zone_name: str | None,
    period_name: str,
    resource_code: str,
) -> int:
    try:
        request = FgisCsMaterialLookupRequest(
            subject_name=subject_name,
            price_zone_name=price_zone_name,
            period_name=period_name,
            resource_code=resource_code,
        )
        result = FgisCsPublicApi().lookup_material(request)
    except (FgisCsPublicApiError, ValueError) as error:
        _emit_fgiscs_probe_error(error)
        return 2
    print(result.model_dump_json(indent=2))
    return 0


def _emit_fgiscs_probe_error(error: Exception) -> None:
    code = error.code if isinstance(error, FgisCsPublicApiError) else "FGIS_REQUEST_INVALID"
    retryable = error.retryable if isinstance(error, FgisCsPublicApiError) else False
    print(
        json.dumps(
            {
                "status": "BLOCKED",
                "code": code,
                "retryable": retryable,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


def probe_boq_xlsx(
    *,
    input_path: Path,
    profile_path: Path,
    archive_entry_sha256: str | None,
    output_path: Path | None = None,
) -> int:
    try:
        from tenderguard.domain.boq_spreadsheet import BoqXlsxProfile
        from tenderguard.infrastructure.boq_spreadsheet import (
            BoqXlsxExtractionError,
            extract_boq_xlsx_candidates,
        )
        from tenderguard.infrastructure.intake import inspect_intake
    except ModuleNotFoundError as error:
        if error.name != "openpyxl":
            raise
        _emit_boq_probe_error("BOQ_PARSER_DEPENDENCY_MISSING")
        return 2

    try:
        settings = get_settings()
        if not input_path.is_file():
            raise BoqXlsxExtractionError(code="BOQ_INPUT_NOT_FOUND")
        if input_path.stat().st_size > settings.max_upload_bytes:
            raise BoqXlsxExtractionError(code="BOQ_INPUT_TOO_LARGE")
        if not profile_path.is_file() or profile_path.stat().st_size > 1_000_000:
            raise BoqXlsxExtractionError(code="BOQ_PROFILE_NOT_FOUND_OR_TOO_LARGE")
        profile_content = profile_path.read_bytes()
        if len(profile_content) > 1_000_000:
            raise BoqXlsxExtractionError(code="BOQ_PROFILE_NOT_FOUND_OR_TOO_LARGE")
        profile_payload = json.loads(profile_content.decode("utf-8"))
        if not isinstance(profile_payload, dict):
            raise BoqXlsxExtractionError(code="BOQ_PROFILE_JSON_INVALID")
        profile = BoqXlsxProfile.model_validate(profile_payload)
        root_content = input_path.read_bytes()
        if len(root_content) > settings.max_upload_bytes:
            raise BoqXlsxExtractionError(code="BOQ_INPUT_TOO_LARGE")
        members: dict[str, bytes] = {}
        manifest = inspect_intake(
            input_path.name,
            root_content,
            settings,
            on_member=lambda path, content: members.__setitem__(path, content),
        )
        suffix = input_path.suffix.casefold()
        if suffix == ".xlsx":
            workbook_content = root_content
            workbook_archive_path = input_path.name
        elif suffix == ".zip":
            expected_hash = archive_entry_sha256 or profile.expected_workbook_sha256
            if expected_hash is None:
                raise BoqXlsxExtractionError(code="BOQ_ARCHIVE_ENTRY_FINGERPRINT_REQUIRED")
            if len(expected_hash) != 64 or any(
                character not in "0123456789abcdef" for character in expected_hash
            ):
                raise BoqXlsxExtractionError(code="BOQ_ARCHIVE_ENTRY_FINGERPRINT_INVALID")
            matching_members = tuple(
                (path, content)
                for path, content in members.items()
                if path.casefold().endswith(".xlsx")
                and hashlib.sha256(content).hexdigest() == expected_hash
            )
            if len(matching_members) != 1:
                raise BoqXlsxExtractionError(code="BOQ_ARCHIVE_ENTRY_NOT_UNIQUE")
            workbook_archive_path, workbook_content = matching_members[0]
        else:
            raise BoqXlsxExtractionError(code="BOQ_INPUT_TYPE_UNSUPPORTED")
        result = extract_boq_xlsx_candidates(
            workbook_content=workbook_content,
            workbook_archive_path=workbook_archive_path,
            manifest=manifest,
            profile=profile,
        )
    except BoqXlsxExtractionError as error:
        _emit_boq_probe_error(error.code)
        return 2
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        _emit_boq_probe_error("BOQ_PROBE_INPUT_INVALID")
        return 2

    rendered = result.model_dump_json(indent=2)
    if output_path is not None:
        try:
            _write_text_exclusive(output_path, rendered)
        except FileExistsError:
            _emit_boq_probe_error("BOQ_OUTPUT_ALREADY_EXISTS")
            return 2
        except OSError:
            _emit_boq_probe_error("BOQ_OUTPUT_WRITE_FAILED")
            return 2
    print(rendered)
    return 0 if result.status == "UNVERIFIED" else 2


def _emit_boq_probe_error(code: str) -> None:
    print(
        json.dumps(
            {
                "status": "BLOCKED",
                "code": code,
                "ready_for_boq": False,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


def _write_text_exclusive(destination: Path, payload: str) -> None:
    resolved = destination.resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        resolved,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        resolved.unlink(missing_ok=True)
        raise


def import_governed_boq_xlsx(
    *,
    project_id: str,
    document_revision_id: str,
    request_id: str,
    reason: str,
) -> int:
    try:
        from tenderguard.application.boq_spreadsheet_import import (
            BoqSpreadsheetImportService,
        )
        from tenderguard.domain.enums import ActorRole
        from tenderguard.infrastructure.auth import Actor
        from tenderguard.infrastructure.orm import AdapterQualificationRow
    except ModuleNotFoundError as error:
        if error.name != "openpyxl":
            raise
        _emit_boq_probe_error("BOQ_PARSER_DEPENDENCY_MISSING")
        return 2

    settings = get_settings()
    if not settings.boq_xlsx_adapter_configured:
        _emit_boq_probe_error("BOQ_XLSX_WORKER_NOT_CONFIGURED")
        return 2
    assert settings.boq_xlsx_worker_actor_id is not None
    assert settings.boq_xlsx_adapter_qualification_id is not None
    engine = create_database_engine(settings)
    try:
        with create_session_factory(engine).begin() as session:
            qualification = session.get(
                AdapterQualificationRow,
                settings.boq_xlsx_adapter_qualification_id,
            )
            organization_id = (
                qualification.payload.get("organization_id") if qualification is not None else None
            )
            service_actor_id = (
                qualification.payload.get("service_actor_id") if qualification is not None else None
            )
            if (
                qualification is None
                or qualification.adapter_name != settings.boq_xlsx_adapter
                or not isinstance(organization_id, str)
                or not organization_id
                or service_actor_id != settings.boq_xlsx_worker_actor_id
            ):
                raise ValueError("Configured BoQ XLSX qualification is invalid")
            result = BoqSpreadsheetImportService(
                session=session,
                settings=settings,
                object_store=build_object_store(settings),
            ).import_current_workbook(
                actor=Actor(
                    actor_id=settings.boq_xlsx_worker_actor_id,
                    organization_id=organization_id,
                    roles=frozenset({ActorRole.SYSTEM}),
                ),
                project_id=project_id,
                document_revision_id=document_revision_id,
                adapter_qualification_id=(settings.boq_xlsx_adapter_qualification_id),
                request_id=request_id,
                reason=reason,
            )
    except Exception as error:
        _emit_boq_probe_error(getattr(error, "code", "BOQ_GOVERNED_IMPORT_BLOCKED"))
        return 2
    finally:
        engine.dispose()
    print(result.model_dump_json(indent=2))
    return 0


def import_governed_fgiscs_material(
    *,
    project_id: str,
    item_id: str,
    resource_code: str,
    request_id: str,
    reason: str,
) -> int:
    from tenderguard.application.fgiscs_acquisition import (
        FgisCsAcquisitionService,
        prepare_fgiscs_material_acquisition,
    )
    from tenderguard.domain.enums import ActorRole
    from tenderguard.infrastructure.auth import Actor
    from tenderguard.infrastructure.orm import AdapterQualificationRow

    settings = get_settings()
    if not settings.fgiscs_adapter_configured:
        _emit_fgiscs_governed_error("FGIS_CS_WORKER_NOT_CONFIGURED")
        return 2
    assert settings.fgiscs_worker_actor_id is not None
    assert settings.fgiscs_adapter_qualification_id is not None
    engine = create_database_engine(settings)
    object_store = build_object_store(settings)
    factory = create_session_factory(engine)
    try:
        with factory() as session:
            qualification = session.get(
                AdapterQualificationRow,
                settings.fgiscs_adapter_qualification_id,
            )
            organization_id = (
                qualification.payload.get("organization_id") if qualification is not None else None
            )
            service_actor_id = (
                qualification.payload.get("service_actor_id") if qualification is not None else None
            )
            if (
                qualification is None
                or qualification.adapter_name != settings.fgiscs_adapter
                or not isinstance(organization_id, str)
                or not organization_id
                or service_actor_id != settings.fgiscs_worker_actor_id
            ):
                raise ValueError("Configured FGIS CS qualification is invalid")
            actor = Actor(
                actor_id=settings.fgiscs_worker_actor_id,
                organization_id=organization_id,
                roles=frozenset({ActorRole.SYSTEM}),
            )
            context = FgisCsAcquisitionService(
                session=session,
                settings=settings,
                object_store=object_store,
            ).request_context(
                actor=actor,
                project_id=project_id,
                item_id=item_id,
                resource_code=resource_code,
            )

        prepared = prepare_fgiscs_material_acquisition(
            api=FgisCsPublicApi(
                timeout_seconds=settings.integration_http_timeout_seconds,
                max_response_bytes=settings.integration_max_response_bytes,
            ),
            request=context.lookup_request,
            expected_context_hash=context.context_hash,
            object_store=object_store,
            max_response_bytes=settings.integration_max_response_bytes,
        )

        with factory.begin() as session:
            result = FgisCsAcquisitionService(
                session=session,
                settings=settings,
                object_store=object_store,
            ).record_stored_acquisition(
                actor=actor,
                project_id=project_id,
                item_id=item_id,
                resource_code=resource_code,
                expected_context_hash=context.context_hash,
                prepared=prepared,
                request_id=request_id,
                reason=reason,
            )
    except Exception as error:
        _emit_fgiscs_governed_error(
            getattr(error, "code", "FGIS_CS_GOVERNED_ACQUISITION_BLOCKED"),
            retryable=bool(getattr(error, "retryable", False)),
        )
        return 2
    finally:
        engine.dispose()
    print(result.model_dump_json(indent=2))
    return 0


def _emit_fgiscs_governed_error(code: str, *, retryable: bool = False) -> None:
    print(
        json.dumps(
            {
                "status": "BLOCKED",
                "code": code,
                "retryable": retryable,
                "ready_for_pricing": False,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


def verify_restored_system(
    *,
    profile_version_id: str,
    expected_profile_hash: str,
    exercise_manifest_path: Path,
    output_path: Path | None,
) -> int:
    settings = get_settings()
    started_at = utc_now()
    engine = create_database_engine(settings)
    try:
        with create_session_factory(engine)() as session:
            profile, _ = load_approved_profile(
                session=session,
                settings=settings,
                version_id=profile_version_id,
                expected_content_hash=expected_profile_hash,
                expected_kind="recovery_profile",
                profile_type=RecoveryProfile,
            )
            exercise = RecoveryExerciseManifest.model_validate(
                read_json_object(exercise_manifest_path)
            )
            result = RecoveryVerificationService(
                session=session,
                settings=settings,
                object_store=build_object_store(settings),
                quarantine_store=build_quarantine_store(settings),
            ).verify(
                profile_version_id=profile_version_id,
                profile_content_hash=expected_profile_hash,
                profile=profile,
                exercise=exercise,
            )
    except Exception as error:
        result = _blocked_qualification_result(
            qualification_type="RECOVERY",
            profile_version_id=profile_version_id,
            profile_content_hash=expected_profile_hash,
            started_at=started_at,
            error=error,
            traffic_started=False,
        )
    finally:
        engine.dispose()
    _emit_qualification_result(result, output_path)
    return 0 if result.status == "TECHNICAL_VERIFICATION_PASSED" else 2


def run_load_qualification(
    *,
    profile_version_id: str,
    expected_profile_hash: str,
    output_path: Path | None,
) -> int:
    settings = get_settings()
    started_at = utc_now()
    engine = create_database_engine(settings)
    try:
        with create_session_factory(engine)() as session:
            profile, _ = load_approved_profile(
                session=session,
                settings=settings,
                version_id=profile_version_id,
                expected_content_hash=expected_profile_hash,
                expected_kind="load_test_profile",
                profile_type=LoadProfile,
            )
    except Exception as error:
        result = _blocked_qualification_result(
            qualification_type="LOAD",
            profile_version_id=profile_version_id,
            profile_content_hash=expected_profile_hash,
            started_at=started_at,
            error=error,
            traffic_started=False,
        )
        _emit_qualification_result(result, output_path)
        return 2
    finally:
        engine.dispose()
    try:
        result = LoadQualificationService().run(
            profile_version_id=profile_version_id,
            profile_content_hash=expected_profile_hash,
            profile=profile,
        )
    except Exception as error:
        result = _blocked_qualification_result(
            qualification_type="LOAD",
            profile_version_id=profile_version_id,
            profile_content_hash=expected_profile_hash,
            started_at=started_at,
            error=error,
            traffic_started=None,
        )
    _emit_qualification_result(result, output_path)
    return 0 if result.status == "TECHNICAL_VERIFICATION_PASSED" else 2


def _blocked_qualification_result(
    *,
    qualification_type: str,
    profile_version_id: str,
    profile_content_hash: str,
    started_at: object,
    error: Exception,
    traffic_started: bool | None,
) -> QualificationResultEnvelope:
    message = str(error).strip() or type(error).__name__
    return build_result_envelope(
        qualification_type=qualification_type,
        status="BLOCKED",
        profile_version_id=profile_version_id,
        profile_content_hash=profile_content_hash,
        started_at=started_at,
        completed_at=utc_now(),
        findings=(
            QualificationFinding(
                code="QUALIFICATION_PREFLIGHT",
                passed=False,
                message=message[:2000],
                details={"error_type": type(error).__name__},
            ),
        ),
        evidence={"traffic_started": traffic_started},
    )


def _emit_qualification_result(
    result: QualificationResultEnvelope,
    output_path: Path | None,
) -> None:
    if output_path is not None:
        write_result_exclusive(result, output_path)
    print(result.model_dump_json(indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(prog="tenderguard")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("doctor", help="Check runtime dependencies without changing data")
    process_parser = subcommands.add_parser(
        "process-quarantined-upload",
        help="Process one CLEAN upload in the isolated document worker runtime",
    )
    process_parser.add_argument("--upload-id", required=True)
    dispatch_parser = subcommands.add_parser(
        "dispatch-document-intake",
        help="Claim and deliver pending clean-upload outbox events",
    )
    dispatch_parser.add_argument("--max-events", type=int, default=1)
    automation_dispatch_parser = subcommands.add_parser(
        "dispatch-final-rework",
        help="Validate final expert rework and queue qualified automatic stage commands",
    )
    automation_dispatch_parser.add_argument("--max-events", type=int, default=1)
    ksr_parser = subcommands.add_parser(
        "probe-fgiscs-ksr",
        help="Retrieve unverified KSR candidates from the public FGIS CS portal",
    )
    ksr_parser.add_argument("--query", required=True)
    material_parser = subcommands.add_parser(
        "probe-fgiscs-material",
        help="Retrieve one raw FGIS CS material record by an exact KSR code",
    )
    material_parser.add_argument("--subject-name", required=True)
    material_parser.add_argument("--price-zone-name")
    material_parser.add_argument("--period-name", required=True)
    material_parser.add_argument("--resource-code", required=True)
    boq_probe_parser = subcommands.add_parser(
        "probe-boq-xlsx",
        help="Extract provenance-rich UNVERIFIED BoQ row candidates by an exact profile",
    )
    boq_probe_parser.add_argument("--input", required=True, type=Path)
    boq_probe_parser.add_argument("--profile", required=True, type=Path)
    boq_probe_parser.add_argument(
        "--archive-entry-sha256",
    )
    boq_probe_parser.add_argument("--output", type=Path)
    boq_import_parser = subcommands.add_parser(
        "import-governed-boq-xlsx",
        help="Import profile-pinned XLSX rows as qualified UNVERIFIED observations",
    )
    boq_import_parser.add_argument("--project-id", required=True)
    boq_import_parser.add_argument("--document-revision-id", required=True)
    boq_import_parser.add_argument("--request-id", required=True)
    boq_import_parser.add_argument("--reason", required=True)
    fgiscs_import_parser = subcommands.add_parser(
        "import-governed-fgiscs-material",
        help="Retain and replay one policy-bound UNVERIFIED FGIS CS material response",
    )
    fgiscs_import_parser.add_argument("--project-id", required=True)
    fgiscs_import_parser.add_argument("--item-id", required=True)
    fgiscs_import_parser.add_argument("--resource-code", required=True)
    fgiscs_import_parser.add_argument("--request-id", required=True)
    fgiscs_import_parser.add_argument("--reason", required=True)
    recovery_parser = subcommands.add_parser(
        "verify-restored-system",
        help="Verify a restored database/object-store pair against an approved profile",
    )
    recovery_parser.add_argument("--profile-version-id", required=True)
    recovery_parser.add_argument("--expected-profile-hash", required=True)
    recovery_parser.add_argument(
        "--exercise-manifest",
        required=True,
        type=Path,
    )
    recovery_parser.add_argument("--output", type=Path)
    load_parser = subcommands.add_parser(
        "run-load-qualification",
        help="Run approved read-only load traffic and emit a tamper-evident result",
    )
    load_parser.add_argument("--profile-version-id", required=True)
    load_parser.add_argument("--expected-profile-hash", required=True)
    load_parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if arguments.command == "doctor":
        raise SystemExit(doctor())
    if arguments.command == "process-quarantined-upload":
        raise SystemExit(process_quarantined_upload(arguments.upload_id))
    if arguments.command == "dispatch-document-intake":
        raise SystemExit(dispatch_document_intake(arguments.max_events))
    if arguments.command == "dispatch-final-rework":
        raise SystemExit(dispatch_final_rework(arguments.max_events))
    if arguments.command == "probe-fgiscs-ksr":
        raise SystemExit(probe_fgiscs_ksr(arguments.query))
    if arguments.command == "probe-fgiscs-material":
        raise SystemExit(
            probe_fgiscs_material(
                subject_name=arguments.subject_name,
                price_zone_name=arguments.price_zone_name,
                period_name=arguments.period_name,
                resource_code=arguments.resource_code,
            )
        )
    if arguments.command == "probe-boq-xlsx":
        raise SystemExit(
            probe_boq_xlsx(
                input_path=arguments.input,
                profile_path=arguments.profile,
                archive_entry_sha256=arguments.archive_entry_sha256,
                output_path=arguments.output,
            )
        )
    if arguments.command == "import-governed-boq-xlsx":
        raise SystemExit(
            import_governed_boq_xlsx(
                project_id=arguments.project_id,
                document_revision_id=arguments.document_revision_id,
                request_id=arguments.request_id,
                reason=arguments.reason,
            )
        )
    if arguments.command == "import-governed-fgiscs-material":
        raise SystemExit(
            import_governed_fgiscs_material(
                project_id=arguments.project_id,
                item_id=arguments.item_id,
                resource_code=arguments.resource_code,
                request_id=arguments.request_id,
                reason=arguments.reason,
            )
        )
    if arguments.command == "verify-restored-system":
        raise SystemExit(
            verify_restored_system(
                profile_version_id=arguments.profile_version_id,
                expected_profile_hash=arguments.expected_profile_hash,
                exercise_manifest_path=arguments.exercise_manifest,
                output_path=arguments.output,
            )
        )
    if arguments.command == "run-load-qualification":
        raise SystemExit(
            run_load_qualification(
                profile_version_id=arguments.profile_version_id,
                expected_profile_hash=arguments.expected_profile_hash,
                output_path=arguments.output,
            )
        )


if __name__ == "__main__":
    main()
