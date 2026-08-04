import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import { getBoqPriceMatrix, getProject, type RequestContext } from "../api";
import { useAuth } from "../auth";
import { ErrorBlock, LoadingBlock } from "../components/Feedback";
import { Icon } from "../components/Icon";
import { StatusPill } from "../components/StatusPill";
import { formatDateTime, formatDecimal, formatMoney } from "../format";
import { Link } from "../navigation";
import type {
  BoqDiagnosticObservedAmount,
  BoqDiagnosticResearchRoute,
  BoqDiagnosticSourceCandidate,
  BoqPriceMatrixRow,
  BoqSourcePrice,
  RuntimeConfig,
} from "../types";

const blockerLabels: Record<string, string> = {
  BOQ_LINE_NOT_VERIFIED: "строка ВОР не подтверждена",
  BOQ_COST_COMPONENT_INVALID: "план затрат строки повреждён",
  BOQ_ITEM_ID_MISSING: "нет идентификатора ценовой позиции",
  QUANTITY_MISSING: "объём отсутствует",
  QUANTITY_NOT_VERIFIED: "объём не подтверждён",
  QUANTITY_UNIT_MISMATCH: "единица объёма не совпадает со строкой",
  NOMENCLATURE_MATCH_MISSING: "нет сопоставления номенклатуры",
  NOMENCLATURE_MATCH_NOT_VERIFIED: "сопоставление не подтверждено",
  NOMENCLATURE_MATCH_NOT_ACCEPTABLE: "аналог технически неприемлем",
  WON_TENDER_PRICE_MISSING: "нет цены выигранного тендера",
  FGIS_CS_PRICE_MISSING: "нет подтверждённой цены ФГИС ЦС",
  MARKET_PRICE_MISSING: "нет независимой рыночной цены",
  PRICE_POLICY_INTEGRITY_FAILED: "ценовая политика не прошла проверку",
  PRICE_DECISION_MISSING: "система ещё не сформировала решение",
  PRICE_DECISION_INTEGRITY_FAILED: "решение не прошло контроль целостности",
  HIDDEN_WORKBOOK_CONTENT: "в книге есть скрытое содержимое",
  INTAKE_CORRUPT_OR_PROTECTED_EXCEL:
    "исходный Excel не прошёл строгую проверку целостности",
  INTAKE_MANIFEST_BLOCKED: "приём исходного комплекта заблокирован",
  CONTROLLED_IMPORT_WORKFLOW_REQUIRED:
    "нужен управляемый импорт в текущую версию проекта",
  INDEPENDENT_ROW_REVIEW_REQUIRED:
    "нужна независимая проверка извлечённых строк",
  MISSING_STABLE_POSITION_ID: "нет устойчивого номера позиции",
  POSITION_ID_FORMULA_NOT_ALLOWED: "номер позиции задан запрещённой формулой",
  CALCULATION_SNAPSHOT_MISSING: "нет зафиксированного расчёта",
  BID_RELEASE_NOT_APPROVED: "проект не допущен к подаче заявки",
  APPROVED_NORMATIVE_ENGINE_REQUIRED:
    "не подключён утверждённый нормативный движок",
  APPROVED_LOGISTICS_METHOD_REQUIRED:
    "не утверждена методика расчёта логистики",
  APPROVED_FGIS_MAPPING_REQUIRED:
    "сопоставление с кодом КСР не утверждено",
  PROJECT_PRICE_PERIOD_NOT_SELECTED: "не выбран расчётный период ФГИС ЦС",
  PRICE_NORMALIZATION_REQUIRED: "исходная сумма не нормализована",
  TECHNICAL_EQUIVALENCE_NOT_ESTABLISHED:
    "техническая эквивалентность не доказана",
  COMMERCIAL_BASIS_INCOMPLETE: "коммерческая база неполна",
  DIAGNOSTIC_SOURCE_CANDIDATE_NOT_PRICE_EVIDENCE:
    "кандидат исследования не является ценовым основанием",
};

function blockerLabel(value: string): string {
  const [code, identifier] = value.split(":", 2);
  const label =
    blockerLabels[code ?? ""] ??
    (code?.startsWith("NOMENCLATURE_")
      ? "нарушена целостность сопоставления"
      : code?.startsWith("PRICE_SOURCE_INTEGRITY_FAILED")
        ? "источник цены не прошёл контроль целостности"
        : code?.startsWith("PRICE_DECISION_NOT_VERIFIED")
          ? "ценовое решение не подтверждено"
          : value.replaceAll("_", " ").toLocaleLowerCase("ru-RU"));
  return identifier === undefined ? label : `${label}: ${identifier}`;
}

