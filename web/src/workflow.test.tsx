import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { WorkflowRail } from "./components/WorkflowRail";
import { NavigationProvider } from "./navigation";
import type { ValidationFinding } from "./types";
import {
  blockedWithoutFindingAction,
  nextActionForFinding,
  workflowStatusFor,
} from "./workflow";

function finding(code: string): ValidationFinding {
  return {
    code,
    severity: "BLOCKING",
    message: code,
    entity_ids: [],
    details: {},
  };
}

describe("project workflow", () => {
  it("marks only proven earlier stages complete", () => {
    expect(workflowStatusFor("PRICING_IN_PROGRESS", 0)).toBe("complete");
    expect(workflowStatusFor("PRICING_IN_PROGRESS", 1)).toBe("complete");
    expect(workflowStatusFor("PRICING_IN_PROGRESS", 2)).toBe("current");
    expect(workflowStatusFor("PRICING_IN_PROGRESS", 3)).toBe("pending");
  });

  it("does not infer completed stages after a blocking state erased the stage", () => {
    for (let stageIndex = 0; stageIndex < 5; stageIndex += 1) {
      expect(workflowStatusFor("BLOCKED", stageIndex)).toBe("blocked");
    }
  });

  it("shows automation ownership and the final expert boundary", () => {
    render(<WorkflowRail />);

    expect(screen.getAllByText("Система")).toHaveLength(4);
    expect(screen.getByText("Эксперт")).toBeVisible();
    expect(
      screen.getByText(
        "Эксперт принимает результат или возвращает его на доработку",
      ),
    ).toBeVisible();
    expect(screen.getByText(/проверяемыми первоисточниками/)).toBeVisible();
  });

  it("links each project stage to its plain-language work area", () => {
    render(
      <NavigationProvider>
        <WorkflowRail state="CALCULATION_IN_PROGRESS" projectId="project 1" />
      </NavigationProvider>,
    );

    expect(screen.getByRole("link", { name: /Источники цен/ })).toHaveAttribute(
      "href",
      "/projects/project%201/boq/pricing-matrix",
    );
    expect(screen.getByRole("link", { name: /Расчёт/ })).toHaveAttribute(
      "aria-current",
      "step",
    );
  });
});

describe("next action routing", () => {
  it("routes a missing price basis to the auditable price matrix", () => {
    const action = nextActionForFinding(
      finding("COST_WITHOUT_BASIS"),
      "Нет источника цены",
    );

    expect(action.path).toBe("boq/pricing-matrix");
    expect(action.label).toBe("Открыть ценовую матрицу");
  });

  it("fails closed to the full release control for an unknown finding", () => {
    const action = nextActionForFinding(
      finding("NEW_UNMAPPED_HARD_STOP"),
      "Новая блокировка",
    );

    expect(action.path).toBe("release");
    expect(action.label).toBe("Открыть полный контроль");
  });

  it("keeps release blocked when the backend omitted finding details", () => {
    const action = blockedWithoutFindingAction();

    expect(action.path).toBe("release");
    expect(action.description).toContain("остаётся запрещено");
  });
});
