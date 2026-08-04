import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type {
  BoqDiagnosticSourceCandidate,
  BoqPriceMatrixRow,
  BoqSourcePrice,
} from "../types";
import {
  DiagnosticSourceCandidateCard,
  MatchCell,
  ProposedPriceCell,
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
  research_route: null,
  won_tender_research_candidates: [],
  fgis_cs_research_candidates: [],
  market_research_candidates: [],
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

const diagnosticCandidate: BoqDiagnosticSourceCandidate = {
  research_id: "diagnostic-market-1",
  source_group: "MARKET",
  source_type: "SUPPLIER_WEBSITE",
  source_display_name: "Поставщик кабеля",
  source_item_name: "Кабель АПвПг 1х240/70",
  source_record_id: "sku:CABLE-1",
  source_uri: "https://supplier.example/cable",
  source_locator: "microdata:product[0]",
  observed_at: "2026-08-03T11:00:00Z",
  period_name: null,
  evidence_sha256: "a".repeat(64),
  candidate_content_hash: "b".repeat(64),
  observed_amounts: [
    {
      amount_kind: "MARKET_OFFER",
      amount: "533.28",
      amount_literal: "533.28",
      currency: "RUB",
      unit: null,
    },
  ],
  attributes: { "Метод извлечения": "MICRODATA" },
  boq_only_literals: [],
  source_only_literals: [],
  comparison_method: "technical-literals/v1",
  blockers: [
    "STRUCTURED_SOURCE_UNIT_MISSING",
    "DIAGNOSTIC_SOURCE_CANDIDATE_NOT_PRICE_EVIDENCE",
  ],
  status: "BLOCKED",
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

  it("shows a raw research amount and both names without presenting a system price", () => {
    render(
      <DiagnosticSourceCandidateCard
        candidate={diagnosticCandidate}
        boqName="Кабель АПвПг 1х240/70"
      />,
    );

    expect(screen.getByText("Поставщик кабеля")).toBeVisible();
    expect(screen.getAllByText("Кабель АПвПг 1х240/70")).toHaveLength(2);
    expect(screen.getByText("533,28 ₽")).toBeVisible();
    expect(screen.getByText(/единица не указана/)).toBeVisible();
    expect(screen.getByText("Кандидат")).toBeVisible();
    expect(screen.queryByText("Цена системы")).not.toBeInTheDocument();
    expect(
      screen
        .getAllByRole("link", { name: "Открыть первоисточник" })
        .some(
          (link) =>
            link.getAttribute("href") === "https://supplier.example/cable",
        ),
    ).toBe(true);
  });

  it("keeps diagnostic blockers collapsed and does not link to a missing calculation", () => {
    const diagnosticRow: BoqPriceMatrixRow = {
      ...matrixRow,
      blockers: ["MARKET_PRICE_MISSING", "PRICE_DECISION_MISSING"],
      proposed_price: {
        ...matrixRow.proposed_price,
        workflow_status: "DIAGNOSTIC_ONLY",
        rationale: [
          "Строка извлечена как непроверенное свидетельство.",
          "Blocker: MARKET_PRICE_MISSING",
          "Blocker: PRICE_DECISION_MISSING",
        ],
      },
    };

    render(
      <ProposedPriceCell
        row={diagnosticRow}
        projectId="diagnostic-alabuga-4527946"
      />,
    );

    expect(
      screen.getByText("Строка извлечена как непроверенное свидетельство."),
    ).toBeVisible();
    expect(screen.queryByText(/Блокировка:/)).not.toBeInTheDocument();
    expect(screen.getByText(/2 причин блокировки/)).toBeVisible();
    expect(screen.getByText("Расчёт ещё не создан")).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    expect(
      screen.queryByRole("link", { name: "Открыть расчёт" }),
    ).not.toBeInTheDocument();
  });
});
