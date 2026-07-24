import { stateLabels } from "../labels";
import type { ApprovalState } from "../types";

const positive = new Set([
  "APPROVED",
  "APPROVED_FOR_BID",
  "APPROVED_FOR_INTERNAL_USE",
  "VALIDATED",
  "VERIFIED",
  "PASSED",
  "CURRENT",
  "FIXED",
  "SIGNED",
  "ALLOWED",
  "BOUND",
]);
const negative = new Set([
  "BLOCKED",
  "REJECTED",
  "UNVERIFIED",
  "UNSUPPORTED",
  "FAILED",
  "CORRUPT",
  "DEAD_LETTER",
]);
const warning = new Set([
  "PENDING",
  "IN_REVIEW",
  "REVIEW_REQUIRED",
  "RFQ_REQUIRED",
  "DOCUMENTS_INCOMPLETE",
  "EXPERT_REVIEW",
  "OPEN",
]);
const genericLabels: Record<string, string> = {
  APPROVED: "Утверждено",
  REJECTED: "Отклонено",
  CHANGES_REQUESTED: "На доработку",
  PENDING: "Ожидает решения",
  COMPLETED: "Завершено",
  CANCELLED: "Отменено",
  VALIDATED: "Проверено",
  VERIFIED: "Подтверждено",
  UNVERIFIED: "Не подтверждено",
  PASSED: "Пройдено",
  FAILED: "Ошибка",
  REVIEW_REQUIRED: "Требует проверки",
};

export function statusTone(
  value: string | null,
): "positive" | "negative" | "warning" | "neutral" {
  if (value === null) {
    return "neutral";
  }
  if (positive.has(value)) {
    return "positive";
  }
  if (negative.has(value)) {
    return "negative";
  }
  if (warning.has(value)) {
    return "warning";
  }
  return "neutral";
}

export function statusLabel(value: string | null): string {
  if (value === null) {
    return "Без статуса";
  }
  return (
    stateLabels[value as ApprovalState] ??
    genericLabels[value] ??
    value.replaceAll("_", " ")
  );
}

export function StatusPill({
  value,
  compact = false,
}: {
  value: string | null;
  compact?: boolean;
}) {
  return (
    <span
      className={`status-pill status-pill--${statusTone(value)} ${
        compact ? "status-pill--compact" : ""
      }`}
    >
      <span className="status-pill__dot" />
      {statusLabel(value)}
    </span>
  );
}
