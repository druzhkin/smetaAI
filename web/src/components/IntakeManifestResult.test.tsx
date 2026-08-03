import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { IntakeManifest } from "../types";
import { IntakeManifestResult } from "./IntakeManifestResult";

const blockedManifest: IntakeManifest = {
  root_filename: "tender.zip",
  root_sha256: "a".repeat(64),
  entries: [],
  all_files_processed: false,
  findings: [
    {
      code: "EXCEL_FORMULA_ERROR",
      severity: "BLOCKER",
      archive_path: "tender.zip!/calculation.xlsx",
      message: "Formula cells contain error tokens",
      details: {
        sheet: "ФС4",
        count: 1,
        cells: [{ coordinate: "H41", errors: ["#REF!"] }],
      },
    },
  ],
};

describe("IntakeManifestResult", () => {
  it("shows parser completion as BLOCKED when the manifest has blockers", () => {
    render(<IntakeManifestResult manifest={blockedManifest} />);

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Приём документов: BLOCKED",
    );
    expect(screen.getByRole("alert")).toHaveTextContent("H41 (#REF!)");
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Техническая обработка завершена",
    );
  });

  it("does not present a blocker-free manifest as proof of a safe price", () => {
    render(
      <IntakeManifestResult
        manifest={{
          ...blockedManifest,
          all_files_processed: true,
          findings: [],
        }}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent(
      "Блокирующих ошибок входного контроля нет",
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "не подтверждает полноту комплекта",
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "безопасность цены предложения",
    );
  });

  it("fails closed when PROCESSED has no authoritative manifest", () => {
    const { container } = render(<IntakeManifestResult manifest={null} />);
    const alert = within(container).getByRole("alert");

    expect(alert).toHaveTextContent("Приём документов: BLOCKED");
    expect(alert).toHaveTextContent("манифест отсутствует");
  });

  it("bounds a large finding list without hiding a late blocker", () => {
    const { container } = render(
      <IntakeManifestResult
        manifest={{
          ...blockedManifest,
          findings: [
            ...Array.from({ length: 105 }, (_, index) => ({
              code: `WARNING_${index}`,
              severity: "WARNING" as const,
              archive_path: `file-${index}.docx`,
              message: `warning ${index}`,
              details: {},
            })),
            {
              code: "LATE_BLOCKER",
              severity: "BLOCKER",
              archive_path: "late.xlsx",
              message: "late blocker remains visible",
              details: {},
            },
          ],
        }}
      />,
    );
    const alert = within(container).getByRole("alert");

    expect(alert).toHaveTextContent("late blocker remains visible");
    expect(alert).toHaveTextContent("Показаны 100 из 106 находок");
    expect(within(alert).getAllByRole("listitem")).toHaveLength(100);
  });
});
