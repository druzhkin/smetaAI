import { describe, expect, it } from "vitest";

import { compactId, displayValue, formatDateTime, formatMoney } from "./format";

describe("operator-safe formatting", () => {
  it("does not invent a monetary value when the calculation is absent", () => {
    expect(formatMoney(null, null)).toBe("Нет подтверждённой суммы");
  });

  it("formats exact decimal strings without changing the stored value", () => {
    expect(formatMoney("1250000.00", "RUB")).toContain("1 250 000");
  });

  it("handles malformed dates and long identifiers explicitly", () => {
    expect(formatDateTime("not-a-date")).toBe("—");
    expect(compactId("abcdefghijklmnopqrstuvwxyz1234567890")).toContain("…");
  });

  it("renders structured evidence without executing it", () => {
    expect(displayValue({ formula: "<script>alert(1)</script>" })).toBe(
      '{"formula":"<script>alert(1)</script>"}',
    );
  });
});
