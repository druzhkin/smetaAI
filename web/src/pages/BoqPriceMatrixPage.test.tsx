import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { BoqPriceMatrixRow, BoqSourcePrice } from "../types";
import {
  MatchCell,
  SourcePriceCard,
  SourcePriceCell,
} from "./BoqPriceMatrixPage";

const sourcePrice: BoqSourcePrice = {
  quote_id: "quote-fgis-1",
  evidence_class: "OFFICIAL_OR_PRIMARY",
  source_reference: {
    source_type: "FGIS_CS",
    display_name: "ФГИС ЦС",
    source_item_name: "Труба ПЭ 100 SDR 17, 110×6,6 мм",
    source_record_id: "23.21.10.110.01.2.01.01-0001",
    source_uri: "https://fgiscs.minstroyrf.gov.ru/prices/record-1",
  },
  source_observation_id: "observation-fgis-1",
  source_origin_id: "fgis-cs",
  source_locator: "registry[row=1]",
  source_document_revision_id: "revision-1",
  observed_at: "2026-07-30T10:00:00Z",
  quote_date: "2026-07-29",
  valid_until: "2026-09-30",
  available: true,
  lead_time_days: 14,
  raw_amount: "1234.56",
  raw_currency: "RUB",
  raw_unit: "m",
  normalized_prices: [
    {
      normalized_price_id: "normalized-1",
      quote_id: "quote-fgis-1",
      amount_per_unit: "1234.56",
      currency: "RUB",
      unit: "m",
      formula_hash: "a".repeat(64),
      policy_version_id: "price-policy-1",
    },
  ],
  technical_attributes: {
    material: "ПЭ 100",
    diameter: "110",
    sdr: "17",
  },
};

const matrixRow: BoqPriceMatrixRow = {
  row_id: "line-1:1",
  boq_line_id: "line-1",
  line_key: "1",
  wbs_node_id: "WBS-1",
  work_code: "PIPE",
  boq_item_name: "Труба полиэтиленовая Ø110 мм",
  boq_unit: "m",
  quantity: "100",
  quantity_status: "VERIFIED",
  item_id: "pipe-110",
  cost_category: "MATERIAL",
  basis_kind: "MARKET",
  row_status: "BLOCKED",
  blockers: ["MARKET_PRICE_MISSING"],
  name_match: {
    match_id: "match-1",
    status: "VERIFIED",
    match_class: "EXACT",
    boq_item_name: "Труба полиэтиленовая Ø110 мм",
    source_item_id: "pipe-110",
    canonical_item_id: "catalog-pipe-110",
    source_attributes: { diameter: "110", material: "ПЭ 100", sdr: "17" },
    canonical_attributes: {
      diameter: "110",
      material: "ПЭ 100",
      sdr: "17",
    },
    mismatched_attributes: [],
    missing_attributes: [],
    catalog_version_id: "catalog-1",
    assessment_method: "DETERMINISTIC_CRITICAL_ATTRIBUTE_COMPARISON",
  },
  won_tender_prices: [],
  fgis_cs_prices: [sourcePrice],
  market_prices: [],
  other_prices: [],
  proposed_price: {
    status: "BLOCKED",
    workflow_status: "RFQ_REQUIRED",
    amount_per_unit: null,
    currency: null,
    unit: null,
    decision_id: null,
    as_of: null,
    selection_method: null,
    normalized_price_ids: [],
    rationale: ["Цена скрыта до получения рыночного источника."],
  },
};

describe("BoqPriceMatrixPage evidence cells", () => {
  it("shows the VOR and exact source names side by side with a direct source link", () => {
    render(
      <SourcePriceCard
        price={sourcePrice}
        boqName="Труба полиэтиленовая Ø110 мм"
      />,
    );

    expect(screen.getByText("Труба полиэтиленовая Ø110 мм")).toBeVisible();
    expect(screen.getByText("Труба ПЭ 100 SDR 17, 110×6,6 мм")).toBeVisible();
    expect(
      screen.getByRole("link", { name: /Открыть первоисточник/ }),
    ).toHaveAttribute(
      "href",
      "https://fgiscs.minstroyrf.gov.ru/prices/record-1",
    );
  });

  it("shows the catalog comparison without an AI confidence score", () => {
    const { container } = render(<MatchCell row={matrixRow} />);

    expect(screen.getByText("catalog-pipe-110")).toBeVisible();
    expect(screen.getByText("Матрица критических атрибутов")).toBeVisible();
    expect(container).not.toHaveTextContent(/confidence|уверенност/i);
  });

  it("renders a hard stop instead of an invented empty market price", () => {
    render(
      <SourcePriceCell
        prices={[]}
        boqName={matrixRow.boq_item_name}
        emptyLabel="Нет независимой цены с прямой ссылкой."
      />,
    );

    expect(screen.getByText("Заблокирован")).toBeVisible();
    expect(
      screen.getByText("Нет независимой цены с прямой ссылкой."),
    ).toBeVisible();
  });
});
