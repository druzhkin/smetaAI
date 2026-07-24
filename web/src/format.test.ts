import { describe, expect, it } from "vitest";

import {
  compactId,
  displayValue,
  formatBytes,
  formatDateTime,
  formatMoney,
} from "./format";

describe("operator-safe formatting", () => {
  it("does not invent a monetary value when the calculation is absent", () => {
    expect(formatMoney(null, null)).toBe("Нет подтверждённой суммы");
  });

  it("formats exact decimal strings without changing the stored value", () => {
    expect(formatMoney("1250000.00", "RUB")).toContain("1 250 000");
    expect(
      formatMoney("123456789012345678901234567890.123456", "RUB"),
    ).toContain("123 456 789 012 345 678 901 234 567 890,123456");
    expect(formatMoney("-10.250000000000", "USD")).toContain("-10,25");
    expect(formatBytes(524_288_000)).toBe("500 МиБ");
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
