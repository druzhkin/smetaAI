import type {
  CommercialBasis,
  PriceItemContext,
  PriceQuoteSummary,
} from "./types";

export interface ControlledAttestation {
  reason: string;
  projectCode: string;
  acknowledged: boolean;
}

export interface PriceNormalizationDraft extends ControlledAttestation {
  unitConversionId: string;
  fxRateId: string;
  adjustmentIds: string[];
  regionAdjustmentId: string;
  partyAdjustmentId: string;
  paymentAdjustmentId: string;
}

export interface PriceEvaluationDraft extends ControlledAttestation {
  asOf: string;
}

export interface NormalizationRequirements {
  unitConversion: boolean;
  fxRate: boolean;
  regionAdjustment: boolean;
  partyAdjustment: boolean;
  paymentAdjustment: boolean;
  adjustmentKinds: string[];
}

export interface NormalizationCommand {
  quoteId: string;
  unitConversionId: string | null;
  fxRateId: string | null;
  adjustmentIds: string[];
  regionAdjustmentId: string | null;
  partyAdjustmentId: string | null;
  paymentAdjustmentId: string | null;
}

export function normalizationRequirements(
  source: CommercialBasis,
  target: CommercialBasis,
): NormalizationRequirements {
  const adjustmentKinds: string[] = [];
  if (target.delivery_included && !source.delivery_included) {
    adjustmentKinds.push("delivery");
  }
  if (target.unloading_included && !source.unloading_included) {
    adjustmentKinds.push("unloading");
  }
  return {
    unitConversion: source.unit !== target.unit,
    fxRate: source.currency !== target.currency,
    regionAdjustment: source.region !== target.region,
    partyAdjustment: source.party_quantity !== target.party_quantity,
    paymentAdjustment: source.payment_terms !== target.payment_terms,
    adjustmentKinds,
  };
}

function attestationError(
  draft: ControlledAttestation,
  expectedProjectCode: string,
): string | null {
  if (draft.reason.trim() === "") {
    return "Укажите проверяемое основание операции.";
  }
  if (draft.reason.length > 2000) {
    return "Основание операции превышает 2000 символов.";
  }
  if (draft.projectCode.trim() !== expectedProjectCode) {
    return "Контрольный шифр проекта не совпадает.";
  }
  if (!draft.acknowledged) {
    return "Подтвердите сверку точного источника и коммерческой базы.";
  }
  return null;
}

function optionalId(
  value: string,
  required: boolean,
  section: string,
  context: PriceItemContext,
  label: string,
): { value: string | null; error: string | null } {
  const normalized = value.trim();
  if (required && normalized === "") {
    return { value: null, error: `${label}: обязательна утверждённая ссылка.` };
  }
  if (!required && normalized !== "") {
    return { value: null, error: `${label}: ссылка является лишней.` };
  }
  if (
    normalized !== "" &&
    context.normalization_references[section]?.[normalized] === undefined
  ) {
    return {
      value: null,
      error: `${label}: ссылка отсутствует в текущей ценовой политике.`,
    };
  }
  return { value: normalized || null, error: null };
}

export function validateNormalizationDraft(
  draft: PriceNormalizationDraft,
  quote: PriceQuoteSummary,
  context: PriceItemContext,
  expectedProjectCode: string,
): { error: string | null; command: NormalizationCommand | null } {
  const attestation = attestationError(draft, expectedProjectCode);
  if (attestation !== null) {
    return { error: attestation, command: null };
  }
  const requirements = normalizationRequirements(
    quote.quote.basis,
    context.target_basis,
  );
  const references = [
    optionalId(
      draft.unitConversionId,
      requirements.unitConversion,
      "unit_conversions",
      context,
      "Преобразование единицы",
    ),
    optionalId(
      draft.fxRateId,
      requirements.fxRate,
      "fx_rates",
      context,
      "Валютный курс",
    ),
    optionalId(
      draft.regionAdjustmentId,
      requirements.regionAdjustment,
      "region_adjustments",
      context,
      "Региональная корректировка",
    ),
    optionalId(
      draft.partyAdjustmentId,
      requirements.partyAdjustment,
      "party_adjustments",
      context,
      "Корректировка партии",
    ),
    optionalId(
      draft.paymentAdjustmentId,
      requirements.paymentAdjustment,
      "payment_adjustments",
      context,
      "Корректировка оплаты",
    ),
  ];
  const referenceError = references.find(
    (reference) => reference.error !== null,
  );
  if (referenceError?.error !== null && referenceError?.error !== undefined) {
    return { error: referenceError.error, command: null };
  }
  const adjustmentIds = [...draft.adjustmentIds].sort();
  if (new Set(adjustmentIds).size !== adjustmentIds.length) {
    return {
      error: "Корректировки стоимости не должны повторяться.",
      command: null,
    };
  }
  const adjustmentCatalog = context.normalization_references.adjustments ?? {};
  if (adjustmentIds.some((id) => adjustmentCatalog[id] === undefined)) {
    return {
      error: "Выбрана корректировка вне текущей ценовой политики.",
      command: null,
    };
  }
  const selectedKinds = new Set(
    adjustmentIds.map((id) => adjustmentCatalog[id]?.kind).filter(Boolean),
  );
  const missingKind = requirements.adjustmentKinds.find(
    (kind) => !selectedKinds.has(kind),
  );
  if (missingKind !== undefined) {
    return {
      error: `Не выбрана обязательная корректировка вида ${missingKind}.`,
      command: null,
    };
  }
  return {
    error: null,
    command: {
      quoteId: quote.quote.quote_id,
      unitConversionId: references[0]?.value ?? null,
      fxRateId: references[1]?.value ?? null,
      adjustmentIds,
      regionAdjustmentId: references[2]?.value ?? null,
      partyAdjustmentId: references[3]?.value ?? null,
      paymentAdjustmentId: references[4]?.value ?? null,
    },
  };
}

export function validatePriceEvaluationDraft(
  draft: PriceEvaluationDraft,
  expectedProjectCode: string,
): string | null {
  const attestation = attestationError(draft, expectedProjectCode);
  if (attestation !== null) {
    return attestation;
  }
  if (!/^\d{4}-\d{2}-\d{2}$/.test(draft.asOf)) {
    return "Укажите дату среза цены.";
  }
  const parsed = new Date(`${draft.asOf}T00:00:00Z`);
  if (
    Number.isNaN(parsed.getTime()) ||
    parsed.toISOString().slice(0, 10) !== draft.asOf
  ) {
    return "Дата среза цены некорректна.";
  }
  return null;
}
