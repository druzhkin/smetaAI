import { describe, expect, it } from "vitest";

import {
  validatePassportDecision,
  validatePassportSubmission,
  type PassportSubmissionDraft,
} from "./passportWorkflow";
import type { PassportContext, PassportEvidenceCandidate } from "./types";

function candidate(
  id: string,
  value: unknown,
  domain: string,
): PassportEvidenceCandidate {
  return {
    observation: {
      observation_id: id,
      field_name: "object_address",
      value,
      unit: null,
      method: id === "one" ? "TABLE_PARSER" : "VISUAL_MODEL",
      method_version: "1",
      source_priority: 1,
      location: {
        document_id: `document-${id}`,
        document_revision_id: `revision-${id}`,
        original_object_hash: "a".repeat(64),
        locator_kind: "table",
        locator: "address",
        page: 1,
        table: null,
        sheet: null,
        cell_or_range: null,
      },
      observed_at: "2026-07-24T10:00:00Z",
      actor_id: `service-${id}`,
      confidence: null,
      status: "UNVERIFIED",
    },
    adapter_qualification_id: `qualification-${id}`,
    adapter_status: "APPROVED",
    adapter_valid_until: "2027-07-24",
    independence_domain: domain,
    eligible: true,
    blockers: [],
  };
}

function context(): PassportContext {
  return {
    project_id: "project-1",
    project_state: "EXTRACTION_REVIEW",
    document_set_revision_id: "set-1",
    requirements_version_id: "requirements-1",
    requirements_content_hash: "a".repeat(64),
    required_fields: ["object_address"],
    independently_verified_fields: ["object_address"],
    optional_fields: [],
    review_role: "REVIEWER",
    selected_field_name: "object_address",
    facts: [],
    evidence_candidates: [
      candidate("one", { city: "Moscow", street: "Test" }, "domain-one"),
      candidate("two", { street: "Test", city: "Moscow" }, "domain-two"),
    ],
    candidates_truncated: false,
    validation: {
      passport: {
        project_id: "project-1",
        facts: [],
        passport_version: "passport-1",
      },
      findings: [],
      requirements_version_id: "requirements-1",
    },
    unresolved_conflict_ids: [],
  };
}

const draft: PassportSubmissionDraft = {
  observationIds: ["one", "two"],
  reason: "Зафиксировать адрес по двум независимым источникам.",
  projectCodeConfirmation: "PR-1",
  acknowledged: true,
};

describe("passport controlled workflow", () => {
  it("compares structured values canonically and requires independent domains", () => {
    const value = context();
    expect(validatePassportSubmission(draft, value, "PR-1")).toBeNull();
    value.evidence_candidates[1]!.independence_domain = "domain-one";
    expect(validatePassportSubmission(draft, value, "PR-1")).toContain(
      "независимых доменов",
    );
  });

  it("blocks submission while a conflict remains open", () => {
    const value = context();
    value.unresolved_conflict_ids = ["conflict-1"];
    expect(validatePassportSubmission(draft, value, "PR-1")).toContain(
      "Conflict",
    );
  });

  it("honours server review blockers before a decision", () => {
    const value = context();
    const review = {
      fact: {
        fact_id: "fact-1",
        field_name: "object_address",
        value: "Moscow",
        unit: null,
        observation_ids: ["one", "two"],
        independence_source_ids: ["one", "two"],
        status: "IN_REVIEW",
        supersedes_fact_id: null,
        is_current: true,
        created_by: "technical-1",
        verified_by: null,
        reviewed_by: null,
        requirements_version_id: "requirements-1",
        document_set_revision_id: "set-1",
        approval_task_id: "task-1",
        updated_at: "2026-07-24T10:00:00Z",
      },
      task_status: "PENDING",
      task_updated_at: "2026-07-24T10:00:00Z",
      assigned_role: "REVIEWER" as const,
      decision_allowed: false,
      decision_blockers: ["FOUR_EYES_FACT_AUTHOR"],
    };
    expect(
      validatePassportDecision(
        {
          reason: "Проверить источники и отклонить самопроверку.",
          projectCodeConfirmation: "PR-1",
          acknowledged: true,
        },
        review,
        "PR-1",
      ),
    ).toContain("FOUR_EYES_FACT_AUTHOR");
    expect(value.facts).toHaveLength(0);
  });
});
