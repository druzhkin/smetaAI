import { describe, expect, it } from "vitest";

import {
  finalReviewCandidates,
  validateFinalRework,
} from "./finalReviewWorkflow";
import type { BoqPriceMatrix, GateDecision } from "./types";

const decision: GateDecision = {
  requested_state: "APPROVED_FOR_BID",
  allowed: false,
  resulting_state: "BLOCKED",
  findings: [
    {
      code: "CRITICAL_DOCUMENT_MISSING",
      severity: "BLOCKER",
      message: "Не хватает критического документа",
      entity_ids: ["document-1"],
      details: {},
    },
  ],
};

const matrix = {
  project_id: "project-1",
  generated_at: "2026-07-31T12:00:00Z",
  blocked_row_count: 1,
  release_warning: "release remains authoritative",
  rows: [
    {
      row_id: "line-1:1",
      boq_line_id: "line-1",
      line_key: "1",
      wbs_node_id: "wbs-1",
      work_code: "work-1",
      boq_item_name: "Кабель силовой",
      boq_unit: "м",
      quantity: "100",
      quantity_status: "VERIFIED",
      item_id: "item-1",
      cost_category: "MATERIAL",
      basis_kind: "MARKET",
      row_status: "BLOCKED",
      blockers: ["MARKET_PRICE_MISSING"],
      name_match: null,
      won_tender_prices: [],
      fgis_cs_prices: [],
      market_prices: [],
      other_prices: [],
      proposed_price: {
        status: "BLOCKED",
        workflow_status: "MISSING",
        amount_per_unit: null,
        currency: null,
        unit: null,
        decision_id: null,
        as_of: null,
        selection_method: null,
        normalized_price_ids: [],
        rationale: [],
      },
    },
  ],
} satisfies BoqPriceMatrix;

describe("final expert review workflow", () => {
  it("creates exact current row and release-finding references", () => {
    const candidates = finalReviewCandidates(matrix, decision);
    expect(candidates).toHaveLength(2);
    expect(candidates[0]).toMatchObject({
      kind: "BOQ_PRICE_ROW",
      reference_id: "line-1:1",
      code: "MARKET_PRICE_MISSING",
    });
    expect(candidates[1]).toMatchObject({
      kind: "RELEASE_FINDING",
      reference_id: "document-1",
      code: "CRITICAL_DOCUMENT_MISSING",
    });
  });

  it("requires a selection and a substantive reason", () => {
    expect(validateFinalRework(new Set(), "Достаточная причина")).toContain(
      "Выберите",
    );
    expect(validateFinalRework(new Set(["row"]), "коротко")).toContain("от 10");
    expect(
      validateFinalRework(
        new Set(["row"]),
        "Повторно проверить источник и условия доставки.",
      ),
    ).toBeNull();
  });
});
