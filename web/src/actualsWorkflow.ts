import type {
  ActualReviewView,
  ActualsContext,
  CalibrationExampleView,
  ForecastCandidate,
  VarianceReason,
  VarianceView,
} from "./types";

export interface ActualsAttestation {
  reason: string;
  projectCodeConfirmation: string;
  acknowledged: boolean;
}

export interface OperationIdentity {
  fingerprint: string;
  key: string;
}

export function resolveOperationIdentity(
  current: OperationIdentity | null,
  payload: object,
  createKey: () => string,
): OperationIdentity {
  const fingerprint = JSON.stringify(payload);
  if (current?.fingerprint === fingerprint) {
    return current;
  }
  return { fingerprint, key: createKey() };
}

function uniqueBy<T>(items: T[], key: (item: T) => string): T[] {
  const seen = new Set<string>();
  return items.filter((item) => {
    const value = key(item);
    if (seen.has(value)) {
      return false;
    }
    seen.add(value);
    return true;
  });
}

export function mergeActualsContextPages(
  pages: ActualsContext[],
): ActualsContext | undefined {
  const first = pages[0];
  const last = pages.at(-1);
  if (first === undefined || last === undefined) {
    return undefined;
  }
  return {
    ...first,
    records: uniqueBy(
      pages.flatMap((page) => page.records),
      (item) => item.record.actual.actual_id,
    ),
    evidence_candidates: uniqueBy(
      pages.flatMap((page) => page.evidence_candidates),
      (item) => item.observation.observation_id,
    ),
    candidates_truncated: pages.some((page) => page.candidates_truncated),
    variances: uniqueBy(
      pages.flatMap((page) => page.variances),
      (item) => item.variance_record_id,
    ),
    calibration_examples: uniqueBy(
      pages.flatMap((page) => page.calibration_examples),
      (item) => item.example.example_id,
    ),
    next_cursor: last.next_cursor,
  };
}

export function mergeForecastCandidatePages(
  pages: ForecastCandidate[][],
): ForecastCandidate[] {
  return uniqueBy(pages.flat(), (item) => item.forecast.forecast_id);
}

export const varianceReasons: VarianceReason[] = [
  "SCOPE_CHANGE",
  "QUANTITY_ERROR",
  "PRICE_CHANGE",
  "SUPPLIER_CHANGE",
  "PRODUCTIVITY_VARIANCE",
  "LOGISTICS_VARIANCE",
  "SCHEDULE_VARIANCE",
  "RISK_REALISED",
  "DATA_QUALITY",
  "METHODOLOGY_ERROR",
  "OTHER_APPROVED",
];

function validateAttestation(
  attestation: ActualsAttestation,
  projectCode: string,
): string | null {
  if (attestation.reason.trim() === "") {
    return "Укажите содержательное основание операции.";
  }
  if (attestation.projectCodeConfirmation !== projectCode) {
    return "Шифр проекта должен точно совпадать с текущим проектом.";
  }
  if (!attestation.acknowledged) {
    return "Подтвердите контрольное утверждение перед операцией.";
  }
  return null;
}

export function validateActualSubmission(
  input: ActualsAttestation & { sourceObservationId: string },
  context: ActualsContext,
  projectCode: string,
): string | null {
  const attestationError = validateAttestation(input, projectCode);
  if (attestationError !== null) {
    return attestationError;
  }
  if (!context.record_roles.length) {
    return "Утверждённая политика не содержит роли регистрации факта.";
  }
  const candidate = context.evidence_candidates.find(
    (item) => item.observation.observation_id === input.sourceObservationId,
  );
  if (candidate === undefined) {
    return "Выберите наблюдение из текущего серверного контекста.";
  }
  if (!candidate.eligible || candidate.evidence_value === null) {
    return `Наблюдение заблокировано: ${candidate.blockers.join(", ") || "структурированное значение отсутствует"}.`;
  }
  if (candidate.evidence_value.metric !== context.selected_metric) {
    return "Метрика наблюдения не совпадает с выбранной политикой.";
  }
  if (candidate.observation_created_at === "") {
    return "Версия наблюдения отсутствует; регистрация факта заблокирована.";
  }
  return null;
}

export function validateActualDecision(
  attestation: ActualsAttestation,
  review: ActualReviewView,
  projectCode: string,
): string | null {
  const attestationError = validateAttestation(attestation, projectCode);
  if (attestationError !== null) {
    return attestationError;
  }
  if (!review.record.is_current) {
    return "Superseded-факт нельзя проверить как текущий.";
  }
  if (!review.decision_allowed) {
    return `Решение по факту заблокировано: ${review.decision_blockers.join(", ") || "сервер не подтвердил допустимость"}.`;
  }
  if (review.record.task_status !== "PENDING") {
    return "Задача проверки факта уже не ожидает решения.";
  }
  return null;
}

export function validateActualComparison(
  input: ActualsAttestation & {
    actualId: string;
    forecastId: string;
    varianceReason: VarianceReason | "";
    varianceReasonDetail: string;
  },
  context: ActualsContext,
  forecastCandidates: ForecastCandidate[],
  projectCode: string,
): string | null {
  const attestationError = validateAttestation(input, projectCode);
  if (attestationError !== null) {
    return attestationError;
  }
  const review = context.records.find(
    (item) => item.record.actual.actual_id === input.actualId,
  );
  if (
    review === undefined ||
    !review.record.is_current ||
    !review.record.actual.verified
  ) {
    return "Для сравнения нужен текущий independently verified факт.";
  }
  if (review.has_classified_variance) {
    return "Для этого факта отклонение уже классифицировано.";
  }
  const forecast = forecastCandidates.find(
    (item) =>
      item.actual_id === input.actualId &&
      item.forecast.forecast_id === input.forecastId,
  );
  if (forecast === undefined) {
    return "Выберите прогноз из выпущенного серверного снимка этого факта.";
  }
  if (
    input.varianceReason === "" ||
    !varianceReasons.includes(input.varianceReason)
  ) {
    return "Выберите причину отклонения из утверждённой классификации.";
  }
  if (input.varianceReasonDetail.trim() === "") {
    return "Объясните классификацию отклонения.";
  }
  return null;
}

function validateReviewTarget(
  attestation: ActualsAttestation,
  target: VarianceView | CalibrationExampleView,
  projectCode: string,
  label: string,
): string | null {
  const attestationError = validateAttestation(attestation, projectCode);
  if (attestationError !== null) {
    return attestationError;
  }
  if (!target.decision_allowed) {
    return `${label} заблокировано: ${target.decision_blockers.join(", ") || "сервер не подтвердил допустимость"}.`;
  }
  if (target.task_status !== "PENDING") {
    return "Связанная задача уже не ожидает решения.";
  }
  return null;
}

export function validateVarianceDecision(
  attestation: ActualsAttestation,
  variance: VarianceView,
  projectCode: string,
): string | null {
  return validateReviewTarget(
    attestation,
    variance,
    projectCode,
    "Решение по отклонению",
  );
}

export function validateCalibrationDecision(
  attestation: ActualsAttestation,
  example: CalibrationExampleView,
  projectCode: string,
): string | null {
  return validateReviewTarget(
    attestation,
    example,
    projectCode,
    "Решение по калибровочному примеру",
  );
}
