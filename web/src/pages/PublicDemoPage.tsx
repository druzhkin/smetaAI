import { useEffect, useMemo, useRef, useState } from "react";

import { Icon } from "../components/Icon";
import { formatDateTime, formatDecimal, formatMoney } from "../format";
import { alabugaPublicSnapshot, safeExternalHttpsUrl } from "../publicSnapshot";
import type {
  BoqDiagnosticObservedAmount,
  BoqDiagnosticSourceCandidate,
  BoqPriceMatrixRow,
  RuntimeConfig,
} from "../types";

type RowFilter = "ALL" | "WITH_AMOUNT" | "FGIS" | "MARKET" | "WITHOUT_SOURCES";

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
  ["04", "Нормализация", "Единица, НДС, доставка и период"],
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

function sourceHost(value: string): string {
  const safeUrl = safeExternalHttpsUrl(value);
  return safeUrl === null ? "некорректная ссылка" : new URL(safeUrl).hostname;
}

function csvCell(value: string): string {
  return `"${value.replaceAll('"', '""')}"`;
}

export function buildPublicMatrixCsv(rows: BoqPriceMatrixRow[]): string {
  const header = [
    "Позиция ВОР",
    "Наименование ВОР",
    "Количество",
    "Единица",
    "Наименования ФГИС ЦС",
    "Сырые значения ФГИС ЦС",
    "Ссылки ФГИС ЦС",
    "Наименования рынка",
    "Сырые рыночные значения",
    "Ссылки рынка",
    "Цена системы",
    "Статус",
    "Причины остановки",
  ];
  const lines = rows.map((row) => {
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
      row.line_key,
      row.boq_item_name,
      row.quantity ?? "",
      row.boq_unit,
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
      "не сформирована",
      row.row_status,
      row.blockers.map(blockerLabel).join(" | "),
    ].map(csvCell);
  });
  return [
    header.map(csvCell).join(";"),
    ...lines.map((line) => line.join(";")),
  ].join("\r\n");
}

