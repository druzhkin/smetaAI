import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import { getWorkbench, type RequestContext } from "../api";
import { useAuth } from "../auth";
import { EmptyState, ErrorBlock, LoadingBlock } from "../components/Feedback";
import { Icon } from "../components/Icon";
import { RecordList } from "../components/RecordList";
import { StatusPill } from "../components/StatusPill";
import { WorkflowRail } from "../components/WorkflowRail";
import { formatDateTime, formatMoney } from "../format";
import { findingLabels, metricLabels, roleLabels, sections } from "../labels";
import { Link } from "../navigation";
import type { RuntimeConfig } from "../types";
import { blockedWithoutFindingAction, nextActionForFinding } from "../workflow";

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
  const firstFinding = release.findings[0];
  const nextAction =
    firstFinding === undefined
      ? release.allowed
        ? null
        : blockedWithoutFindingAction()
      : nextActionForFinding(
          firstFinding,
          findingLabels[firstFinding.code] ?? firstFinding.message,
        );

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
          <small>
            {workbench.latest_total === null
              ? "Расчёт ещё не сформирован"
              : "Не является конкурсной ценой без итогового допуска"}
          </small>
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
          <p className="eyebrow">Готовность результата</p>
          <h2>
            {release.allowed
              ? "Результат прошёл обязательные проверки"
              : "Итоговую цену пока нельзя использовать"}
          </h2>
          <p>
            {release.allowed
              ? "Все обязательные проверки пройдены для текущей зафиксированной версии расчёта."
              : `Система нашла ${release.findings.length} блокирующих причин. Пока они не устранены, цена скрыта от выпуска.`}
          </p>
        </div>
        <div className="release-banner__actions">
          <StatusPill value={release.resulting_state} />
          <Link
            className="button button--secondary"
            to={`/projects/${encodeURIComponent(projectId)}/release`}
          >
            Итоговый контроль <Icon name="arrow" size={15} />
          </Link>
        </div>
      </section>

      <section className="project-route" aria-labelledby="project-route-title">
        <header className="section-heading">
          <div>
            <p className="eyebrow">Ход работы</p>
            <h2 id="project-route-title">Путь расчёта</h2>
          </div>
          <p>
            Откройте любой этап, чтобы увидеть исходные данные, результат и
            причины остановки.
          </p>
        </header>
        <WorkflowRail state={workbench.project.state} projectId={projectId} />
      </section>

      {nextAction !== null && (
        <section className="next-action" aria-labelledby="next-action-title">
          <div className="next-action__marker">
            <Icon name="warning" size={24} />
          </div>
          <div>
            <p className="eyebrow">Что делать сейчас</p>
            <h2 id="next-action-title">{nextAction.title}</h2>
            <p>{nextAction.description}</p>
          </div>
          <Link
            className="button button--primary"
            to={`/projects/${encodeURIComponent(projectId)}/${nextAction.path}`}
          >
            {nextAction.label}
            <Icon name="arrow" size={15} />
          </Link>
        </section>
      )}

      {release.findings.length > 0 && (
        <section className="finding-register">
          <header className="section-heading">
            <div>
              <p className="eyebrow">Остановка расчёта</p>
              <h2>Все причины блокировки</h2>
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

      <details className="professional-panel">
        <summary>
          <span>
            <strong>Профессиональные разделы проекта</strong>
            <small>
              Все исходные записи, риски, версии, согласования и журнал аудита
            </small>
          </span>
          <span>10 разделов</span>
        </summary>
        <section className="section-directory">
          <header className="section-heading">
            <div>
              <p className="eyebrow">Полная доказательная цепочка</p>
              <h2>Детальные разделы</h2>
            </div>
            <p>От исходного документа до зафиксированного результата</p>
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
      </details>

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
              description="Итоговый допуск определяется всеми обязательными проверками, а не только этой выборкой."
            />
          )}
        </section>
        <section>
          <header className="section-heading">
            <div>
              <p className="eyebrow">История проекта</p>
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
