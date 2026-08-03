import { describe, expect, it } from "vitest";

import {
  validateBoqLine,
  validateNomenclatureAssessment,
  validateReconciliation,
  type BoqLineDraft,
  type NomenclatureDraft,
  type ReconciliationDraft,
} from "./controlledWorkflows";
import type {
  BoqAuthoringContext,
  EvidenceObservation,
  NomenclatureContext,
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

  it("rejects a nomenclature draft until the server context is bound to its BoQ item", () => {
    const sourceObservation = {
      ...observation("attributes-pipe", "RULE_ENGINE", {
        source_item_id: "pipe",
        attributes: { diameter: "DN100" },
      }),
      field_name: "technical_attributes:pipe",
      status: "VERIFIED" as const,
    };
    const context: NomenclatureContext = {
      project_id: "project-1",
      project_state: "PRICING_IN_PROGRESS",
      document_set_revision_id: "set-1",
      catalog_version_id: "catalog-1",
      source_items: [
        {
          source_item_id: "pipe",
          boq_line_id: "line-1",
          line_key: "pipe-line",
          wbs_node_id: "wbs-1",
          work_code: "PIPE",
          description: "Труба DN100",
          unit: "m",
        },
      ],
      selected_source_item_id: "other",
      selected_source_description: null,
      catalog_items: [
        {
          canonical_item_id: "pipe-dn100",
          attributes: { diameter: "DN100" },
          critical_attributes: ["diameter"],
          critical_price: true,
          retrieval_exact_identifier: false,
          retrieval_matched_terms: ["dn", "100"],
          retrieval_matched_critical_attributes: ["diameter"],
        },
      ],
      catalog_items_truncated: false,
      evidence_field_name: "technical_attributes",
      evidence_candidates: [
        {
          observation: sourceObservation,
          attributes: { diameter: "DN100" },
        },
      ],
      evidence_candidates_truncated: false,
      retrieval_notice:
        "Candidate order is lexical retrieval only and is not evidence of technical equivalence.",
    };
    const draft: NomenclatureDraft = {
      sourceItemId: "pipe",
      canonicalItemId: "pipe-dn100",
      sourceAttributesObservationId: "attributes-pipe",
      reason: "Сопоставить критические характеристики.",
      projectCodeConfirmation: "PR-1",
      acknowledged: true,
    };

    expect(validateNomenclatureAssessment(draft, context, "PR-1")).toContain(
      "серверного контекста",
    );
    context.selected_source_item_id = "pipe";
    expect(validateNomenclatureAssessment(draft, context, "PR-1")).toBeNull();
  });
});
