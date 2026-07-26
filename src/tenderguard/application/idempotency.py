from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, cast
from uuid import uuid4

from fastapi import HTTPException, Request, Response, status
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker
from starlette.datastructures import UploadFile
from starlette.responses import JSONResponse

from tenderguard.config import Settings
from tenderguard.domain.common import content_hash, utc_now
from tenderguard.infrastructure.auth import Actor, Authenticator
from tenderguard.infrastructure.orm import IdempotencyRecordRow

IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
REPLAY_HEADER_ALLOWLIST = frozenset({"location", "etag"})


class IdempotencyConflictError(RuntimeError):
    pass


class IdempotencyInProgressError(RuntimeError):
    pass


@dataclass(frozen=True)
class IdempotencyReplay:
    response_status: int
    response_media_type: str | None
    response_payload: Any
    response_has_body: bool
    response_headers: dict[str, str]


class IdempotencyService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def acquire(
        self,
        *,
        actor: Actor,
        idempotency_key: str,
        request_method: str,
        request_path: str,
        request_hash: str,
        initial_request_id: str,
    ) -> tuple[IdempotencyRecordRow, IdempotencyReplay | None]:
        key = _idempotency_key(idempotency_key)
        row = self._locked_record(actor=actor, key=key)
        created = False
        if row is None:
            record_id = f"idempotency-{uuid4()}"
            values = {
                "id": record_id,
                "organization_id": actor.organization_id,
                "actor_id": actor.actor_id,
                "idempotency_key": key,
                "request_method": request_method,
                "request_path": request_path,
                "request_hash": request_hash,
                "initial_request_id": initial_request_id,
                "status": "PENDING",
                "response_status": None,
                "response_media_type": None,
                "response_payload": None,
                "response_has_body": None,
                "response_headers": None,
                "created_at": utc_now(),
                "completed_at": None,
            }
            conflict_columns = [
                "organization_id",
                "actor_id",
                "idempotency_key",
            ]
            dialect_name = self.session.get_bind().dialect.name
            if dialect_name == "postgresql":
                statement = (
                    postgresql_insert(IdempotencyRecordRow)
                    .values(**values)
                    .on_conflict_do_nothing(index_elements=conflict_columns)
                    .returning(IdempotencyRecordRow.id)
                )
            elif dialect_name == "sqlite":
                statement = (
                    sqlite_insert(IdempotencyRecordRow)
                    .values(**values)
                    .on_conflict_do_nothing(index_elements=conflict_columns)
                    .returning(IdempotencyRecordRow.id)
                )
            else:
                raise RuntimeError("Persisted idempotency supports only PostgreSQL and SQLite")
            inserted_id = self.session.scalar(statement)
            if inserted_id is not None:
                row = self.session.get(IdempotencyRecordRow, inserted_id)
                created = True
            else:
                row = self._locked_record(actor=actor, key=key)
            if row is None:
                raise RuntimeError("Idempotency insert did not expose its durable record")
        if (
            row.request_method != request_method
            or row.request_path != request_path
            or row.request_hash != request_hash
        ):
            raise IdempotencyConflictError(
                "Idempotency key was already used for a different request"
            )
        if row.status == "PENDING":
            if created:
                return row, None
            raise IdempotencyInProgressError("Idempotent request is still in progress")
        if row.status != "COMPLETED":
            raise RuntimeError("Idempotency record has an unsupported state")
        if (
            row.response_status is None
            or row.response_has_body is None
            or row.response_headers is None
            or row.completed_at is None
        ):
            raise RuntimeError("Completed idempotency record is incomplete")
        return (
            row,
            IdempotencyReplay(
                response_status=row.response_status,
                response_media_type=row.response_media_type,
                response_payload=row.response_payload,
                response_has_body=row.response_has_body,
                response_headers=row.response_headers,
            ),
        )

    def complete(
        self,
        *,
        row: IdempotencyRecordRow,
        response_status: int,
        response_media_type: str | None,
        response_payload: Any,
        response_has_body: bool,
        response_headers: dict[str, str],
    ) -> None:
        if row.status != "PENDING":
            raise RuntimeError("Only a pending idempotency record can be completed")
        row.status = "COMPLETED"
        row.response_status = response_status
        row.response_media_type = response_media_type
        row.response_payload = response_payload
        row.response_has_body = response_has_body
        row.response_headers = response_headers
        row.completed_at = utc_now()
        self.session.flush()

    def _locked_record(
        self,
        *,
        actor: Actor,
        key: str,
    ) -> IdempotencyRecordRow | None:
        return self.session.scalar(
            select(IdempotencyRecordRow)
            .where(
                IdempotencyRecordRow.organization_id == actor.organization_id,
                IdempotencyRecordRow.actor_id == actor.actor_id,
                IdempotencyRecordRow.idempotency_key == key,
            )
            .with_for_update()
        )


