import type { IntakeFinding, IntakeManifest } from "./types";

const MAX_TEXT_LENGTH = 158;
const MAX_DETAIL_ITEMS = 5;

const findingTitles: Record<string, string> = {
  ARCHIVE_ADAPTER_REQUIRED: "Требуется проверенный адаптер архива",
  ARCHIVE_COMPRESSION_RATIO_EXCEEDED: "Опасная степень сжатия архива",
  ARCHIVE_DEPTH_EXCEEDED: "Превышена глубина вложенных архивов",
  ARCHIVE_LIMIT_EXCEEDED: "Превышен лимит обработки архива",
  ARCHIVE_PATH_TRAVERSAL: "Опасный путь внутри архива",
  CORRUPT_ARCHIVE: "Повреждённый архив",
  CORRUPT_OFFICE_STRUCTURE: "Некорректная структура Office",
  CORRUPT_OR_PROTECTED_EXCEL: "Excel не удалось безопасно открыть",
  CORRUPT_OR_PROTECTED_OFFICE_FILE: "Office-файл повреждён или защищён",
  CORRUPT_OR_UNSAFE_IMAGE: "Изображение не прошло безопасное декодирование",
  CORRUPT_PDF: "Повреждённый PDF",
  DOCUMENT_ADAPTER_REQUIRED: "Требуется проверенный адаптер документа",
  EXCEL_CELL_ERROR: "Ошибочное значение в ячейке Excel",
  EXCEL_EXTERNAL_LINKS: "Внешние связи Excel",
  EXCEL_FORMULA_CACHE_MISSING: "Нет сохранённого результата формулы Excel",
  EXCEL_FORMULA_ERROR: "Ошибка вычисления в формуле Excel",
  FILE_SIGNATURE_MISMATCH: "Расширение не соответствует содержимому",
  HIDDEN_EXCEL_DIMENSIONS: "Скрытые строки или столбцы Excel",
  HIDDEN_EXCEL_SHEET: "Скрытый лист Excel",
  LEGACY_EXCEL_ADAPTER_REQUIRED: "Требуется проверенный адаптер XLS",
  OFFICE_EMBEDDED_OBJECTS: "Вложенные объекты Office",
  OFFICE_EXTERNAL_DEPENDENCY: "Внешняя зависимость Office",
  OFFICE_EXTERNAL_HYPERLINK: "Внешняя ссылка в Office",
  OFFICE_MACROS_PRESENT: "Макросы в Office",
  PDF_EMBEDDED_FILES: "Вложенные файлы PDF",
  PROTECTED_ARCHIVE_MEMBER: "Защищённый файл внутри архива",
  PROTECTED_OFFICE_MEMBER: "Защищённая часть Office",
  PROTECTED_PDF: "Защищённый PDF",
  TEXT_CONTENT_INVALID: "Некорректный текстовый файл",
  UNSUPPORTED_FILE_TYPE: "Неподдерживаемый тип файла",
};

export interface IntakeManifestSummary {
  blocked: boolean;
  blockerCount: number;
  warningCount: number;
}

const clipped = (value: string): string =>
  value.length <= MAX_TEXT_LENGTH
    ? value
    : `${value.slice(0, MAX_TEXT_LENGTH - 1)}…`;

const stringValue = (
  record: Record<string, unknown>,
  key: string,
): string | null => {
  const value = record[key];
  return typeof value === "string" && value !== "" ? clipped(value) : null;
};

const numberValue = (
  record: Record<string, unknown>,
  key: string,
): number | null => {
  const value = record[key];
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : null;
};

const objectValue = (value: unknown): Record<string, unknown> | null =>
  typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;

const stringList = (value: unknown): string[] =>
  Array.isArray(value)
    ? value
        .filter((item): item is string => typeof item === "string")
        .slice(0, MAX_DETAIL_ITEMS)
        .map(clipped)
    : [];

export const summarizeIntakeManifest = (
  manifest: IntakeManifest,
): IntakeManifestSummary => {
  const blockerCount = manifest.findings.filter(
    (finding) => finding.severity === "BLOCKER",
  ).length;
  const warningCount = manifest.findings.filter(
    (finding) => finding.severity === "WARNING",
  ).length;
  return {
    blocked:
      !manifest.all_files_processed ||
      blockerCount > 0 ||
      manifest.entries.some(
        (entry) => entry.corrupt || entry.protected || entry.unsupported,
      ),
    blockerCount,
    warningCount,
  };
};

export const intakeFindingTitle = (code: string): string =>
  findingTitles[code] ?? "Результат проверки входного файла";

export const formatIntakeFindingDetails = (
  finding: IntakeFinding,
): string[] => {
  const details = finding.details;
  const result: string[] = [];
  const sheet = stringValue(details, "sheet");
  const count = numberValue(details, "count");
  const errorType = stringValue(details, "error_type");
  if (sheet !== null) {
    result.push(`Лист: ${sheet}`);
  }
  if (count !== null) {
    result.push(`Количество: ${count}`);
  }

  if (Array.isArray(details.cells)) {
    const cells = details.cells
      .slice(0, MAX_DETAIL_ITEMS)
      .map(objectValue)
      .filter((cell): cell is Record<string, unknown> => cell !== null)
      .map((cell) => {
        const coordinate = stringValue(cell, "coordinate");
        if (coordinate === null) {
          return null;
        }
        const errors = stringList(cell.errors);
        const error = stringValue(cell, "error");
        const signals =
          errors.length > 0 ? errors : error === null ? [] : [error];
        return signals.length > 0
          ? `${coordinate} (${signals.join(", ")})`
          : coordinate;
      })
      .filter((cell): cell is string => cell !== null);
    if (cells.length > 0) {
      result.push(`Ячейки: ${cells.join(", ")}`);
    }
  }

  if (Array.isArray(details.errors)) {
    const errors = details.errors
      .slice(0, MAX_DETAIL_ITEMS)
      .map(objectValue)
      .filter((error): error is Record<string, unknown> => error !== null)
      .map((error) => {
        const code = stringValue(error, "code");
        const part = stringValue(error, "part");
        if (code === null) {
          return null;
        }
        return part === null ? code : `${code} · ${part}`;
      })
      .filter((error): error is string => error !== null);
    if (errors.length > 0) {
      result.push(`Структура: ${errors.join("; ")}`);
    }
  }

  if (Array.isArray(details.relationships)) {
    const relationships = details.relationships
      .slice(0, MAX_DETAIL_ITEMS)
      .map(objectValue)
      .filter(
        (relationship): relationship is Record<string, unknown> =>
          relationship !== null,
      )
      .map((relationship) => {
        const relationshipType = stringValue(relationship, "relationship_type");
        const scheme = stringValue(relationship, "target_scheme");
        const hash = stringValue(relationship, "target_sha256");
        if (relationshipType === null || scheme === null || hash === null) {
          return null;
        }
        return `${relationshipType}, схема ${scheme}, SHA-256 ${hash.slice(0, 12)}…`;
      })
      .filter((relationship): relationship is string => relationship !== null);
    if (relationships.length > 0) {
      result.push(`Связи: ${relationships.join("; ")}`);
    }
  }

  if (errorType !== null) {
    result.push(`Тип ошибки парсера: ${errorType}`);
  }
  if (
    details.cells_truncated === true ||
    details.errors_truncated === true ||
    details.relationships_truncated === true
  ) {
    result.push("Показана только ограниченная часть результатов.");
  }
  return result;
};
