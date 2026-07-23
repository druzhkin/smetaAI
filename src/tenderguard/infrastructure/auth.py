from __future__ import annotations

from dataclasses import dataclass

import jwt
from fastapi import Header, HTTPException, status
from jwt import PyJWKClient

from tenderguard.config import Settings
from tenderguard.domain.enums import ActorRole


@dataclass(frozen=True)
class Actor:
    actor_id: str
    organization_id: str
    roles: frozenset[ActorRole]

    def require_any(self, *roles: ActorRole) -> None:
        if not self.roles.intersection(roles):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Required role is missing",
            )


class Authenticator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._jwks: PyJWKClient | None = (
            PyJWKClient(settings.oidc_jwks_url) if settings.oidc_jwks_url else None
        )

    def authenticate(
        self,
        *,
        authorization: str | None,
        dev_actor: str | None,
        dev_organization: str | None,
        dev_roles: str | None,
    ) -> Actor:
        if self.settings.allow_insecure_dev_auth:
            if not (dev_actor and dev_organization):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Development actor and organization headers are required",
                )
            roles = _parse_roles(dev_roles or "")
            return Actor(dev_actor, dev_organization, roles)
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Bearer token is required",
            )
        if not (self._jwks and self.settings.oidc_audience and self.settings.oidc_issuer):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="OIDC authentication is not configured",
            )
        token = authorization.removeprefix("Bearer ").strip()
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                audience=self.settings.oidc_audience,
                issuer=self.settings.oidc_issuer,
                options={"require": ["exp", "iat", "sub"]},
            )
        except jwt.PyJWTError as error:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token validation failed",
            ) from error
        organization_id = claims.get("organization_id")
        if not organization_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Token lacks organization scope",
            )
        raw_roles = claims.get("roles", [])
        roles = _parse_roles(",".join(raw_roles) if isinstance(raw_roles, list) else str(raw_roles))
        return Actor(str(claims["sub"]), str(organization_id), roles)


def _parse_roles(value: str) -> frozenset[ActorRole]:
    parsed: set[ActorRole] = set()
    for item in value.split(","):
        candidate = item.strip().upper()
        if not candidate:
            continue
        try:
            parsed.add(ActorRole(candidate))
        except ValueError:
            continue
    return frozenset(parsed)


def actor_headers(
    authorization: str | None = Header(default=None),
    x_dev_actor: str | None = Header(default=None),
    x_dev_organization: str | None = Header(default=None),
    x_dev_roles: str | None = Header(default=None),
) -> tuple[str | None, str | None, str | None, str | None]:
    return authorization, x_dev_actor, x_dev_organization, x_dev_roles
