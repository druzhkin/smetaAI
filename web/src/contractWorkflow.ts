import type {
  ApprovalDecision,
  ContractContext,
  ContractImpactCandidate,
  ContractTermReview,
} from "./types";

export interface ContractAttestation {
  reason: string;
  projectCodeConfirmation: string;
  acknowledged: boolean;
}

export interface ContractSubmissionDraft extends ContractAttestation {
  observationIds: string[];
}

export type ContractImpactDraft =
  | (ContractAttestation & {
      mode: "NO_DETERMINISTIC_COST";
      noCostReason: string;
      candidateId: "";
    })
  | (ContractAttestation & {
      mode: "DERIVED_MODEL";
      noCostReason: "";
      candidateId: string;
    });

function validateAttestation(
  draft: ContractAttestation,
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

export function validateContractSubmission(
  draft: ContractSubmissionDraft,
  context: ContractContext,
  projectCode: string,
): string | null {
  const attestationError = validateAttestation(draft, projectCode);
  if (attestationError !== null) {
    return attestationError;
  }
  if (context.unresolved_conflict_ids.length > 0) {
    return "Сначала разрешите конфликт доказательств по выбранному условию.";
  }
  if (draft.observationIds.length === 0) {
    return "Выберите доказательство из подтверждённого комплекта документов.";
  }
  if (new Set(draft.observationIds).size !== draft.observationIds.length) {
    return "Доказательства не должны повторяться.";
  }
  const selected = draft.observationIds.map((id) =>
    context.evidence_candidates.find(
      (candidate) => candidate.observation.observation_id === id,
    ),
  );
  if (selected.some((candidate) => candidate === undefined)) {
    return "Одно из доказательств исчезло из актуального серверного контекста.";
  }
  const candidates = selected.filter((candidate) => candidate !== undefined);
  if (candidates.some((candidate) => !candidate.eligible)) {
    return "Выбрано доказательство с блокирующей проверкой.";
  }
  if (candidates.some((candidate) => candidate.observation.unit !== null)) {
    return "Договорное условие не должно иметь единицу измерения.";
  }
  const values = new Set(
    candidates.map((candidate) => JSON.stringify(candidate.observation.value)),
  );
  if (values.size !== 1) {
    return "Выбранные источники содержат разные значения.";
  }
  if (
    context.independently_verified_term_kinds.includes(context.selected_kind)
  ) {
    const domains = new Set(
      candidates
        .map((candidate) => candidate.independence_domain)
        .filter((value): value is string => Boolean(value)),
    );
    if (candidates.length < 2 || domains.size < 2) {
      return "Для этого условия нужны два квалифицированных независимых источника.";
    }
  }
  return null;
}

export function validateContractDecision(
  draft: ContractAttestation,
  review: ContractTermReview,
  decision: ApprovalDecision,
  projectCode: string,
): string | null {
  const attestationError = validateAttestation(draft, projectCode);
  if (attestationError !== null) {
    return attestationError;
  }
  if (!review.decision_allowed) {
    return `Проверка заблокирована: ${review.decision_blockers.join(", ")}`;
  }
  if (!["APPROVED", "CHANGES_REQUESTED", "REJECTED"].includes(decision)) {
    return "Выберите допустимое решение.";
  }
  return null;
}

export function validateContractImpact(
  draft: ContractImpactDraft,
  review: ContractTermReview,
  candidates: ContractImpactCandidate[],
  projectCode: string,
): string | null {
  const attestationError = validateAttestation(draft, projectCode);
  if (attestationError !== null) {
    return attestationError;
  }
  if (!review.term.verified) {
    return "Сначала требуется независимое подтверждение договорного условия.";
  }
  if (draft.mode === "NO_DETERMINISTIC_COST") {
    const explanation = draft.noCostReason.trim();
    if (explanation.length < 16 || explanation.length > 2000) {
      return "Обоснование нулевого детерминированного влияния должно содержать от 16 до 2000 символов.";
    }
    return null;
  }
  const candidate = candidates.find(
    (item) => item.derived_cost_model_id === draft.candidateId,
  );
  if (candidate === undefined) {
    return "Выберите актуальную проверенную финансовую модель.";
  }
  if (!candidate.eligible) {
    return `Финансовая модель заблокирована: ${candidate.blockers.join(", ")}`;
  }
  return null;
}

export function validateContractFinalization(
  draft: ContractAttestation,
  review: ContractTermReview,
  projectCode: string,
): string | null {
  const attestationError = validateAttestation(draft, projectCode);
  if (attestationError !== null) {
    return attestationError;
  }
  if (review.term.cost_impact_proposal === null) {
    return "Предложение по стоимостному влиянию отсутствует.";
  }
  if (review.term.approval_task_ids.length === 0) {
    return "Обязательная задача согласования не создана.";
  }
  if (
    review.term.approval_task_ids.some(
      (id) => review.term.cost_impact_task_statuses[id] !== "APPROVED",
    )
  ) {
    return "Не все обязательные согласования стоимостного влияния завершены.";
  }
  return null;
}

export function validateContractValidation(
  draft: ContractAttestation,
  projectCode: string,
): string | null {
  return validateAttestation(draft, projectCode);
}
