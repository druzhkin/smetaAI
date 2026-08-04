import { describe, expect, it } from "vitest";

import {
  alabugaPublicSnapshot,
  parsePublicDiagnosticSnapshot,
  safeExternalHttpsUrl,
} from "./publicSnapshot";

describe("public diagnostic snapshot", () => {
  it("contains the complete fail-closed Alabuga matrix", () => {
    expect(alabugaPublicSnapshot.summary).toEqual({
      boq_rows: 23,
      blocked_rows: 23,
      won_tender_candidates: 0,
      fgis_candidates: 73,
      work_rows: 15,
      material_rows: 7,
      logistics_rows: 1,
      fgis_catalog_candidates: 60,
      fgis_selected_codes: 60,
      fgis_queried_periods: 13,
      fgis_raw_responses: 783,
      fgis_rows_with_published_prices: 2,
      fgis_published_observations: 16,
      fgis_codes_with_published_prices: 3,
      fgis_exact_literal_published_observations: 4,
      fgis_alternative_published_observations: 12,
      market_candidates: 22,
      observed_amounts: 70,
    });
    expect(alabugaPublicSnapshot.matrix.rows).toHaveLength(23);
    expect(
      alabugaPublicSnapshot.matrix.rows.every(
        (row) => row.proposed_price.amount_per_unit === null,
      ),
    ).toBe(true);
  });

  it("rejects active or malformed source URLs", () => {
    const unsafe = structuredClone(alabugaPublicSnapshot) as unknown as {
      matrix: {
        rows: Array<{
          fgis_cs_research_candidates: Array<{ source_uri: string }>;
        }>;
      };
    };
    const row = unsafe.matrix.rows.find(
      (candidateRow) => candidateRow.fgis_cs_research_candidates.length > 0,
    );
    expect(row).toBeDefined();
    if (row === undefined) return;
    row.fgis_cs_research_candidates[0]!.source_uri = "javascript:alert(1)";

    expect(() => parsePublicDiagnosticSnapshot(unsafe)).toThrow(
      "unsafe source candidate",
    );
    expect(safeExternalHttpsUrl("javascript:alert(1)")).toBeNull();
    expect(safeExternalHttpsUrl("https://example.com/item")).toBe(
      "https://example.com/item",
    );
  });
});
