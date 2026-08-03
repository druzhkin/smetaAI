import { describe, expect, it } from "vitest";

import { automationReworkPresentation } from "./automationReworkWorkflow";
import type { AutomationReworkStatusItem } from "./types";

const baseItem: AutomationReworkStatusItem = {
  rework_request_id: "rework-1",
  project_id: "project-1",
  snapshot_id: "snapshot-1",
  target_stage: "PRICING_IN_PROGRESS",
  requested_by: "expert-1",
  requested_at: "2026-08-03T10:00:00Z",
  status: "STAGE_COMMAND_QUEUED",
  dispatch_id: "dispatch-1",
  dispatch_hash: "a".repeat(64),
  command_topic: "project.automation.pricing.requested",
  command_delivery_status: "PENDING",
  integrity_error_code: null,
  issue_references: [
    {
      kind: "BOQ_PRICE_ROW",
      reference_id: "line-1:1",
      code: "EXPERT_RECHECK_REQUESTED",
    },
  ],
};

describe("automatic rework status", () => {
  it("does not call a queued command a completed recalculation", () => {
    const result = automationReworkPresentation(baseItem);

    expect(result.label).toBe("КОМАНДА В ОЧЕРЕДИ");
    expect(result.explanation).toContain("ещё не завершён");
  });

  it("keeps a dead-lettered command visibly blocked", () => {
    const result = automationReworkPresentation({
      ...baseItem,
      command_delivery_status: "DEAD_LETTERED",
    });

    expect(result.tone).toBe("danger");
    expect(result.explanation).toContain("Выпуск остаётся заблокирован");
  });

  it("does not treat acknowledgement as proof of readiness", () => {
    const result = automationReworkPresentation({
      ...baseItem,
      command_delivery_status: "ACKNOWLEDGED",
    });

    expect(result.label).toBe("КОМАНДА ПРИНЯТА");
    expect(result.explanation).toContain(
      "только после новых проверяемых результатов",
    );
  });
});
