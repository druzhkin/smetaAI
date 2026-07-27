import { useDeferredValue, useMemo, useState } from "react";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

import { getProject, listRecords, type RequestContext } from "../api";
import { useAuth } from "../auth";
import { ErrorBlock, LoadingBlock } from "../components/Feedback";
import { Icon } from "../components/Icon";
import { RecordList } from "../components/RecordList";
import { StatusPill } from "../components/StatusPill";
import { sections } from "../labels";
import { Link } from "../navigation";
import type { ProjectRecordSection, RuntimeConfig } from "../types";

export function RecordsPage({
  config,
  projectId,
  section,
}: {
  config: RuntimeConfig;
  projectId: string;
  section: ProjectRecordSection;
}) {
  const auth = useAuth();
  const [search, setSearch] = useState("");
  const [currentOnly, setCurrentOnly] = useState(false);
  const deferredSearch = useDeferredValue(search.trim());
  const context = useMemo<RequestContext>(
    () => ({
      apiBasePath: config.api_base_path,
      authorizationHeaders: auth.authorizationHeaders,
    }),
    [auth.authorizationHeaders, config.api_base_path],
  );

  const definition = sections.find((item) => item.code === section)!;

  const projectQuery = useQuery({
    queryKey: ["project", projectId],
    queryFn: ({ signal }) => getProject(context, projectId, signal),
    enabled: projectId !== "",
  });
  const recordsQuery = useInfiniteQuery({
    queryKey: ["records", projectId, section, deferredSearch, currentOnly],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam, signal }) =>
      listRecords(
        context,
        projectId,
        section,
        {
          query: deferredSearch || undefined,
          currentOnly,
          cursor: pageParam,
          limit: 40,
        },
        signal,
      ),
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled: projectId !== "",
  });
  const records = recordsQuery.data?.pages.flatMap((page) => page.items) ?? [];

  if (projectQuery.isPending) {
    return (
      <div className="page">
        <LoadingBlock label="Загрузка проекта" />
      </div>
    );
  }
  if (projectQuery.isError) {
    return (
      <div className="page">
        <ErrorBlock
          error={projectQuery.error}
          onRetry={() => void projectQuery.refetch()}
        />
      </div>
    );
  }

  return (
    <div className="page records-page">
      <nav className="breadcrumbs" aria-label="Навигационная цепочка">
        <Link to="/">Проекты</Link>
        <span>/</span>
        <Link to={`/projects/${encodeURIComponent(projectId)}`}>
          {projectQuery.data.code}
        </Link>
        <span>/</span>
        <span>{definition.shortLabel}</span>
      </nav>

      <header className="records-header">
        <div>
          <p className="eyebrow">
            {definition.index} · {projectQuery.data.code}
          </p>
          <h1>{definition.label}</h1>
          <p>{definition.description}</p>
        </div>
        <div className="records-header__actions">
          {section === "DOCUMENTS" &&
            (auth.roles.includes("ESTIMATOR") ||
              auth.roles.includes("TECHNICAL_EXPERT")) && (
              <Link
                className="button button--primary"
                to={`/projects/${encodeURIComponent(projectId)}/documents/new`}
              >
                <Icon name="plus" size={16} />
                Загрузить документ
              </Link>
            )}
          {section === "EVIDENCE" &&
            (auth.roles.includes("TECHNICAL_EXPERT") ||
              auth.roles.includes("REVIEWER")) && (
              <>
                <Link
                  className="button button--secondary"
                  to={`/projects/${encodeURIComponent(projectId)}/passport/manage`}
                >
                  <Icon name="shield" size={16} />
                  Паспорт проекта
                </Link>
                <Link
                  className="button button--secondary"
                  to={`/projects/${encodeURIComponent(projectId)}/evidence/reconcile`}
                >
                  <Icon name="trace" size={16} />
                  Независимая сверка
                </Link>
                <Link
                  className="button button--primary"
                  to={`/projects/${encodeURIComponent(projectId)}/evidence/manual`}
                >
                  <Icon name="edit" size={16} />
                  Ручное наблюдение
                </Link>
              </>
            )}
          {section === "CONTRACT_RISK" &&
            (auth.roles.includes("ESTIMATOR") ||
              auth.roles.includes("TECHNICAL_EXPERT") ||
              auth.roles.includes("REVIEWER")) && (
              <>
                <Link
                  className="button button--secondary"
                  to={`/projects/${encodeURIComponent(projectId)}/risk/manage`}
                >
                  <Icon name="warning" size={16} />
                  Риск-резерв
                </Link>
                <Link
                  className="button button--primary"
                  to={`/projects/${encodeURIComponent(projectId)}/contract/manage`}
                >
                  <Icon name="shield" size={16} />
                  Условия договора
                </Link>
              </>
            )}
          {section === "BOQ_SCOPE" &&
            projectQuery.data.state === "BOQ_IN_PROGRESS" &&
            (auth.roles.includes("ESTIMATOR") ||
              auth.roles.includes("TECHNICAL_EXPERT")) && (
              <Link
                className="button button--primary"
                to={`/projects/${encodeURIComponent(projectId)}/boq/new`}
              >
                <Icon name="plus" size={16} />
                Новая строка BoQ
              </Link>
            )}
          {section === "BOQ_SCOPE" &&
            projectQuery.data.state === "BOQ_REVIEW" &&
            (auth.roles.includes("REVIEWER") ||
              auth.roles.includes("TECHNICAL_EXPERT")) && (
              <Link
                className="button button--primary"
                to={`/projects/${encodeURIComponent(projectId)}/boq/scope-review`}
              >
                <Icon name="shield" size={16} />
                Проверить полноту
              </Link>
            )}
          {section === "BOQ_SCOPE" &&
            ["PRICING_IN_PROGRESS", "RFQ_REQUIRED"].includes(
              projectQuery.data.state,
            ) &&
            (auth.roles.includes("PROCUREMENT") ||
              auth.roles.includes("TECHNICAL_EXPERT")) && (
              <Link
                className="button button--primary"
                to={`/projects/${encodeURIComponent(projectId)}/nomenclature/new`}
              >
                <Icon name="plus" size={16} />
                Сопоставить номенклатуру
              </Link>
            )}
          {section === "CALCULATION" && (
            <Link
              className="button button--secondary"
              to={`/projects/${encodeURIComponent(projectId)}/scenarios`}
            >
              <Icon name="refresh" size={16} />
              Сценарии
            </Link>
          )}
          <StatusPill value={projectQuery.data.state} />
        </div>
      </header>

      <div className="records-layout">
        <aside className="section-nav" aria-label="Контуры проекта">
          {sections.map((item) => (
            <Link
              key={item.code}
              to={`/projects/${encodeURIComponent(projectId)}/${item.code}`}
              className={item.code === section ? "is-active" : ""}
            >
              <span>{item.index}</span>
              {item.shortLabel}
            </Link>
          ))}
        </aside>

        <div className="records-content">
          <section className="toolbar toolbar--records">
            <label className="search-field">
              <Icon name="search" size={17} />
              <span className="sr-only">Поиск записей</span>
              <input
                type="search"
                value={search}
                placeholder="Поиск по наименованию или коду"
                onChange={(event) => setSearch(event.target.value)}
              />
            </label>
            <label className="switch-field">
              <input
                type="checkbox"
                checked={currentOnly}
                onChange={(event) => setCurrentOnly(event.target.checked)}
              />
              <span aria-hidden="true" />
              Только актуальные
            </label>
            <div className="toolbar__count">
              <strong>{records.length}</strong>
              <span>записей</span>
            </div>
          </section>

          {recordsQuery.isPending && (
            <LoadingBlock label="Загрузка доказательной цепочки" />
          )}
          {recordsQuery.isError && (
            <ErrorBlock
              error={recordsQuery.error}
              onRetry={() => void recordsQuery.refetch()}
            />
          )}
          {!recordsQuery.isPending && !recordsQuery.isError && (
            <RecordList
              records={records}
              renderAction={(record) => {
                if (
                  section === "EVIDENCE" &&
                  record.kind === "PASSPORT_FACT" &&
                  record.current === true
                ) {
                  return (
                    <Link
                      className="button button--secondary"
                      to={`/projects/${encodeURIComponent(projectId)}/passport/manage`}
                    >
                      <Icon name="shield" size={15} />
                      Открыть паспорт
                    </Link>
                  );
                }
                if (
                  section === "CONTRACT_RISK" &&
                  ["RISK_ITEM", "RISK_CALCULATION"].includes(record.kind) &&
                  record.current === true
                ) {
                  return (
                    <Link
                      className="button button--secondary"
                      to={`/projects/${encodeURIComponent(projectId)}/risk/manage`}
                    >
                      <Icon name="warning" size={15} />
                      Открыть риск-резерв
                    </Link>
                  );
                }
                if (
                  section === "CONTRACT_RISK" &&
                  record.kind === "CONTRACT_TERM" &&
                  record.current === true
                ) {
                  return (
                    <Link
                      className="button button--secondary"
                      to={`/projects/${encodeURIComponent(projectId)}/contract/manage`}
                    >
                      <Icon name="shield" size={15} />
                      Открыть договор
                    </Link>
                  );
                }
                if (
                  section === "BOQ_SCOPE" &&
                  record.kind === "BOQ_LINE" &&
                  record.current === true &&
                  record.status === "IN_REVIEW" &&
                  (auth.roles.includes("REVIEWER") ||
                    auth.roles.includes("TECHNICAL_EXPERT"))
                ) {
                  return (
                    <Link
                      className="button button--secondary"
                      to={`/projects/${encodeURIComponent(projectId)}/boq-lines/${encodeURIComponent(record.id)}/review`}
                    >
                      <Icon name="shield" size={15} />
                      Проверить строку
                    </Link>
                  );
                }
                if (
                  section === "BOQ_SCOPE" &&
                  record.kind === "BOQ_LINE" &&
                  record.current === true &&
                  record.status === "VERIFIED" &&
                  record.attributes.quantity_status !== "MISSING" &&
                  (auth.roles.includes("ESTIMATOR") ||
                    auth.roles.includes("TECHNICAL_EXPERT"))
                ) {
                  return (
                    <Link
                      className="button button--secondary"
                      to={`/projects/${encodeURIComponent(projectId)}/boq-lines/${encodeURIComponent(record.id)}/quantity-change`}
                    >
                      <Icon name="edit" size={15} />
                      Изменить объём
                    </Link>
                  );
                }
                if (
                  section === "BOQ_SCOPE" &&
                  record.kind === "MANUAL_CHANGE"
                ) {
                  return (
                    <Link
                      className="button button--secondary"
                      to={`/projects/${encodeURIComponent(projectId)}/manual-changes/${encodeURIComponent(record.id)}`}
                    >
                      <Icon name="trace" size={15} />
                      Открыть изменение
                    </Link>
                  );
                }
                if (
                  section === "BOQ_SCOPE" &&
                  record.kind === "NOMENCLATURE_MATCH" &&
                  record.current === true &&
                  record.status !== "VERIFIED" &&
                  (auth.roles.includes("PROCUREMENT") ||
                    auth.roles.includes("TECHNICAL_EXPERT") ||
                    auth.roles.includes("REVIEWER"))
                ) {
                  return (
                    <Link
                      className="button button--secondary"
                      to={`/projects/${encodeURIComponent(projectId)}/nomenclature/${encodeURIComponent(record.id)}/review`}
                    >
                      <Icon name="shield" size={15} />
                      Проверить сопоставление
                    </Link>
                  );
                }
                if (
                  ((section === "BOQ_SCOPE" &&
                    record.kind === "NOMENCLATURE_MATCH" &&
                    record.current === true &&
                    record.status === "VERIFIED") ||
                    (section === "PRICING" &&
                      [
                        "PRICE_QUOTE",
                        "NORMALIZED_PRICE",
                        "PRICE_DECISION",
                        "RFQ",
                      ].includes(record.kind))) &&
                  (auth.roles.includes("PROCUREMENT") ||
                    auth.roles.includes("ESTIMATOR") ||
                    auth.roles.includes("TECHNICAL_EXPERT") ||
                    auth.roles.includes("REVIEWER") ||
                    auth.roles.includes("APPROVER") ||
                    auth.roles.includes("AUDITOR"))
                ) {
                  return (
                    <Link
                      className="button button--secondary"
                      to={`/projects/${encodeURIComponent(projectId)}/pricing/items/${encodeURIComponent(record.title)}`}
                    >
                      <Icon name="trace" size={15} />
                      Открыть ценовой контур
                    </Link>
                  );
                }
                if (
                  section === "EVIDENCE" &&
                  record.kind === "OBSERVATION" &&
                  record.subtitle === "MANUAL" &&
                  record.status === "UNVERIFIED" &&
                  (auth.roles.includes("REVIEWER") ||
                    auth.roles.includes("TECHNICAL_EXPERT"))
                ) {
                  return (
                    <Link
                      className="button button--secondary"
                      to={`/projects/${encodeURIComponent(projectId)}/evidence/observations/${encodeURIComponent(record.id)}/review`}
                    >
                      <Icon name="shield" size={15} />
                      Проверить наблюдение
                    </Link>
                  );
                }
                if (
                  section === "EVIDENCE" &&
                  record.kind === "CONFLICT" &&
                  record.status === "CONFLICT" &&
                  (auth.roles.includes("REVIEWER") ||
                    auth.roles.includes("TECHNICAL_EXPERT"))
                ) {
                  return (
                    <Link
                      className="button button--secondary"
                      to={`/projects/${encodeURIComponent(projectId)}/conflicts/${encodeURIComponent(record.id)}/resolve`}
                    >
                      <Icon name="warning" size={15} />
                      Разрешить конфликт
                    </Link>
                  );
                }
                if (
                  section !== "DOCUMENTS" ||
                  record.kind !== "DOCUMENT_SET_REVISION" ||
                  record.status !== "DRAFT" ||
                  (!auth.roles.includes("REVIEWER") &&
                    !auth.roles.includes("APPROVER"))
                ) {
                  return undefined;
                }
                return (
                  <Link
                    className="button button--secondary"
                    to={`/projects/${encodeURIComponent(projectId)}/document-sets/${encodeURIComponent(record.id)}/confirm`}
                  >
                    <Icon name="check" size={15} />
                    Проверить комплект
                  </Link>
                );
              }}
            />
          )}

          {recordsQuery.hasNextPage && (
            <div className="load-more">
              <button
                className="button button--secondary"
                type="button"
                disabled={recordsQuery.isFetchingNextPage}
                onClick={() => void recordsQuery.fetchNextPage()}
              >
                {recordsQuery.isFetchingNextPage
                  ? "Загрузка…"
                  : "Показать следующую страницу"}
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
