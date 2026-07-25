export interface CalculationExecutionDraft {
  reason: string;
  projectCode: string;
  acknowledged: boolean;
}

export function validateCalculationExecutionDraft(
  draft: CalculationExecutionDraft,
  expectedProjectCode: string,
  candidateAvailable: boolean,
  serverBlockers: readonly string[],
): string | null {
  if (!candidateAvailable) {
    return serverBlockers[0] ?? "Сервер не сформировал расчётный кандидат";
  }
  if (serverBlockers.length > 0) {
    return serverBlockers[0] ?? "Расчёт заблокирован";
  }
  const reason = draft.reason.trim();
  if (reason.length < 10 || reason.length > 2000) {
    return "Основание должно содержать от 10 до 2000 символов";
  }
  if (draft.projectCode.trim() !== expectedProjectCode) {
    return `Введите точный шифр проекта: ${expectedProjectCode}`;
  }
  if (!draft.acknowledged) {
    return "Подтвердите неизменность серверного кандидата и независимый пересчёт";
  }
  return null;
}

export function calculationBasisId(input: {
  source_observation_id: string | null;
  approved_assumption_id: string | null;
  normative_rate_id: string | null;
  risk_reserve_id: string | null;
  derived_cost_model_id: string | null;
}): string | null {
  return (
    input.source_observation_id ??
    input.approved_assumption_id ??
    input.normative_rate_id ??
    input.risk_reserve_id ??
    input.derived_cost_model_id
  );
}
