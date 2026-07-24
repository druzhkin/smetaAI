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
        <StatusPill value={projectQuery.data.state} />
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
            <RecordList records={records} />
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
