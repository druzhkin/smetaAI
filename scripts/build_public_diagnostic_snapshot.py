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
    fgis_published_candidates = [
        candidate
        for row in rows
        if isinstance(row, dict)
        for candidate in row["fgis_cs_research_candidates"]
        if isinstance(candidate, dict) and candidate["observed_amounts"]
    ]
    fgis_exact_literal_published_observations = sum(
        candidate["comparison_method"] == "EXACT_LITERAL_NAME_AND_UNIT"
        for candidate in fgis_published_candidates
    )
    fgis_alternative_published_observations = (
        len(fgis_published_candidates) - fgis_exact_literal_published_observations
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
    research_bundle = project.research
    cost_nature_counts = {"WORK": 0, "MATERIAL": 0, "LOGISTICS": 0}
    fgis_catalog_candidates = 0
    fgis_selected_codes = 0
    fgis_queried_periods = 0
    fgis_raw_responses = 0
    fgis_rows_with_published_prices = 0
    fgis_published_observations = 0
    fgis_codes_with_published_prices = 0
    if research_bundle is not None:
        for line in research_bundle.free_source.lines:
            cost_nature_counts[line.cost_nature] += 1
            if line.fgis_search_result is not None:
                fgis_catalog_candidates += len(line.fgis_search_result.candidates)
        if research_bundle.fgis_history is not None:
            history = research_bundle.fgis_history
            fgis_selected_codes = len(history.history.resource_codes)
            fgis_queried_periods = len(history.history.periods)
            fgis_raw_responses = len(history.raw_responses)
            fgis_rows_with_published_prices = sum(
                line.published_observation_count > 0 for line in history.line_results
            )
            fgis_published_observations = sum(
                line.published_observation_count for line in history.line_results
            )
            fgis_codes_with_published_prices = len(
                {
                    observation.requested_resource_code
                    for observation in history.history.observations
                    if observation.price is not None
                }
            )
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
            "work_rows": cost_nature_counts["WORK"],
            "material_rows": cost_nature_counts["MATERIAL"],
            "logistics_rows": cost_nature_counts["LOGISTICS"],
            "fgis_catalog_candidates": fgis_catalog_candidates,
            "fgis_selected_codes": fgis_selected_codes,
            "fgis_queried_periods": fgis_queried_periods,
            "fgis_raw_responses": fgis_raw_responses,
            "fgis_rows_with_published_prices": fgis_rows_with_published_prices,
            "fgis_published_observations": fgis_published_observations,
            "fgis_codes_with_published_prices": fgis_codes_with_published_prices,
            "fgis_exact_literal_published_observations": (
                fgis_exact_literal_published_observations
            ),
            "fgis_alternative_published_observations": (
                fgis_alternative_published_observations
            ),
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
