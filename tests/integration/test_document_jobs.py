from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from tenderguard.api.main import create_app
from tenderguard.application import document_processing
from tenderguard.application.document_jobs import DocumentIntakeDispatcher
from tenderguard.application.outbox import (
    OutboxDeliveryService,
    OutboxLeaseLostError,
)
from tenderguard.config import Settings
from tenderguard.domain.common import content_hash, utc_now
from tenderguard.domain.jobs import DispatchDisposition
from tenderguard.domain.quarantine import QuarantineStatus
from tenderguard.infrastructure.database import (
    create_database_engine,
    create_schema_for_tests,
    create_session_factory,
)
from tenderguard.infrastructure.object_store import LocalObjectStore
from tenderguard.infrastructure.orm import (
    AdapterQualificationRow,
    AuditEventRow,
    DocumentRevisionRow,
    OutboxEventRow,
    QuarantinedUploadRow,
    VerificationFindingRow,
)


def _settings(tmp_path: Path, *, max_attempts: int = 3) -> Settings:
    return Settings(
        app_env="test",
        database_url="sqlite+pysqlite://",
        local_object_store_path=tmp_path / "objects",
        local_quarantine_store_path=tmp_path / "quarantine",
        allow_insecure_dev_auth=True,
        audit_signing_key="test-audit-signing-key-at-least-32-bytes",
        malware_scanner_adapter="qualified-test-scanner",
        malware_scanner_qualification_id="qualification-malware-test",
        document_processor_adapter="qualified-test-intake",
        document_processor_qualification_id="qualification-intake-test",
        document_worker_actor_id="document-worker",
        document_job_lease_seconds=60,
        document_job_timeout_seconds=30,
        document_job_max_attempts=max_attempts,
        document_job_retry_base_seconds=1,
        document_job_retry_max_seconds=4,
    )


def _seed_qualifications(engine: Engine) -> None:
    now = datetime.now(UTC)
    with create_session_factory(engine).begin() as session:
        session.add_all(
            [
                AdapterQualificationRow(
                    id="qualification-malware-test",
                    adapter_name="qualified-test-scanner",
                    adapter_version="test-1",
                    status="APPROVED",
                    valid_until=None,
                    test_evidence_hash="a" * 64,
                    payload={
                        "organization_id": "org-1",
                        "supported_methods": ["MALWARE_SCAN"],
                    },
                    approved_by="methodology-owner-b",
                    approved_at=now,
                ),
                AdapterQualificationRow(
                    id="qualification-intake-test",
                    adapter_name="qualified-test-intake",
                    adapter_version="test-1",
                    status="APPROVED",
                    valid_until=None,
                    test_evidence_hash="b" * 64,
                    payload={
                        "organization_id": "org-1",
                        "supported_methods": ["DOCUMENT_INTAKE"],
                    },
                    approved_by="methodology-owner-b",
                    approved_at=now,
                ),
            ]
        )


def _create_clean_upload(client: TestClient, *, code: str) -> dict[str, object]:
    estimator = {
        "X-Dev-Actor": "estimator-1",
        "X-Dev-Organization": "org-1",
        "X-Dev-Roles": "ESTIMATOR",
    }
    system = {
        "X-Dev-Actor": "scanner-1",
        "X-Dev-Organization": "org-1",
        "X-Dev-Roles": "SYSTEM",
    }
    project_response = client.post(
        "/v1/projects",
        headers=estimator,
        json={"code": code, "name": "Document job test", "reason": "Register"},
    )
    assert project_response.status_code == 201, project_response.text
    project = project_response.json()
    upload_response = client.post(
        f"/v1/projects/{project['id']}/documents",
        headers=estimator,
        data={
            "logical_key": "terms",
            "title": "Tender terms",
            "document_type": "TENDER_TERMS",
            "revision_label": "1",
            "reason": "Submit untrusted document",
            "critical": "true",
        },
        files={"upload": ("terms.txt", b"verified tender terms", "text/plain")},
    )
    assert upload_response.status_code == 202, upload_response.text
    upload = upload_response.json()
    report = {
        "engine": "qualified-test-scanner",
        "object_hash": upload["object_hash"],
        "verdict": "CLEAN",
    }
    scan_response = client.post(
        (f"/v1/projects/{project['id']}/document-uploads/{upload['upload_id']}/scan-results"),
        headers=system,
        json={
            "result": {
                "scanner_run_id": f"scan-{upload['upload_id']}",
                "adapter_qualification_id": "qualification-malware-test",
                "scanned_object_hash": upload["object_hash"],
                "verdict": "CLEAN",
                "definitions_version": "test-definitions-1",
                "detected_threats": [],
                "report": report,
                "report_hash": content_hash(report),
                "completed_at": datetime.now(UTC).isoformat(),
            },
            "reason": "Record qualified clean scan",
        },
    )
    assert scan_response.status_code == 200, scan_response.text
    return upload


