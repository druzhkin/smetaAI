import { useMemo, useState } from "react";

import { Icon } from "../components/Icon";
import {
  basisPointsToPercent,
  calculateCommercialScenario,
  kopecksToDecimal,
  type CommercialLineResult,
  type QuantityScenario,
} from "../commercialScenario";
import {
  alabugaCommercialAssumptions,
  alabugaCommercialLines,
  alabugaCommercialProvenance,
} from "../data/alabuga-commercial-scenario";
import { formatDateTime, formatDecimal, formatMoney } from "../format";
import { alabugaPublicSnapshot, safeExternalHttpsUrl } from "../publicSnapshot";
import type {
  BoqDiagnosticObservedAmount,
  BoqDiagnosticSourceCandidate,
  BoqPriceMatrixRow,
  RuntimeConfig,
} from "../types";

type RowFilter = "ALL" | "WITH_AMOUNT" | "FGIS" | "MARKET" | "WITHOUT_SOURCES";

const scenarioLabels: Record<QuantityScenario, string> = {
  BOQ: "По ВОР заказчика",
  PROJECT: "По проектной документации",
};

function formatKopecks(value: bigint): string {
  return formatMoney(kopecksToDecimal(value), "RUB");
}

function formatRubles(value: bigint | string): string {
  return formatMoney(String(value), "RUB");
}

function formatPercent(value: bigint): string {
  return `${formatDecimal(basisPointsToPercent(value))}%`;
}

const amountKindLabels: Record<
  BoqDiagnosticObservedAmount["amount_kind"],
  string
> = {
  FGIS_AGGREGATED: "Агрегированная цена",
  FGIS_ESTIMATED: "Сметная цена",
  FGIS_DISTANCE: "Перевозка",
  MARKET_OFFER: "Цена на странице",
};

const blockerLabels: Record<string, string> = {
  APPROVED_FGIS_MAPPING_REQUIRED: "не утверждено сопоставление с кодом КСР",
  APPROVED_LOGISTICS_METHOD_REQUIRED: "нет утверждённой методики логистики",
  APPROVED_NORMATIVE_ENGINE_REQUIRED:
    "не подключён утверждённый сметный движок",
  BID_RELEASE_NOT_APPROVED: "проект не допущен к выпуску цены",
  BOQ_LINE_NOT_VERIFIED: "строка ВОР ещё не прошла контроль",
  CALCULATION_SNAPSHOT_MISSING: "итоговый расчёт не зафиксирован",
  CONTROLLED_IMPORT_WORKFLOW_REQUIRED:
    "нужен управляемый импорт исходной книги",
  DELIVERY_BASIS_UNKNOWN: "условия доставки не определены",
  DIAGNOSTIC_MARKET_RESEARCH_NOT_GOVERNED:
    "рыночная находка ещё не стала ценовым основанием",
  FGIS_CS_PRICE_MISSING: "нет подтверждённой цены ФГИС ЦС",
  FGIS_HISTORY_SOURCE_ERRORS_PRESENT: "в истории ФГИС ЦС есть ошибки источника",
  FGIS_KSR_CANDIDATES_NOT_FOUND: "кандидаты КСР не найдены",
  FGIS_KSR_EXACT_LITERAL_CANDIDATE_AMBIGUOUS:
    "точное буквальное совпадение КСР неоднозначно",
  FGIS_KSR_EXACT_LITERAL_CANDIDATE_NOT_FOUND:
    "точное буквальное совпадение КСР не найдено",
  FGIS_KSR_SEARCH_CANDIDATE_NOT_SELECTED:
    "вариант найден поиском КСР, но не выбран для истории цен",
  FGIS_KSR_SUITABLE_CANDIDATE_NOT_SELECTED: "подходящий код КСР не выбран",
  FGIS_PRICE_ACQUISITION_REQUIRED: "цену ФГИС ЦС ещё нужно получить",
  FGIS_PRICE_NOT_PUBLISHED_FOR_RETRIEVED_PERIODS:
    "для найденных периодов цена ФГИС ЦС не опубликована",
  FGIS_PRICE_PERIOD_REQUIRED: "нужно выбрать период ФГИС ЦС",
  HIDDEN_WORKBOOK_CONTENT: "в исходной книге есть скрытое содержимое",
  INDEPENDENT_ROW_REVIEW_REQUIRED: "извлечённая строка не прошла контроль",
  INDEPENDENT_VALIDATION_REQUIRED: "нет независимого пересчёта",
  INTAKE_CORRUPT_OR_PROTECTED_EXCEL:
    "исходный Excel не прошёл строгую проверку целостности",
  INTAKE_MANIFEST_BLOCKED: "приём исходного комплекта заблокирован",
  MARKET_PRICE_MISSING: "нет подтверждённой рыночной цены",
  MARKET_PUBLIC_SOURCE_NOT_SELECTED: "рыночный источник не выбран",
  MARKET_SOURCE_ACQUISITION_REQUIRED: "нужен дополнительный рыночный поиск",
  MARKET_STRUCTURED_DATA_FINDINGS_PRESENT:
    "структурированные данные страницы требуют проверки",
  MARKET_UNIT_MAPPING_REQUIRED: "единица рыночной цены не сопоставлена",
  MISSING_STABLE_POSITION_ID: "нет устойчивого номера позиции",
  NOMENCLATURE_MATCH_MISSING: "сопоставление номенклатуры не подтверждено",
  PAYMENT_TERMS_UNKNOWN: "условия оплаты не определены",
  POSITION_ID_FORMULA_NOT_ALLOWED: "номер позиции задан формулой",
  PRICE_DECISION_MISSING: "система ещё не сформировала ценовое решение",
  PRICE_NORMALIZATION_REQUIRED: "цены не приведены к единой базе",
  PRICE_POLICY_INTEGRITY_FAILED: "ценовая политика не прошла контроль",
  PRICE_VALIDITY_NOT_ESTABLISHED: "срок действия цены не подтверждён",
  PROJECT_PRICE_PERIOD_NOT_SELECTED: "расчётный период проекта не выбран",
  QUANTITY_NOT_VERIFIED: "объём не подтверждён",
  STRUCTURED_MARKET_OFFER_NOT_FOUND:
    "структурированная цена на странице не найдена",
  TECHNICAL_EQUIVALENCE_NOT_ESTABLISHED:
    "техническая эквивалентность не доказана",
  UNLOADING_BASIS_UNKNOWN: "условия разгрузки не определены",
  VAT_BASIS_UNKNOWN: "не определено включение НДС",
  WON_TENDER_LINE_PRICE_REQUIRED: "нет доказуемой построчной цены контракта",
  WON_TENDER_PRICE_MISSING: "нет сопоставимой цены выигранного тендера",
};

