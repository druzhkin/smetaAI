import { useQuery } from "@tanstack/react-query";

import { getAutomationReworkStatus, type RequestContext } from "../api";
import { automationReworkPresentation } from "../automationReworkWorkflow";
import { ErrorBlock, LoadingBlock } from "./Feedback";
import { Icon } from "./Icon";

export function AutomationReworkStatusPanel({
  context,
  projectId,
}: {
  context: RequestContext;
  projectId: string;
}) {
  const statusQuery = useQuery({
    queryKey: ["automation-rework-status", projectId],
    queryFn: ({ signal }) =>
      getAutomationReworkStatus(context, projectId, signal),
    refetchInterval: 5_000,
    refetchOnWindowFocus: true,
  });

  if (statusQuery.isPending) {
    return <LoadingBlock label="Проверка автоматической доработки" />;
  }
  if (statusQuery.isError) {
    return (
      <ErrorBlock
        error={statusQuery.error}
        onRetry={() => void statusQuery.refetch()}
      />
    );
  }
  if (statusQuery.data.items.length === 0) {
    return null;
  }

  return (
    <section
      className="automation-rework-status"
      aria-labelledby="automation-rework-status-title"
    >
      <header className="section-heading">
        <div>
          <p className="eyebrow">Без промежуточного участия сотрудников</p>
          <h2 id="automation-rework-status-title">
            Автоматическая доработка расчёта
          </h2>
          <p>
            Здесь показана передача замечаний этапам системы. Статус очереди не
            означает, что новый расчёт уже готов или безопасен для выпуска.
          </p>
        </div>
        <span>{statusQuery.data.items.length}</span>
      </header>

      <div className="automation-rework-list">
        {statusQuery.data.items.map((item) => {
          const presentation = automationReworkPresentation(item);
          return (
            <article
              className={`automation-rework-card automation-rework-card--${presentation.tone}`}
              key={item.rework_request_id}
            >
              <Icon
                name={presentation.tone === "danger" ? "warning" : "refresh"}
                size={22}
              />
              <div>
                <div className="automation-rework-card__heading">
                  <strong>{presentation.label}</strong>
                  <span>{item.target_stage}</span>
                </div>
                <p>{presentation.explanation}</p>
                <p>
                  Замечаний: {item.issue_references.length} · запрос от{" "}
                  {new Date(item.requested_at).toLocaleString("ru-RU")}
                </p>
                <code>{item.rework_request_id}</code>
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}