def _dispatcher(
    *,
    engine: Engine,
    settings: Settings,
    tmp_path: Path,
) -> DocumentIntakeDispatcher:
    return DocumentIntakeDispatcher(
        session_factory=create_session_factory(engine),
        settings=settings,
        evidence_store=LocalObjectStore(tmp_path / "objects"),
        quarantine_store=LocalObjectStore(tmp_path / "quarantine"),
    )


def _make_app(
    *,
    engine: Engine,
    settings: Settings,
    tmp_path: Path,
):
    return create_app(
        settings,
        engine=engine,
        object_store=LocalObjectStore(tmp_path / "objects"),
        quarantine_store=LocalObjectStore(tmp_path / "quarantine"),
    )


def test_outbox_lease_reclaim_rejects_stale_owner_and_dead_letters(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, max_attempts=3)
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    factory = create_session_factory(engine)
    now = utc_now()
    with factory.begin() as session:
        session.add(
            OutboxEventRow(
                id="outbox-test",
                topic="document.upload.scan-clean",
                aggregate_id="upload-test",
                payload={"upload_id": "upload-test", "project_id": "project-test"},
                attempts=0,
                available_at=now,
                published_at=None,
                last_error=None,
                locked_by=None,
                lease_token=None,
                lease_expires_at=None,
                last_attempt_at=None,
                dead_lettered_at=None,
                created_at=now,
            )
        )

    with factory.begin() as session:
        first = OutboxDeliveryService(session=session, settings=settings).claim_next(
            topics={"document.upload.scan-clean"},
            worker_id="worker-a",
        )
    assert first is not None
    with factory.begin() as session:
        blocked = OutboxDeliveryService(session=session, settings=settings).claim_next(
            topics={"document.upload.scan-clean"},
            worker_id="worker-b",
        )
    assert blocked is None

    with factory.begin() as session:
        row = session.get(OutboxEventRow, "outbox-test")
        assert row is not None
        row.lease_expires_at = utc_now() - timedelta(seconds=1)
    with factory.begin() as session:
        second = OutboxDeliveryService(session=session, settings=settings).claim_next(
            topics={"document.upload.scan-clean"},
            worker_id="worker-b",
        )
    assert second is not None
    assert second.delivery_attempt == 2
    assert second.lease_token != first.lease_token

    with (
        factory.begin() as session,
        pytest.raises(OutboxLeaseLostError, match="another worker"),
    ):
        OutboxDeliveryService(session=session, settings=settings).acknowledge(first)
    with factory.begin() as session:
        settlement = OutboxDeliveryService(session=session, settings=settings).reject(
            second,
            error_code="TEST_RETRYABLE_FAILURE",
        )
    assert not settlement.dead_lettered
    assert settlement.next_available_at is not None

    with factory.begin() as session:
        row = session.get(OutboxEventRow, "outbox-test")
        assert row is not None
        row.available_at = utc_now() - timedelta(seconds=1)
    with factory.begin() as session:
        third = OutboxDeliveryService(session=session, settings=settings).claim_next(
            topics={"document.upload.scan-clean"},
            worker_id="worker-c",
        )
    assert third is not None
    with factory.begin() as session:
        terminal = OutboxDeliveryService(session=session, settings=settings).reject(
            third,
            error_code="TEST_FINAL_FAILURE",
        )
    assert terminal.dead_lettered
    with factory() as session:
        row = session.get(OutboxEventRow, "outbox-test")
        assert row is not None
        assert row.dead_lettered_at is not None
        assert row.published_at is None
        assert row.lease_token is None