const flow = [
  ["01", "ВОР", "23 позиции и объёма извлечены"],
  ["02", "Поиск", "ФГИС ЦС и открытый рынок"],
  ["03", "Сверка", "Наименования и характеристики рядом"],
  ["04", "Приведение цен", "Единица, НДС, доставка и период"],
  ["05", "Решение", "Цена или точная причина остановки"],
] as const;

function blockerLabel(value: string): string {
  return (
    blockerLabels[value] ??
    value.replaceAll("_", " ").toLocaleLowerCase("ru-RU")
  );
}

function rowCandidates(row: BoqPriceMatrixRow): BoqDiagnosticSourceCandidate[] {
  return [
    ...row.won_tender_research_candidates,
    ...row.fgis_cs_research_candidates,
    ...row.market_research_candidates,
  ];
}

function rowHasObservedAmount(row: BoqPriceMatrixRow): boolean {
  return rowCandidates(row).some(
    (candidate) => candidate.observed_amounts.length > 0,
  );
}

function observedCandidateCount(
  candidates: BoqDiagnosticSourceCandidate[],
): number {
  return candidates.filter((candidate) => candidate.observed_amounts.length > 0)
    .length;
}

function rowHasFgisObservedAmount(row: BoqPriceMatrixRow): boolean {
  return observedCandidateCount(row.fgis_cs_research_candidates) > 0;
}

function rowHasMarketObservedAmount(row: BoqPriceMatrixRow): boolean {
  return observedCandidateCount(row.market_research_candidates) > 0;
}

function sourceHost(value: string): string {
  const safeUrl = safeExternalHttpsUrl(value);
  return safeUrl === null ? "некорректная ссылка" : new URL(safeUrl).hostname;
}

function csvCell(value: string): string {
  return `"${value.replaceAll('"', '""')}"`;
}

export function buildPublicMatrixCsv(
  rows: BoqPriceMatrixRow[],
  scenario: QuantityScenario = "BOQ",
): string {
  const calculation = calculateCommercialScenario(
    alabugaCommercialLines,
    alabugaCommercialAssumptions,
    scenario,
  );
  const commercialByLineKey = new Map(
    calculation.lines.map((line) => [line.lineKey, line]),
  );
  const header = [
    "№",
    "Позиция ВОР",
    "Наименование ВОР",
    "Количество",
    "Единица",
    "Тендерный/сметный ориентир, руб./ед. без НДС",
    "ФГИС ЦС/РИМ, руб./ед. без НДС",
    "Рынок, руб./ед. без НДС",
    "Предварительная цена системы, руб./ед. без НДС",
    "Предварительная себестоимость строки, руб. без НДС",
    "Наименования ФГИС ЦС",
    "Сырые значения ФГИС ЦС",
    "Ссылки ФГИС ЦС",
    "Наименования рынка",
    "Сырые рыночные значения",
    "Ссылки рынка",
    "Цена системы",
    "Статус",
    "Причины остановки",
    "Источник предварительного сценария",
    "SHA256 источника",
  ];
  const lines = rows.map((row, index) => {
    const commercial = commercialByLineKey.get(row.line_key);
    const describeAmounts = (candidates: BoqDiagnosticSourceCandidate[]) =>
      candidates
        .flatMap((candidate) =>
          candidate.observed_amounts.map(
            (amount) =>
              `${amount.amount_literal} ${amount.currency ?? "валюта не указана"} / ${amount.unit ?? "единица не указана"}`,
          ),
        )
        .join(" | ");
    return [
      String(index + 1),
      row.line_key,
      row.boq_item_name,
      commercial?.quantity ?? row.quantity ?? "",
      row.boq_unit,
      commercial?.tenderUnitPrice ?? "",
      commercial?.fgisUnitPrice ?? "",
      commercial?.marketUnitPrice ?? "",
      commercial?.preliminaryUnitPriceRubles.toString() ?? "",
      commercial === undefined
        ? ""
        : kopecksToDecimal(commercial.directCostKopecks),
      row.fgis_cs_research_candidates
        .map((item) => item.source_item_name)
        .join(" | "),
      describeAmounts(row.fgis_cs_research_candidates),
      row.fgis_cs_research_candidates
        .map((item) => item.source_uri)
        .join(" | "),
      row.market_research_candidates
        .map((item) => item.source_item_name)
        .join(" | "),
      describeAmounts(row.market_research_candidates),
      row.market_research_candidates.map((item) => item.source_uri).join(" | "),
      commercial?.preliminaryUnitPriceRubles.toString() ?? "не сформирована",
      row.row_status,
      row.blockers.map(blockerLabel).join(" | "),
      alabugaCommercialProvenance.workbook,
      alabugaCommercialProvenance.workbookSha256,
    ].map(csvCell);
  });
  return [
    header.map(csvCell).join(";"),
    ...lines.map((line) => line.join(";")),
  ].join("\r\n");
}

