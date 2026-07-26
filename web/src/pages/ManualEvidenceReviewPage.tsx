import { useMemo, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  decideManualEvidence,
  getManualEvidenceReview,
  getProject,
  newIdempotencyKey,
  type RequestContext,
} from "../api";
import { useAuth } from "../auth";
import { ErrorBlock, LoadingBlock } from "../components/Feedback";
import { Icon } from "../components/Icon";
import { StatusPill } from "../components/StatusPill";
import { formatDateTime } from "../format";
import {
  validateManualEvidenceReview,
  type ManualEvidenceReviewDraft,
} from "../manualEvidence";
import { Link, useNavigation } from "../navigation";
import type {
  ApprovalDecision,
  EvidenceObservation,
  RuntimeConfig,
} from "../types";

const decisionLabels: Record<ApprovalDecision, string> = {
  APPROVED: "Подтвердить наблюдение",
  CHANGES_REQUESTED: "Вернуть на исправление",
  REJECTED: "Отклонить",
};

const blockerLabels: Record<string, string> = {
  MANUAL_EVIDENCE_SCOPE_MISMATCH:
    "Наблюдение больше не соответствует политике или текущему комплекту документов.",
  PROJECT_STATE_NOT_ALLOWED:
    "Текущее состояние проекта не допускает решение по этой корректировке.",
  TASK_INTEGRITY_FAILED:
    "Неизменяемый контекст обязательной задачи не прошёл проверку.",
  TASK_NOT_REQUIRED: "Задача ошибочно не отмечена обязательной.",
  TASK_ROLE_MISMATCH:
    "Назначенная роль задачи не совпадает с утверждённой политикой.",
  TASK_NOT_PENDING: "Задача уже завершена.",
  FOUR_EYES_SOURCE_AUTHOR:
    "Автор ручного наблюдения не может проверить его самостоятельно.",
  FOUR_EYES_TASK_CREATOR:
    "Создатель обязательной задачи не может принять решение по ней.",
  ACTOR_ID_MISSING: "Identity-сессия не содержит проверяемый actor ID.",
};

function formatEvidenceValue(value: unknown): string {
  if (typeof value === "string") {
    return value;
  }
  if (value === null || typeof value === "boolean") {
    return String(value);
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return "[значение не может быть отображено]";
  }
}

function locationLabel(observation: EvidenceObservation): string {
  const location = observation.location;
  return [
    location.locator,
    location.page === null ? null : `стр. ${location.page}`,
    location.table === null ? null : `таблица ${location.table}`,
    location.sheet === null ? null : `лист ${location.sheet}`,
    location.cell_or_range,
  ]
    .filter((value): value is string => value !== null && value !== "")
    .join(" · ");
}

const initialDraft: ManualEvidenceReviewDraft = {
  decision: "CHANGES_REQUESTED",
  reason: "",
  projectCode: "",
  acknowledged: false,
};

