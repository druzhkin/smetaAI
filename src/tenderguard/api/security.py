from __future__ import annotations

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestSizeLimitMiddleware:
    """Reject known oversized bodies before multipart parsing.

    The endpoint also enforces the actual streamed file size. Production ingress
    must independently reject oversized/chunked bodies before they reach ASGI.
    """

    def __init__(
        self,
        app: ASGIApp,
        max_bytes: int,
        path_suffix_limits: dict[str, int] | None = None,
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.path_suffix_limits = path_suffix_limits or {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = Headers(scope=scope)
            content_length = headers.get("content-length")
            if content_length:
                effective_limit = min(
                    (
                        limit
                        for suffix, limit in self.path_suffix_limits.items()
                        if scope.get("path", "").endswith(suffix)
                    ),
                    default=self.max_bytes,
                )
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
        await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

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
                    ]
                )
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)
