import type { ApprovalDecision } from "./types";

export type ManualEvidenceValueFormat =
  "TEXT" | "EXACT_NUMBER" | "BOOLEAN" | "JSON";

export interface ManualEvidenceEntryDraft {
  fieldName: string;
  valueText: string;
  valueFormat: ManualEvidenceValueFormat;
  unit: string;
  sourcePriority: string;
  documentRevisionId: string;
  locatorKind: string;
  locator: string;
  page: string;
  table: string;
  sheet: string;
  cellOrRange: string;
  observedAt: string;
  reason: string;
  projectCode: string;
  acknowledged: boolean;
}

export interface ManualEvidenceReviewDraft {
  decision: ApprovalDecision;
  reason: string;
  projectCode: string;
  acknowledged: boolean;
}

export type ParsedManualEvidenceValue =
  { ok: true; value: unknown } | { ok: false; error: string };

export function parseManualEvidenceValue(
  format: ManualEvidenceValueFormat,
  rawValue: string,
): ParsedManualEvidenceValue {
  if (format === "TEXT") {
    if (!rawValue.trim()) {
      return { ok: false, error: "Введите извлечённое значение" };
    }
    return { ok: true, value: rawValue };
  }
  if (format === "EXACT_NUMBER") {
    const normalized = rawValue.trim();
    if (!/^[+-]?(?:0|[1-9]\d*)(?:\.\d+)?$/.test(normalized)) {
      return {
        ok: false,
        error:
          "Точное число должно быть записано десятичной строкой с точкой без экспоненты",
      };
    }
    return { ok: true, value: normalized };
  }
  if (format === "BOOLEAN") {
    if (rawValue === "true") {
      return { ok: true, value: true };
    }
    if (rawValue === "false") {
      return { ok: true, value: false };
    }
    return { ok: false, error: "Выберите логическое значение" };
  }
  try {
    const parsed: unknown = JSON.parse(rawValue);
    if (parsed === null) {
      return { ok: false, error: "JSON-значение не может быть null" };
    }
    if (containsJavaScriptNumber(parsed)) {
      return {
        ok: false,
        error:
          "JSON не должен содержать числовые литералы: точные числа укажите строками в кавычках",
      };
    }
    return { ok: true, value: parsed };
  } catch {
    return { ok: false, error: "JSON-значение синтаксически некорректно" };
  }
}

export function validateManualEvidenceEntry(
  draft: ManualEvidenceEntryDraft,
  expectedProjectCode: string,
): string | null {
  if (!draft.fieldName.trim()) {
    return "Укажите формализованное имя поля";
  }
  if (draft.fieldName.trim().length > 300) {
    return "Имя поля превышает 300 символов";
  }
  const parsedValue = parseManualEvidenceValue(
    draft.valueFormat,
    draft.valueText,
  );
  if (!parsedValue.ok) {
    return parsedValue.error;
  }
  if (!draft.documentRevisionId) {
    return "Выберите документ из текущего подтверждённого комплекта";
  }
  const priority = Number(draft.sourcePriority);
  if (
    !/^\d+$/.test(draft.sourcePriority.trim()) ||
    !Number.isSafeInteger(priority) ||
    priority < 0
  ) {
    return "Приоритет источника должен быть целым неотрицательным числом";
  }
  if (!draft.locatorKind.trim()) {
    return "Укажите тип локатора";
  }
  if (!draft.locator.trim()) {
    return "Укажите точное место значения в документе";
  }
  if (draft.page !== "") {
    const page = Number(draft.page);
    if (
      !/^\d+$/.test(draft.page.trim()) ||
      !Number.isSafeInteger(page) ||
      page < 1
    ) {
      return "Номер страницы должен быть положительным целым числом";
    }
  }
  if (
    !draft.observedAt ||
    !Number.isFinite(new Date(draft.observedAt).getTime())
  ) {
    return "Укажите дату и время наблюдения";
  }
  if (!draft.reason.trim()) {
    return "Объясните причину ручного исправления";
  }
  if (draft.reason.trim().length > 2000) {
    return "Основание ручного исправления превышает 2000 символов";
  }
  if (draft.projectCode !== expectedProjectCode) {
    return "Введите точный шифр проекта для подтверждения действия";
  }
  if (!draft.acknowledged) {
    return "Подтвердите проверку документа, редакции, локатора и единицы";
  }
  return null;
}

export function validateManualEvidenceReview(
  draft: ManualEvidenceReviewDraft,
  expectedProjectCode: string,
  sourceActorId: string,
  currentActorId: string | null,
): string | null {
  if (currentActorId === null) {
    return "Identity-сессия не содержит проверяемый actor ID";
  }
  if (sourceActorId === currentActorId) {
    return "Автор ручного наблюдения не может проверить его самостоятельно";
  }
  if (!draft.reason.trim()) {
    return "Укажите содержательное основание экспертного решения";
  }
  if (draft.reason.trim().length > 4000) {
    return "Основание экспертного решения превышает 4000 символов";
  }
  if (draft.projectCode !== expectedProjectCode) {
    return "Введите точный шифр проекта для подтверждения решения";
  }
  if (!draft.acknowledged) {
    return "Подтвердите независимую проверку значения и исходного документа";
  }
  return null;
}

function containsJavaScriptNumber(value: unknown): boolean {
  if (typeof value === "number") {
    return true;
  }
  if (Array.isArray(value)) {
    return value.some(containsJavaScriptNumber);
  }
  if (value !== null && typeof value === "object") {
    return Object.values(value).some(containsJavaScriptNumber);
  }
  return false;
}