function downloadCsv(rows: BoqPriceMatrixRow[]) {
  const blob = new Blob(["\uFEFF", buildPublicMatrixCsv(rows)], {
    type: "text/csv;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "Алабуга_4527946_проверяемая_матрица.csv";
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
  return (
    <article className="public-candidate">
      <header className="public-candidate__header">
        <div>
          <span>{sourceKind}</span>
          <strong>{candidate.source_item_name}</strong>
          <small>{candidate.source_display_name}</small>
        </div>
        <em>Кандидат</em>
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
          Наименование найдено, но опубликованная сумма не получена.
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

      {safeUrl === null ? (
        <span className="public-candidate__link is-invalid">
          Ссылка источника недоступна
        </span>
      ) : (
        <a
          className="public-candidate__link"
          href={safeUrl}
          target="_blank"
          rel="noreferrer noopener"
        >
          Открыть первоисточник
          <Icon name="arrow" size={15} />
        </a>
      )}
    </article>
  );
}

function SourceColumn({
  title,
  description,
  candidates,
  sourceKind,
}: {
  title: string;
  description: string;
  candidates: BoqDiagnosticSourceCandidate[];
  sourceKind: "ФГИС ЦС" | "Рынок";
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
          <strong>Данных нет</strong>
          <span>Сопоставимый проверяемый источник пока не найден.</span>
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

function PositionDetail({ row }: { row: BoqPriceMatrixRow }) {
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
          <small>итоговая цена отсутствует</small>
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

      <div className="public-source-grid">
        <SourceColumn
          title="ФГИС ЦС"
          description="Официальные записи и история периодов"
          candidates={row.fgis_cs_research_candidates}
          sourceKind="ФГИС ЦС"
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
            <span>Предложение системы</span>
            <strong>Итоговая цена не сформирована</strong>
            <p>
              Это не скрытая сумма: расчёт остановлен до доказанного
              сопоставления и приведения исходных значений к одной коммерческой
              базе.
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
  const initialRow = rows.find(rowHasObservedAmount) ?? rows[0];
  const [selectedRowId, setSelectedRowId] = useState(initialRow?.row_id ?? "");
  const positionListRef = useRef<HTMLElement>(null);

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
          return row.fgis_cs_research_candidates.length > 0;
        case "MARKET":
          return row.market_research_candidates.length > 0;
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

  useEffect(() => {
    const list = positionListRef.current;
    const selected = list?.querySelector<HTMLElement>(
      ".public-position-item.is-selected",
    );
    if (list === null || selected === null || selected === undefined) return;
    const target =
      selected.offsetTop -
      Math.max(0, (list.clientHeight - selected.offsetHeight) / 2);
    list.scrollTop = Math.max(0, target);
  }, [selectedRow?.row_id]);

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
            <a className="button button--public" href="#positions">
              Открыть позиции
              <Icon name="arrow" />
            </a>
            <button
              className="public-download"
              type="button"
              onClick={() => downloadCsv(rows)}
            >
              Скачать CSV
            </button>
          </div>
        </div>
        <aside className="public-hero__decision" aria-label="Статус расчёта">
          <div className="public-hero__decision-label">
            <span>Текущий результат</span>
            <Icon name="warning" size={20} />
          </div>
          <strong>BLOCKED</strong>
          <p>Итоговая сметная оценка пока не сформирована</p>
          <dl>
            <div>
              <dt>Сырых сумм найдено</dt>
              <dd>{snapshot.summary.observed_amounts}</dd>
            </div>
            <div>
              <dt>Цен выпущено</dt>
              <dd>0</dd>
            </div>
          </dl>
        </aside>
      </section>

      <section className="public-stats" aria-label="Сводка обработки">
        <article>
          <span>01 / Позиции ВОР</span>
          <strong>{snapshot.summary.boq_rows}</strong>
          <p>исходные названия, количества и единицы доступны полностью</p>
        </article>
        <article>
          <span>02 / ФГИС ЦС</span>
          <strong>{snapshot.summary.fgis_candidates}</strong>
          <p>найденных записей с наименованием, периодом и первоисточником</p>
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

      <section className="public-workbench" id="positions">
        <header className="public-workbench__heading">
          <div>
            <p className="eyebrow">Проверяемая ценовая матрица</p>
            <h2>Все позиции и источники</h2>
          </div>
          <p>
            Выберите строку слева. Справа откроются исходное наименование,
            найденные совпадения, суммы, технические различия и прямые ссылки.
          </p>
        </header>

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
                ["ALL", "Все"],
                ["WITH_AMOUNT", "Есть сумма"],
                ["FGIS", "ФГИС ЦС"],
                ["MARKET", "Рынок"],
                ["WITHOUT_SOURCES", "Без источников"],
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

        <div className="public-workbench__layout">
          <aside
            ref={positionListRef}
            className="public-position-list"
            aria-label="Позиции ВОР"
          >
            {filteredRows.length === 0 ? (
              <div className="public-position-list__empty">
                <Icon name="search" size={24} />
                <strong>Ничего не найдено</strong>
                <span>Измените запрос или фильтр.</span>
              </div>
            ) : (
              filteredRows.map((row, index) => {
                const candidateCount = rowCandidates(row).length;
                const isSelected = selectedRow?.row_id === row.row_id;
                return (
                  <button
                    type="button"
                    key={row.row_id}
                    className={`public-position-item${isSelected ? " is-selected" : ""}`}
                    aria-pressed={isSelected}
                    onClick={() => setSelectedRowId(row.row_id)}
                  >
                    <span className="public-position-item__index">
                      {String(index + 1).padStart(2, "0")}
                    </span>
                    <span className="public-position-item__body">
                      <code>{row.line_key}</code>
                      <strong>{row.boq_item_name}</strong>
                      <small>
                        {row.quantity === null
                          ? "—"
                          : formatDecimal(row.quantity)}{" "}
                        {row.boq_unit}
                      </small>
                      <span className="public-position-item__sources">
                        <em
                          className={
                            row.fgis_cs_research_candidates.length > 0
                              ? "has-data"
                              : ""
                          }
                        >
                          ФГИС {row.fgis_cs_research_candidates.length}
                        </em>
                        <em
                          className={
                            row.market_research_candidates.length > 0
                              ? "has-data"
                              : ""
                          }
                        >
                          Рынок {row.market_research_candidates.length}
                        </em>
                        {rowHasObservedAmount(row) && (
                          <em className="has-amount">Есть сумма</em>
                        )}
                      </span>
                    </span>
                    <span className="public-position-item__count">
                      {candidateCount}
                    </span>
                  </button>
                );
              })
            )}
          </aside>
          <div className="public-workbench__detail">
            {selectedRow === null ? (
              <div className="public-position-list__empty">
                <strong>Позиция не выбрана</strong>
              </div>
            ) : (
              <PositionDetail key={selectedRow.row_id} row={selectedRow} />
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
