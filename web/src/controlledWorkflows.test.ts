import { describe, expect, it } from "vitest";

import {
  validateBoqLine,
  validateReconciliation,
  type BoqLineDraft,
  type ReconciliationDraft,
} from "./controlledWorkflows";
import type {
  BoqAuthoringContext,
  EvidenceObservation,
  ReconciliationContext,
} from "./types";

const location = {
  document_id: "document-1",
  document_revision_id: "revision-1",
  original_object_hash: "a".repeat(64),
  locator_kind: "table",
  locator: "row=1",
  page: 1,
  table: "boq",
  sheet: null,
  cell_or_range: null,
};

function observation(
  id: string,
  method: string,
  value: unknown,
): EvidenceObservation {
  return {
    observation_id: id,
    field_name: "boq_line",
    value,
    unit: null,
    method,
    method_version: "1",
    source_priority: 1,
    location,
    observed_at: "2026-07-24T10:00:00Z",
    actor_id: `actor-${id}`,
    confidence: null,
    status: "UNVERIFIED",
  };
}

describe("controlled workflow validation", () => {
  it("requires genuinely independent reconciliation sources", () => {
    const context: ReconciliationContext = {
      project_id: "project-1",
      document_set_revision_id: "set-1",
      reconciliation_version_id: "rules-1",
      available_field_names: ["boq_line"],
      field_names_truncated: false,
      selected_field_name: "boq_line",
      candidates: [
        {
          observation: observation("one", "TABLE_PARSER", "100"),
          adapter_qualification_id: "qualification-one",
          adapter_status: "APPROVED",
          adapter_valid_until: "2027-01-01",
          independence_domain: "domain-one",
          eligible: true,
          blockers: [],
        },
        {
          observation: observation("two", "VISUAL_MODEL", "100"),
          adapter_qualification_id: "qualification-two",
          adapter_status: "APPROVED",
          adapter_valid_until: "2027-01-01",
          independence_domain: "domain-one",
          eligible: true,
          blockers: [],
        },
      ],
      candidates_truncated: false,
    };
    const draft: ReconciliationDraft = {
      observationIds: ["one", "two"],
      reason: "Сверить два независимых способа извлечения.",
      projectCodeConfirmation: "PR-1",
      acknowledged: true,
    };
    expect(validateReconciliation(draft, context, "PR-1")).toContain(
      "независимые домены",
    );
    context.candidates[1]!.independence_domain = "domain-two";
    expect(validateReconciliation(draft, context, "PR-1")).toBeNull();
  });

  it("rejects duplicate BoQ semantic keys before transport", () => {
    const verified = {
      ...observation("verified-boq", "RULE_ENGINE", {
        work_code: "PIPE_INSTALLATION",
        unit: "m",
      }),
      status: "VERIFIED",
    };
    const context: BoqAuthoringContext = {
      project_id: "project-1",
      project_state: "BOQ_IN_PROGRESS",
      document_set_revision_id: "set-1",
      evidence_field_name: "boq_line",
      evidence_candidates: [
        {
          observation: verified,
          work_code: "PIPE_INSTALLATION",
          unit: "m",
        },
      ],
      candidates_truncated: false,
    };
    const draft: BoqLineDraft = {
      lineKey: "pipeline-main",
      wbsNodeId: "wbs-pipeline",
      description: "Монтаж трубопровода",
      evidenceObservationIds: ["verified-boq"],
      costComponents: [
        {
          semantic_key: "pipe",
          category: "MATERIAL",
          basis_kind: "MARKET",
          sign: 1,
          factor_ids: [],
        },
        {
          semantic_key: "pipe",
          category: "LABOUR",
          basis_kind: "NORMATIVE",
          sign: 1,
          factor_ids: [],
        },
      ],
      criticalQuantity: true,
      reason: "Сформировать строку по согласованным доказательствам.",
      projectCodeConfirmation: "PR-1",
      acknowledged: true,
    };
    expect(validateBoqLine(draft, context, "PR-1")).toContain("уникальные");
  });

  it("keeps BoQ semantic keys within the downstream pricing schema", () => {
    const verified = {
      ...observation("verified-boq", "RULE_ENGINE", {
        work_code: "PIPE_INSTALLATION",
        unit: "m",
      }),
      status: "VERIFIED",
    };
    const context: BoqAuthoringContext = {
      project_id: "project-1",
      project_state: "BOQ_IN_PROGRESS",
      document_set_revision_id: "set-1",
      evidence_field_name: "boq_line",
      evidence_candidates: [
        {
          observation: verified,
          work_code: "PIPE_INSTALLATION",
          unit: "m",
        },
      ],
      candidates_truncated: false,
    };
    const draft: BoqLineDraft = {
      lineKey: "pipeline-main",
      wbsNodeId: "wbs-pipeline",
      description: "Монтаж трубопровода",
      evidenceObservationIds: ["verified-boq"],
      costComponents: [
        {
          semantic_key: "x".repeat(129),
          category: "MATERIAL",
          basis_kind: "MARKET",
          sign: 1,
          factor_ids: [],
        },
      ],
      criticalQuantity: true,
      reason: "Сформировать строку по согласованным доказательствам.",
      projectCodeConfirmation: "PR-1",
      acknowledged: true,
    };
    expect(validateBoqLine(draft, context, "PR-1")).toContain(
      "нормализованные ключи",
    );
  });
});