function rationaleLabel(value: string): string {
  if (
    value ===
    "The proposed price is withheld because mandatory evidence or approval gates are incomplete."
  ) {
    return "Предлагаемая цена скрыта: обязательные источники или согласования не завершены.";
  }
  if (
    value ===
    "FGIS CS, won-tender and independent market source names are exposed for operator comparison."
  ) {
    return "Наименования из ФГИС ЦС, выигранных тендеров и рынка показаны для ручной сверки.";
  }
  if (
    value === "This row status does not replace the project bid-release gates."
  ) {
    return "Статус строки не заменяет итоговый контроль допуска цены к заявке.";
  }
  if (value.startsWith("Approved selection method:")) {
    return `Утверждённый метод выбора: ${value
      .slice("Approved selection method:".length)
      .trim()}`;
  }
  const sourceCount = value.match(
    /^The verified decision uses (\d+) normalized source prices\.$/,
  );
  if (sourceCount !== null) {
    return `Решение использует ${sourceCount[1]} нормализованных ценовых источника(ов).`;
  }
  if (value.startsWith("Blocker: ")) {
    return `Блокировка: ${blockerLabel(value.slice("Blocker: ".length))}.`;
  }
  return value;
}

export function SourcePriceCard({
  price,
  boqName,
}: {
  price: BoqSourcePrice;
  boqName: string;
}) {
  return (
    <article className="source-price-card">
      <div className="source-price-card__heading">
        <div>
          <strong>{price.source_reference.display_name}</strong>
          <span>{price.source_reference.source_record_id}</span>
        </div>
        <StatusPill
          value={
            price.normalized_prices.length > 0 ? "NORMALIZED" : "IN_REVIEW"
          }
          compact
        />
      </div>

      <div className="source-price-card__amount">
        {price.normalized_prices.length > 0 ? (
          price.normalized_prices.map((normalized) => (
            <span key={normalized.normalized_price_id}>
              {formatMoney(normalized.amount_per_unit, normalized.currency)}
              <small>/ {normalized.unit}</small>
            </span>
          ))
        ) : (
          <span>
            {formatMoney(price.raw_amount, price.raw_currency)}
            <small>/ {price.raw_unit} · исходная база</small>
          </span>
        )}
      </div>

      <dl className="source-name-check">
        <div>
          <dt>В ВОР</dt>
          <dd>{boqName}</dd>
        </div>
        <div>
          <dt>У источника</dt>
          <dd>{price.source_reference.source_item_name}</dd>
        </div>
      </dl>

      <details className="source-price-card__details">
        <summary>Доказательства и атрибуты</summary>
        <dl>
          <div>
            <dt>Дата цены</dt>
            <dd>{price.quote_date}</dd>
          </div>
          <div>
            <dt>Действует до</dt>
            <dd>{price.valid_until ?? "не подтверждено"}</dd>
          </div>
          <div>
            <dt>Доступность</dt>
            <dd>
              {price.available === true
                ? "подтверждена"
                : price.available === false
                  ? "нет"
                  : "не подтверждена"}
            </dd>
          </div>
          <div>
            <dt>Наблюдение</dt>
            <dd>{price.source_observation_id}</dd>
          </div>
          <div>
            <dt>Локатор</dt>
            <dd>{price.source_locator}</dd>
          </div>
        </dl>
        <div className="attribute-chip-list">
          {Object.entries(price.technical_attributes).map(([key, value]) => (
            <code key={key}>
              {key}: {value}
            </code>
          ))}
        </div>
      </details>

      {price.source_reference.source_uri !== null ? (
        <a
          className="source-price-card__link"
          href={price.source_reference.source_uri}
          target="_blank"
          rel="noreferrer noopener"
        >
          Открыть первоисточник
          <Icon name="arrow" size={14} />
        </a>
      ) : (
        <span className="source-price-card__link source-price-card__link--muted">
          Коммерческое предложение хранится в комплекте документов
        </span>
      )}
    </article>
  );
}

const amountKindLabels: Record<
  BoqDiagnosticObservedAmount["amount_kind"],
  string