class _RollbackResponse(Exception):
    def __init__(self, response: Response) -> None:
        self.response = response


class IdempotentAPIRoute(APIRoute):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        methods = {str(method).upper() for method in kwargs.get("methods", ())}
        if methods.intersection(MUTATING_METHODS):
            openapi_extra = dict(kwargs.get("openapi_extra") or {})
            parameters = list(openapi_extra.get("parameters") or [])
            parameters.append(
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": True,
                    "schema": {
                        "type": "string",
                        "minLength": 8,
                        "maxLength": 128,
                        "pattern": IDEMPOTENCY_KEY_PATTERN.pattern,
                    },
                    "description": (
                        "Stable key for one logical mutation; exact retries replay "
                        "the persisted response."
                    ),
                }
            )
            openapi_extra["parameters"] = parameters
            kwargs["openapi_extra"] = openapi_extra
        super().__init__(*args, **kwargs)

    def get_route_handler(self) -> Any:
        original_handler = super().get_route_handler()

        async def idempotent_handler(request: Request) -> Response:
            if request.method not in MUTATING_METHODS:
                return await original_handler(request)
            settings = cast(Settings, request.app.state.settings)
            actor: Actor | None = None
            if settings.rate_limit_enabled:
                actor = _authenticate_request(request)
                _enforce_rate_limit(request, actor)
                request.state.authenticated_actor = actor
            key = request.headers.get("idempotency-key")
            if key is None:
                if settings.require_idempotency_keys:
                    raise HTTPException(
                        status_code=status.HTTP_428_PRECONDITION_REQUIRED,
                        detail="Idempotency-Key header is required for mutating requests",
                    )
                return await original_handler(request)
            try:
                normalized_key = _idempotency_key(key)
            except ValueError as error:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=str(error),
                ) from error
            request_hash = await _request_hash(request, settings=settings)
            actor = actor or _authenticate_request(request)
            session_factory = cast(
                sessionmaker[Session],
                request.app.state.session_factory,
            )
            session = session_factory()
            request.state.database_session = session
            request.state.authenticated_actor = actor
            try:
                try:
                    with session.begin():
                        record, replay = IdempotencyService(session).acquire(
                            actor=actor,
                            idempotency_key=normalized_key,
                            request_method=request.method,
                            request_path=request.url.path,
                            request_hash=request_hash,
                            initial_request_id=getattr(
                                request.state,
                                "request_id",
                                f"request-{uuid4()}",
                            ),
                        )
                        if replay is not None:
                            return _replay_response(
                                replay=replay,
                                idempotency_key=normalized_key,
                            )
                        response = await original_handler(request)
                        if not 200 <= response.status_code < 300:
                            raise _RollbackResponse(response)
                        payload, has_body = _response_payload(response)
                        response_headers = {
                            name: value
                            for name, value in response.headers.items()
                            if name.lower() in REPLAY_HEADER_ALLOWLIST
                        }
                        IdempotencyService(session).complete(
                            row=record,
                            response_status=response.status_code,
                            response_media_type=response.headers.get("content-type"),
                            response_payload=payload,
                            response_has_body=has_body,
                            response_headers=response_headers,
                        )
                        response.headers["Idempotency-Key"] = normalized_key
                        response.headers["Idempotency-Replayed"] = "false"
                        return response
                except IdempotencyConflictError as error:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=str(error),
                    ) from error
                except IdempotencyInProgressError as error:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=str(error),
                    ) from error
                except _RollbackResponse as rollback:
                    return rollback.response
            finally:
                request.state.database_session = None
                request.state.authenticated_actor = None
                session.close()

        return idempotent_handler


