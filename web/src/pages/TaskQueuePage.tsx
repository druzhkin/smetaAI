import { useMemo, useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";

import { listWorkItems, type RequestContext } from "../api";
import { useAuth } from "../auth";
import { EmptyState, ErrorBlock, LoadingBlock } from "../components/Feedback";
import { Icon } from "../components/Icon";
import { PageHeader } from "../components/PageHeader";
import { StatusPill } from "../components/StatusPill";
import { formatDateTime } from "../format";
import { roleLabels, taskLabels } from "../labels";
import { Link } from "../navigation";
import type { RuntimeConfig } from "../types";

export function TaskQueuePage({ config }: { config: RuntimeConfig }) {
  const auth = useAuth();
  const [status, setStatus] = useState("PENDING");
  const context = useMemo<RequestContext>(
    () => ({
      apiBasePath: config.api_base_path,
      authorizationHeaders: auth.authorizationHeaders,
    }),
    [auth.authorizationHeaders, config.api_base_path],
  );
  const query = useInfiniteQuery({
    queryKey: ["work-items", status],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam, signal }) =>
      listWorkItems(
        context,
        {
          statuses: status === "" ? [] : [status],
          cursor: pageParam,
          limit: 50,
        },
        signal,
      ),
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
  });
  const items = query.data?.pages.flatMap((page) => page.items) ?? [];

  return (
    <div className="page">
      <PageHeader
        eyebrow="Принцип четырёх глаз"
        title="Мои проверки"
        description="Здесь только задачи, назначенные вашей роли в конкретном проекте. Критическое изменение не может утвердить его автор."
      />

      <section className="toolbar">
        <label className="select-field">
          <span>Статус задачи</span>
          <select
            value={status}
            onChange={(event) => setStatus(event.target.value)}
          >
            <option value="PENDING">Ожидает решения</option>
            <option value="COMPLETED">Завершена</option>
            <option value="CANCELLED">Отменена</option>
            <option value="">Все</option>
          </select>
        </label>
        <div className="toolbar__count">
          <strong>{items.length}</strong>
          <span>задач</span>
        </div>
      </section>

      {query.isPending && <LoadingBlock label="Загрузка задач" />}
      {query.isError && (
        <ErrorBlock error={query.error} onRetry={() => void query.refetch()} />
      )}
      {!query.isPending && !query.isError && items.length === 0 && (
        <EmptyState
          title="Назначенных задач нет"
          description="Это означает только отсутствие задач для вашей роли, а не готовность проекта к выпуску."
        />
      )}

      {items.length > 0 && (
        <div className="task-list">
          {items.map((item) => (
            <Link
              key={item.task_id}
              to={`/tasks/${encodeURIComponent(item.task_id)}`}
              className="task-row"
            >
              <span
                className={`task-row__priority ${item.required ? "is-required" : ""}`}
                title={
                  item.required
                    ? "Обязательная проверка"
                    : "Дополнительная проверка"
                }
              />
              <div className="task-row__type">
                <small>{item.task_type}</small>
                <strong>{taskLabels[item.task_type] ?? item.task_type}</strong>
                <span>
                  {item.entity_type} · {item.entity_id}
                </span>
              </div>
              <div className="task-row__project">
                <strong>{item.project_code}</strong>
                <span>{item.project_name}</span>
              </div>
              <div>
                <StatusPill value={item.status} compact />
                <small>{roleLabels[item.assigned_role]}</small>
              </div>
              <time dateTime={item.updated_at}>
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
