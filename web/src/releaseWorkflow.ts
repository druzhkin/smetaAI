import type { ApprovalState, GateDecision } from "./types";

export type ReleaseTarget = "bid" | "internal";

export interface ReleaseDraft {
  target: ReleaseTarget;
  reason: string;
  projectCode: string;
  targetState: string;
  acknowledged: boolean;
}

export function releaseTargetState(target: ReleaseTarget): ApprovalState {
  return target === "bid" ? "APPROVED_FOR_BID" : "APPROVED_FOR_INTERNAL_USE";
}

export function releaseStateEligible(
  state: ApprovalState,
  target: ReleaseTarget,
): boolean {
  if (target === "internal") {
    return state === "EXPERT_REVIEW";
  }
  return state === "EXPERT_REVIEW" || state === "APPROVED_FOR_INTERNAL_USE";
}

export function validateReleaseDraft(
  draft: ReleaseDraft,
  expectedProjectCode: string,
  projectState: ApprovalState,
  decision: GateDecision,
  hasApproverRole: boolean,
): string | null {
  if (!hasApproverRole) {
    return "Требуется проектная роль утверждающего.";
  }
  if (!releaseStateEligible(projectState, draft.target)) {
    return `Переход в ${releaseTargetState(draft.target)} недоступен из состояния ${projectState}.`;
  }
  if (!decision.allowed) {
    return (
      decision.findings[0]?.message ??
      "Обязательный контрольный контур заблокировал выпуск."
    );
  }
  const reason = draft.reason.trim();
  if (reason.length < 10 || reason.length > 2000) {
    return "Основание должно содержать от 10 до 2000 символов.";
  }
  if (draft.projectCode.trim() !== expectedProjectCode) {
    return `Введите точный шифр проекта: ${expectedProjectCode}`;
  }
  const expectedState = releaseTargetState(draft.target);
  if (draft.targetState.trim() !== expectedState) {
    return `Введите точное целевое состояние: ${expectedState}`;
  }
  if (!draft.acknowledged) {
    return "Подтвердите независимую проверку полного списка hard stops.";
  }
  return null;
}