> = {
  FGIS_AGGREGATED: "Агрегированная цена",
  FGIS_ESTIMATED: "Сметная цена",
  FGIS_DISTANCE: "Перевозка в составе записи",
  MARKET_OFFER: "Цена на странице",
};

const routeLabels: Record<
  BoqDiagnosticResearchRoute["pricing_route"],
  string
> = {
  NORMATIVE_ENGINE: "Нормативный расчёт работ",
  FGIS_AND_MARKET: "ФГИС ЦС + рыночные источники",
  LOGISTICS_MODEL: "Расчёт логистики",
};

export function ResearchRouteCard({
  route,
}: {
  route: BoqDiagnosticResearchRoute;
}) {
  return (
    <aside className="research-route-card">
      <div>
        <span>Маршрут исследования</span>
        <StatusPill value={route.status} compact />
      </div>
      <strong>{routeLabels[route.pricing_route]}</strong>
      <small>
        Кандидат типа: {route.cost_nature} · профиль {route.profile_version_id}
      </small>
      <ul>
        {route.rationale.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
    </aside>
  );
}

export function DiagnosticSourceCandidateCard({
  candidate,
  boqName,
}: {
  candidate: BoqDiagnosticSourceCandidate;
  boqName: string;
}) {
  return (
    <article className="source-price-card source-price-card--diagnostic">
      <div className="source-price-card__heading">
        <div>
          <strong>{candidate.source_display_name}</strong>
          <span>{candidate.source_record_id}</span>
        </div>
        <span className="research-candidate-badge">Кандидат</span>
      </div>

      {candidate.observed_amounts.length > 0 ? (
        <div className="diagnostic-amount-list">
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
                  ? "единица не указана источником"
                  : `/ ${amount.unit}`}
                {amount.currency === null && " · валюта не указана источником"}
              </small>
            </div>
          ))}
        </div>
      ) : (
        <div className="diagnostic-no-amount">
          Найдено наименование, но опубликованная сумма не получена.
        </div>
      )}

      <dl className="source-name-check">
        <div>
          <dt>В ВОР</dt>
          <dd>{boqName}</dd>
        </div>
        <div>
          <dt>У источника</dt>
          <dd>{candidate.source_item_name}</dd>
        </div>
      </dl>

      <details className="source-price-card__details">
        <summary>Почему это ещё не цена расчёта</summary>
        <dl>
          <div>
            <dt>Период</dt>
            <dd>{candidate.period_name ?? "не относится к периоду"}</dd>
          </div>
          <div>
            <dt>Получено</dt>
            <dd>{formatDateTime(candidate.observed_at)}</dd>
          </div>
          <div>
            <dt>Метод буквальной проверки</dt>
            <dd>{candidate.comparison_method}</dd>
          </div>
          <div>
            <dt>SHA-256 ответа</dt>
            <dd className="hash-value">{candidate.evidence_sha256}</dd>
          </div>
        </dl>
        {candidate.boq_only_literals.length > 0 && (
          <div className="literal-difference">
            <strong>Есть только в ВОР</strong>
            <p>{candidate.boq_only_literals.join(" · ")}</p>
          </div>
        )}
        {candidate.source_only_literals.length > 0 && (
          <div className="literal-difference">
            <strong>Есть только у источника</strong>
            <p>{candidate.source_only_literals.join(" · ")}</p>
          </div>
        )}
        <div className="attribute-chip-list">
          {Object.entries(candidate.attributes).map(([key, value]) => (
            <code key={key}>
              {key}: {value}
            </code>
          ))}
        </div>
        <ul className="diagnostic-blocker-list">
          {candidate.blockers.map((blocker) => (
            <li key={blocker}>{blockerLabel(blocker)}</li>
          ))}
        </ul>
      </details>

      <a
        className="source-price-card__link"
        href={candidate.source_uri}
        target="_blank"
        rel="noreferrer noopener"
      >
        Открыть первоисточник
        <Icon name="arrow" size={14} />
      </a>
    </article>
  );
}

