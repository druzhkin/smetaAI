import { describe, expect, it } from "vitest";

import {
  parseManualEvidenceValue,
  validateManualEvidenceEntry,
  validateManualEvidenceReview,
  type ManualEvidenceEntryDraft,
} from "./manualEvidence";

const validEntry: ManualEvidenceEntryDraft = {
  fieldName: "pipeline.nominal_diameter",
  valueText: "500.00",
  valueFormat: "EXACT_NUMBER",
  unit: "mm",
  sourcePriority: "10",
  documentRevisionId: "revision-1",
  locatorKind: "PDF_PAGE_REGION",
  locator: "page=12;x=0.10;y=0.22;width=0.40;height=0.08",
  page: "12",
  table: "",
  sheet: "",
  cellOrRange: "",
  observedAt: "2026-07-24T12:30",
  reason: "Исправлено после сверки размера на чертеже.",
  projectCode: "TG-001",
  acknowledged: true,
};

describe("manual evidence value parsing", () => {
  it("keeps exact decimal values as strings", () => {
    expect(parseManualEvidenceValue("EXACT_NUMBER", "500.00")).toEqual({
      ok: true,
      value: "500.00",
    });
  });

  it("rejects numeric literals at any JSON nesting level", () => {
    expect(
      parseManualEvidenceValue("JSON", '{"components":[{"quantity":500.01}]}'),
    ).toEqual({
      ok: false,
      error:
        "JSON не должен содержать числовые литералы: точные числа укажите строками в кавычках",
    });
  });
});

describe("manual evidence entry validation", () => {
  it("accepts a complete governed correction", () => {
    expect(validateManualEvidenceEntry(validEntry, "TG-001")).toBeNull();
  });

  it("requires exact project confirmation and acknowledgement", () => {
    expect(
      validateManualEvidenceEntry(
        { ...validEntry, projectCode: "tg-001" },
        "TG-001",
      ),
    ).toBe("Введите точный шифр проекта для подтверждения действия");
    expect(
      validateManualEvidenceEntry(
        { ...validEntry, acknowledged: false },
        "TG-001",
      ),
    ).toBe("Подтвердите проверку документа, редакции, локатора и единицы");
  });
});

describe("manual evidence review validation", () => {
  it("blocks the source author", () => {
    expect(
      validateManualEvidenceReview(
        {
          decision: "APPROVED",
          reason: "Проверено по исходной странице.",
          projectCode: "TG-001",
          acknowledged: true,
        },
        "TG-001",
        "actor-1",
        "actor-1",
      ),
    ).toBe("Автор ручного наблюдения не может проверить его самостоятельно");
  });
});
