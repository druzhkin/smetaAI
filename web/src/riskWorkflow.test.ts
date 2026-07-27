import { describe, expect, it } from "vitest";

import {
  validateRiskCalculation,
  validateRiskSubmission,
} from "./riskWorkflow";
import type { RiskContext } from "./types";

const context = {
  selected_risk_key: "supplier-delay",
  independently_verified_risk_keys: [],
  unresolved_conflict_ids: [],
  calculation_blockers: [],
  evidence_candidates: [
    {
      observation: { observation_id: "obs-1" },
      eligible: true,
      independence_domain: null,
      draft: {
        risk_key: "supplier-delay",
        description: "Supplier delay",
        probability: "0.2",
        impact_min: "10",
        impact_most_likely: "20",
        impact_max: "30",
        currency: "RUB",
        observation_ids: ["obs-1"],
        correlated: false,
        correlation_group: null,
        mitigation_cost_input_id: null,
      },
    },
  ],
} as unknown as RiskContext;

const attestation = {
  reason: "Проверить риск поставки",
  projectCodeConfirmation: "P-1",
  acknowledged: true,
};

describe("risk workflow guards", () => {
  it("accepts only exact server-held risk candidates", () => {
    expect(
      validateRiskSubmission(
        { ...attestation, observationIds: ["obs-1"] },
        context,
        "P-1",
      ),
    ).toBeNull();
    expect(
      validateRiskSubmission(
        { ...attestation, observationIds: ["invented"] },
        context,
        "P-1",
      ),
    ).toContain("серверного контекста");
  });

  it("blocks reserve calculation when the server reports a hard stop", () => {
    expect(
      validateRiskCalculation(
        attestation,
        { ...context, calculation_blockers: ["RISK_REQUIRED_MISSING"] },
        "P-1",
      ),
    ).toContain("RISK_REQUIRED_MISSING");
  });

  it("blocks correlated risk even when a candidate is otherwise eligible", () => {
    const correlated = {
      ...context,
      evidence_candidates: [
        {
          ...context.evidence_candidates[0],
          draft: {
            ...context.evidence_candidates[0]!.draft!,
            correlated: true,
            correlation_group: "imports",
          },
        },
      ],
    } as RiskContext;
    expect(
      validateRiskSubmission(
        { ...attestation, observationIds: ["obs-1"] },
        correlated,
        "P-1",
      ),
    ).toContain("Коррелированный риск");
  });
});
