import { describe, expect, it } from "vitest";

import {
  validateDocumentSetConfirmationDraft,
  validateDocumentUploadDraft,
  validateProjectDraft,
} from "./intake";

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

  it("requires exact project confirmation for a document set", () => {
    const draft = {
      reason: "Reviewed every revision against the received register",
      projectCode: "TG-002",
      acknowledged: true,
    };
    expect(validateDocumentSetConfirmationDraft(draft, "TG-001")).toContain(
      "точный шифр",
    );
    expect(
      validateDocumentSetConfirmationDraft(
        { ...draft, projectCode: "TG-001" },
        "TG-001",
      ),
    ).toBeNull();
  });
});