export function DiagnosticCandidateStack({
  candidates,
  boqName,
}: {
  candidates: BoqDiagnosticSourceCandidate[];
  boqName: string;
}) {
  const visible = candidates.slice(0, 3);
  const remaining = candidates.slice(3);
  return (
    <section className="diagnostic-candidate-stack">
      <header>
        <strong>Собрано кандидатов: {candidates.length}</strong>
        <span>Сырые данные · не нормализованы</span>
      </header>
      {visible.map((candidate) => (
        <DiagnosticSourceCandidateCard
          key={candidate.research_id}
          candidate={candidate}
          boqName={boqName}
        />
      ))}
      {remaining.length > 0 && (
        <details className="diagnostic-candidate-more">
          <summary>Показать ещё {remaining.length}</summary>
          <div>
            {remaining.map((candidate) => (
              <DiagnosticSourceCandidateCard
                key={candidate.research_id}
                candidate={candidate}
                boqName={boqName}
              />
            ))}
          </div>
        </details>
      )}
    </section>
  );
}

export function SourcePriceCell({
  prices,
  researchCandidates = [],
  boqName,
  emptyLabel,
}: {
  prices: BoqSourcePrice[];
  researchCandidates?: BoqDiagnosticSourceCandidate[];
  boqName: string;
  emptyLabel: string;
}) {
  if (prices.length === 0 && researchCandidates.length === 0) {
    return (
      <div className="matrix-empty-source">
        <StatusPill value="BLOCKED" compact />
        <span>{emptyLabel}</span>
      </div>
    );
  }
  return (
    <div className="source-price-stack">
      {prices.map((price) => (
        <SourcePriceCard key={price.quote_id} price={price} boqName={boqName} />
      ))}
      {researchCandidates.length > 0 && (
        <DiagnosticCandidateStack
          candidates={researchCandidates}
          boqName={boqName}
        />
      )}
    </div>
  );
}

export function MatchCell({ row }: { row: BoqPriceMatrixRow }) {
  const match = row.name_match;
  if (match === null) {
    return (
      <div className="matrix-empty-source">
        <StatusPill value="BLOCKED" compact />
        <span>Сопоставление отсутствует.</span>
      </div>
    );
  }
  return (
    <div className="matrix-match">
      <div className="matrix-match__heading">
        <StatusPill value={match.status} compact />
        <code>{match.match_class}</code>
      </div>
      <dl className="source-name-check">
        <div>
          <dt>ВОР / ТЗ</dt>
          <dd>{row.boq_item_name}</dd>
        </div>
        <div>
          <dt>Каталог</dt>
          <dd>{match.canonical_item_id ?? "не выбран"}</dd>
        </div>
      </dl>
      <details>
        <summary>Матрица критических атрибутов</summary>
        <div className="match-attribute-grid">
          {Array.from(
            new Set([
              ...Object.keys(match.source_attributes),
              ...Object.keys(match.canonical_attributes),
            ]),
          )
            .sort()
            .map((key) => (
              <div
                key={key}
                className={
                  match.mismatched_attributes.includes(key) ||
                  match.missing_attributes.includes(key)
                    ? "is-mismatch"
                    : ""
                }
              >
                <strong>{key}</strong>
                <span>{match.source_attributes[key] ?? "—"}</span>
                <span>{match.canonical_attributes[key] ?? "—"}</span>
              </div>
            ))}
        </div>
      </details>
      <small>
        {match.assessment_method ?? "метод не указан"} ·{" "}
        {match.catalog_version_id}
      </small>
    </div>
  );
}

export function ProposedPriceCell({
  row,
  projectId,
}: {
  row: BoqPriceMatrixRow;
  projectId: string;
}) {
  const proposed = row.proposed_price;
  const explanations = proposed.rationale.filter(
    (reason) => !reason.startsWith("Blocker: "),
  );
  return (
    <div className="matrix-proposal">
      <div className="matrix-proposal__heading">
        <StatusPill value={proposed.status} compact />
        <span>
          {proposed.workflow_status === "DIAGNOSTIC_ONLY"
            ? "Только анализ"
            : proposed.workflow_status}
        </span>
      </div>
      {proposed.amount_per_unit !== null &&
      proposed.currency !== null &&
      proposed.unit !== null ? (
        <strong className="matrix-proposal__amount">
          {formatMoney(proposed.amount_per_unit, proposed.currency)}
          <small>/ {proposed.unit}</small>
        </strong>
      ) : (
        <strong className="matrix-proposal__withheld">Цена скрыта</strong>
      )}
      <ul>
        {explanations.map((reason) => (
          <li key={reason}>{rationaleLabel(reason)}</li>
        ))}
      </ul>
      {row.blockers.length > 0 && (
        <p className="matrix-proposal__blocker-summary">
          {row.blockers.length} причин блокировки. Полный список раскрывается в
          позиции ВОР.
        </p>
      )}
      {proposed.workflow_status === "DIAGNOSTIC_ONLY" ? (
        <span className="button button--secondary" aria-disabled="true">
          Расчёт ещё не создан
        </span>
      ) : (
        <Link
          className="button button--secondary"
          to={`/projects/${encodeURIComponent(projectId)}/pricing/items/${encodeURIComponent(row.item_id)}`}
        >
          Открыть расчёт
          <Icon name="arrow" size={14} />
        </Link>
      )}
    </div>
  );
}

