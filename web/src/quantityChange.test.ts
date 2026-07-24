import { describe, expect, it } from "vitest";

import {
  draftFromQuantityContext,
  validateQuantityChangeDraft,
} from "./quantityChange";
import type { QuantityChangeContext } from "./types";

const context: QuantityChangeContext = {
  project_id: "project-1",
  line_id: "line-1",
  line_key: "line",
  description: "Монтаж трубопровода",
  unit: "m",
  current_quantity_id: "quantity-1",
  current_quantity_status: "VERIFIED",
  current_submission: {
    draft: {
      value: "100",
      unit: "m",
      source_observation_ids: ["observation-1"],
      source_priority: 1,
      rounding_scale: 2,
      waste_factor: "0",
      alternative_quantity_ids: [],
      manual_change_id: null,
    },
    formula: null,
    formula_input_observation_ids: {},
  },
  document_set_revision_id: "documents-1",
  quantity_policy_version_id: "quantity-policy-1",
  quantity_formula_rules_version_id: "formula-rules-1",
  manual_change_policy_version_id: "manual-change-policy-1",
  critical: true,
  approval_role: "REVIEWER",
};

describe("quantity manual-change validation", () => {
  it("builds an exact submission only after explicit acknowledgement", () => {
    const draft = {
      ...draftFromQuantityContext(context),
      value: "101.25",
      sourceObservationIds: "observation-2",
      reason: "Исправлено по проверенной спецификации",
      projectCode: "T-001",
      acknowledged: true,
    };

    expect(validateQuantityChangeDraft(draft, context, "T-001")).toEqual({
      error: null,
      submission: {
        draft: {
          value: "101.25",
          unit: "m",
          source_observation_ids: ["observation-2"],
          source_priority: 1,
          rounding_scale: 2,
          waste_factor: "0",
          alternative_quantity_ids: [],
          manual_change_id: null,
        },
        formula: null,
        formula_input_observation_ids: {},
      },
    });
  });

  it("rejects no-op, duplicate evidence, and floating-point JSON inputs", () => {
    const base = {
      ...draftFromQuantityContext(context),
      reason: "Проверка",
      projectCode: "T-001",
      acknowledged: true,
    };
    expect(validateQuantityChangeDraft(base, context, "T-001").error).toMatch(
      /совпадает/,
    );
    expect(
      validateQuantityChangeDraft(
        {
          ...base,
          value: "101",
          sourceObservationIds: "observation-1\nobservation-1",
        },
        context,
        "T-001",
      ).error,
    ).toMatch(/дубликаты/);
    expect(
      validateQuantityChangeDraft(
        {
          ...base,
          value: "101",
          formulaEnabled: true,
          formulaId: "formula-1",
          formulaDisplay: "length",
          formulaInputsJson: '{"length": 101}',
          formulaEvidenceJson: '{"length": "observation-1"}',
        },
        context,
        "T-001",
      ).error,
    ).toMatch(/строковые значения/);
  });
});
