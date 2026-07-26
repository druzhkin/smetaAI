import { describe, expect, it } from "vitest";

import {
  validateContractFinalization,
  validateContractSubmission,
} from "./contractWorkflow";
import type { ContractContext, ContractTermReview } from "./types";

const context = {
  selected_kind: "FIXED_PRICE",
  independently_verified_term_kinds: [],
  unresolved_conflict_ids: [],
  evidence_candidates: [
    {
      observation: {
        observation_id: "obs-1",
        value: "Fixed",
        unit: null,
      },
      eligible: true,
      independence_domain: null,
    },
  ],
} as unknown as ContractContext;

describe("contract workflow guards", () => {
  it("accepts only exact server candidates", () => {
    expect(
      validateContractSubmission(
        {
          observationIds: ["obs-1"],
          reason: "Проверить условие договора",
          projectCodeConfirmation: "P-1",
          acknowledged: true,
        },
        context,
        "P-1",
      ),
    ).toBeNull();
    expect(
      validateContractSubmission(
        {
          observationIds: ["invented"],
          reason: "Проверить условие договора",
          projectCodeConfirmation: "P-1",
          acknowledged: true,
        },
        context,
        "P-1",
      ),
    ).toContain("серверного контекста");
  });

  it("blocks finalization until every task is approved", () => {
    const review = {
      term: {
        cost_impact_proposal: { amount: "0" },
        approval_task_ids: ["task-1"],
        cost_impact_task_statuses: { "task-1": "PENDING" },
      },
    } as unknown as ContractTermReview;
    expect(
      validateContractFinalization(
        {
          reason: "Зафиксировать проверенное влияние",
          projectCodeConfirmation: "P-1",
          acknowledged: true,
        },
        review,
        "P-1",
      ),
    ).toContain("Не все");
  });
});
