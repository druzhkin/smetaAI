import type {
  BoqAuthoringContext,
  BoqCostComponent,
  BoqLineReview,
  NomenclatureContext,
  NomenclatureMatchClass,
  NomenclatureReviewContext,
  ReconciliationContext,
} from "./types";

interface Attestation {
  reason: string;
  projectCodeConfirmation: string;
  acknowledged: boolean;
}

export interface ReconciliationDraft extends Attestation {
  observationIds: string[];
}

export interface BoqLineDraft extends Attestation {
  lineKey: string;
  wbsNodeId: string;
  description: string;
  evidenceObservationIds: string[];
  costComponents: BoqCostComponent[];
  criticalQuantity: boolean;
}

export interface NomenclatureDraft extends Attestation {
  sourceItemId: string;
  canonicalItemId: string;
  sourceAttributesObservationId: string;
}

export type AnalogueClass = Exclude<
  NomenclatureMatchClass,
  "EXACT" | "TECHNICALLY_UNACCEPTABLE" | "INSUFFICIENT_DATA"
>;

export interface AnalogueDraft extends Attestation {
  analogueClass: AnalogueClass;
}

function attestationError(
  draft: Attestation,
  projectCode: string,
): string | null {
  if (draft.reason.trim().length < 10 || draft.reason.trim().length > 2000) {
    return "Укажите проверяемое основание длиной от 10 до 2000 символов.";
  }
  if (draft.projectCodeConfirmation.trim() !== projectCode) {
    return "Введите точный код проекта для подтверждения области действия.";
  }
  if (!draft.acknowledged) {
    return "Подтвердите ответственность за точный выбранный контекст.";
  }
  return null;
}

export function validateReconciliation(
  draft: ReconciliationDraft,
  context: ReconciliationContext,
  projectCode: string,
): string | null {
  const attestation = attestationError(draft, projectCode);
  if (attestation !== null) {
    return attestation;
  }
  const ids = draft.observationIds;
  if (ids.length < 2 || new Set(ids).size !== ids.length) {
    return "Выберите не менее двух разных наблюдений.";
  }
  const candidates = ids.map((id) =>
    context.candidates.find(
      (candidate) => candidate.observation.observation_id === id,
    ),
  );
  if (
    candidates.some(
      (candidate) => candidate === undefined || !candidate.eligible,
    )
  ) {
    return "Каждый источник должен оставаться допустимым по серверному контексту.";
  }
  const present = candidates.filter((candidate) => candidate !== undefined);
  if (
    present.some(
      (candidate) =>
        candidate.observation.field_name !== context.selected_field_name,
    )
  ) {
    return "Наблюдения должны относиться к одному выбранному полю.";
  }
  const methods = new Set(
    present.map(
      (candidate) =>
        `${candidate.observation.method}:${candidate.observation.method_version}`,
    ),
  );
  const domains = new Set(
    present.map((candidate) => candidate.independence_domain),
  );
  const qualifications = new Set(
    present.map((candidate) => candidate.adapter_qualification_id),
  );
  if (
    methods.size !== present.length ||
    domains.has(null) ||
    domains.size !== present.length ||
    qualifications.has(null) ||
    qualifications.size !== present.length
  ) {
    return "Источники должны иметь разные методы, квалификации и независимые домены.";
  }
  return null;
}

function normalizedText(
  value: string,
  minimum: number,
  maximum: number,
): boolean {
  return (
    value === value.trim() && value.length >= minimum && value.length <= maximum
  );
}