export function ManualEvidenceReviewPage({
  config,
  projectId,
  observationId,
}: {
  config: RuntimeConfig;
  projectId: string;
  observationId: string;
}) {
  const auth = useAuth();
  const queryClient = useQueryClient();
  const { navigate } = useNavigation();
  const [draft, setDraft] = useState<ManualEvidenceReviewDraft>(initialDraft);
  const [formError, setFormError] = useState<string | null>(null);
  const [operationKey, setOperationKey] = useState<string | null>(null);
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
  const reviewQuery = useQuery({
    queryKey: ["manual-evidence-review", projectId, observationId],
    queryFn: ({ signal }) =>
      getManualEvidenceReview(context, projectId, observationId, signal),
  });
  const mutation = useMutation({
    mutationFn: (input: {
      decision: ApprovalDecision;
      reason: string;
      expectedTaskUpdatedAt: string;
      idempotencyKey: string;
    }) =>
      decideManualEvidence(context, {
        projectId,
        observationId,
        decision: input.decision,
        reason: input.reason,
        expectedTaskUpdatedAt: input.expectedTaskUpdatedAt,
        idempotencyKey: input.idempotencyKey,
      }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["manual-evidence-review", projectId, observationId],
        }),
        queryClient.invalidateQueries({
          queryKey: ["records", projectId, "EVIDENCE"],
        }),
        queryClient.invalidateQueries({
          queryKey: ["records", projectId, "APPROVALS"],
        }),
        queryClient.invalidateQueries({ queryKey: ["work-items"] }),
        queryClient.invalidateQueries({ queryKey: ["workbench", projectId] }),
      ]);
      navigate(`/projects/${encodeURIComponent(projectId)}/EVIDENCE`, {
        replace: true,
      });
    },
  });

  if (projectQuery.isPending || reviewQuery.isPending) {
    return (
      <div className="page">
        <LoadingBlock label="Загрузка ручного наблюдения и provenance" />
      </div>
    );
  }
  if (projectQuery.isError || reviewQuery.isError) {
    const failed = projectQuery.isError ? projectQuery : reviewQuery;
    return (
      <div className="page">
        <ErrorBlock
          error={failed.error}
          onRetry={() => {
            void projectQuery.refetch();
            void reviewQuery.refetch();
          }}
        />
      </div>
    );
  }

  const project = projectQuery.data;
  const review = reviewQuery.data;
  const source = review.source_observation;
  const blockers = [
    ...review.decision_blockers,
    ...(auth.actorId === null ? ["ACTOR_ID_MISSING"] : []),
  ];
  const validationError = validateManualEvidenceReview(
    draft,
    project.code,
    source.actor_id,
    auth.actorId,
  );

  const change = (patch: Partial<ManualEvidenceReviewDraft>) => {
    setDraft((current) => ({ ...current, ...patch, acknowledged: false }));
    setOperationKey(null);
    setFormError(null);
    mutation.reset();
  };
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (blockers.length > 0) {
      setFormError(
        blockerLabels[blockers[0] ?? ""] ?? "Экспертное решение заблокировано",
      );
      return;
    }
    setFormError(validationError);
    if (validationError !== null) {
      return;
    }
    let key = operationKey;
    if (key === null) {
      try {
        key = newIdempotencyKey();
      } catch (error) {
        setFormError(
          error instanceof Error
            ? error.message
            : "Не удалось создать ключ операции",
        );
        return;
      }
      setOperationKey(key);
    }
    mutation.mutate({
      decision: draft.decision,
      reason: draft.reason.trim(),
      expectedTaskUpdatedAt: review.task_updated_at,
      idempotencyKey: key,
    });
  };

  return (
    <div className="page">
      <nav className="breadcrumbs" aria-label="Навигационная цепочка">
        <Link to="/tasks">Мои проверки</Link>
        <span>/</span>
        <Link to={`/projects/${encodeURIComponent(projectId)}`}>
          {project.code}
        </Link>
        <span>/</span>
        <Link to={`/projects/${encodeURIComponent(projectId)}/EVIDENCE`}>
          Доказательства
        </Link>
        <span>/</span>
        <span>Проверка ручного наблюдения</span>
      </nav>

      <header className="entry-header">
        <div>
          <p className="eyebrow">{project.code} · четыре глаза</p>
          <h1>{source.field_name}</h1>
          <p>
            Утверждение создаст отдельное производное VERIFIED-наблюдение.
            Исходная ручная запись останется неизменяемой и UNVERIFIED.
          </p>
        </div>
        <div className="entry-header__guard">
          <Icon name="shield" size={22} />
          <span>
            Сервер повторно проверит автора, политику, комплект документов,
            SHA-256 источника и версию обязательной задачи.
          </span>
        </div>
      </header>

      <section className="conflict-summary">
        <div className="conflict-summary__header">
          <div>
            <p className="eyebrow">Ручное наблюдение</p>
            <h2>{source.observation_id}</h2>
            <p>SHA-256 записи: {review.source_observation_hash}</p>
          </div>
          <StatusPill value={source.status} />
        </div>
        <pre className="manual-evidence-value">
          {formatEvidenceValue(source.value)}
          {source.unit === null ? "" : ` ${source.unit}`}
        </pre>
        <div className="manual-evidence-reason">
          <span>Причина ручной корректировки</span>
          <p>{review.submission_reason}</p>
        </div>
        <dl className="task-facts">
          <div>
            <dt>Автор наблюдения</dt>
            <dd>{source.actor_id}</dd>
          </div>
          <div>
            <dt>Наблюдалось</dt>
            <dd>{formatDateTime(source.observed_at)}</dd>
          </div>
          <div>
            <dt>Метод / политика</dt>
            <dd>
              {source.method} · {review.policy_version_id}
            </dd>
          </div>
          <div>
            <dt>Комплект документов</dt>
            <dd>{review.document_set_revision_id}</dd>
          </div>
          <div>
            <dt>Редакция документа</dt>
            <dd>{source.location.document_revision_id}</dd>
          </div>
          <div>
            <dt>SHA-256 исходника</dt>
            <dd className="hash-value">
              {source.location.original_object_hash}
            </dd>
          </div>
          <div>
            <dt>Точный локатор</dt>
            <dd>{locationLabel(source)}</dd>
          </div>
          <div>
            <dt>Приоритет источника</dt>
            <dd>{source.source_priority}</dd>
          </div>
          <div>
            <dt>Задача / версия</dt>
            <dd>
              {review.task_id} · {formatDateTime(review.task_updated_at)}
            </dd>
          </div>
          <div>
            <dt>Статус задачи</dt>
            <dd>
              <StatusPill value={review.task_status} compact />
            </dd>
          </div>
        </dl>
      </section>

      {blockers.length > 0 ? (
        <section className="decision-blocker" role="alert">
          <Icon name="shield" size={24} />
          <div>
            <h2>Экспертное решение недоступно</h2>
            <ul>
              {blockers.map((blocker) => (
                <li key={blocker}>{blockerLabels[blocker] ?? blocker}</li>
              ))}
            </ul>
          </div>
        </section>
      ) : (
        <form className="entry-form" onSubmit={submit}>
          <div className="entry-form__intro">
            <p className="eyebrow">Независимое решение</p>
            <h2>Зафиксировать результат сверки</h2>
            <p>
              Проверьте фактическое значение в исходном документе, его единицу и
              локатор. Удобство итоговой сметы не является доказательством.
            </p>
          </div>
          <fieldset className="decision-options">
            <legend>Решение</legend>
            {(
              [
                "CHANGES_REQUESTED",
                "REJECTED",
                "APPROVED",
              ] as ApprovalDecision[]
            ).map((decision) => (
              <label key={decision}>
                <input
                  type="radio"
                  name="manual-evidence-decision"
                  value={decision}
                  checked={draft.decision === decision}
                  onChange={() => change({ decision })}
                />
                <span>
                  <strong>{decisionLabels[decision]}</strong>
                  <small>
                    {decision === "APPROVED"
                      ? "Создаёт отдельное подтверждённое наблюдение с полной lineage."
                      : decision === "REJECTED"
                        ? "Фиксирует недопустимость значения без создания VERIFIED-записи."
                        : "Требует новой исправленной ручной записи и повторной проверки."}
                  </small>
                </span>
              </label>
            ))}
          </fieldset>
          <label className="decision-field">
            <span>Основание экспертного решения</span>
            <textarea
              rows={6}
              maxLength={4000}
              value={draft.reason}
              onChange={(event) => change({ reason: event.target.value })}
              placeholder="Опишите независимую сверку значения, единицы, редакции и локатора."
              required
            />
            <small>{draft.reason.length} / 4000</small>
          </label>
          <label className="decision-field decision-field--confirmation">
            <span>Введите шифр проекта: {project.code}</span>
            <input
              type="text"
              autoComplete="off"
              spellCheck={false}
              value={draft.projectCode}
              onChange={(event) => change({ projectCode: event.target.value })}
              required
            />
          </label>
          <label className="decision-acknowledgement">
            <input
              type="checkbox"
              checked={draft.acknowledged}
              onChange={(event) => {
                setDraft((current) => ({
                  ...current,
                  acknowledged: event.target.checked,
                }));
                setOperationKey(null);
                setFormError(null);
                mutation.reset();
              }}
            />
            <span>
              Я независимо открыл указанную редакцию, проверил SHA-256, локатор,
              значение и единицу. Я не являюсь автором этой записи.
            </span>
          </label>

          {(formError !== null || mutation.isError) && (
            <div className="decision-form__error" role="alert">
              <Icon name="warning" size={18} />
              <span>
                {formError ??
                  (mutation.error instanceof Error
                    ? mutation.error.message
                    : "Не удалось зафиксировать экспертное решение")}
              </span>
            </div>
          )}
          <div className="decision-form__actions">
            <Link
              className="button button--secondary"
              to={`/projects/${encodeURIComponent(projectId)}/EVIDENCE`}
            >
              Отмена
            </Link>
            <button
              className="button button--critical"
              type="submit"
              disabled={mutation.isPending}
            >
              {mutation.isPending
                ? "Фиксация решения…"
                : decisionLabels[draft.decision]}
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
