from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from tenderguard.application.diagnostic_project import DiagnosticProject
from tenderguard.domain.enums import ActorRole
from tenderguard.infrastructure.auth import Actor

SCHEMA_VERSION = "smetaai.public-diagnostic-snapshot/v1"
MAX_SOURCE_BYTES = 64 * 1024 * 1024


def _sha256_json(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _assert_fail_closed_matrix(matrix: dict[str, Any]) -> None:
    rows = matrix.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("The public snapshot requires at least one BoQ row")
    if matrix.get("blocked_row_count") != len(rows):
        raise ValueError("Diagnostic snapshot must keep every BoQ row blocked")

    for row in rows:
        if not isinstance(row, dict) or row.get("row_status") != "BLOCKED":
            raise ValueError("Diagnostic snapshot contains a non-blocked BoQ row")
        proposed = row.get("proposed_price")
        if not isinstance(proposed, dict):
            raise ValueError("Diagnostic snapshot row has no proposed-price decision")
        if proposed.get("status") != "BLOCKED" or proposed.get("amount_per_unit") is not None:
            raise ValueError("Diagnostic snapshot must not publish an unreleased bid price")
        for field_name in (
            "won_tender_prices",
            "fgis_cs_prices",
            "market_prices",
            "other_prices",
        ):
            if row.get(field_name) != []:
                raise ValueError(
                    f"Diagnostic snapshot unexpectedly contains governed values in {field_name}"
                )
        for field_name in (
            "won_tender_research_candidates",
            "fgis_cs_research_candidates",
            "market_research_candidates",
        ):
            candidates = row.get(field_name)
            if not isinstance(candidates, list):
                raise ValueError(f"Diagnostic snapshot has invalid {field_name}")
            for candidate in candidates:
                if not isinstance(candidate, dict) or candidate.get("status") != "BLOCKED":
                    raise ValueError("Research candidate escaped fail-closed status")
                source_uri = candidate.get("source_uri")
                if not isinstance(source_uri, str) or not source_uri.startswith("https://"):
                    raise ValueError("Every public research candidate must use a direct HTTPS URL")


def build_snapshot(manifest_path: Path) -> dict[str, object]:
    project = DiagnosticProject.load(
        manifest_path,
        max_extraction_bytes=MAX_SOURCE_BYTES,
    )
    actor = Actor(
        actor_id="public-snapshot-builder",
        organization_id=project.manifest.organization_id,
        roles=frozenset({ActorRole.AUDITOR}),
    )
    matrix = json.loads(project.price_matrix(actor=actor).model_dump_json())
    if not isinstance(matrix, dict):
        raise ValueError("Price matrix serialization must produce an object")
    _assert_fail_closed_matrix(matrix)

    rows = matrix["rows"]
    fgis_candidates = sum(
        len(row["fgis_cs_research_candidates"]) for row in rows if isinstance(row, dict)
    )
    market_candidates = sum(
        len(row["market_research_candidates"]) for row in rows if isinstance(row, dict)
    )
    tender_candidates = sum(
        len(row["won_tender_research_candidates"]) for row in rows if isinstance(row, dict)
    )
    observed_amounts = sum(
        len(candidate["observed_amounts"])
        for row in rows
        if isinstance(row, dict)
        for field_name in (
            "won_tender_research_candidates",
            "fgis_cs_research_candidates",
            "market_research_candidates",
        )
        for candidate in row[field_name]
        if isinstance(candidate, dict)
    )
    research = project.manifest.research
    source_hashes = {
        "extraction": project.manifest.extraction_sha256,
        "free_source_research": (
            research.free_source_research.sha256 if research is not None else None
        ),
        "fgis_history": research.fgis_history.sha256 if research is not None else None,
        "market_research": research.market_research.sha256 if research is not None else None,
        "market_assessment": (research.market_assessment.sha256 if research is not None else None),
    }
    snapshot: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "project": {
            "project_id": project.manifest.project_id,
            "code": project.manifest.code,
            "name": project.manifest.name,
        },
        "source_hashes": source_hashes,
        "summary": {
            "boq_rows": len(rows),
            "blocked_rows": matrix["blocked_row_count"],
            "won_tender_candidates": tender_candidates,
            "fgis_candidates": fgis_candidates,
            "market_candidates": market_candidates,
            "observed_amounts": observed_amounts,
        },
        "matrix_content_sha256": _sha256_json(matrix),
        "matrix": matrix,
    }
    return snapshot


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the checked, public read-only Alabuga diagnostic snapshot."
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("var/diagnostic-projects/alabuga-4527946.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("web/src/data/alabuga-public-snapshot.json"),
    )
    args = parser.parse_args()

    payload = build_snapshot(args.manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"Wrote {args.output} with {payload['summary']} "
        f"and matrix SHA-256 {payload['matrix_content_sha256']}"
    )


if __name__ == "__main__":
    main()
