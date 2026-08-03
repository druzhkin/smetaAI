import type { BoqPriceMatrix, ExpertReworkIssue, GateDecision } from "./types";

export interface FinalReviewCandidate extends Omit<
  ExpertReworkIssue,
  "comment"
> {
  key: string;
  label: string;
  detail: string;
  blocked: boolean;
}

export function finalReviewCandidates(
  matrix: BoqPriceMatrix,
  decision: GateDecision,
): FinalReviewCandidate[] {
  const rows = matrix.rows.map<FinalReviewCandidate>((row) => ({
    key: `BOQ_PRICE_ROW:${row.row_id}`,
    kind: "BOQ_PRICE_ROW",
    reference_id: row.row_id,
    code: row.blockers[0] ?? "EXPERT_RECHECK_REQUESTED",
    label: `${row.line_key}. ${row.boq_item_name}`,
    detail:
      row.blockers.length > 0
        ? row.blockers.join(", ")
        : `Система предлагает ${row.proposed_price.amount_per_unit ?? "—"} ${row.proposed_price.currency ?? ""}/${row.proposed_price.unit ?? row.boq_unit}`,
    blocked: row.row_status === "BLOCKED",
  }));
  const findings = decision.findings.flatMap<FinalReviewCandidate>((finding) =>
    (finding.entity_ids.length > 0 ? finding.entity_ids : [finding.code]).map(
      (referenceId) => ({
        key: `RELEASE_FINDING:${finding.code}:${referenceId}`,
        kind: "RELEASE_FINDING",
        reference_id: referenceId,
        code: finding.code,
        label: finding.message,
        detail: referenceId,
        blocked: true,
      }),
    ),
  );
  return [...rows, ...findings];
}

export function validateFinalRework(
  selectedKeys: ReadonlySet<string>,
  reason: string,
): string | null {
  if (selectedKeys.size === 0) {
    return "Выберите хотя бы одну строку или проблему.";
  }
  const normalized = reason.trim();
  if (normalized.length < 10 || normalized.length > 2000) {
    return "Причина должна содержать от 10 до 2000 символов.";
  }
  return null;
}
