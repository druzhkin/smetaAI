import { describe, expect, it } from "vitest";

import {
  calculationBasisId,
  validateCalculationExecutionDraft,
} from "./calculationWorkflow";

describe("controlled calculation workflow", () => {
  it("requires the exact project code, attestation and meaningful reason", () => {
    const base = {
      reason: "Run the exact governed cost candidate",
      projectCode: "T-100",
      acknowledged: true,
    };
    expect(
      validateCalculationExecutionDraft(base, "T-100", true, []),
    ).toBeNull();
    expect(
      validateCalculationExecutionDraft(
        { ...base, projectCode: "T-101" },
        "T-100",
        true,
        [],
      ),
    ).toContain("T-100");
    expect(
      validateCalculationExecutionDraft(
        { ...base, acknowledged: false },
        "T-100",
        true,
        [],
      ),
    ).toContain("Подтвердите");
    expect(
      validateCalculationExecutionDraft(
        { ...base, reason: "short" },
        "T-100",
        true,
        [],
      ),
    ).toContain("10");
  });

  it("preserves server blockers and never treats a missing candidate as ready", () => {
    expect(
      validateCalculationExecutionDraft(
        {
          reason: "Run exact candidate",
          projectCode: "T-100",
          acknowledged: true,
        },
        "T-100",
        false,
        ["Price evidence is incomplete"],
      ),
    ).toBe("Price evidence is incomplete");
  });

  it("selects the single server-provided basis without calculating a value", () => {
    expect(
      calculationBasisId({
        source_observation_id: null,
        approved_assumption_id: null,
        normative_rate_id: null,
        risk_reserve_id: "risk-1",
        derived_cost_model_id: null,
      }),
    ).toBe("risk-1");
  });
});
