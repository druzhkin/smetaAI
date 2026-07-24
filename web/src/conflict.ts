export interface ConflictResolutionDraft {
  selectedObservationId: string;
  reason: string;
  projectCode: string;
  acknowledged: boolean;
}

export function validateConflictResolutionDraft(
  draft: ConflictResolutionDraft,
  expectedProjectCode: string,
  sourceActorId: string | null,
  currentActorId: string | null,
): string | null {
  if (!draft.selectedObservationId) {
    return "Выберите одно из исходных наблюдений";
  }
  if (sourceActorId === null) {
    return "Выбранное наблюдение отсутствует в загруженном конфликте";
  }
  if (currentActorId === null) {
    return "Identity-сессия не содержит проверяемый actor ID";
  }
  if (sourceActorId === currentActorId) {
    return "Автор исходного наблюдения не может выбрать собственное значение";
  }
  if (!draft.reason.trim()) {
    return "Укажите основание выбора значения";
  }
  if (draft.reason.trim().length > 2000) {
    return "Основание разрешения конфликта превышает 2000 символов";
  }
  if (draft.projectCode !== expectedProjectCode) {
    return "Введите точный шифр проекта для подтверждения действия";
  }
  if (!draft.acknowledged) {
    return "Подтвердите независимую сверку источников и выбранного значения";
  }
  return null;
}