@contextmanager
def mutation_transaction(session: Session) -> Iterator[None]:
    if session.in_transaction():
        yield
        return
    with session.begin():
        yield


def request_scoped_session(request: Request) -> Session | None:
    value = getattr(request.state, "database_session", None)
    return value if isinstance(value, Session) else None


def request_scoped_actor(request: Request) -> Actor | None:
    value = getattr(request.state, "authenticated_actor", None)
    return value if isinstance(value, Actor) else None


def _authenticate_request(request: Request) -> Actor:
    authenticator = cast(Authenticator, request.app.state.authenticator)
    return authenticator.authenticate(
        authorization=request.headers.get("authorization"),
        dev_actor=request.headers.get("x-dev-actor"),
        dev_organization=request.headers.get("x-dev-organization"),
        dev_roles=request.headers.get("x-dev-roles"),
    )


def _enforce_rate_limit(request: Request, actor: Actor) -> None:
    enforce = cast(
        Callable[[Request, Actor], None],
        request.app.state.enforce_rate_limit,
    )
    enforce(request, actor)


async def _request_hash(request: Request, *, settings: Settings) -> str:
    content_type = request.headers.get("content-type", "")
    media_type = content_type.partition(";")[0].strip().lower()
    body_descriptor: dict[str, Any]
    if media_type == "multipart/form-data":
        body_descriptor = {
            "multipart": await _multipart_descriptor(request),
        }
    else:
        body = await request.body()
        if len(body) > settings.max_api_request_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Request body exceeds configured API limit",
            )
        body_descriptor = {
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "body_size": len(body),
        }
    return content_hash(
        {
            "method": request.method,
            "path": request.url.path,
            "query": sorted(request.query_params.multi_items()),
            "content_type": media_type,
            **body_descriptor,
        }
    )


async def _multipart_descriptor(request: Request) -> list[dict[str, Any]]:
    form = await request.form()
    items: list[dict[str, Any]] = []
    for field_name, value in form.multi_items():
        if isinstance(value, UploadFile):
            digest = hashlib.sha256()
            size = 0
            while chunk := await value.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
            await value.seek(0)
            items.append(
                {
                    "field": field_name,
                    "filename": value.filename,
                    "content_type": value.content_type,
                    "size": size,
                    "sha256": digest.hexdigest(),
                }
            )
        else:
            items.append({"field": field_name, "value": str(value)})
    return sorted(
        items,
        key=lambda item: (
            str(item.get("field")),
            str(item.get("filename", "")),
            str(item.get("value", "")),
            str(item.get("sha256", "")),
        ),
    )


def _response_payload(response: Response) -> tuple[Any, bool]:
    body = getattr(response, "body", None)
    if not isinstance(body, bytes):
        raise RuntimeError("Idempotent mutation responses must be buffered")
    if not body:
        return None, False
    try:
        return json.loads(body), True
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Idempotent mutation responses must be JSON") from error


def _replay_response(
    *,
    replay: IdempotencyReplay,
    idempotency_key: str,
) -> Response:
    headers = {
        **replay.response_headers,
        "Idempotency-Key": idempotency_key,
        "Idempotency-Replayed": "true",
    }
    if not replay.response_has_body:
        return Response(
            status_code=replay.response_status,
            headers=headers,
            media_type=replay.response_media_type,
        )
    return JSONResponse(
        content=replay.response_payload,
        status_code=replay.response_status,
        headers=headers,
        media_type=replay.response_media_type or "application/json",
    )


def _idempotency_key(value: str) -> str:
    normalized = value.strip()
    if normalized != value or not IDEMPOTENCY_KEY_PATTERN.fullmatch(normalized):
        raise ValueError(
            "Idempotency-Key must be 8-128 characters using letters, digits, '.', '_', ':', '-'"
        )
    return normalized