function downloadCsv(rows: BoqPriceMatrixRow[], scenario: QuantityScenario) {
  const blob = new Blob(["\uFEFF", buildPublicMatrixCsv(rows, scenario)], {
    type: "text/csv;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `Алабуга_4527946_ВОР_${scenario.toLocaleLowerCase()}.csv`;
  anchor.click();
  URL.revokeObjectURL(url);
}

function CandidateCard({
  candidate,
  sourceKind,
}: {
  candidate: BoqDiagnosticSourceCandidate;
  sourceKind: "ФГИС ЦС" | "Рынок";
}) {
  const safeUrl = safeExternalHttpsUrl(candidate.source_uri);
  const safeLocatorUrl = safeExternalHttpsUrl(candidate.source_locator);
  return (
    <article className="public-candidate">
      <header className="public-candidate__header">
        <div>
          <span>{sourceKind}</span>
          <strong>{candidate.source_item_name}</strong>
          <small>{candidate.source_display_name}</small>
        </div>
        <em>
          {sourceKind === "ФГИС ЦС"
            ? candidate.observed_amounts.length > 0
              ? candidate.comparison_method === "EXACT_LITERAL_NAME_AND_UNIT"
                ? "Точное имя и единица"
                : "Альтернативная запись"
              : "Вариант КСР"
            : "Рыночная находка"}
        </em>
      </header>

      {candidate.observed_amounts.length > 0 ? (
        <div className="public-candidate__amounts">
          {candidate.observed_amounts.map((amount) => (
            <div key={`${amount.amount_kind}:${amount.amount_literal}`}>
              <span>{amountKindLabels[amount.amount_kind]}</span>
              <strong>
                {amount.currency === null
                  ? formatDecimal(amount.amount)
                  : formatMoney(amount.amount, amount.currency)}
              </strong>
              <small>
                {amount.unit === null
                  ? "единица не указана"
                  : `/ ${amount.unit}`}
                {amount.currency === null ? " · валюта не указана" : ""}
              </small>
            </div>
          ))}
        </div>
      ) : (
        <p className="public-candidate__no-amount">
          {sourceKind === "ФГИС ЦС"
            ? "Наименование найдено в каталоге КСР, но для этой карточки опубликованная сумма не получена."
            : "Наименование найдено, но опубликованная сумма не получена."}
        </p>
      )}

      <dl className="public-candidate__facts">
        <div>
          <dt>Источник</dt>
          <dd>{sourceHost(candidate.source_uri)}</dd>
        </div>
        <div>
          <dt>Период</dt>
          <dd>{candidate.period_name ?? "не указан"}</dd>
        </div>
        <div>
          <dt>Получено</dt>
          <dd>{formatDateTime(candidate.observed_at)}</dd>
        </div>
      </dl>

      <details className="public-candidate__details">
        <summary>Сопоставление и доказательства</summary>
        <div className="public-candidate__detail-body">
          <dl>
            <div>
              <dt>Метод сравнения</dt>
              <dd>{candidate.comparison_method}</dd>
            </div>
            <div>
              <dt>Запись источника</dt>
              <dd>{candidate.source_record_id}</dd>
            </div>
            <div>
              <dt>SHA-256 доказательства</dt>
              <dd className="public-hash">{candidate.evidence_sha256}</dd>
            </div>
          </dl>
          {candidate.boq_only_literals.length > 0 && (
            <div className="public-literal-diff">
              <strong>Есть только в ВОР</strong>
              <p>{candidate.boq_only_literals.join(" · ")}</p>
            </div>
          )}
          {candidate.source_only_literals.length > 0 && (
            <div className="public-literal-diff">
              <strong>Есть только у источника</strong>
              <p>{candidate.source_only_literals.join(" · ")}</p>
            </div>
          )}
          {Object.keys(candidate.attributes).length > 0 && (
            <div className="public-attribute-list">
              {Object.entries(candidate.attributes).map(([key, value]) => (
                <span key={key}>
                  <b>{key}</b>
                  {value}
                </span>
              ))}
            </div>
          )}
          <ul className="public-candidate__blockers">
            {candidate.blockers.map((blocker) => (
              <li key={blocker}>{blockerLabel(blocker)}</li>
            ))}
          </ul>
        </div>
      </details>

      <div className="public-candidate__links">
        {safeUrl === null && safeLocatorUrl === null ? (
          <span className="public-candidate__link is-invalid">
            Ссылка источника недоступна
          </span>
        ) : (
          <>
            {safeUrl !== null && (
              <a
                className="public-candidate__link"
                href={safeUrl}
                target="_blank"
                rel="noreferrer noopener"
              >
                Открыть портал
                <Icon name="arrow" size={15} />
              </a>
            )}
            {safeLocatorUrl !== null && safeLocatorUrl !== safeUrl && (
              <a
                className="public-candidate__link"
                href={safeLocatorUrl}
                target="_blank"
                rel="noreferrer noopener"
              >
                Открыть точный запрос
                <Icon name="arrow" size={15} />
              </a>
            )}
          </>
        )}
      </div>
    </article>
  );
}

function SourceColumn({
  title,
  description,
  candidates,
  sourceKind,
  emptyTitle = "Данных нет",
  emptyDescription = "Сопоставимый проверяемый источник пока не найден.",
}: {
  title: string;
  description: string;
  candidates: BoqDiagnosticSourceCandidate[];
  sourceKind: "ФГИС ЦС" | "Рынок";
  emptyTitle?: string;
  emptyDescription?: string;
}) {
  return (
    <section className="public-source-column">
      <header>
        <div>
          <span>{title}</span>
          <p>{description}</p>
        </div>
        <strong>{candidates.length}</strong>
      </header>
      {candidates.length === 0 ? (
        <div className="public-source-empty">
          <Icon name="search" size={20} />
          <strong>{emptyTitle}</strong>
          <span>{emptyDescription}</span>
        </div>
      ) : (
        <div className="public-source-column__cards">
          {candidates.map((candidate) => (
            <CandidateCard
              key={candidate.research_id}
              candidate={candidate}
              sourceKind={sourceKind}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function PositionDetail({
  row,
  commercialLine,
}: {
  row: BoqPriceMatrixRow;
  commercialLine: CommercialLineResult | undefined;
}) {
  const costNature = row.research_route?.cost_nature;
  const route =
    costNature === "WORK"
      ? {
          label: "Работа · нормативный маршрут",
          title: "Цена ресурса ФГИС ЦС здесь не применяется",
          description:
            "Эта строка описывает работу. Её стоимость должна рассчитываться по ГЭСН утверждённым сметным движком, а не искаться как товар в КСР.",
        }
      : costNature === "LOGISTICS"
        ? {
            label: "Логистика · отдельный маршрут",
            title: "Нужен расчёт перевозки",
            description:
              "Стоимость зависит от расстояния, класса груза, транспорта и условий маршрута. Прямая цена материала ФГИС ЦС для этой строки неприменима.",
          }
        : {
            label: "Материал · ФГИС ЦС и рынок",
            title: "Проверяются КСР, опубликованные цены и рынок",
            description:
              "Найденные суммы показаны ниже, но остаются исходными наблюдениями до проверки технического соответствия и нормализации.",
          };

  return (
    <article className="public-position-detail">
      <header className="public-position-detail__header">
        <div>
          <p>{row.line_key} · исходная позиция ВОР</p>
          <h3>{row.boq_item_name}</h3>
          <div className="public-position-detail__meta">
            <span>
              <b>{row.quantity === null ? "—" : formatDecimal(row.quantity)}</b>{" "}
              {row.boq_unit}
            </span>
            <span>
              Объём:{" "}
              {row.quantity_status === "UNVERIFIED" ? "не проверен" : "нет"}
            </span>
            <span>{row.item_id}</span>
          </div>
        </div>
        <div className="public-position-detail__status">
          <span>BLOCKED</span>
          <small>предварительные данные показаны</small>
        </div>
      </header>

      <section
        className="public-name-compare"
        aria-label="Сопоставление наименований"
      >
        <div>
          <span>Наименование в ВОР</span>
          <strong>{row.boq_item_name}</strong>
        </div>
        <div>
          <span>Подтверждённое сопоставление</span>
          <strong>Пока не установлено</strong>
          <small>
            Найденные ниже варианты открыты для проверки, но не объявлены
            эквивалентными.
          </small>
        </div>
      </section>

      <section className="public-tender-strip">
        <div>
          <span>Выигранные тендеры</span>
          <strong>0 сопоставимых построчных цен</strong>
        </div>
        <p>
          В открытых данных не найдена доказуемая цена именно этой позиции. Цена
          всего контракта не подставляется вместо цены строки.
        </p>
      </section>

      <section className="public-pricing-route" aria-label="Маршрут расчёта">
        <span>{route.label}</span>
        <div>
          <strong>{route.title}</strong>
          <p>{route.description}</p>
        </div>
      </section>

      <div className="public-source-grid">
        <SourceColumn
          title="ФГИС ЦС"
          description={
            costNature === "MATERIAL"
              ? "Официальные записи и история периодов"
              : "Для этой строки ресурсный каталог не является расчётным маршрутом"
          }
          candidates={row.fgis_cs_research_candidates}
          sourceKind="ФГИС ЦС"
          emptyTitle={
            costNature === "WORK"
              ? "Нужен расчёт по ГЭСН"
              : costNature === "LOGISTICS"
                ? "Нужна модель перевозки"
                : "Данных ФГИС ЦС нет"
          }
          emptyDescription={route.description}
        />
        <SourceColumn
          title="Открытый рынок"
          description="Сайты поставщиков и публичные прайс-листы"
          candidates={row.market_research_candidates}
          sourceKind="Рынок"
        />
      </div>

      <section className="public-system-decision">
        <div className="public-system-decision__verdict">
          <Icon name="warning" size={24} />
          <div>
            <span>Предварительный расчёт системы</span>
            <strong>
              {commercialLine === undefined
                ? "Нет расчётного значения"
                : `${formatRubles(commercialLine.preliminaryUnitPriceRubles)} / ${row.boq_unit}`}
            </strong>
            <p>
              {commercialLine === undefined
                ? "Для строки не найдено связанное коммерческое допущение."
                : `Количество ${formatDecimal(commercialLine.quantity)} ${row.boq_unit}; предварительная себестоимость строки ${formatKopecks(commercialLine.directCostKopecks)}.`}
            </p>
            <p>
              Значение показано для анализа маржи, но не является безопасной
              ценой заявки: источники и сопоставления ещё не подтверждены.
            </p>
          </div>
        </div>
        <details>
          <summary>Все причины остановки ({row.blockers.length})</summary>
          <ul>
            {row.blockers.map((blocker) => (
              <li key={blocker}>
                <span>{blockerLabel(blocker)}</span>
                <code>{blocker}</code>
              </li>
            ))}
          </ul>
        </details>
      </section>
    </article>
  );
}

export function PublicDemoPage({ config }: { config: RuntimeConfig }) {
  const snapshot = alabugaPublicSnapshot;
  const rows = snapshot.matrix.rows;
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<RowFilter>("ALL");
  const [quantityScenario, setQuantityScenario] =
    useState<QuantityScenario>("BOQ");
  const initialRow = rows.find(rowHasObservedAmount) ?? rows[0];
  const [selectedRowId, setSelectedRowId] = useState(initialRow?.row_id ?? "");
  const commercialCalculation = useMemo(
    () =>
      calculateCommercialScenario(
        alabugaCommercialLines,
        alabugaCommercialAssumptions,
        quantityScenario,
      ),
    [quantityScenario],
  );
  const commercialByLineKey = useMemo(
    () =>
      new Map(commercialCalculation.lines.map((line) => [line.lineKey, line])),
    [commercialCalculation.lines],
  );
  const amountRowCount = rows.filter(rowHasObservedAmount).length;
  const fgisPriceRowCount = rows.filter(rowHasFgisObservedAmount).length;
  const marketPriceRowCount = rows.filter(rowHasMarketObservedAmount).length;
  const sourceFreeRowCount = rows.filter(
    (row) => rowCandidates(row).length === 0,
  ).length;
  const rowOrdinalById = useMemo(
    () => new Map(rows.map((row, index) => [row.row_id, index + 1])),
    [rows],
  );

  const filteredRows = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase("ru-RU");
    return rows.filter((row) => {
      const candidates = rowCandidates(row);
      const matchesQuery =
        normalizedQuery.length === 0 ||
        [
          row.line_key,
          row.boq_item_name,
          row.boq_unit,
          ...candidates.map((candidate) => candidate.source_item_name),
          ...candidates.map((candidate) => candidate.source_display_name),
        ].some((value) =>
          value.toLocaleLowerCase("ru-RU").includes(normalizedQuery),
        );
      if (!matchesQuery) return false;
      switch (filter) {
        case "WITH_AMOUNT":
          return rowHasObservedAmount(row);
        case "FGIS":
          return rowHasFgisObservedAmount(row);
        case "MARKET":
          return rowHasMarketObservedAmount(row);
        case "WITHOUT_SOURCES":
          return candidates.length === 0;
        case "ALL":
          return true;
      }
    });
  }, [filter, query, rows]);

  const selectedRow =
    filteredRows.find((row) => row.row_id === selectedRowId) ??
    filteredRows[0] ??
    null;

  return (
    <main className="public-demo">
      <header className="public-demo__topbar">
        <a
          className="public-demo__brand"
          href="#overview"
          aria-label="СметаИИ — к обзору"
        >
          <span>СИ</span>
          <strong>СметаИИ</strong>
        </a>
        <nav aria-label="Разделы проекта">
          <a href="#overview">Сводка</a>
          <a href="#positions">Позиции ВОР</a>
          <a href="#method">Как работает</a>
          {config.showcase_operator_upload_enabled && (
            <a href="/import">Новый проект</a>
          )}
        </nav>
        <div className="public-demo__mode">
          <Icon name="trace" size={16} />
          <span>Рабочий срез</span>
        </div>
      </header>

      <section className="public-hero public-hero--workbench" id="overview">
        <div className="public-hero__copy">
          <p className="eyebrow">
            Реальные данные проекта ·{" "}
            {formatDateTime(snapshot.matrix.generated_at)}
          </p>
          <h1>
            Алабуга <span>4527946</span>
          </h1>
          <p className="public-hero__lead">
            Здесь показаны все {snapshot.summary.boq_rows} позиции ВОР: исходные
            наименования, объёмы, найденные варианты ФГИС ЦС и рынка, сырые
            суммы, ссылки и причины, по которым значение ещё нельзя принять в
            расчёт.
          </p>
          <div className="public-hero__actions">
            {config.showcase_operator_upload_enabled && (
              <a className="button button--public" href="/import">
                Загрузить свой проект
                <Icon name="arrow" />
              </a>
            )}
            <a className="button button--public" href="#positions">
              Открыть позиции
              <Icon name="arrow" />
            </a>
            <button
              className="public-download"
              type="button"
              onClick={() => downloadCsv(rows, quantityScenario)}
            >
              Скачать ВОР CSV
            </button>
          </div>
        </div>
        <aside className="public-hero__decision" aria-label="Статус расчёта">
          <div className="public-hero__decision-label">
            <span>Текущий результат</span>
            <Icon name="warning" size={20} />
          </div>
          <strong>УБЫТОК</strong>
          <p>
            {formatKopecks(commercialCalculation.operatingResultKopecks)} ·
            предварительный сценарий
          </p>
          <dl>
            <div>
              <dt>Цена тендера</dt>
              <dd>{formatKopecks(commercialCalculation.tenderGrossKopecks)}</dd>
            </div>
            <div>
              <dt>Полная себестоимость</dt>
              <dd>{formatKopecks(commercialCalculation.fullCostKopecks)}</dd>
            </div>
          </dl>
          <small>BLOCKED · не является решением о подаче заявки</small>
        </aside>
      </section>

      <section className="public-commercial" aria-label="Экономика участия">
        <header className="public-commercial__header">
          <div>
            <p className="eyebrow">Экономика участия</p>
            <h2>При текущей цене тендер убыточен</h2>
            <p>
              Показаны все расчётные данные, даже несмотря на блокировку.
              Источник — файл коллеги; ошибочные ссылки в формуле исправлены, но
              исходные цены и методика ещё не подтверждены.
            </p>
          </div>
          <div
            className="public-commercial__scenario"
            aria-label="Основание объёмов"
          >
            <span>Основание объёмов</span>
            <div>
              {(Object.keys(scenarioLabels) as QuantityScenario[]).map(
                (scenario) => (
                  <button
                    type="button"
                    key={scenario}
                    className={quantityScenario === scenario ? "is-active" : ""}
                    aria-pressed={quantityScenario === scenario}
                    onClick={() => setQuantityScenario(scenario)}
                  >
                    {scenarioLabels[scenario]}
                  </button>
                ),
              )}
            </div>
          </div>
        </header>

        <div className="public-commercial__metrics">
          <article>
            <span>Цена тендера, с НДС</span>
            <strong>
              {formatKopecks(commercialCalculation.tenderGrossKopecks)}
            </strong>
            <small>из книги · не подтверждена извне</small>
          </article>
          <article>
            <span>Прямые затраты</span>
            <strong>
              {formatKopecks(commercialCalculation.directCostKopecks)}
            </strong>
            <small>23 позиции выбранного сценария</small>
          </article>
          <article>
            <span>Полная себестоимость</span>
            <strong>
              {formatKopecks(commercialCalculation.fullCostKopecks)}
            </strong>
            <small>с накладными и резервом из книги</small>
          </article>
          <article className="is-loss">
            <span>Финансовый результат</span>
            <strong>
              {formatKopecks(commercialCalculation.operatingResultKopecks)}
            </strong>
            <small>
              маржа {formatPercent(commercialCalculation.marginBps)}
            </small>
          </article>
          <article className="is-required">
            <span>Требуемая цена, с НДС</span>
            <strong>
              {formatKopecks(commercialCalculation.requiredGrossKopecks)}
            </strong>
            <small>
              выше НМЦ на {formatKopecks(commercialCalculation.priceGapKopecks)}
            </small>
          </article>
        </div>

        <div className="public-commercial__basis">
          <strong>Предварительный сценарий · BLOCKED</strong>
          <span>
            Доступно на исполнение после НДС и удержаний:{" "}
            {formatKopecks(commercialCalculation.availableAfterTermsKopecks)}
          </span>
          <span>
            Допущения книги: НДС 22% · услуги заказчика/ГК 21% · накладные 8% ·
            резерв 7,5% · финансирование 1,5% · целевая маржа 8%
          </span>
          <span>
            Цена строки: 25% тендерный ориентир + 35% ФГИС/РИМ + 40% рынок,
            затем риск строки.
          </span>
          <span>
            Источник: {alabugaCommercialProvenance.workbook} · SHA256{" "}
            {alabugaCommercialProvenance.workbookSha256.slice(0, 12)}…
          </span>
        </div>
      </section>

      <section className="public-stats" aria-label="Сводка обработки">
        <article>
          <span>01 / Позиции ВОР</span>
          <strong>{snapshot.summary.boq_rows}</strong>
          <p>исходные названия, количества и единицы доступны полностью</p>
        </article>
        <article>
          <span>02 / ФГИС ЦС</span>
          <strong>{snapshot.summary.fgis_catalog_candidates}</strong>
          <p>
            вариантов КСР найдено;{" "}
            {snapshot.summary.fgis_published_observations} наблюдений имеют
            опубликованные суммы
          </p>
        </article>
        <article>
          <span>03 / Рынок</span>
          <strong>{snapshot.summary.market_candidates}</strong>
          <p>кандидата с сайтов поставщиков и публичных прайс-листов</p>
        </article>
        <article className="is-blocked">
          <span>04 / Итоговая цена</span>
          <strong>0</strong>
          <p>не скрыта — отсутствует до завершения обязательных проверок</p>
        </article>
      </section>

      <section className="public-fgis-coverage" aria-label="Охват ФГИС ЦС">
        <header>
          <p className="eyebrow">Что именно проверено в ФГИС ЦС</p>
          <h2>
            Не 12 карточек: полный журнал содержит{" "}
            {snapshot.summary.fgis_raw_responses} ответа ФГИС ЦС
          </h2>
          <p>
            Работы и логистика не ищутся в каталоге материалов. Для них нужны
            нормативный и логистический расчёты. ФГИС-маршрут выполнен по всем{" "}
            {snapshot.summary.material_rows} строкам, классифицированным как
            материалы.
          </p>
        </header>
        <div>
          <article>
            <span>Состав ВОР</span>
            <strong>{snapshot.summary.material_rows} материалов</strong>
            <p>
              {snapshot.summary.work_rows} работ ·{" "}
              {snapshot.summary.logistics_rows} строка логистики
            </p>
          </article>
          <article>
            <span>Поиск КСР</span>
            <strong>
              {snapshot.summary.fgis_catalog_candidates} вариантов
            </strong>
            <p>
              {snapshot.summary.fgis_selected_codes} кодов проверено по истории
            </p>
          </article>
          <article>
            <span>История цен</span>
            <strong>{snapshot.summary.fgis_queried_periods} кварталов</strong>
            <p>
              {snapshot.summary.fgis_raw_responses} сохранённых HTTP-ответов
            </p>
          </article>
          <article>
            <span>Опубликованные цены</span>
            <strong>
              {snapshot.summary.fgis_published_observations} наблюдений
            </strong>
            <p>
              по {snapshot.summary.fgis_codes_with_published_prices} кодам и{" "}
              {snapshot.summary.fgis_rows_with_published_prices} строкам ВОР
            </p>
          </article>
          <article>
            <span>Буквально совпало</span>
            <strong>
              {snapshot.summary.fgis_exact_literal_published_observations} из{" "}
              {snapshot.summary.fgis_published_observations}
            </strong>
            <p>
              остальные{" "}
              {snapshot.summary.fgis_alternative_published_observations} записей
              — близкие, но технически отличающиеся варианты
            </p>
          </article>
        </div>
      </section>

      <section className="public-workbench" id="positions">
        <header className="public-workbench__heading">
          <div>
            <p className="eyebrow">Расчётная ведомость объёмов работ</p>
            <h2>ВОР · себестоимость проекта</h2>
          </div>
          <p>
            Одна позиция — одна строка. Нажмите на наименование, чтобы открыть
            сопоставления ФГИС ЦС, рыночные источники и причины блокировки.
          </p>
        </header>

        <div className="public-vor-warning" role="status">
          <Icon name="warning" size={20} />
          <div>
            <strong>Формула книги исправлена, данные не утверждены</strong>
            <p>{alabugaCommercialProvenance.correction}</p>
          </div>
          <span>Источник: {alabugaCommercialProvenance.workbookDate}</span>
        </div>

        <div className="public-workbench__toolbar">
          <label className="public-search">
            <Icon name="search" size={18} />
            <span className="sr-only">Поиск по позиции или источнику</span>
            <input
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Поиск: кабель, песок, труба…"
            />
          </label>
          <div className="public-filters" aria-label="Фильтр позиций">
            {(
              [
                ["ALL", `Все ${rows.length}`],
                ["WITH_AMOUNT", `С суммами ${amountRowCount}`],
                ["FGIS", `Цена ФГИС ${fgisPriceRowCount}`],
                ["MARKET", `Цена рынка ${marketPriceRowCount}`],
                ["WITHOUT_SOURCES", `Без источников ${sourceFreeRowCount}`],
              ] as const
            ).map(([value, label]) => (
              <button
                type="button"
                key={value}
                className={filter === value ? "is-active" : ""}
                aria-pressed={filter === value}
                onClick={() => setFilter(value)}
              >
                {label}
              </button>
            ))}
          </div>
          <span className="public-workbench__count">
            {filteredRows.length} из {rows.length}
          </span>
        </div>

        <div className="public-vor-scroll">
          <table className="public-vor-table">
            <colgroup>
              <col className="public-vor-col--number" />
              <col className="public-vor-col--code" />
              <col className="public-vor-col--name" />
              <col className="public-vor-col--unit" />
              <col className="public-vor-col--quantity" />
              <col className="public-vor-col--source" />
              <col className="public-vor-col--source" />
              <col className="public-vor-col--source" />
              <col className="public-vor-col--system" />
              <col className="public-vor-col--total" />
              <col className="public-vor-col--control" />
            </colgroup>
            <thead>
              <tr className="public-vor-table__groups">
                <th rowSpan={2}>№</th>
                <th colSpan={2}>Позиция ВОР</th>
                <th colSpan={2}>Объём</th>
                <th colSpan={3}>Исходные цены, руб./ед. без НДС</th>
                <th colSpan={2}>Предварительный расчёт</th>
                <th rowSpan={2}>Контроль</th>
              </tr>
              <tr>
                <th>Код</th>
                <th>Наименование</th>
                <th>Ед.</th>
                <th>{scenarioLabels[quantityScenario]}</th>
                <th>Тендер / смета</th>
                <th>ФГИС / РИМ</th>
                <th>Рынок</th>
                <th>Цена системы, руб./ед.</th>
                <th>Себестоимость строки</th>
              </tr>
            </thead>
            <tbody>
              {filteredRows.length === 0 ? (
                <tr>
                  <td className="public-vor-table__empty" colSpan={11}>
                    Ничего не найдено. Измените запрос или фильтр.
                  </td>
                </tr>
              ) : (
                filteredRows.map((row, index) => {
                  const commercial = commercialByLineKey.get(row.line_key);
                  const candidateCount = rowCandidates(row).length;
                  const isSelected = selectedRow?.row_id === row.row_id;
                  const costNature = row.research_route?.cost_nature;
                  return (
                    <tr
                      key={row.row_id}
                      className={isSelected ? "is-selected" : ""}
                    >
                      <td>{rowOrdinalById.get(row.row_id) ?? index + 1}</td>
                      <td>
                        <code>{row.line_key}</code>
                      </td>
                      <th scope="row">
                        <button
                          type="button"
                          aria-pressed={isSelected}
                          onClick={() => setSelectedRowId(row.row_id)}
                        >
                          <strong>{row.boq_item_name}</strong>
                          <span>
                            {costNature === "WORK"
                              ? "Работа · требуется ГЭСН"
                              : costNature === "LOGISTICS"
                                ? "Логистика · отдельный расчёт"
                                : "Материал · ФГИС ЦС и рынок"}
                          </span>
                        </button>
                      </th>
                      <td>{row.boq_unit}</td>
                      <td className="is-number">
                        {commercial === undefined
                          ? "—"
                          : formatDecimal(commercial.quantity)}
                      </td>
                      <td className="is-number">
                        {commercial === undefined
                          ? "—"
                          : formatRubles(commercial.tenderUnitPrice)}
                        <small>из книги</small>
                      </td>
                      <td className="is-number">
                        {commercial === undefined
                          ? "—"
                          : formatRubles(commercial.fgisUnitPrice)}
                        <small>
                          {row.fgis_cs_research_candidates.length} канд. КСР
                        </small>
                      </td>
                      <td className="is-number">
                        {commercial === undefined
                          ? "—"
                          : formatRubles(commercial.marketUnitPrice)}
                        <small>
                          {row.market_research_candidates.length} ист.
                        </small>
                      </td>
                      <td className="is-number is-system">
                        {commercial === undefined
                          ? "—"
                          : formatRubles(commercial.preliminaryUnitPriceRubles)}
                        <small>не выпущена</small>
                      </td>
                      <td className="is-number is-total">
                        {commercial === undefined
                          ? "—"
                          : formatKopecks(commercial.directCostKopecks)}
                      </td>
                      <td className="public-vor-table__status">
                        <strong>BLOCKED</strong>
                        <span>{candidateCount} ист.</span>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
            <tfoot>
              <tr>
                <th colSpan={9}>
                  Итого прямые затраты · {scenarioLabels[quantityScenario]}
                </th>
                <td className="is-number">
                  {formatKopecks(commercialCalculation.directCostKopecks)}
                </td>
                <td>BLOCKED</td>
              </tr>
            </tfoot>
          </table>
        </div>

        <div className="public-vor-detail">
          <header>
            <span>Проверка выбранной строки</span>
            <small>наименование, источники и причины остановки</small>
          </header>
          <div>
            {selectedRow === null ? (
              <div className="public-position-list__empty">
                <strong>Позиция не выбрана</strong>
              </div>
            ) : (
              <PositionDetail
                key={`${selectedRow.row_id}:${quantityScenario}`}
                row={selectedRow}
                commercialLine={commercialByLineKey.get(selectedRow.line_key)}
              />
            )}
          </div>
        </div>
      </section>

      <section className="public-method" id="method">
        <header>
          <p className="eyebrow">Как система принимает решение</p>
          <h2>От строки ВОР до итоговой цены</h2>
          <p>
            Автоматика выполняет поиск и проверку. Эксперт подключается в конце
            и принимает готовый результат либо возвращает конкретные позиции на
            доработку.
          </p>
        </header>
        <div className="public-method__flow">
          {flow.map(([number, title, detail], index) => (
            <article key={number}>
              <span>{number}</span>
              <div>
                <strong>{title}</strong>
                <p>{detail}</p>
              </div>
              {index < flow.length - 1 && <Icon name="arrow" size={17} />}
            </article>
          ))}
        </div>
      </section>

      <footer className="public-demo__footer">
        <div>
          <strong>СметаИИ</strong>
          <span>Проект Алабуга 4527946 · реальные диагностические данные</span>
        </div>
        <p>
          Сырые суммы показаны полностью, но не являются сметной ценой до
          нормализации и выпуска расчёта. Контрольный хеш матрицы:{" "}
          {snapshot.matrix_content_sha256}.
        </p>
        <code>
          v{config.application_version} · {config.environment}
        </code>
      </footer>
    </main>
  );
}
