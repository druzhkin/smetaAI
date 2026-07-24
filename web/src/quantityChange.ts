import type {
  QuantityChangeContext,
  QuantityFormula,
  QuantityOperation,
  QuantitySubmission,
} from "./types";

const decimalPattern = /^-?(?:0|[1-9]\d*)(?:\.\d+)?$/;

export interface QuantityChangeDraft {
  value: string;
  sourceObservationIds: string;
  sourcePriority: string;
  roundingScale: string;
  wasteFactor: string;
  alternativeQuantityIds: string;
  formulaEnabled: boolean;
  formulaId: string;
  formulaOperation: QuantityOperation;
  formulaDisplay: string;
  formulaInputsJson: string;
  formulaEvidenceJson: string;
  reason: string;
  projectCode: string;
  acknowledged: boolean;
}

export interface QuantityChangeValidation {
  error: string | null;
  submission: QuantitySubmission | null;
}

function identifierList(raw: string): string[] {
  return raw
    .split(/[\n,]/)
    .map((value) => value.trim())
    .filter(Boolean)
    .sort();
}

function hasDuplicates(values: string[]): boolean {
  return new Set(values).size !== values.length;
}

function stringRecord(raw: string, label: string): Record<string, string> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new Error(`${label}: требуется корректный JSON-объект.`);
  }
  if (parsed === null || Array.isArray(parsed) || typeof parsed !== "object") {
    throw new Error(`${label}: требуется JSON-объект.`);
  }
  const entries = Object.entries(parsed);
  if (
    entries.length === 0 ||
    entries.some(
      ([key, value]) =>
        key.trim() === "" || typeof value !== "string" || value.trim() === "",
    )
  ) {
    throw new Error(`${label}: ключи и строковые значения обязательны.`);
  }
  return Object.fromEntries(
    entries
      .map(([key, value]) => [key.trim(), (value as string).trim()])
      .sort((left, right) => (left[0] ?? "").localeCompare(right[0] ?? "")),
  );
}

function stable(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(stable);
  }
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, item]) => [key, stable(item)]),
    );
  }
  return value;
}

function sameSubmission(
  left: QuantitySubmission,
  right: QuantitySubmission,
): boolean {
  return JSON.stringify(stable(left)) === JSON.stringify(stable(right));
}

export function draftFromQuantityContext(
  context: QuantityChangeContext,
): QuantityChangeDraft {
  const current = context.current_submission;
  return {
    value: current.draft.value,
    sourceObservationIds: current.draft.source_observation_ids.join("\n"),
    sourcePriority: String(current.draft.source_priority),
    roundingScale: String(current.draft.rounding_scale),
    wasteFactor: current.draft.waste_factor,
    alternativeQuantityIds: current.draft.alternative_quantity_ids.join("\n"),
    formulaEnabled: current.formula !== null,
    formulaId: current.formula?.formula_id ?? "",
    formulaOperation: current.formula?.operation ?? "PRODUCT",
    formulaDisplay: current.formula?.display_formula ?? "",
    formulaInputsJson: JSON.stringify(current.formula?.inputs ?? {}, null, 2),
    formulaEvidenceJson: JSON.stringify(
      current.formula_input_observation_ids,
      null,
      2,
    ),
    reason: "",
    projectCode: "",
    acknowledged: false,
  };
}