def test_dispatcher_reclaims_stale_document_lease_and_is_idempotent(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    _seed_qualifications(engine)
    factory = create_session_factory(engine)
    app = _make_app(engine=engine, settings=settings, tmp_path=tmp_path)
    with TestClient(app) as client:
        upload = _create_clean_upload(client, code="JOB-STALE")
        upload_id = str(upload["upload_id"])
        past = utc_now() - timedelta(minutes=2)
        with factory.begin() as session:
            row = session.get(QuarantinedUploadRow, upload_id)
            assert row is not None
            row.status = QuarantineStatus.PROCESSING.value
            row.processing_attempts = 1
            row.processing_worker_id = "crashed-worker"
            row.processing_lease_token = "document-lease-crashed"
            row.processing_lease_expires_at = past
            row.processing_deadline_at = past
            row.processing_started_at = past

        dispatcher = _dispatcher(engine=engine, settings=settings, tmp_path=tmp_path)
        result = dispatcher.dispatch_next(
            worker_id="worker-instance-a",
            upload_id=upload_id,
        )
        assert result.disposition is DispatchDisposition.PROCESSED
        assert result.upload is not None
        assert result.upload.processing_attempts == 2
        assert result.upload.processing_lease_expires_at is None

        repeated = dispatcher.dispatch_next(
            worker_id="worker-instance-b",
            upload_id=upload_id,
        )
        assert repeated.disposition is DispatchDisposition.PROCESSED
        with factory() as session:
            revisions = list(session.scalars(select(DocumentRevisionRow)))
            assert len(revisions) == 1
            outbox = session.scalar(
                select(OutboxEventRow).where(
                    OutboxEventRow.topic == "document.upload.scan-clean",
                    OutboxEventRow.aggregate_id == upload_id,
                )
            )
            assert outbox is not None
            assert outbox.published_at is not None
            assert outbox.dead_lettered_at is None
            started = list(
                session.scalars(
                    select(AuditEventRow).where(
                        AuditEventRow.aggregate_id == upload_id,
                        AuditEventRow.event_type == "document_processing_started",
                    )
                )
            )
            assert len(started) == 1
            assert started[0].actor_id == "document-worker"
            assert started[0].payload["worker_id"] == "worker-instance-a"
            assert started[0].payload["stale_lease_reclaimed"] is True


def test_dispatcher_persists_failure_outside_parser_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    _seed_qualifications(engine)
    factory = create_session_factory(engine)
    app = _make_app(engine=engine, settings=settings, tmp_path=tmp_path)
    with TestClient(app) as client:
        upload = _create_clean_upload(client, code="JOB-FAIL")
        upload_id = str(upload["upload_id"])

        def fail_after_observing_committed_claim(*args: object, **kwargs: object):
            with factory() as session:
                row = session.get(QuarantinedUploadRow, upload_id)
                assert row is not None
                assert row.status == QuarantineStatus.PROCESSING.value
                assert row.processing_lease_token is not None
            raise document_processing.DocumentProcessingTimeoutError("test timeout")

        monkeypatch.setattr(
            document_processing,
            "inspect_intake_stream",
            fail_after_observing_committed_claim,
        )
        result = _dispatcher(
            engine=engine,
            settings=settings,
            tmp_path=tmp_path,
        ).dispatch_next(worker_id="document-worker", upload_id=upload_id)
        assert result.disposition is DispatchDisposition.RETRY_SCHEDULED
        assert result.upload is not None
        assert result.upload.status is QuarantineStatus.PROCESSING_FAILED
        assert result.upload.failure_code == "DOCUMENT_PROCESSING_TIMEOUT"
        assert result.upload.processing_attempts == 1
        assert result.upload.processing_lease_expires_at is None
        with factory() as session:
            assert session.scalar(select(DocumentRevisionRow)) is None
            outbox = session.scalar(
                select(OutboxEventRow).where(
                    OutboxEventRow.aggregate_id == upload_id,
                    OutboxEventRow.topic == "document.upload.scan-clean",
                )
            )
            assert outbox is not None
            assert outbox.published_at is None
            assert outbox.dead_lettered_at is None
            assert outbox.available_at > outbox.last_attempt_at


def test_dispatcher_dead_letters_after_bounded_qualification_failures(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, max_attempts=3)
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    _seed_qualifications(engine)
    factory = create_session_factory(engine)
    app = _make_app(engine=engine, settings=settings, tmp_path=tmp_path)
    with TestClient(app) as client:
        upload = _create_clean_upload(client, code="JOB-DLQ")
        upload_id = str(upload["upload_id"])
        with factory.begin() as session:
            qualification = session.get(
                AdapterQualificationRow,
                "qualification-intake-test",
            )
            assert qualification is not None
            qualification.status = "SUSPENDED"

        dispatcher = _dispatcher(engine=engine, settings=settings, tmp_path=tmp_path)
        for attempt in range(1, 4):
            result = dispatcher.dispatch_next(
                worker_id="document-worker",
                upload_id=upload_id,
            )
            expected = (
                DispatchDisposition.DEAD_LETTERED
                if attempt == 3
                else DispatchDisposition.RETRY_SCHEDULED
            )
            assert result.disposition is expected
            if attempt < 3:
                with factory.begin() as session:
                    outbox = session.scalar(
                        select(OutboxEventRow).where(
                            OutboxEventRow.aggregate_id == upload_id,
                            OutboxEventRow.topic == "document.upload.scan-clean",
                        )
                    )
                    assert outbox is not None
                    outbox.available_at = utc_now() - timedelta(seconds=1)

        with factory() as session:
            outbox = session.scalar(
                select(OutboxEventRow).where(
                    OutboxEventRow.aggregate_id == upload_id,
                    OutboxEventRow.topic == "document.upload.scan-clean",
                )
            )
            row = session.get(QuarantinedUploadRow, upload_id)
            assert outbox is not None
            assert row is not None
            assert outbox.attempts == 3
            assert outbox.dead_lettered_at is not None
            assert outbox.published_at is None
            assert row.status == QuarantineStatus.PROCESSING_DEAD_LETTERED.value
            assert row.processing_dead_lettered_at is not None
            assert row.failure_code == "DOCUMENT_JOB_VALIDATION_FAILED"
            blocker = session.scalar(
                select(VerificationFindingRow).where(
                    VerificationFindingRow.code == "QUARANTINE_SCAN_PENDING",
                    VerificationFindingRow.resolved.is_(False),
                )
            )
            assert blocker is not None
            assert not blocker.resolved
            dead_letter_audit = session.scalar(
                select(AuditEventRow).where(
                    AuditEventRow.aggregate_id == upload_id,
                    AuditEventRow.event_type == "document_processing_dead_lettered",
                )
            )
            assert dead_letter_audit is not None
        assert not [path for path in (tmp_path / "objects").rglob("*") if path.is_file()]

        denied = client.post(
            (
                f"/v1/projects/{upload['project_id']}/document-uploads/"
                f"{upload_id}/requeue-processing"
            ),
            headers={
                "X-Dev-Actor": "estimator-1",
                "X-Dev-Organization": "org-1",
                "X-Dev-Roles": "ESTIMATOR",
            },
            json={"reason": "Estimator must not replay operational dead letters"},
        )
        assert denied.status_code == 403
        with factory.begin() as session:
            qualification = session.get(
                AdapterQualificationRow,
                "qualification-intake-test",
            )
            assert qualification is not None
            qualification.status = "APPROVED"
        requeued = client.post(
            (
                f"/v1/projects/{upload['project_id']}/document-uploads/"
                f"{upload_id}/requeue-processing"
            ),
            headers={
                "X-Dev-Actor": "operations-admin",
                "X-Dev-Organization": "org-1",
                "X-Dev-Roles": "ADMIN",
            },
            json={"reason": "Processor qualification was restored after incident review"},
        )
        assert requeued.status_code == 200, requeued.text
        assert requeued.json()["status"] == "CLEAN"
        assert requeued.json()["processing_attempts"] == 0
        replayed = dispatcher.dispatch_next(
            worker_id="document-worker",
            upload_id=upload_id,
        )
        assert replayed.disposition is DispatchDisposition.PROCESSED
        with factory() as session:
            events = list(
                session.scalars(
                    select(OutboxEventRow)
                    .where(
                        OutboxEventRow.aggregate_id == upload_id,
                        OutboxEventRow.topic == "document.upload.scan-clean",
                    )
                    .order_by(OutboxEventRow.created_at, OutboxEventRow.id)
                )
            )
            assert len(events) == 2
            assert events[0].dead_lettered_at is not None
            assert events[0].published_at is None
            assert events[1].published_at is not None
            assert events[1].dead_lettered_at is None
            requeue_audit = session.scalar(
                select(AuditEventRow).where(
                    AuditEventRow.aggregate_id == upload_id,
                    AuditEventRow.event_type == "document_processing_requeued",
                )
            )
            assert requeue_audit is not None


def test_invalid_outbox_target_cannot_overwrite_malware_rejection(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, max_attempts=1)
    engine = create_database_engine(settings)
    create_schema_for_tests(engine)
    _seed_qualifications(engine)
    factory = create_session_factory(engine)
    app = _make_app(engine=engine, settings=settings, tmp_path=tmp_path)
    with TestClient(app) as client:
        upload = _create_clean_upload(client, code="JOB-INVALID-STATE")
        upload_id = str(upload["upload_id"])
        with factory.begin() as session:
            row = session.get(QuarantinedUploadRow, upload_id)
            assert row is not None
            row.status = QuarantineStatus.REJECTED.value
            row.failure_code = "MALWARE_DETECTED"
        result = _dispatcher(
            engine=engine,
            settings=settings,
            tmp_path=tmp_path,
        ).dispatch_next(worker_id="worker-instance-invalid", upload_id=upload_id)
        assert result.disposition is DispatchDisposition.DEAD_LETTERED
        assert result.error_code == "DOCUMENT_DEAD_LETTER_STATE_INVALID"
        with factory() as session:
            row = session.get(QuarantinedUploadRow, upload_id)
            outbox = session.scalar(
                select(OutboxEventRow).where(
                    OutboxEventRow.aggregate_id == upload_id,
                    OutboxEventRow.topic == "document.upload.scan-clean",
                )
            )
            assert row is not None
            assert outbox is not None
            assert row.status == QuarantineStatus.REJECTED.value
            assert row.failure_code == "MALWARE_DETECTED"
            assert row.processing_dead_lettered_at is None
            assert outbox.dead_lettered_at is not None
