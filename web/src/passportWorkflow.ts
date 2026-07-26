import type { PassportContext, PassportFactReview } from "./types";

interface Attestation {
  reason: string;
  projectCodeConfirmation: string;
  acknowledged: boolean;
}

export interface PassportSubmissionDraft extends Attestation {
  observationIds: string[];
}

function attestationError(
  draft: Attestation,
  projectCode: string,
): string | null {
  const reason = draft.reason.trim();
  if (reason.length < 10 || reason.length > 2000) {
    return "Укажите проверяемое основание длиной от 10 до 2000 символов.";
  }
  if (draft.projectCodeConfirmation.trim() !== projectCode) {
    return "Введите точный код проекта для подтверждения области действия.";
  }
  if (!draft.acknowledged) {
    return "Подтвердите ответственность за точный выбранный контекст.";
  }
  return null;
}

function canonicalize(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(canonicalize);
  }
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, item]) => [key, canonicalize(item)]),
    );
  }
  return value;
}

function sameValue(left: unknown, right: unknown): boolean {
  return (
    JSON.stringify(canonicalize(left)) === JSON.stringify(canonicalize(right))
  );
}

export function validatePassportSubmission(
  draft: PassportSubmissionDraft,
  context: PassportContext,
  projectCode: string,
): string | null {
  const attestation = attestationError(draft, projectCode);
  if (attestation !== null) {
    return attestation;
  }
  if (
    draft.observationIds.length < 1 ||
    new Set(draft.observationIds).size !== draft.observationIds.length
  ) {
    return "Выберите хотя бы одно уникальное доказательство факта.";
  }
  const candidates = draft.observationIds.map((id) =>
    context.evidence_candidates.find(
      (candidate) => candidate.observation.observation_id === id,
    ),
  );
  if (
    candidates.some(
      (candidate) => candidate === undefined || !candidate.eligible,
    )
  ) {
    return "Каждый источник должен оставаться допустимым по серверному контексту.";
  }
  const selected = candidates.filter((candidate) => candidate !== undefined);
  if (
    selected.some(
      (candidate) =>
        candidate.observation.field_name !== context.selected_field_name,
    )
  ) {
    return "Все доказательства должны относиться к выбранному полю паспорта.";
  }
  const reference = selected[0]!.observation;
  if (
    selected.some(
      (candidate) =>
        candidate.observation.unit !== reference.unit ||
        !sameValue(candidate.observation.value, reference.value),
    )
  ) {
    return "Выбранные источники расходятся по значению или единице; создайте Conflict.";
  }
  if (
    context.independently_verified_fields.includes(context.selected_field_name)
  ) {
    if (selected.length < 2) {
      return "Критическое поле требует не менее двух независимых источников.";
    }
    const qualifications = new Set(
      selected.map((candidate) => candidate.adapter_qualification_id),
    );
    const domains = new Set(
      selected.map((candidate) => candidate.independence_domain),
    );
    if (
      qualifications.has(null) ||
      qualifications.size !== selected.length ||
      domains.has(null) ||
      domains.size !== selected.length
    ) {
      return "Критическое поле требует разных квалификаций и независимых доменов.";
    }
  }
  if (context.unresolved_conflict_ids.length > 0) {
    return "По полю остаётся неразрешённый Conflict; выпуск факта заблокирован.";
  }
  return null;
}

export function validatePassportDecision(
  draft: Attestation,
  review: PassportFactReview,
  projectCode: string,
): string | null {
  const attestation = attestationError(draft, projectCode);
  if (attestation !== null) {
    return attestation;
  }
  if (!review.decision_allowed) {
    return `Решение заблокировано: ${review.decision_blockers.join(", ")}`;
  }
  return null;
}

export function validatePassportValidation(
  draft: Attestation,
  projectCode: string,
): string | null {
  return attestationError(draft, projectCode);
}
