import type { ActorRole, ApprovalState, ProjectRecordSection } from "./types";

export const stateLabels: Record<ApprovalState, string> = {
  DRAFT: "Черновик",
  DOCUMENTS_INCOMPLETE: "Документы неполны",
  EXTRACTION_IN_PROGRESS: "Извлечение",
  EXTRACTION_REVIEW: "Проверка извлечения",
  BOQ_IN_PROGRESS: "Формирование BoQ",
  BOQ_REVIEW: "Проверка BoQ",
  PRICING_IN_PROGRESS: "Сбор цен",
  RFQ_REQUIRED: "Требуется RFQ",
  CALCULATION_IN_PROGRESS: "Расчёт",
  INDEPENDENT_VALIDATION: "Независимая проверка",
  EXPERT_REVIEW: "Экспертное согласование",
  BLOCKED: "Заблокирован",
  APPROVED_FOR_INTERNAL_USE: "Для внутреннего использования",
  APPROVED_FOR_BID: "Допущен к конкурсу",
  SUPERSEDED: "Заменён",
  ARCHIVED: "Архив",
};

export const roleLabels: Record<ActorRole, string> = {
  ESTIMATOR: "Сметчик",
  PROCUREMENT: "Закупки",
  TECHNICAL_EXPERT: "Технический эксперт",
  REVIEWER: "Проверяющий",
  APPROVER: "Утверждающий",
  METHODOLOGY_OWNER: "Владелец методологии",
  CATALOG_OWNER: "Владелец справочника",
  AUDITOR: "Аудитор",
  ADMIN: "Администратор",
  SYSTEM: "Системный сервис",
};

export interface SectionDefinition {
  code: ProjectRecordSection;
  label: string;
  shortLabel: string;
  description: string;
  index: string;
}

export const sections: SectionDefinition[] = [
  {
    code: "DOCUMENTS",
    label: "Документы и редакции",
    shortLabel: "Документы",
    description: "Комплектность, актуальность, карантин и версии",
    index: "01",
  },
  {
    code: "EVIDENCE",
    label: "Доказательства и паспорт",
    shortLabel: "Доказательства",
    description: "Наблюдения, конфликты и подтверждённые факты",
    index: "02",
  },
  {
    code: "BOQ_SCOPE",
    label: "BoQ и полнота работ",
    shortLabel: "BoQ / Scope",
    description: "Объёмы, номенклатура и сопутствующие работы",
    index: "03",
  },
  {
    code: "PRICING",
    label: "Цены и RFQ",
    shortLabel: "Цены",
    description: "Источники, коммерческая база и triangulation",
    index: "04",
  },
  {
    code: "CONTRACT_RISK",
    label: "Договор, логистика и риски",
    shortLabel: "Риски",
    description: "Условия исполнения, резерв и коммерческие модели",
    index: "05",
  },
  {
    code: "CALCULATION",
    label: "Расчёт и сценарии",
    shortLabel: "Расчёт",
    description: "Атомарные входы, пересчёт и снимки",
    index: "06",
  },
  {
    code: "APPROVALS",
    label: "Согласования",
    shortLabel: "Согласования",
    description: "Задачи, решения и принцип четырёх глаз",
    index: "07",
  },
  {
    code: "ACTUALS",
    label: "Факт и калибровка",
    shortLabel: "Факт",
    description: "Отклонения прогноза и проверенный факт",
    index: "08",
  },
  {
    code: "GOVERNANCE",
    label: "Методология и выпуски",
    shortLabel: "Управление",
    description: "Утверждённые версии и подписанные экспорты",
    index: "09",
  },
  {
    code: "AUDIT",
    label: "Журнал аудита",
    shortLabel: "Аудит",
    description: "Переходы, авторы и криптографическая цепочка",
    index: "10",
  },
];

export const taskLabels: Record<string, string> = {
  HIGH_VALUE_REVIEW: "Проверка дорогостоящей позиции",
  CONFLICT_REVIEW: "Разрешение конфликта",
  CONFLICT_RESOLUTION: "Разрешение конфликта источников",
  QUANTITY_REVIEW: "Проверка объёма",
  ANALOGUE_REVIEW: "Проверка аналога",
  PRICE_REVIEW: "Проверка цены",
  RFQ_REVIEW: "Проверка RFQ",
  MANUAL_CHANGE_REVIEW: "Проверка ручного изменения",
  RISK_REVIEW: "Проверка риска",
};

export const findingLabels: Record<string, string> = {
  CURRENT_DOCUMENT_SET_NOT_CONFIRMED:
    "Не подтверждена актуальная редакция комплекта конкурсной документации",
  CRITICAL_DOCUMENT_MISSING: "Отсутствует критический документ",
  KEY_QUANTITY_UNVERIFIED: "Ключевые объёмы не подтверждены",
  UNRESOLVED_CONFLICT: "Обнаружен неразрешённый конфликт источников",
  COST_WITHOUT_BASIS: "Часть стоимости не имеет источника или допущения",
  TECHNICAL_ANALOGUE_UNVERIFIED: "Технический аналог не проверен",
  PRICE_NORMALIZATION_FAILED: "Нарушена нормализация коммерческой базы цены",
  REQUIRED_APPROVAL_MISSING:
    "Не завершены обязательные экспертные согласования",
  CONTRACT_RISK_UNRESOLVED:
    "Не разрешены договорные риски, влияющие на стоимость исполнения",
  BLOCKING_CONTOUR_FINDING:
    "Контуры верификации содержат блокирующие замечания",
  INDEPENDENT_VALIDATION_MISSING: "Независимый пересчёт не выполнен",
  INDEPENDENT_RECALCULATION_MISMATCH:
    "Независимый пересчёт не сошёлся с основным",
  UNVERIFIED_COST_THRESHOLD_UNCONFIGURED:
    "Не настроена допустимая доля непроверенной стоимости",
  UNVERIFIED_COST_SHARE_EXCEEDED:
    "Превышена допустимая доля непроверенной стоимости",
  CONTROLLED_VERSION_MISSING:
    "Не привязаны обязательные утверждённые версии справочников и моделей",
  CALCULATION_SNAPSHOT_MISSING: "Не зафиксирован calculation snapshot",
  NORMATIVE_ENGINE_UNAVAILABLE:
    "Нет квалифицированного сметного движка или полного нормативного основания",
  NORMATIVE_CALCULATION_MISSING:
    "К проекту не привязан проверенный нормативный расчёт",
  PRODUCTION_QUALIFICATION_INCOMPLETE:
    "Не завершены производственные quality gates и утверждение методологии",
};

export const metricLabels: Record<string, string> = {
  DOCUMENTS: "Актуальные документы",
  BOQ: "Актуальные строки BoQ",
  CONFLICTS: "Неразрешённые конфликты",
  APPROVALS: "Ожидают согласования",
  FINDINGS: "Блокирующие замечания",
};
