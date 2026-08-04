import type { ApprovalState, ValidationFinding } from "./types";

export type WorkflowStageStatus =
  "complete" | "current" | "pending" | "blocked" | "closed";

export interface WorkflowStage {
  id: "documents" | "boq" | "pricing" | "calculation" | "expert";
  index: string;
  title: string;
  description: string;
  owner: "Система" | "Эксперт";
  path: string;
}

export const workflowStages: WorkflowStage[] = [
  {
    id: "documents",
    index: "01",
    title: "Документы",
    description: "Загрузка, проверка комплектности и выбор актуальной версии",
    owner: "Система",
    path: "DOCUMENTS",
  },
  {
    id: "boq",
    index: "02",
    title: "ВОР и сопоставление",
    description: "Позиции, объёмы, единицы и критические характеристики",
    owner: "Система",
    path: "BOQ_SCOPE",
  },
  {
    id: "pricing",
    index: "03",
    title: "Источники цен",
    description: "ФГИС ЦС, тендеры и рынок с проверяемыми первоисточниками",
    owner: "Система",
    path: "boq/pricing-matrix",
  },
  {
    id: "calculation",
    index: "04",
    title: "Расчёт",
    description: "Нормализация, стоимость по строкам и независимый пересчёт",
    owner: "Система",
    path: "CALCULATION",
  },
  {
    id: "expert",
    index: "05",
    title: "Итоговый контроль",
    description: "Эксперт принимает результат или возвращает его на доработку",
    owner: "Эксперт",
    path: "release",
  },
];

const stageByState: Partial<Record<ApprovalState, number>> = {
  DRAFT: 0,
  DOCUMENTS_INCOMPLETE: 0,
  EXTRACTION_IN_PROGRESS: 0,
  EXTRACTION_REVIEW: 0,
  BOQ_IN_PROGRESS: 1,
  BOQ_REVIEW: 1,
  PRICING_IN_PROGRESS: 2,
  RFQ_REQUIRED: 2,
  CALCULATION_IN_PROGRESS: 3,
  INDEPENDENT_VALIDATION: 3,
  EXPERT_REVIEW: 4,
  APPROVED_FOR_INTERNAL_USE: 4,
};

export function workflowStatusFor(
  state: ApprovalState | undefined,
  stageIndex: number,
): WorkflowStageStatus {
  if (state === undefined) {
    return "pending";
  }
  if (state === "APPROVED_FOR_BID") {
    return "complete";
  }
  if (state === "BLOCKED") {
    return "blocked";
  }
  if (state === "SUPERSEDED" || state === "ARCHIVED") {
    return "closed";
  }
  const currentStage = stageByState[state];
  if (currentStage === undefined) {
    return "pending";
  }
  if (stageIndex < currentStage) {
    return "complete";
  }
  return stageIndex === currentStage ? "current" : "pending";
}

export interface NextAction {
  title: string;
  description: string;
  label: string;
  path: string;
}

const findingRoutes: Array<{
  matches: (code: string) => boolean;
  label: string;
  path: string;
}> = [
  {
    matches: (code) => code.includes("DOCUMENT"),
    label: "Открыть документы",
    path: "DOCUMENTS",
  },
  {
    matches: (code) =>
      code.includes("QUANTITY") ||
      code.includes("CONFLICT") ||
      code.includes("ANALOGUE") ||
      code.includes("BOQ"),
    label: "Проверить ВОР",
    path: "BOQ_SCOPE",
  },
  {
    matches: (code) =>
      code.includes("PRICE") || code.includes("COST_WITHOUT_BASIS"),
    label: "Открыть ценовую матрицу",
    path: "boq/pricing-matrix",
  },
  {
    matches: (code) => code.includes("CONTRACT_RISK"),
    label: "Открыть риски договора",
    path: "CONTRACT_RISK",
  },
  {
    matches: (code) =>
      code.includes("CALCULATION") ||
      code.includes("RECALCULATION") ||
      code.includes("VALIDATION") ||
      code.includes("NORMATIVE"),
    label: "Открыть расчёт",
    path: "CALCULATION",
  },
  {
    matches: (code) => code.includes("APPROVAL"),
    label: "Открыть итоговый контроль",
    path: "release",
  },
  {
    matches: (code) =>
      code.includes("VERSION") ||
      code.includes("QUALIFICATION") ||
      code.includes("METHODOLOGY") ||
      code.includes("THRESHOLD"),
    label: "Открыть методологию",
    path: "GOVERNANCE",
  },
];

export function nextActionForFinding(
  finding: ValidationFinding,
  findingTitle: string,
): NextAction {
  const route = findingRoutes.find((candidate) =>
    candidate.matches(finding.code),
  );
  return {
    title: findingTitle,
    description:
      "Это первая зафиксированная причина остановки. После исправления система повторит проверки и покажет оставшиеся причины.",
    label: route?.label ?? "Открыть полный контроль",
    path: route?.path ?? "release",
  };
}

export function blockedWithoutFindingAction(): NextAction {
  return {
    title: "Причина блокировки не расшифрована",
    description:
      "Использование цены остаётся запрещено. Откройте полный контроль и передайте идентификатор проекта администратору.",
    label: "Открыть полный контроль",
    path: "release",
  };
}
