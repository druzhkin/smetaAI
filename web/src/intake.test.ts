import { describe, expect, it } from "vitest";

import { validateDocumentUploadDraft, validateProjectDraft } from "./intake";

describe("intake validation", () => {
  it("rejects blank or ambiguous project codes", () => {
    expect(
      validateProjectDraft({
        code: "TG 001",
        name: "Tender",
        reason: "Register received tender",
        acknowledged: true,
      }),
    ).toContain("пробелы");
    expect(
      validateProjectDraft({
        code: "TG-001",
        name: "Tender",
        reason: "Register received tender",
        acknowledged: true,
      }),
    ).toBeNull();
  });

  it("blocks oversized uploads before transferring bytes", () => {
    const file = new File(["12345"], "tender.pdf", {
      type: "application/pdf",
    });
    const draft = {
      logicalKey: "tender-terms",
      title: "Tender terms",
      documentType: "TENDER_TERMS",
      revisionLabel: "R1",
      reason: "Initial package",
      file,
      acknowledged: true,
    };
    expect(validateDocumentUploadDraft(draft, 4)).toContain("лимит");
    expect(validateDocumentUploadDraft(draft, 5)).toBeNull();
  });
});
