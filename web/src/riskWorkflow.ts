import type { RiskContext, RiskDraft, RiskItemReview } from "./types";

export interface RiskAttestation {
  reason: string;
  projectCodeConfirmation: string;
  acknowledged: boolean;
}

export interface RiskSubmissionDraft extends RiskAttestation {
  observationIds: string[];
}

function validateAttestation(
  draft: RiskAttestation,
  projectCode: string,
): string | null {
  if (draft.projectCodeConfirmation.trim() !== projectCode) {
    return "Введите точный шифр проекта.";
  }
  if (!draft.acknowledged) {
    return "Подтвердите ответственность за действие.";
  }
  const reason = draft.reason.trim();
  if (reason.length < 8 || reason.length > 2000) {
    return "Обоснование должно содержать от 8 до 2000 символов.";
  }
  return null;
}

function evidenceValue(draft: RiskDraft): string {
  return JSON.stringify({
    risk_key: draft.risk_key,
    description: draft.description,
    probability: draft.probability,
    impact_min: draft.impact_min,
    impact_most_likely: draft.impact_most_likely,
    impact_max: draft.impact_max,
    currency: draft.currency,
    correlated: draft.correlated,
    correlation_group: draft.correlation_group,
    mitigation_cost_input_id: draft.mitigation_cost_input_id,
  });
}

export function validateRiskSubmission(
  draft: RiskSubmissionDraft,
  context: RiskContext,
  projectCode: string,
): string | null {
  const attestationError = validateAttestation(draft, projectCode);
  if (attestationError !== null) {
    return attestationError;
  }
  if (context.unresolved_conflict_ids.length > 0) {
    return "Сначала разрешите конфликт доказательств по выбранному риску.";
  }
  if (draft.observationIds.length === 0) {
    return "Выберите серверное наблюдение из актуального комплекта.";
  }
  if (new Set(draft.observationIds).size !== draft.observationIds.length) {
    return "Наблюдения не должны повторяться.";
  }
  const selected = draft.observationIds.map((id) =>
    context.evidence_candidates.find(
      (candidate) => candidate.observation.observation_id === id,
    ),
  );
  if (selected.some((candidate) => candidate === undefined)) {
    return "Одно из наблюдений исчезло из актуального серверного контекста.";
  }
  const candidates = selected.filter((candidate) => candidate !== undefined);
  if (candidates.some((candidate) => !candidate.eligible)) {
    return "Выбрано наблюдение с блокирующей проверкой.";
  }
  if (candidates.some((candidate) => candidate.draft === null)) {
    return "Серверное наблюдение не содержит валидной структуры риска.";
  }
  const riskDrafts = candidates
    .map((candidate) => candidate.draft)
    .filter((candidate): candidate is RiskDraft => candidate !== null);
  if (
    riskDrafts.some(
      (candidate) => candidate.risk_key !== context.selected_risk_key,
    )
  ) {
    return "Наблюдение относится к другому ключу утверждённой риск-модели.";
  }
  if (new Set(riskDrafts.map(evidenceValue)).size !== 1) {
    return "Выбранные источники содержат разные параметры риска.";
  }
  if (riskDrafts.some((candidate) => candidate.correlated)) {
    return "Коррелированный риск заблокирован до интеграции квалифицированного движка корреляции.";
  }
  if (
    context.independently_verified_risk_keys.includes(context.selected_risk_key)
  ) {
    const domains = new Set(
      candidates
        .map((candidate) => candidate.independence_domain)
        .filter((value): value is string => Boolean(value)),
    );
    if (candidates.length < 2 || domains.size < 2) {
      return "Для этого риска нужны два квалифицированных независимых источника.";
    }
  }
  return null;
}

export function validateRiskDecision(
  draft: RiskAttestation,
  review: RiskItemReview,
  projectCode: string,
): string | null {
  const attestationError = validateAttestation(draft, projectCode);
  if (attestationError !== null) {
    return attestationError;
  }
  if (!review.decision_allowed) {
    return `Проверка заблокирована: ${review.decision_blockers.join(", ")}`;
  }
  return null;
}

export function validateRiskCalculation(
  draft: RiskAttestation,
  context: RiskContext,
  projectCode: string,
): string | null {
  const attestationError = validateAttestation(draft, projectCode);
  if (attestationError !== null) {
    return attestationError;
  }
  if (context.calculation_blockers.length > 0) {
    return `Расчёт резерва заблокирован: ${context.calculation_blockers.join(", ")}`;
  }
  return null;
}
