import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import { getWorkbench, type RequestContext } from "../api";
import { useAuth } from "../auth";
import { EmptyState, ErrorBlock, LoadingBlock } from "../components/Feedback";
import { Icon } from "../components/Icon";
import { RecordList } from "../components/RecordList";
import { StatusPill } from "../components/StatusPill";
import { formatDateTime, formatMoney } from "../format";
import { findingLabels, metricLabels, roleLabels, sections } from "../labels";
import { Link } from "../navigation";
import type { RuntimeConfig } from "../types";

export function ProjectWorkbenchPage({
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
  const query = useQuery({
    queryKey: ["workbench", projectId],
    queryFn: ({ signal }) => getWorkbench(context, projectId, signal),
    enabled: projectId !== "",
  });

  if (query.isPending) {
    return (
      <div className="page">
        <LoadingBlock label="Сбор контрольной картины проекта" />
      </div>
    );
  }
  if (query.isError) {
    return (
      <div className="page">
        <ErrorBlock error={query.error} onRetry={() => void query.refetch()} />
      </div>
    );
  }

  const workbench = query.data;
  const release = workbench.release_decision;

  return (
    <div className="page project-page">
      <nav className="breadcrumbs" aria-label="Навигационная цепочка">
        <Link to="/">Проекты</Link>
        <span>/</span>
        <span>{workbench.project.code}</span>
      </nav>

      <header className="project-header">
        <div>
          <div className="project-header__line">
            <p className="eyebrow">{workbench.project.code}</p>
            <StatusPill value={workbench.project.state} />
          </div>
          <h1>{workbench.project.name}</h1>
          <p>
            {workbench.access.access_level === "OWNER"
              ? "Владелец"
              : "Участник"}
            {" · "}
            {workbench.access.roles.map((role) => roleLabels[role]).join(", ")}
          </p>
        </div>
        <div className="project-total">
          <span>Последний сохранённый итог</span>
          <strong>
            {formatMoney(workbench.latest_total, workbench.latest_currency)}
          </strong>
          <small>Не является конкурсной ценой без допуска</small>
        </div>
      </header>

      <section
        className={`release-banner ${
          release.allowed
            ? "release-banner--allowed"
            : "release-banner--blocked"
        }`}
      >
        <div className="release-banner__icon">
          <Icon name={release.allowed ? "check" : "warning"} size={28} />
        </div>
        <div>
          <p className="eyebrow">Решение контрольного контура</p>
          <h2>
            {release.allowed
              ? "Выпуск разрешён текущими проверками"
              : "Выпуск конкурсной цены заблокирован"}
          </h2>
          <p>
            {release.allowed
              ? "Все обязательные hard stops пройдены для текущего зафиксированного снимка."
              : `${release.findings.length} причин требуют доказательства, исправления или независимого согласования.`}
          </p>
        </div>
        <div className="release-banner__actions">
          <StatusPill value={release.resulting_state} />
          <Link
            className="button button--secondary"
            to={`/projects/${encodeURIComponent(projectId)}/release`}
          >
            Полная оценка <Icon name="arrow" size={15} />
          </Link>
        </div>
      </section>

      {release.findings.length > 0 && (
        <section className="finding-register">
          <header className="section-heading">
            <div>
              <p className="eyebrow">Hard stops</p>
              <h2>Причины блокировки</h2>
            </div>
            <span>{release.findings.length}</span>
          </header>
          <div className="finding-grid">
            {release.findings.map((finding, index) => (
              <article key={`${finding.code}:${index}`}>
                <span>{String(index + 1).padStart(2, "0")}</span>
                <div>
                  <strong>
                    {findingLabels[finding.code] ?? finding.message}
                  </strong>
                  <code>{finding.code}</code>
                  {finding.entity_ids.length > 0 && (
                    <p>Объекты: {finding.entity_ids.join(", ")}</p>
                  )}
                </div>
              </article>
            ))}
          </div>
        </section>
      )}

      <section
        className="metrics-strip"
        aria-label="Ключевые показатели контроля"
      >
        {workbench.metrics.map((metric) => (
          <article key={metric.code}>
            <span>{metricLabels[metric.code] ?? metric.label}</span>
            <strong>{metric.value}</strong>
            <small className={metric.blocking > 0 ? "is-danger" : ""}>
              {metric.blocking > 0
                ? `${metric.blocking} блокирует`
                : "критичных нет"}
            </small>
          </article>
        ))}
      </section>

      <section className="section-directory">
        <header className="section-heading">
          <div>
            <p className="eyebrow">Доказательная цепочка</p>
            <h2>Контуры проекта</h2>
          </div>
          <p>От исходного документа до согласованного результата</p>
        </header>
        <div className="section-grid">
          {sections.map((section) => (
            <Link
              key={section.code}
              to={`/projects/${encodeURIComponent(projectId)}/${section.code}`}
              className="section-card"
            >
              <span className="section-card__index">{section.index}</span>
              <div>
                <h3>{section.label}</h3>
                <p>{section.description}</p>
              </div>
              <Icon name="arrow" size={17} />
            </Link>
          ))}
        </div>
      </section>

      <div className="workbench-columns">
        <section>
          <header className="section-heading">
            <div>
              <p className="eyebrow">Требует внимания</p>
              <h2>Открытые замечания</h2>
            </div>
          </header>
          {workbench.attention.length > 0 ? (
            <RecordList records={workbench.attention} />
          ) : (
            <EmptyState
              title="Открытых записей нет"
              description="Окончательный допуск всё равно определяется всеми hard stops, а не только этой выборкой."
            />
          )}
        </section>
        <section>
          <header className="section-heading">
            <div>
              <p className="eyebrow">Audit trail</p>
              <h2>Последние действия</h2>
            </div>
            <Link
              to={`/projects/${encodeURIComponent(projectId)}/AUDIT`}
              className="text-link"
            >
              Весь журнал <Icon name="arrow" size={14} />
            </Link>
          </header>
          <div className="activity-list">
            {workbench.recent_activity.map((record) => (
              <article key={`${record.kind}:${record.id}`}>
                <span className="activity-list__dot" />
                <div>
                  <strong>{record.title}</strong>
                  <p>{record.subtitle ?? record.kind}</p>
                  <time dateTime={record.occurred_at}>
                    {formatDateTime(record.occurred_at)}
                  </time>
                </div>
              </article>
            ))}
          </div>
        </section>
      </div>
      <p className="generated-at">
        Контрольная картина сформирована{" "}
        {formatDateTime(workbench.generated_at)}
      </p>
    </div>
  );
}
