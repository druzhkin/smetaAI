from __future__ import annotations

import argparse
import json
from uuid import uuid4

from sqlalchemy import text

from tenderguard.application.audit_integrity import AuditIntegrityService
from tenderguard.application.projects import ProjectService
from tenderguard.config import get_settings
from tenderguard.domain.jobs import DispatchDisposition
from tenderguard.infrastructure.database import (
    CURRENT_SCHEMA_REVISION,
    create_database_engine,
    create_session_factory,
)
from tenderguard.infrastructure.object_store import (
    build_object_store,
    build_quarantine_store,
)


def doctor() -> int:
    settings = get_settings()
    checks: dict[str, bool | str] = {
        "environment": settings.app_env,
        "database": False,
        "schema_current": False,
        "object_store": False,
        "object_store_worm": False,
        "quarantine_store": False,
        "audit_anchor_valid": False,
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
                "schema_current",
                "object_store",
                "object_store_worm",
                "quarantine_store",
                "audit_anchor_valid",
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
    arguments = parser.parse_args()
    if arguments.command == "doctor":
        raise SystemExit(doctor())
    if arguments.command == "process-quarantined-upload":
        raise SystemExit(process_quarantined_upload(arguments.upload_id))
    if arguments.command == "dispatch-document-intake":
        raise SystemExit(dispatch_document_intake(arguments.max_events))


if __name__ == "__main__":
    main()
