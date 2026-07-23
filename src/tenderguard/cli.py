from __future__ import annotations

import argparse
import json

from sqlalchemy import text

from tenderguard.config import get_settings
from tenderguard.infrastructure.database import create_database_engine
from tenderguard.infrastructure.object_store import build_object_store


def doctor() -> int:
    settings = get_settings()
    checks: dict[str, bool | str] = {
        "environment": settings.app_env,
        "database": False,
        "object_store": False,
        "oidc_configured": bool(
            settings.allow_insecure_dev_auth
            or (settings.oidc_issuer and settings.oidc_audience and settings.oidc_jwks_url)
        ),
        "normative_adapter_configured": settings.normative_adapter_configured,
    }
    engine = create_database_engine(settings)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception as error:
        checks["database_error"] = type(error).__name__
    finally:
        engine.dispose()
    try:
        checks["object_store"] = build_object_store(settings).healthcheck()
    except Exception as error:
        checks["object_store_error"] = type(error).__name__
    print(json.dumps(checks, ensure_ascii=False, indent=2, sort_keys=True))
    return (
        0
        if all(checks[key] is True for key in ("database", "object_store", "oidc_configured"))
        else 1
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="tenderguard")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("doctor", help="Check runtime dependencies without changing data")
    arguments = parser.parse_args()
    if arguments.command == "doctor":
        raise SystemExit(doctor())


if __name__ == "__main__":
    main()
