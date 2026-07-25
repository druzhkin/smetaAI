import { describe, expect, it } from "vitest";

import {
  normalizationRequirements,
  validateNormalizationDraft,
  validatePriceEvaluationDraft,
  type PriceNormalizationDraft,
} from "./priceWorkflow";
import type {
  CommercialBasis,
  PriceItemContext,
  PriceQuoteSummary,
} from "./types";

const sourceBasis: CommercialBasis = {
  currency: "USD",
  vat_basis: "EXCLUSIVE",
  vat_rate: "0.2",
  unit: "package",
  package_quantity: "10",
  party_quantity: "100",
  region: "Kazan",
  delivery_included: false,
  unloading_included: false,
  payment_terms: "prepayment",
};

const targetBasis: CommercialBasis = {
  ...sourceBasis,
  currency: "RUB",
  unit: "m",
  party_quantity: "1000",
  region: "Moscow",
  delivery_included: true,
  unloading_included: true,
  payment_terms: "30 days",
};

const quote = {
  quote: {
    quote_id: "quote-1",
    item_id: "pipe-1",
    supplier_id: "supplier-1",
    evidence_class: "COMMERCIAL_QUOTE",
    source_observation_id: "observation-1",
    technical_attributes: { diameter: "DN100" },
    amount: "100",
    basis: sourceBasis,
    quote_date: "2026-07-20",
    valid_until: "2026-08-20",
    lead_time_days: 10,
    available: true,
    source_reliability: "0.9",
    status: "UNNORMALIZED",
  },
  source_origin_id: "supplier-origin",
  normalized_prices: [],
} satisfies PriceQuoteSummary;

const context = {
  project_id: "project-1",
  item_id: "pipe-1",
  match_id: "match-1",
  match_class: "EXACT",
  critical_price: true,
  required_critical_attributes: ["diameter"],
  technical_attributes: { diameter: "DN100" },
  document_set_revision_id: "document-set-1",
  catalog_version_id: "catalog-1",
  price_policy_version_id: "price-policy-1",
  normalization_rounding_scale: 2,
  normalization_rounding_mode: "ROUND_HALF_UP",
  target_basis: targetBasis,
  normalization_references: {
    unit_conversions: { "unit-1": {} },
    fx_rates: { "fx-1": {} },
    adjustments: {
      "delivery-1": { kind: "delivery" },
      "unloading-1": { kind: "unloading" },
    },
    region_adjustments: { "region-1": {} },
    party_adjustments: { "party-1": {} },
    payment_adjustments: { "payment-1": {} },
  },
  quotes: [quote],
  current_decision: null,
} satisfies PriceItemContext;

const validDraft: PriceNormalizationDraft = {
  unitConversionId: "unit-1",
  fxRateId: "fx-1",
  adjustmentIds: ["unloading-1", "delivery-1"],
  regionAdjustmentId: "region-1",
  partyAdjustmentId: "party-1",
  paymentAdjustmentId: "payment-1",
  reason: "All commercial-basis references checked",
  projectCode: "PRICE-1",
  acknowledged: true,
};

describe("governed price workflow validation", () => {
  it("derives every required commercial-basis reference without arithmetic", () => {
    expect(normalizationRequirements(sourceBasis, targetBasis)).toEqual({
      unitConversion: true,
      fxRate: true,
      regionAdjustment: true,
      partyAdjustment: true,
      paymentAdjustment: true,
      adjustmentKinds: ["delivery", "unloading"],
    });
    const result = validateNormalizationDraft(
      validDraft,
      quote,
      context,
      "PRICE-1",
    );
    expect(result.error).toBeNull();
    expect(result.command).toEqual({
      quoteId: "quote-1",
      unitConversionId: "unit-1",
      fxRateId: "fx-1",
      adjustmentIds: ["delivery-1", "unloading-1"],
      regionAdjustmentId: "region-1",
      partyAdjustmentId: "party-1",
      paymentAdjustmentId: "payment-1",
    });
  });

  it("rejects missing, unknown, duplicate and extraneous references", () => {
    expect(
      validateNormalizationDraft(
        { ...validDraft, fxRateId: "" },
        quote,
        context,
        "PRICE-1",
      ).error,
    ).toContain("Валютный курс");
    expect(
      validateNormalizationDraft(
        { ...validDraft, fxRateId: "unknown" },
        quote,
        context,
        "PRICE-1",
      ).error,
    ).toContain("отсутствует");
    expect(
      validateNormalizationDraft(
        {
          ...validDraft,
          adjustmentIds: ["delivery-1", "delivery-1", "unloading-1"],
        },
        quote,
        context,
        "PRICE-1",
      ).error,
    ).toContain("повторяться");
    expect(
      validateNormalizationDraft(
        { ...validDraft, adjustmentIds: ["delivery-1"] },
        quote,
        context,
        "PRICE-1",
      ).error,
    ).toContain("unloading");
    expect(
      validateNormalizationDraft(
        { ...validDraft, unitConversionId: "unit-1" },
        { ...quote, quote: { ...quote.quote, basis: targetBasis } },
        context,
        "PRICE-1",
      ).error,
    ).toContain("лишней");
  });

  it("requires an exact date and explicit project attestation", () => {
    expect(
      validatePriceEvaluationDraft(
        {
          asOf: "2026-02-30",
          reason: "Evaluate current evidence",
          projectCode: "PRICE-1",
          acknowledged: true,
        },
        "PRICE-1",
      ),
    ).toContain("некорректна");
    expect(
      validatePriceEvaluationDraft(
        {
          asOf: "2026-07-24",
          reason: "Evaluate current evidence",
          projectCode: "WRONG",
          acknowledged: true,
        },
        "PRICE-1",
      ),
    ).toContain("шифр");
  });
});
