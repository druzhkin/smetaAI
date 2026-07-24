from __future__ import annotations

import http.client
import json
import ssl
from datetime import datetime
from urllib.parse import urlsplit

from pydantic import SecretStr

from tenderguard.domain.common import canonical_json, utc_now
from tenderguard.domain.integration import (
    SignedIntegrationEnvelope,
    SignedIntegrationReceipt,
)
from tenderguard.integrations.contracts import (
    AdapterQualification,
    ConnectorDeliveryError,
    ConnectorHealth,
)

_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


class HttpsJsonIntegrationConnector:
    """Bounded TLS JSON transport; business trust still comes from signed receipts."""

    def __init__(
        self,
        *,
        qualification: AdapterQualification,
        endpoint: str,
        allowed_hosts: frozenset[str],
        timeout_seconds: int,
        max_response_bytes: int,
        bearer_token: SecretStr | None = None,
        ssl_context: ssl.SSLContext | None = None,
        health_path: str = "/health",
    ) -> None:
        parsed = urlsplit(endpoint)
        normalized_hosts = frozenset(item.strip().lower() for item in allowed_hosts if item.strip())
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.query
        ):
            raise ValueError(
                "Integration endpoint must be an absolute HTTPS URL without credentials"
            )
        if parsed.hostname.lower() not in normalized_hosts:
            raise ValueError("Integration endpoint host is not allowlisted")
        if timeout_seconds <= 0:
            raise ValueError("Integration connector timeout must be positive")
        if max_response_bytes <= 0:
            raise ValueError("Integration connector response limit must be positive")
        if not health_path.startswith("/") or "?" in health_path or "#" in health_path:
            raise ValueError("Integration health path is invalid")
        if bearer_token is not None and not bearer_token.get_secret_value():
            raise ValueError("Integration bearer token is empty")
        self.qualification = qualification
        self._host = parsed.hostname
        self._port = parsed.port or 443
        self._path = parsed.path or "/"
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._bearer_token = bearer_token
        self._ssl_context = ssl_context or ssl.create_default_context()
        self._health_path = health_path

    def deliver(self, envelope: SignedIntegrationEnvelope) -> SignedIntegrationReceipt:
        payload = canonical_json(envelope)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Content-Length": str(len(payload)),
            "Idempotency-Key": envelope.body.delivery_deduplication_key,
            "X-TenderGuard-Message-Id": envelope.body.message_id,
        }
        if self._bearer_token is not None:
            headers["Authorization"] = f"Bearer {self._bearer_token.get_secret_value()}"
        status, media_type, response_payload = self._request(
            method="POST",
            path=self._path,
            body=payload,
            headers=headers,
        )
        if status in _RETRYABLE_STATUS_CODES:
            raise ConnectorDeliveryError(
                error_code=f"CONNECTOR_HTTP_{status}",
                retryable=True,
            )
        if not (200 <= status < 300 or status == 409):
            raise ConnectorDeliveryError(
                error_code=f"CONNECTOR_HTTP_{status}",
                retryable=False,
            )
        if media_type != "application/json":
            raise ConnectorDeliveryError(
                error_code="CONNECTOR_RESPONSE_MEDIA_TYPE_INVALID",
                retryable=False,
            )
        try:
            raw = json.loads(response_payload.decode("utf-8"))
            return SignedIntegrationReceipt.model_validate(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise ConnectorDeliveryError(
                error_code="CONNECTOR_RECEIPT_INVALID",
                retryable=False,
            ) from error

    def health(self) -> ConnectorHealth:
        checked_at = utc_now()
        try:
            status, media_type, payload = self._request(
                method="GET",
                path=self._health_path,
                body=None,
                headers={"Accept": "application/json"},
            )
            if status != 200 or media_type != "application/json":
                return ConnectorHealth(
                    connector_name=self.qualification.adapter_name,
                    healthy=False,
                    checked_at=checked_at,
                    source_as_of=None,
                    message=f"HTTP_{status}",
                )
            raw = json.loads(payload.decode("utf-8"))
            source_as_of_raw = raw.get("source_as_of") if isinstance(raw, dict) else None
            source_as_of = (
                datetime.fromisoformat(source_as_of_raw)
                if isinstance(source_as_of_raw, str)
                else None
            )
            healthy = bool(raw.get("healthy")) if isinstance(raw, dict) else False
            return ConnectorHealth(
                connector_name=self.qualification.adapter_name,
                healthy=healthy,
                checked_at=checked_at,
                source_as_of=source_as_of,
                message=(
                    str(raw.get("message", "")) if isinstance(raw, dict) else "INVALID_HEALTH_BODY"
                ),
            )
        except (
            ConnectorDeliveryError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
        ):
            return ConnectorHealth(
                connector_name=self.qualification.adapter_name,
                healthy=False,
                checked_at=checked_at,
                source_as_of=None,
                message="CONNECTOR_HEALTH_FAILED",
            )

    def _request(
        self,
        *,
        method: str,
        path: str,
        body: bytes | None,
        headers: dict[str, str],
    ) -> tuple[int, str, bytes]:
        connection = http.client.HTTPSConnection(
            self._host,
            port=self._port,
            timeout=self._timeout_seconds,
            context=self._ssl_context,
        )
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read(self._max_response_bytes + 1)
            if len(payload) > self._max_response_bytes:
                raise ConnectorDeliveryError(
                    error_code="CONNECTOR_RESPONSE_TOO_LARGE",
                    retryable=False,
                )
            media_type = response.headers.get_content_type().lower()
            return response.status, media_type, payload
        except ConnectorDeliveryError:
            raise
        except (OSError, TimeoutError, http.client.HTTPException) as error:
            raise ConnectorDeliveryError(
                error_code="CONNECTOR_TRANSPORT_FAILED",
                retryable=True,
            ) from error
        finally:
            connection.close()
