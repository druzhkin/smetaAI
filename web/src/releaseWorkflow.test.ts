import { describe, expect, it } from "vitest";

import {
  releaseStateEligible,
  releaseTargetState,
  validateReleaseDraft,
  type ReleaseDraft,
} from "./releaseWorkflow";
import type { GateDecision } from "./types";

const allowedBid: GateDecision = {
  requested_state: "APPROVED_FOR_BID",
  allowed: true,
  resulting_state: "APPROVED_FOR_BID",
  findings: [],
};

const draft: ReleaseDraft = {
  target: "bid",
  reason: "Independent hard-stop review completed",
  projectCode: "TG-100",
  targetState: "APPROVED_FOR_BID",
  acknowledged: true,
};

describe("controlled release workflow", () => {
  it("requires four-eyes authority and exact typed attestations", () => {
    expect(
      validateReleaseDraft(draft, "TG-100", "EXPERT_REVIEW", allowedBid, true),
    ).toBeNull();
    expect(
      validateReleaseDraft(draft, "TG-100", "EXPERT_REVIEW", allowedBid, false),
    ).toContain("утверждающего");
    expect(
      validateReleaseDraft(
        { ...draft, projectCode: "TG-101" },
        "TG-100",
        "EXPERT_REVIEW",
        allowedBid,
        true,
      ),
    ).toContain("TG-100");
    expect(
      validateReleaseDraft(
        { ...draft, targetState: "APPROVED" },
        "TG-100",
        "EXPERT_REVIEW",
        allowedBid,
        true,
      ),
    ).toContain("APPROVED_FOR_BID");
  });

  it("fails closed on findings and illegal workflow states", () => {
    const blocked: GateDecision = {
      ...allowedBid,
      allowed: false,
      resulting_state: "BLOCKED",
      findings: [
        {
          code: "CALCULATION_SNAPSHOT_MISSING",
          severity: "BLOCKING",
          message: "Fixed calculation snapshot is absent",
          entity_ids: [],
          details: {},
        },
      ],
    };
    expect(
      validateReleaseDraft(draft, "TG-100", "EXPERT_REVIEW", blocked, true),
    ).toBe("Fixed calculation snapshot is absent");
    expect(
      validateReleaseDraft(
        draft,
        "TG-100",
        "CALCULATION_IN_PROGRESS",
        allowedBid,
        true,
      ),
    ).toContain("недоступен");
  });

  it("models the only release-state transitions exposed by the UI", () => {
    expect(releaseTargetState("internal")).toBe(
      "APPROVED_FOR_INTERNAL_USE",
    );
    expect(releaseStateEligible("EXPERT_REVIEW", "internal")).toBe(true);
    expect(releaseStateEligible("APPROVED_FOR_INTERNAL_USE", "internal")).toBe(
      false,
    );
    expect(releaseStateEligible("APPROVED_FOR_INTERNAL_USE", "bid")).toBe(
      true,
    );
  });
});
