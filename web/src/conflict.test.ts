import { describe, expect, it } from "vitest";

import { validateConflictResolutionDraft } from "./conflict";

describe("conflict resolution validation", () => {
  const validDraft = {
    selectedObservationId: "observation-1",
    reason: "Compared both source locations against the signed native document",
    projectCode: "TG-001",
    acknowledged: true,
  };

  it("requires an independent source selection", () => {
    expect(
      validateConflictResolutionDraft(
        validDraft,
        "TG-001",
        "reviewer-1",
        "reviewer-1",
      ),
    ).toContain("собственное");
  });

  it("requires the exact project code and acknowledgement", () => {
    expect(
      validateConflictResolutionDraft(
        { ...validDraft, projectCode: "TG-002" },
        "TG-001",
        "extractor-1",
        "reviewer-1",
      ),
    ).toContain("точный шифр");
    expect(
      validateConflictResolutionDraft(
        { ...validDraft, acknowledged: false },
        "TG-001",
        "extractor-1",
        "reviewer-1",
      ),
    ).toContain("Подтвердите");
  });
});