export function validateQuantityChangeDraft(
  draft: QuantityChangeDraft,
  context: QuantityChangeContext,
  expectedProjectCode: string,
): QuantityChangeValidation {
  const value = draft.value.trim();
  const wasteFactor = draft.wasteFactor.trim();
  if (!decimalPattern.test(value)) {
    return {
      error:
        "Количество должно быть точным десятичным числом без экспоненты и разделителей тысяч.",
      submission: null,
    };
  }
  if (!decimalPattern.test(wasteFactor) || wasteFactor.startsWith("-")) {
    return {
      error:
        "Коэффициент отхода должен быть неотрицательным десятичным числом.",
      submission: null,
    };
  }
  if (!/^\d+$/.test(draft.sourcePriority.trim())) {
    return {
      error: "Приоритет источника должен быть целым неотрицательным числом.",
      submission: null,
    };
  }
  if (!/^\d+$/.test(draft.roundingScale.trim())) {
    return {
      error: "Точность округления должна быть целым числом от 0 до 12.",
      submission: null,
    };
  }
  const sourcePriority = Number(draft.sourcePriority);
  const roundingScale = Number(draft.roundingScale);
  if (!Number.isSafeInteger(sourcePriority) || sourcePriority < 0) {
    return {
      error:
        "Приоритет источника выходит за допустимый целочисленный диапазон.",
      submission: null,
    };
  }
  if (
    !Number.isSafeInteger(roundingScale) ||
    roundingScale < 0 ||
    roundingScale > 12
  ) {
    return {
      error: "Точность округления должна быть целым числом от 0 до 12.",
      submission: null,
    };
  }
  const sourceObservationIds = identifierList(draft.sourceObservationIds);
  if (sourceObservationIds.length === 0) {
    return {
      error: "Укажите хотя бы одно проверенное исходное наблюдение.",
      submission: null,
    };
  }
  if (hasDuplicates(sourceObservationIds)) {
    return {
      error: "Список исходных наблюдений содержит дубликаты.",
      submission: null,
    };
  }
  const alternativeQuantityIds = identifierList(draft.alternativeQuantityIds);
  if (hasDuplicates(alternativeQuantityIds)) {
    return {
      error: "Список альтернативных количеств содержит дубликаты.",
      submission: null,
    };
  }

  let formula: QuantityFormula | null = null;
  let formulaEvidence: Record<string, string> = {};
  if (draft.formulaEnabled) {
    if (context.quantity_formula_rules_version_id === null) {
      return {
        error:
          "Для проекта не привязана утверждённая версия правил формул количества.",
        submission: null,
      };
    }
    if (draft.formulaId.trim() === "" || draft.formulaDisplay.trim() === "") {
      return {
        error: "Для формулы обязательны идентификатор и отображаемая запись.",
        submission: null,
      };
    }
    let formulaInputs: Record<string, string>;
    try {
      formulaInputs = stringRecord(draft.formulaInputsJson, "Входы формулы");
      formulaEvidence = stringRecord(
        draft.formulaEvidenceJson,
        "Доказательства входов формулы",
      );
    } catch (error) {
      return {
        error: error instanceof Error ? error.message : "Некорректная формула.",
        submission: null,
      };
    }
    if (
      Object.values(formulaInputs).some((input) => !decimalPattern.test(input))
    ) {
      return {
        error:
          "Все входы формулы должны быть точными десятичными строками без экспоненты.",
        submission: null,
      };
    }
    const inputKeys = Object.keys(formulaInputs).sort();
    const evidenceKeys = Object.keys(formulaEvidence).sort();
    if (JSON.stringify(inputKeys) !== JSON.stringify(evidenceKeys)) {
      return {
        error:
          "Каждому входу формулы должно соответствовать ровно одно доказательство.",
        submission: null,
      };
    }
    if (
      Object.values(formulaEvidence).some(
        (id) => !sourceObservationIds.includes(id),
      )
    ) {
      return {
        error:
          "Доказательства входов формулы должны входить в общий список исходных наблюдений.",
        submission: null,
      };
    }
    const requiredKeys: Partial<Record<QuantityOperation, string[]>> = {
      RECTANGULAR_VOLUME: ["length", "width", "height"],
      CYLINDER_VOLUME: ["diameter", "length", "pi"],
    };
    if (
      (requiredKeys[draft.formulaOperation] ?? []).some(
        (key) => !inputKeys.includes(key),
      )
    ) {
      return {
        error: "В формуле отсутствуют обязательные именованные входы.",
        submission: null,
      };
    }
    formula = {
      formula_id: draft.formulaId.trim(),
      formula_version: context.quantity_formula_rules_version_id,
      operation: draft.formulaOperation,
      inputs: formulaInputs,
      output_unit: context.unit,
      display_formula: draft.formulaDisplay.trim(),
    };
  } else if (draft.formulaEvidenceJson.trim() !== "{}") {
    return {
      error:
        "Прямое количество не может содержать доказательства входов формулы.",
      submission: null,
    };
  }

  const submission: QuantitySubmission = {
    draft: {
      value,
      unit: context.unit,
      source_observation_ids: sourceObservationIds,
      source_priority: sourcePriority,
      rounding_scale: roundingScale,
      waste_factor: wasteFactor,
      alternative_quantity_ids: alternativeQuantityIds,
      manual_change_id: null,
    },
    formula,
    formula_input_observation_ids: formulaEvidence,
  };
  if (sameSubmission(submission, context.current_submission)) {
    return {
      error: "Предлагаемая запись полностью совпадает с текущей.",
      submission: null,
    };
  }
  if (draft.reason.trim() === "") {
    return {
      error: "Укажите проверяемое основание изменения.",
      submission: null,
    };
  }
  if (draft.reason.length > 2000) {
    return {
      error: "Основание изменения превышает 2000 символов.",
      submission: null,
    };
  }
  if (draft.projectCode.trim() !== expectedProjectCode) {
    return {
      error: "Контрольный шифр проекта не совпадает.",
      submission: null,
    };
  }
  if (!draft.acknowledged) {
    return {
      error: "Подтвердите проверку источников, единицы и формулы.",
      submission: null,
    };
  }
  return { error: null, submission };
}
