import { describe, expect, it } from "vitest";

import {
  formatIntakeFindingDetails,
  intakeFindingTitle,
  summarizeIntakeManifest,
} from "./intakeManifest";
import type { IntakeFinding, IntakeManifest } from "./types";

const manifest = (
  allFilesProcessed: boolean,
  findings: IntakeFinding[],
): IntakeManifest => ({
  root_filename: "tender.zip",
  root_sha256: "a".repeat(64),
  entries: [],
  findings,
  all_files_processed: allFilesProcessed,
});

describe("intake manifest presentation", () => {
  it("keeps formula errors blocked and exposes exact cell locators", () => {
    const finding: IntakeFinding = {
      code: "EXCEL_FORMULA_ERROR",
      severity: "BLOCKER",
      archive_path: "tender.zip!/calculation.xlsx",
      message: "Formula cells contain error tokens",
      details: {
        sheet: "ФС4",
        count: 2,
        cells: [
          { coordinate: "H41", errors: ["#REF!"] },
          { coordinate: "H42", errors: ["#REF!"] },
        ],
        cells_truncated: false,
      },
    };

    expect(summarizeIntakeManifest(manifest(false, [finding]))).toEqual({
      blocked: true,
      blockerCount: 1,
      warningCount: 0,
    });
    expect(formatIntakeFindingDetails(finding)).toEqual([
      "Лист: ФС4",
      "Количество: 2",
      "Ячейки: H41 (#REF!), H42 (#REF!)",
    ]);
    expect(summarizeIntakeManifest(manifest(true, [finding])).blocked).toBe(
      true,
    );
  });

  it("does not turn ordinary external hyperlinks into blockers", () => {
    const finding: IntakeFinding = {
      code: "OFFICE_EXTERNAL_HYPERLINK",
      severity: "WARNING",
      archive_path: "contract.docx",
      message: "Office package contains external hyperlinks",
      details: {
        count: 1,
        relationships: [
          {
            relationship_part: "word/_rels/document.xml.rels",
            relationship_type: "hyperlink",
            target_scheme: "https",
            target_sha256: "b".repeat(64),
          },
        ],
      },
    };

    expect(summarizeIntakeManifest(manifest(true, [finding]))).toEqual({
      blocked: false,
      blockerCount: 0,
      warningCount: 1,
    });
    expect(intakeFindingTitle(finding.code)).toBe("Внешняя ссылка в Office");
    expect(formatIntakeFindingDetails(finding)).toContain(
      "Связи: hyperlink, схема https, SHA-256 bbbbbbbbbbbb…",
    );
  });

  it("ignores unknown detail fields and bounds displayed values", () => {
    const finding: IntakeFinding = {
      code: "UNKNOWN_CHECK",
      severity: "INFO",
      archive_path: "unknown.bin",
      message: "Unknown",
      details: {
        sheet: "x".repeat(300),
        secret: "must-not-be-rendered",
      },
    };

    const details = formatIntakeFindingDetails(finding);

    expect(details).toHaveLength(1);
    expect(details[0]).toMatch(/^Лист: x{157}…$/);
    expect(details.join(" ")).not.toContain("must-not-be-rendered");
  });
});
