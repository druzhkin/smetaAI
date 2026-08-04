import rawAlabugaSnapshot from "./data/alabuga-public-snapshot.json";
import type { BoqPriceMatrix } from "./types";

export interface PublicDiagnosticSnapshot {
  schema_version: "smetaai.public-diagnostic-snapshot/v1";
  project: {
    project_id: string;
    code: string;
    name: string;
  };
  source_hashes: Record<string, string | null>;
  summary: {
    boq_rows: number;
    blocked_rows: number;
    won_tender_candidates: number;
    fgis_candidates: number;
    market_candidates: number;
    observed_amounts: number;
  };
  matrix_content_sha256: string;
  matrix: BoqPriceMatrix;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasHttpsSourceUri(value: unknown): boolean {
  if (!isRecord(value) || typeof value.source_uri !== "string") {
    return false;
  }
  try {
    return new URL(value.source_uri).protocol === "https:";
  } catch {
    return false;
  }
}

export function parsePublicDiagnosticSnapshot(
  value: unknown,
): PublicDiagnosticSnapshot {
  if (!isRecord(value)) {
    throw new Error("Public diagnostic snapshot must be an object");
  }
  if (value.schema_version !== "smetaai.public-diagnostic-snapshot/v1") {
    throw new Error("Unsupported public diagnostic snapshot schema");
  }
  const project = value.project;
  const summary = value.summary;
  const matrix = value.matrix;
  if (
    !isRecord(project) ||
    typeof project.project_id !== "string" ||
    typeof project.code !== "string" ||
    typeof project.name !== "string" ||
    !isRecord(summary) ||
    !isRecord(matrix) ||
    !Array.isArray(matrix.rows)
  ) {
    throw new Error("Public diagnostic snapshot is structurally incomplete");
  }
  const numericSummaryFields = [
    "boq_rows",
    "blocked_rows",
    "won_tender_candidates",
    "fgis_candidates",
    "market_candidates",
    "observed_amounts",
  ] as const;
  if (
    numericSummaryFields.some(
      (field) =>
        !Number.isSafeInteger(summary[field]) || (summary[field] as number) < 0,
    )
  ) {
    throw new Error("Public diagnostic snapshot has invalid summary counters");
  }
  if (
    summary.boq_rows !== matrix.rows.length ||
    summary.blocked_rows !== matrix.rows.length ||
    matrix.blocked_row_count !== matrix.rows.length
  ) {
    throw new Error(
      "Public diagnostic snapshot does not preserve fail-closed row counts",
    );
  }

  for (const row of matrix.rows) {
    if (
      !isRecord(row) ||
      row.row_status !== "BLOCKED" ||
      typeof row.boq_item_name !== "string" ||
      typeof row.boq_unit !== "string" ||
      typeof row.line_key !== "string" ||
      !Array.isArray(row.blockers) ||
      !isRecord(row.proposed_price) ||
      row.proposed_price.status !== "BLOCKED" ||
      row.proposed_price.amount_per_unit !== null
    ) {
      throw new Error("Public diagnostic snapshot contains an unsafe BoQ row");
    }
    for (const fieldName of [
      "won_tender_research_candidates",
      "fgis_cs_research_candidates",
      "market_research_candidates",
    ] as const) {
      const candidates = row[fieldName];
      if (
        !Array.isArray(candidates) ||
        candidates.some(
          (candidate) =>
            !isRecord(candidate) ||
            candidate.status !== "BLOCKED" ||
            !hasHttpsSourceUri(candidate),
        )
      ) {
        throw new Error(
          "Public diagnostic snapshot contains an unsafe source candidate",
        );
      }
    }
  }

  return value as unknown as PublicDiagnosticSnapshot;
}

export function safeExternalHttpsUrl(value: string): string | null {
  try {
    const parsed = new URL(value);
    return parsed.protocol === "https:" ? parsed.toString() : null;
  } catch {
    return null;
  }
}

export const alabugaPublicSnapshot =
  parsePublicDiagnosticSnapshot(rawAlabugaSnapshot);
