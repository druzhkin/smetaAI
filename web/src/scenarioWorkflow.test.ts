import { describe, expect, it } from "vitest";

import { validateScenarioExecutionDraft } from "./scenarioWorkflow";

describe("controlled scenario workflow", () => {
  const validDraft = {
    reason: "Evaluate the approved supplier stress scenario",
    projectCode: "T-100",
    acknowledged: true,
  };

  it("requires an exact project code, attestation and meaningful reason", () => {
    expect(
      validateScenarioExecutionDraft(
        validDraft,
        "T-100",
        "snapshot-1",
        "supplier-stress",
        [],
      ),
    ).toBeNull();
    expect(
      validateScenarioExecutionDraft(
        { ...validDraft, projectCode: "T-101" },
        "T-100",
        "snapshot-1",
        "supplier-stress",
        [],
      ),
    ).toContain("T-100");
    expect(
      validateScenarioExecutionDraft(
        { ...validDraft, acknowledged: false },
        "T-100",
        "snapshot-1",
        "supplier-stress",
        [],
      ),
    ).toContain("Подтвердите");
    expect(
      validateScenarioExecutionDraft(
        { ...validDraft, reason: "short" },
        "T-100",
        "snapshot-1",
        "supplier-stress",
        [],
      ),
    ).toContain("10");
  });

  it("fails closed on missing governed selections and server blockers", () => {
    expect(
      validateScenarioExecutionDraft(
        validDraft,
        "T-100",
        null,
        "supplier-stress",
        [],
      ),
    ).toContain("snapshot");
    expect(
      validateScenarioExecutionDraft(
        validDraft,
        "T-100",
        "snapshot-1",
        null,
        [],
      ),
    ).toContain("сценарий");
    expect(
      validateScenarioExecutionDraft(
        validDraft,
        "T-100",
        "snapshot-1",
        "supplier-stress",
        ["SCENARIO_POLICY_INTEGRITY_FAILED"],
      ),
    ).toBe("SCENARIO_POLICY_INTEGRITY_FAILED");
  });
});
