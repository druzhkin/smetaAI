from __future__ import annotations

from collections.abc import Sequence

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class _RequestBodyTooLarge(Exception):
    pass


class RequestSizeLimitMiddleware:
    """Reject declared or streamed oversized bodies before application logic.

    The endpoint also enforces the actual file size. Production ingress must
    independently enforce the same or stricter limits.
    """

    def __init__(
        self,
        app: ASGIApp,
        max_bytes: int,
        max_non_multipart_bytes: int,
        path_suffix_limits: dict[str, int] | None = None,
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.max_non_multipart_bytes = max_non_multipart_bytes
        self.path_suffix_limits = path_suffix_limits or {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = Headers(scope=scope)
            content_type = headers.get("content-type", "").lower()
            default_limit = (
                self.max_bytes
                if content_type.startswith("multipart/form-data")
                else self.max_non_multipart_bytes
            )
            effective_limit = min(
                (
                    limit
                    for suffix, limit in self.path_suffix_limits.items()
                    if scope.get("path", "").endswith(suffix)
                ),
                default=default_limit,
            )
            content_length = headers.get("content-length")
            if content_length:
                try:
                    declared = int(content_length)
                except ValueError:
                    response = JSONResponse(
                        {"detail": "Invalid Content-Length"},
                        status_code=400,
                    )
                    await response(scope, receive, send)
                    return
                if declared > effective_limit:
                    response = JSONResponse(
                        {"detail": "Request body exceeds configured limit"},
                        status_code=413,
                    )
                    await response(scope, receive, send)
                    return
            received = 0
            body_too_large = False

            async def limited_receive() -> Message:
                nonlocal body_too_large, received
                message = await receive()
                if message["type"] == "http.request":
                    received += len(message.get("body", b""))
                    if received > effective_limit:
                        body_too_large = True
                        raise _RequestBodyTooLarge
                return message

            async def guarded_send(message: Message) -> None:
                if not body_too_large:
                    await send(message)

            try:
                await self.app(scope, limited_receive, guarded_send)
            except _RequestBodyTooLarge:
                body_too_large = True
            if body_too_large:
                response = JSONResponse(
                    {"detail": "Request body exceeds configured limit"},
                    status_code=413,
                )
                await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        connect_sources: Sequence[str] = (),
        include_hsts: bool = False,
    ) -> None:
        self.app = app
        self.include_hsts = include_hsts
        sources = " ".join(("'self'", *connect_sources))
        self.content_security_policy = (
            "default-src 'self'; "
            "base-uri 'none'; "
            f"connect-src {sources}; "
            "font-src 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'; "
            "img-src 'self' data:; "
            "object-src 'none'; "
            "script-src 'self'; "
            "style-src 'self'"
        ).encode("ascii")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(
                    [
                        (b"x-content-type-options", b"nosniff"),
                        (b"x-frame-options", b"DENY"),
                        (b"referrer-policy", b"no-referrer"),
                        (b"cache-control", b"no-store"),
                        (b"content-security-policy", self.content_security_policy),
                        (b"cross-origin-opener-policy", b"same-origin"),
                        (b"cross-origin-resource-policy", b"same-origin"),
                        (
                            b"permissions-policy",
                            b"camera=(), geolocation=(), microphone=(), payment=(), usb=()",
                        ),
                    ]
                )
                if self.include_hsts:
                    headers.append(
                        (
                            b"strict-transport-security",
                            b"max-age=31536000; includeSubDomains",
                        )
                    )
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)
