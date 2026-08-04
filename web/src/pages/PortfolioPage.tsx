import { useDeferredValue, useMemo, useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";

import { listProjects, type RequestContext } from "../api";
import { useAuth } from "../auth";
import { EmptyState, ErrorBlock, LoadingBlock } from "../components/Feedback";
import { Icon } from "../components/Icon";
import { PageHeader } from "../components/PageHeader";
import { StatusPill } from "../components/StatusPill";
import { WorkflowRail } from "../components/WorkflowRail";
import { formatDateTime, formatMoney } from "../format";
import { roleLabels, stateLabels } from "../labels";
import { Link } from "../navigation";
import type { ApprovalState, RuntimeConfig } from "../types";

const filterStates: ApprovalState[] = [
  "BLOCKED",
  "DOCUMENTS_INCOMPLETE",
  "RFQ_REQUIRED",
  "EXPERT_REVIEW",
  "INDEPENDENT_VALIDATION",
  "APPROVED_FOR_INTERNAL_USE",
  "APPROVED_FOR_BID",
  "DRAFT",
];

export function PortfolioPage({ config }: { config: RuntimeConfig }) {
  const auth = useAuth();
  const [search, setSearch] = useState("");
  const [state, setState] = useState<ApprovalState | "">("");
  const deferredSearch = useDeferredValue(search.trim());
  const context = useMemo<RequestContext>(
    () => ({
      apiBasePath: config.api_base_path,
      authorizationHeaders: auth.authorizationHeaders,
    }),
    [auth.authorizationHeaders, config.api_base_path],
  );
  const query = useInfiniteQuery({
    queryKey: ["projects", deferredSearch, state],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam, signal }) =>
      listProjects(
        context,
        {
          query: deferredSearch || undefined,
          states: state === "" ? undefined : [state],
          cursor: pageParam,
          limit: 40,
        },
        signal,
      ),
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
  });
  const projects = query.data?.pages.flatMap((page) => page.items) ?? [];
  const portfolioSummary = useMemo(
    () => ({
      shown: projects.length,
      blocked: projects.filter(
        (item) =>
          item.project.state === "BLOCKED" || item.unresolved_blocker_count > 0,
      ).length,
      expertReview: projects.filter(
        (item) => item.project.state === "EXPERT_REVIEW",
      ).length,
      approved: projects.filter(
        (item) => item.project.state === "APPROVED_FOR_BID",
      ).length,
    }),
    [projects],
  );

  return (
    <div className="page">
      <PageHeader
        eyebrow="Рабочий стол"
        title="Проекты и расчёты"
        description="Загрузите документы — система соберёт ВОР, сопоставит позиции, найдёт доступные цены и подготовит расчёт. Эксперт подключается к итоговому результату."
        actions={
          <>
            {auth.roles.includes("ESTIMATOR") && (
              <Link className="button button--primary" to="/projects/new">
                <Icon name="plus" size={16} />
                Новый проект
              </Link>
            )}
            <Link className="button button--secondary" to="/tasks">
              <Icon name="tasks" size={16} />
              Контроль и решения
            </Link>
          </>
        }
      />

      <section className="portfolio-intro" aria-labelledby="workflow-title">
        <header className="section-heading">
          <div>
            <p className="eyebrow">Как получается результат</p>
            <h2 id="workflow-title">Пять этапов от документов до цены</h2>
          </div>
          <p>
            После загрузки файлов первые четыре этапа ведёт автоматический
            контур. Если данных или квалифицированного расчёта не хватает,
            проект останавливается. Эксперт принимает только итог.
          </p>
        </header>
        <WorkflowRail />
      </section>

      {!query.isPending && !query.isError && projects.length > 0 && (
        <section
          className="portfolio-summary"
          aria-label="Сводка по показанным проектам"
        >
          <article>
            <span>Показано проектов</span>
            <strong>{portfolioSummary.shown}</strong>
          </article>
          <article
            className={portfolioSummary.blocked > 0 ? "is-negative" : ""}
          >
            <span>Требуют исправления</span>
            <strong>{portfolioSummary.blocked}</strong>
          </article>
          <article>
            <span>На итоговом контроле</span>
            <strong>{portfolioSummary.expertReview}</strong>
          </article>
          <article className="is-positive">
            <span>Допущены к конкурсу</span>
            <strong>{portfolioSummary.approved}</strong>
          </article>
          <small>Сводка относится только к загруженным ниже строкам.</small>
        </section>
      )}

      <section className="toolbar" aria-label="Фильтры проектов">
        <label className="search-field">
          <Icon name="search" size={17} />
          <span className="sr-only">Поиск проектов</span>
          <input
            type="search"
            value={search}
            placeholder="Шифр или наименование объекта"
            onChange={(event) => setSearch(event.target.value)}
          />
        </label>
        <label className="select-field">
          <span>Состояние</span>
          <select
            value={state}
            onChange={(event) =>
              setState(event.target.value as ApprovalState | "")
            }
          >
            <option value="">Все доступные</option>
            {filterStates.map((item) => (
              <option key={item} value={item}>
                {stateLabels[item]}
              </option>
            ))}
          </select>
        </label>
        <div className="toolbar__count">
          <strong>{projects.length}</strong>
          <span>показано</span>
        </div>
      </section>

      {query.isPending && <LoadingBlock label="Загрузка портфеля" />}
      {query.isError && (
        <ErrorBlock error={query.error} onRetry={() => void query.refetch()} />
      )}
      {!query.isPending && !query.isError && projects.length === 0 && (
        <EmptyState
          title="Доступных проектов нет"
          description="Проверьте фильтры и назначение вашей роли владельцем проекта."
        />
      )}

      {projects.length > 0 && (
        <div className="portfolio-table" role="table" aria-label="Проекты">
          <div className="portfolio-table__head" role="row">
            <span>Проект</span>
            <span>Состояние</span>
            <span>Что требует внимания</span>
            <span>Последний расчёт</span>
            <span>Обновлён</span>
            <span />
          </div>
          {projects.map((item) => (
            <Link
              to={`/projects/${encodeURIComponent(item.project.id)}`}
              className="portfolio-row"
              role="row"
              key={item.project.id}
            >
              <div className="portfolio-row__project" role="cell">
                <strong>{item.project.code}</strong>
                <span>{item.project.name}</span>
                <small>
                  {item.access.access_level === "OWNER"
                    ? "Владелец"
                    : "Участник"}
                  {" · "}
                  {item.access.roles.map((role) => roleLabels[role]).join(", ")}
                </small>
              </div>
              <div className="portfolio-row__state" role="cell">
                <StatusPill value={item.project.state} />
              </div>
              <div className="control-counts" role="cell">
                <span
                  className={
                    item.unresolved_blocker_count > 0 ? "is-danger" : ""
                  }
                >
                  <strong>{item.unresolved_blocker_count}</strong>
                  блокеров
                </span>
                <span>
                  <strong>{item.open_approval_count}</strong>
                  проверок
                </span>
              </div>
              <div className="portfolio-row__amount" role="cell">
                <strong>
                  {formatMoney(item.latest_total, item.latest_currency)}
                </strong>
                {item.latest_total === null && (
                  <small>итог ещё не рассчитан</small>
                )}
              </div>
              <time role="cell" dateTime={item.updated_at}>
                {formatDateTime(item.updated_at)}
              </time>
              <Icon name="arrow" />
            </Link>
          ))}
        </div>
      )}

      {query.hasNextPage && (
        <div className="load-more">
          <button
            className="button button--secondary"
            type="button"
            disabled={query.isFetchingNextPage}
            onClick={() => void query.fetchNextPage()}
          >
            {query.isFetchingNextPage ? "Загрузка…" : "Показать ещё"}
          </button>
        </div>
      )}
    </div>
  );
}
