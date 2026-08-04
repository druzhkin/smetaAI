import { describe, expect, it } from "vitest";

import {
  basisPointsToPercent,
  calculateCommercialScenario,
  kopecksToDecimal,
} from "./commercialScenario";
import {
  alabugaCommercialAssumptions,
  alabugaCommercialLines,
} from "./data/alabuga-commercial-scenario";
import { alabugaPublicSnapshot } from "./publicSnapshot";

describe("Alabuga preliminary commercial scenario", () => {
  it.each([
    ["BOQ", "4624122.11", "5368605.77", "-2391884.77", "-62.27", "9424027.39"],
    [
      "PROJECT",
      "5789508.71",
      "6721619.61",
      "-3744898.61",
      "-97.50",
      "11799102.05",
    ],
    [
      "NORMALIZED",
      "5346432.23",
      "6207207.82",
      "-3230486.82",
      "-84.11",
      "10896105.81",
    ],
  ] as const)(
    "recalculates the %s scenario deterministically",
    (scenario, direct, full, result, margin, required) => {
      const calculation = calculateCommercialScenario(
        alabugaCommercialLines,
        alabugaCommercialAssumptions,
        scenario,
      );

      expect(kopecksToDecimal(calculation.directCostKopecks)).toBe(direct);
      expect(kopecksToDecimal(calculation.fullCostKopecks)).toBe(full);
      expect(kopecksToDecimal(calculation.operatingResultKopecks)).toBe(result);
      expect(basisPointsToPercent(calculation.marginBps)).toBe(margin);
      expect(kopecksToDecimal(calculation.requiredGrossKopecks)).toBe(required);
      expect(calculation.verdict).toBe("LOSS");
      expect(calculation.status).toBe("BLOCKED");
    },
  );

  it("uses the labelled 25/35/40 source weights instead of the broken workbook references", () => {
    const calculation = calculateCommercialScenario(
      alabugaCommercialLines,
      alabugaCommercialAssumptions,
      "BOQ",
    );

    expect(calculation.lines[0]?.preliminaryUnitPriceRubles).toBe(492_450n);
    expect(calculation.lines[9]?.preliminaryUnitPriceRubles).toBe(777n);
    expect(alabugaCommercialAssumptions.tenderWeightBps).toBe(2500);
    expect(alabugaCommercialAssumptions.fgisWeightBps).toBe(3500);
    expect(alabugaCommercialAssumptions.marketWeightBps).toBe(4000);
  });

  it("keeps one commercial input for every public VOR row", () => {
    expect(alabugaCommercialLines).toHaveLength(
      alabugaPublicSnapshot.matrix.rows.length,
    );
    expect(alabugaCommercialLines.map((line) => line.lineKey)).toEqual(
      alabugaPublicSnapshot.matrix.rows.map((row) => row.line_key),
    );
  });

  it("fails closed when weights do not total 100%", () => {
    expect(() =>
      calculateCommercialScenario(
        alabugaCommercialLines,
        { ...alabugaCommercialAssumptions, marketWeightBps: 3999 },
        "NORMALIZED",
      ),
    ).toThrow("must total 10000 basis points");
  });

  it("rejects missing commercial bases instead of dividing by zero", () => {
    expect(() =>
      calculateCommercialScenario(
        alabugaCommercialLines,
        { ...alabugaCommercialAssumptions, tenderGrossPrice: "0" },
        "NORMALIZED",
      ),
    ).toThrow("Tender gross price must be positive");
  });

  it("rejects negative source prices", () => {
    expect(() =>
      calculateCommercialScenario(
        [
          {
            ...alabugaCommercialLines[0]!,
            marketUnitPrice: "-1",
          },
        ],
        alabugaCommercialAssumptions,
        "NORMALIZED",
      ),
    ).toThrow("Commercial source prices must be non-negative");
  });
});