export function BoqPriceMatrixPage({
  config,
  projectId,
}: {
  config: RuntimeConfig;
  projectId: string;
}) {
  const auth = useAuth();
  const context = useMemo<RequestContext>(
    () => ({
      apiBasePath: config.api_base_path,
      authorizationHeaders: auth.authorizationHeaders,
    }),
    [auth.authorizationHeaders, config.api_base_path],
  );
  const projectQuery = useQuery({
    queryKey: ["project", projectId],
    queryFn: ({ signal }) => getProject(context, projectId, signal),
  });
  const matrixQuery = useQuery({
    queryKey: ["boq-price-matrix", projectId],
    queryFn: ({ signal }) => getBoqPriceMatrix(context, projectId, signal),
  });

  if (projectQuery.isPending || matrixQuery.isPending) {
    return (
      <div className="page">
        <LoadingBlock label="Сбор проверяемой ценовой матрицы ВОР" />
      </div>
    );
  }
  if (projectQuery.isError || matrixQuery.isError) {
    const failed = projectQuery.isError ? projectQuery : matrixQuery;
    return (
      <div className="page">
        <ErrorBlock
          error={failed.error}
          onRetry={() => {
            void projectQuery.refetch();
            void matrixQuery.refetch();
          }}
        />
      </div>
    );
  }

  const project = projectQuery.data;
  const matrix = matrixQuery.data;
  const verified = matrix.rows.length - matrix.blocked_row_count;
  const researched = matrix.rows.filter(
    (row) => row.research_route !== null,
  ).length;
  const researchCandidateCount = matrix.rows.reduce(
    (total, row) =>
      total +
      row.won_tender_research_candidates.length +
      row.fgis_cs_research_candidates.length +
      row.market_research_candidates.length,
    0,
  );
  const diagnosticMode = matrix.rows.some(
    (row) => row.proposed_price.workflow_status === "DIAGNOSTIC_ONLY",
  );

  return (
    <div className="page boq-price-matrix-page">
      <nav className="breadcrumbs" aria-label="Навигационная цепочка">
        <Link to="/">Проекты</Link>
        <span>/</span>
        <Link to={`/projects/${encodeURIComponent(projectId)}`}>
          {project.code}
        </Link>
        <span>/</span>
        {diagnosticMode ? (
          <span>Диагностический импорт</span>
        ) : (
          <Link to={`/projects/${encodeURIComponent(projectId)}/PRICING`}>
            Цены
          </Link>
        )}
        <span>/</span>
        <span>Ценовая матрица ВОР</span>
      </nav>

      <header className="matrix-hero">
        <div>
          <p className="eyebrow">{project.code} · построчная проверка</p>
          <h1>Ценовая матрица ВОР</h1>
          <p>
            В каждой позиции рядом показаны точные наименования из ВОР,
            каталога, ФГИС ЦС, выигранных тендеров и рынка. Финальная цена
            выводится только после проверки источников, коммерческой базы,
            независимости и согласований.
          </p>
        </div>
        <div className="matrix-hero__guard">
          <Icon name="shield" size={24} />
          <div>
            <strong>
              {diagnosticMode
                ? matrix.release_warning
                : "Проверенная цена строки не является решением APPROVED_FOR_BID. Итоговый шлюз выпуска проекта остаётся обязательным."}
            </strong>
            <span>Сформировано {formatDateTime(matrix.generated_at)}</span>
          </div>
        </div>
      </header>

      <section className="matrix-guide" aria-labelledby="matrix-guide-title">
        <div className="matrix-guide__intro">
          <p className="eyebrow">Как читать таблицу</p>
          <h2 id="matrix-guide-title">
            Одна строка — одна проверяемая позиция
          </h2>
        </div>
        <ol>
          <li>
            <span>01</span>
            <div>
              <strong>Сверьте наименования</strong>
              <p>ВОР и источник показаны рядом вместе с характеристиками.</p>
            </div>
          </li>
          <li>
            <span>02</span>
            <div>
              <strong>Откройте источник</strong>
              <p>
                Цена должна иметь дату, единицу, условия и проверяемую ссылку
                или исходный документ.
              </p>
            </div>
          </li>
          <li>
            <span>03</span>
            <div>
              <strong>Проверьте предложение</strong>
              <p>Система покажет цену только после обязательных проверок.</p>
            </div>
          </li>
        </ol>
      </section>

      <section className="matrix-summary" aria-label="Сводка матрицы">
        <div>
          <span>Позиций</span>
          <strong>{matrix.rows.length}</strong>
        </div>
        <div className="is-positive">
          <span>Проверены</span>
          <strong>{verified}</strong>
        </div>
        <div>
          <span>Маршрут определён</span>
          <strong>{researched}</strong>
        </div>
        <div>
          <span>Кандидаты источников</span>
          <strong>{researchCandidateCount}</strong>
        </div>
        <div className={matrix.blocked_row_count > 0 ? "is-negative" : ""}>
          <span>Заблокированы</span>
          <strong>{matrix.blocked_row_count}</strong>
        </div>
      </section>

      {matrix.rows.length === 0 ? (
        <section className="empty-state">
          <Icon name="portfolio" size={28} />
          <h2>В текущей ВОР нет строк</h2>
          <p>Сначала создайте и независимо подтвердите строки и объёмы.</p>
        </section>
      ) : (
        <div className="price-matrix-scroll">
          <table className="price-matrix-table">
            <thead>
              <tr>
                <th>Позиция ВОР</th>
                <th>Сопоставление наименования</th>
                <th>Выигранные тендеры</th>
                <th>ФГИС ЦС</th>
                <th>Рынок / порталы</th>
                <th>Предложение системы</th>
              </tr>
            </thead>
            <tbody>
              {matrix.rows.map((row) => (
                <tr
                  key={row.row_id}
                  className={
                    row.row_status === "BLOCKED" ? "is-blocked" : "is-verified"
                  }
                >
                  <th scope="row">
                    <div className="matrix-boq-item">
                      <div>
                        <StatusPill value={row.row_status} compact />
                        <code>{row.line_key}</code>
                      </div>
                      <strong>{row.boq_item_name}</strong>
                      <span>
                        {row.work_code} · {row.wbs_node_id}
                      </span>
                      <span>
                        {row.quantity === null
                          ? "объём не подтверждён"
                          : `${formatDecimal(row.quantity)} ${row.boq_unit}`}
                      </span>
                      <small>
                        {row.item_id} ·{" "}
                        {row.cost_category ?? "категория не задана"} ·{" "}
                        {row.basis_kind ?? "база не задана"}
                      </small>
                      {row.research_route !== null && (
                        <ResearchRouteCard route={row.research_route} />
                      )}
                      {row.blockers.length > 0 && (
                        <details className="matrix-blockers">
                          <summary>
                            Причины блокировки: {row.blockers.length}
                          </summary>
                          <ul>
                            {row.blockers.map((blocker) => (
                              <li key={blocker}>{blockerLabel(blocker)}</li>
                            ))}
                          </ul>
                        </details>
                      )}
                    </div>
                  </th>
                  <td>
                    <MatchCell row={row} />
                  </td>
                  <td>
                    <SourcePriceCell
                      prices={row.won_tender_prices}
                      researchCandidates={
                        row.won_tender_research_candidates
                      }
                      boqName={row.boq_item_name}
                      emptyLabel="Нет подтверждённой сопоставимой цены из выигранного тендера."
                    />
                  </td>
                  <td>
                    <SourcePriceCell
                      prices={row.fgis_cs_prices}
                      researchCandidates={row.fgis_cs_research_candidates}
                      boqName={row.boq_item_name}
                      emptyLabel="Нет записи ФГИС ЦС с проверяемым кодом и наименованием."
                    />
                  </td>
                  <td>
                    <SourcePriceCell
                      prices={row.market_prices}
                      researchCandidates={row.market_research_candidates}
                      boqName={row.boq_item_name}
                      emptyLabel="Нет независимой цены с прямой ссылкой на сайт или портал."
                    />
                  </td>
                  <td>
                    <ProposedPriceCell row={row} projectId={projectId} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
