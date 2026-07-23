from __future__ import annotations

import logging
import re
import sys
from time import perf_counter
from uuid import uuid4

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def configure_logging() -> None:
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.INFO,
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


class RequestLoggingMiddleware:
    """Metadata-only access log; request bodies and auth headers are excluded."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.logger = structlog.get_logger("tenderguard.http")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = perf_counter()
        status_code = 500
        raw_headers = dict(scope.get("headers", []))
        supplied_request_id = raw_headers.get(b"x-request-id", b"").decode("ascii", errors="ignore")
        request_id = (
            supplied_request_id
            if REQUEST_ID_PATTERN.fullmatch(supplied_request_id)
            else f"request-{uuid4()}"
        )
        scope.setdefault("state", {})["request_id"] = request_id

        async def capture_status(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = list(message.get("headers", []))
                headers = [
                    (name, value) for name, value in headers if name.lower() != b"x-request-id"
                ]
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, capture_status)
        finally:
            self.logger.info(
                "http_request",
                request_id=request_id,
                method=scope.get("method"),
                path=scope.get("path"),
                status_code=status_code,
                duration_ms=round((perf_counter() - started) * 1000, 2),
            )
