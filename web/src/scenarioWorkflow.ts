export interface ScenarioExecutionDraft {
  reason: string;
  projectCode: string;
  acknowledged: boolean;
}

export function validateScenarioExecutionDraft(
  draft: ScenarioExecutionDraft,
  expectedProjectCode: string,
  selectedSnapshotId: string | null,
  selectedScenarioKey: string | null,
  serverBlockers: readonly string[],
): string | null {
  if (serverBlockers.length > 0) {
    return serverBlockers[0] ?? "Сценарный расчёт заблокирован";
  }
  if (selectedSnapshotId === null) {
    return "Выберите фиксированный snapshot";
  }
  if (selectedScenarioKey === null) {
    return "Выберите утверждённый сценарий";
  }
  const reason = draft.reason.trim();
  if (reason.length < 10 || reason.length > 2000) {
    return "Основание должно содержать от 10 до 2000 символов";
  }
  if (draft.projectCode.trim() !== expectedProjectCode) {
    return `Введите точный шифр проекта: ${expectedProjectCode}`;
  }
  if (!draft.acknowledged) {
    return "Подтвердите выбранный snapshot, управляемую политику и независимый пересчёт";
  }
  return null;
}
