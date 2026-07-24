import { describe, expect, it } from "vitest";

import { parseIdentifierList, validateDecisionDraft } from "./decision";

describe("approval decision controls", () => {
  it("normalizes explicit evidence identifiers without duplicating them", () => {
    expect(parseIdentifierList("obs-1,\nobs-2; obs-1")).toEqual([
      "obs-1",
      "obs-2",
    ]);
  });

  it("fails closed when approval lacks evidence or exact project confirmation", () => {
    const base = {
      decision: "APPROVED" as const,
      reason: "Independent evidence reviewed",
      evidenceText: "",
      acknowledged: true,
      approvalProjectCode: "TG-OTHER",
    };
    expect(validateDecisionDraft(base, "TG-2026-041")).toContain(
      "доказательства",
    );
    expect(
      validateDecisionDraft(
        { ...base, evidenceText: "observation-1" },
        "TG-2026-041",
      ),
    ).toContain("шифр проекта");
    expect(
      validateDecisionDraft(
        {
          ...base,
          evidenceText: "observation-1",
          approvalProjectCode: "TG-2026-041",
        },
        "TG-2026-041",
      ),
    ).toBeNull();
  });

  it("requires acknowledgement for a changes request", () => {
    expect(
      validateDecisionDraft(
        {
          decision: "CHANGES_REQUESTED",
          reason: "Supplier basis must be corrected",
          evidenceText: "",
          acknowledged: false,
          approvalProjectCode: "",
        },
        "TG-2026-041",
      ),
    ).toContain("Подтвердите");
  });
});
