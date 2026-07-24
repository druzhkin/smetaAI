import type { ApprovalDecision } from "./types";

export interface DecisionDraft {
  decision: ApprovalDecision;
  reason: string;
  evidenceText: string;
  acknowledged: boolean;
  approvalProjectCode: string;
}

export function parseIdentifierList(value: string): string[] {
  return [
    ...new Set(
      value
        .split(/[\s,;]+/)
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  ];
}

export function validateDecisionDraft(
  draft: DecisionDraft,
  projectCode: string,
): string | null {
  const reason = draft.reason.trim();
  if (!reason) {
    return "Укажите содержательное основание решения";
  }
  if (reason.length > 4000) {
    return "Основание решения превышает 4000 символов";
  }
  const evidenceIds = parseIdentifierList(draft.evidenceText);
  if (evidenceIds.length > 100) {
    return "За одно решение можно связать не более 100 доказательств";
  }
  if (evidenceIds.some((identifier) => identifier.length > 128)) {
    return "Идентификатор доказательства превышает 128 символов";
  }
  if (draft.decision === "APPROVED" && evidenceIds.length === 0) {
    return "Утверждение требует хотя бы одного проверенного доказательства";
  }
  if (
    draft.decision === "APPROVED" &&
    draft.approvalProjectCode !== projectCode
  ) {
    return "Для утверждения введите шифр проекта без изменений";
  }
  if (!draft.acknowledged) {
    return "Подтвердите, что решение и связанные доказательства проверены";
  }
  return null;
}