export function validateBoqLine(
  draft: BoqLineDraft,
  context: BoqAuthoringContext,
  projectCode: string,
): string | null {
  const attestation = attestationError(draft, projectCode);
  if (attestation !== null) {
    return attestation;
  }
  if (
    !normalizedText(draft.lineKey, 1, 128) ||
    !normalizedText(draft.wbsNodeId, 1, 128) ||
    !normalizedText(draft.description, 3, 2000)
  ) {
    return "Ключ строки, WBS и описание должны быть заполнены без внешних пробелов.";
  }
  if (
    draft.evidenceObservationIds.length < 1 ||
    new Set(draft.evidenceObservationIds).size !==
      draft.evidenceObservationIds.length
  ) {
    return "Выберите хотя бы одно уникальное серверное доказательство строки.";
  }
  const selected = draft.evidenceObservationIds.map((id) =>
    context.evidence_candidates.find(
      (candidate) => candidate.observation.observation_id === id,
    ),
  );
  if (selected.some((candidate) => candidate === undefined)) {
    return "Выбранное доказательство больше не входит в текущий контекст BoQ.";
  }
  const present = selected.filter((candidate) => candidate !== undefined);
  if (
    new Set(present.map((candidate) => candidate.work_code)).size !== 1 ||
    new Set(present.map((candidate) => candidate.unit)).size !== 1
  ) {
    return "Все доказательства строки должны воспроизводить один код работы и единицу.";
  }
  if (draft.costComponents.length < 1) {
    return "Добавьте хотя бы один плановый компонент стоимости.";
  }
  const semanticKeys = draft.costComponents.map((item) => item.semantic_key);
  if (
    new Set(semanticKeys).size !== semanticKeys.length ||
    draft.costComponents.some(
      (item) =>
        !normalizedText(item.semantic_key, 1, 128) ||
        ![-1, 1].includes(item.sign) ||
        new Set(item.factor_ids).size !== item.factor_ids.length ||
        item.factor_ids.some((id) => !normalizedText(id, 1, 200)),
    )
  ) {
    return "Компоненты стоимости должны иметь уникальные нормализованные ключи и факторы.";
  }
  return null;
}

export function validateBoqVerification(
  draft: Attestation,
  review: BoqLineReview,
  projectCode: string,
): string | null {
  const attestation = attestationError(draft, projectCode);
  if (attestation !== null) {
    return attestation;
  }
  if (!review.verification_allowed) {
    return `Проверка заблокирована: ${review.verification_blockers.join(", ")}`;
  }
  return null;
}

export function validateNomenclatureAssessment(
  draft: NomenclatureDraft,
  context: NomenclatureContext,
  projectCode: string,
): string | null {
  const attestation = attestationError(draft, projectCode);
  if (attestation !== null) {
    return attestation;
  }
  if (!normalizedText(draft.sourceItemId, 1, 128)) {
    return "Укажите точный semantic key компонента текущего BoQ.";
  }
  if (
    context.selected_source_item_id !== draft.sourceItemId ||
    !context.source_items.some(
      (item) => item.source_item_id === draft.sourceItemId,
    )
  ) {
    return "Выберите позицию из текущего проверенного BoQ и дождитесь связанного серверного контекста.";
  }
  if (
    !context.catalog_items.some(
      (item) => item.canonical_item_id === draft.canonicalItemId,
    )
  ) {
    return "Выберите позицию из текущей утверждённой версии каталога.";
  }
  if (
    !context.evidence_candidates.some(
      (item) =>
        item.observation.observation_id === draft.sourceAttributesObservationId,
    )
  ) {
    return "Выберите проверенное наблюдение технических атрибутов.";
  }
  return null;
}

export function validateAnalogueProposal(
  draft: AnalogueDraft,
  review: NomenclatureReviewContext,
  projectCode: string,
): string | null {
  const attestation = attestationError(draft, projectCode);
  if (attestation !== null) {
    return attestation;
  }
  if (review.match.match.missing_attributes.length > 0) {
    return "Аналог нельзя предложить при отсутствующих критических атрибутах.";
  }
  if (review.match.match.match_class !== "TECHNICALLY_UNACCEPTABLE") {
    return "Предложение аналога начинается только с детерминированного несовпадения.";
  }
  return null;
}

export function validateAnalogueFinalization(
  draft: Attestation,
  review: NomenclatureReviewContext,
  projectCode: string,
): string | null {
  const attestation = attestationError(draft, projectCode);
  if (attestation !== null) {
    return attestation;
  }
  if (!review.finalization_allowed) {
    return `Финализация заблокирована: ${review.finalization_blockers.join(", ")}`;
  }
  return null;
}
