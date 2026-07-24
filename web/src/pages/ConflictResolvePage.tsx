import { useMemo, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  getConflictReview,
  getProject,
  newIdempotencyKey,
  resolveConflict,
  type RequestContext,
} from "../api";
import { useAuth } from "../auth";
import {
  validateConflictResolutionDraft,
  type ConflictResolutionDraft,
} from "../conflict";
import { ErrorBlock, LoadingBlock } from "../components/Feedback";
import { Icon } from "../components/Icon";
import { StatusPill } from "../components/StatusPill";
import { formatDateTime } from "../format";
import { Link, useNavigation } from "../navigation";
import type { EvidenceObservation, RuntimeConfig } from "../types";

const blockerLabels: Record<string, string> = {
  CONFLICT_NOT_OPEN:
    "Конфликт уже разрешён или больше не находится в открытом состоянии.",
  CONFLICT_OBSERVATION_MISSING:
    "Одно или несколько исходных наблюдений отсутствуют. Решение заблокировано.",
  CONFLICT_OBSERVATION_FIELD_MISMATCH:
    "Поле исходного наблюдения не совпадает с полем конфликта.",
  CONFLICT_INDEPENDENCE_INVALID:
    "Квалификации или независимые домены исходных наблюдений больше не действительны.",
  CONFLICT_COMMERCIAL_BASIS_INVALID:
    "Коммерческий базис одного из исходных наблюдений повреждён или не согласован со значением и единицей.",
  CONFLICT_TASK_MISSING:
    "Обязательная задача разрешения конфликта отсутствует.",
  CONFLICT_STATE_MISMATCH:
    "Статус записи конфликта не совпадает с его неизменяемым payload.",
  CONFLICT_TASK_NOT_REQUIRED:
    "Задача конфликта ошибочно не отмечена обязательной.",
  CONFLICT_TASK_SCOPE_MISMATCH:
    "Состав наблюдений задачи не совпадает с составом конфликта.",
  CONFLICT_TASK_CREATOR_MISSING:
    "У обязательной задачи отсутствует проверяемый создатель.",
  TASK_NOT_PENDING: "Обязательная задача уже завершена.",
  FOUR_EYES_TASK_CREATOR:
    "Инициатор сверки, создавшей конфликт, не может разрешить его собственную задачу.",
  NO_INDEPENDENT_OBSERVATION:
    "Нет исходного наблюдения, независимого от текущего пользователя.",
};

const initialDraft: ConflictResolutionDraft = {
  selectedObservationId: "",
  reason: "",
  projectCode: "",
  acknowledged: false,
};

function formatEvidenceValue(value: unknown): string {
  if (typeof value === "string") {
    return value;
  }
  if (
    value === null ||
    typeof value === "number" ||
    typeof value === "boolean"
  ) {
    return String(value);
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return "[значение не может быть отображено]";
  }
}

function observationLocation(observation: EvidenceObservation): string {
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

export function ConflictResolvePage({
  config,
  projectId,
  conflictId,
}: {
  config: RuntimeConfig;
  projectId: string;
  conflictId: string;
}) {
  const auth = useAuth();
  const queryClient = useQueryClient();
  const { navigate } = useNavigation();
  const [draft, setDraft] = useState<ConflictResolutionDraft>(initialDraft);
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
  const conflictQuery = useQuery({
    queryKey: ["conflict", projectId, conflictId],
    queryFn: ({ signal }) =>
      getConflictReview(context, projectId, conflictId, signal),
  });
  const mutation = useMutation({
    mutationFn: (input: {
      selectedObservationId: string;
      reason: string;
      expectedConflictUpdatedAt: string;
      expectedTaskUpdatedAt: string;
      idempotencyKey: string;
    }) =>
      resolveConflict(context, {
        projectId,
        conflictId,
        selectedObservationId: input.selectedObservationId,
        resolutionReason: input.reason,
        expectedConflictUpdatedAt: input.expectedConflictUpdatedAt,
        expectedTaskUpdatedAt: input.expectedTaskUpdatedAt,
        idempotencyKey: input.idempotencyKey,
      }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["conflict", projectId, conflictId],
        }),
        queryClient.invalidateQueries({ queryKey: ["work-items"] }),
        queryClient.invalidateQueries({ queryKey: ["workbench", projectId] }),
        queryClient.invalidateQueries({
          queryKey: ["records", projectId, "EVIDENCE"],
        }),
        queryClient.invalidateQueries({
          queryKey: ["records", projectId, "APPROVALS"],
        }),
      ]);
      navigate(`/projects/${encodeURIComponent(projectId)}/EVIDENCE`, {
        replace: true,
      });
    },
  });

  if (projectQuery.isPending || conflictQuery.isPending) {
    return (
      <div className="page">
        <LoadingBlock label="Загрузка конфликта и исходных наблюдений" />
      </div>
    );
  }
  if (projectQuery.isError || conflictQuery.isError) {
    const failed = projectQuery.isError ? projectQuery : conflictQuery;
    return (
      <div className="page">
        <ErrorBlock
          error={failed.error}
          onRetry={() => {
            void projectQuery.refetch();
            void conflictQuery.refetch();
          }}
        />
      </div>
    );
  }

  const project = projectQuery.data;
  const review = conflictQuery.data;
  const selectedObservation = review.observations.find(
    (observation) => observation.observation_id === draft.selectedObservationId,
  );
  const decisionBlockers = [
    ...review.resolution_blockers,
    ...(auth.actorId === null ? ["ACTOR_ID_MISSING"] : []),
    ...(review.task_updated_at === null ? ["TASK_VERSION_MISSING"] : []),
  ];
  const validationError = validateConflictResolutionDraft(
    draft,
    project.code,
    selectedObservation?.actor_id ?? null,
    auth.actorId,
  );

  const change = (patch: Partial<ConflictResolutionDraft>) => {
    setDraft((current) => ({ ...current, ...patch, acknowledged: false }));
    setOperationKey(null);
    setFormError(null);
    mutation.reset();
  };
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (decisionBlockers.length > 0) {
      setFormError(
        blockerLabels[decisionBlockers[0] ?? ""] ??
          "Разрешение конфликта заблокировано",
      );
      return;
    }
    setFormError(validationError);
    if (validationError !== null || review.task_updated_at === null) {
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
      selectedObservationId: draft.selectedObservationId,
      reason: draft.reason.trim(),
      expectedConflictUpdatedAt: review.conflict_updated_at,
      expectedTaskUpdatedAt: review.task_updated_at,
      idempotencyKey: key,
    });
  };

  return (
    <div className="page">
      <nav className="breadcrumbs" aria-label="Навигационная цепочка">
        <Link to="/">Проекты</Link>
        <span>/</span>
        <Link to={`/projects/${encodeURIComponent(projectId)}`}>
          {project.code}
        </Link>
        <span>/</span>
        <Link to={`/projects/${encodeURIComponent(projectId)}/EVIDENCE`}>
          Доказательства
        </Link>
        <span>/</span>
        <span>Разрешение конфликта</span>
      </nav>

      <header className="entry-header">
        <div>
          <p className="eyebrow">{project.code} · независимая проверка</p>
          <h1>{review.conflict.field_name}</h1>
          <p>
            Значения источников не объединяются автоматически. Выбор создаст
            новое подтверждённое наблюдение и закроет обязательную задачу.
          </p>
        </div>
        <div className="entry-header__guard">
          <Icon name="shield" size={22} />
          <span>
            Сервер повторно сверит версии конфликта и задачи, принадлежность
            наблюдения и независимость проверяющего.
          </span>
        </div>
      </header>

      <section className="conflict-summary">
        <div className="conflict-summary__header">
          <div>
            <p className="eyebrow">Conflict</p>
            <h2>{review.conflict.conflict_id}</h2>
            <p>{review.conflict.reason}</p>
          </div>
          <StatusPill value={review.conflict.status} />
        </div>
        <dl className="task-facts">
          <div>
            <dt>Версия конфликта</dt>
            <dd>{formatDateTime(review.conflict_updated_at)}</dd>
          </div>
          <div>
            <dt>Обязательная задача</dt>
            <dd>{review.task_id}</dd>
          </div>
          <div>
            <dt>Создатель задачи</dt>
            <dd>{review.task_created_by ?? "Не указан"}</dd>
          </div>
          <div>
            <dt>Статус задачи</dt>
            <dd>
              <StatusPill value={review.task_status} compact />
            </dd>
          </div>
          <div>
            <dt>Обязательная</dt>
            <dd>{review.task_required === true ? "Да" : "Нет"}</dd>
          </div>
        </dl>
        {review.conflict.resolved_by !== null && (
          <div className="conflict-resolution-result">
            <strong>Разрешено: {review.conflict.resolved_by}</strong>
            <span>{review.conflict.resolution_reason}</span>
            <pre>{formatEvidenceValue(review.conflict.resolved_value)}</pre>
          </div>
        )}
      </section>

      <section
        className="conflict-observations"
        aria-labelledby="conflict-observations-title"
      >
        <div className="section-heading">
          <div>
            <p className="eyebrow">Независимые источники</p>
            <h2 id="conflict-observations-title">
              Сравнить исходные наблюдения
            </h2>
          </div>
          <span>{review.observations.length}</span>
        </div>
        <div className="conflict-observation-grid">
          {review.observations.map((observation, index) => {
            const ownSource = observation.actor_id === auth.actorId;
            const selected =
              observation.observation_id === draft.selectedObservationId;
            return (
              <label
                className={`conflict-observation-card ${
                  selected ? "is-selected" : ""
                } ${ownSource ? "is-disabled" : ""}`}
                key={observation.observation_id}
              >
                <div className="conflict-observation-card__choice">
                  <input
                    type="radio"
                    name="selected-observation"
                    value={observation.observation_id}
                    checked={selected}
                    disabled={ownSource || decisionBlockers.length > 0}
                    onChange={() =>
                      change({
                        selectedObservationId: observation.observation_id,
                      })
                    }
                  />
                  <span>Источник {index + 1}</span>
                  {ownSource && <small>ваше наблюдение — выбор запрещён</small>}
                </div>
                <pre className="conflict-observation-card__value">
                  {formatEvidenceValue(observation.value)}
                  {observation.unit === null ? "" : ` ${observation.unit}`}
                </pre>
                <dl>
                  <div>
                    <dt>Метод</dt>
                    <dd>
                      {observation.method} · {observation.method_version}
                    </dd>
                  </div>
                  <div>
                    <dt>Источник/actor</dt>
                    <dd>{observation.actor_id}</dd>
                  </div>
                  <div>
                    <dt>Квалификация</dt>
                    <dd>
                      {observation.adapter_qualification_id ?? "Не указана"}
                      {observation.adapter_qualification_status === null
                        ? ""
                        : ` · ${observation.adapter_qualification_status}`}
                      {observation.adapter_qualification_valid_until === null
                        ? ""
                        : ` · до ${observation.adapter_qualification_valid_until}`}
                    </dd>
                  </div>
                  <div>
                    <dt>Домен независимости</dt>
                    <dd>{observation.independence_domain ?? "Не указан"}</dd>
                  </div>
                  <div>
                    <dt>Коммерческий базис</dt>
                    <dd>{formatEvidenceValue(observation.basis_metadata)}</dd>
                  </div>
                  <div>
                    <dt>Наблюдалось</dt>
                    <dd>{formatDateTime(observation.observed_at)}</dd>
                  </div>
                  <div>
                    <dt>Локатор</dt>
                    <dd>{observationLocation(observation)}</dd>
                  </div>
                  <div>
                    <dt>Редакция документа</dt>
                    <dd>{observation.location.document_revision_id}</dd>
                  </div>
                  <div>
                    <dt>SHA-256 исходника</dt>
                    <dd className="hash-value">
                      {observation.location.original_object_hash}
                    </dd>
                  </div>
                </dl>
                <code>{observation.observation_id}</code>
              </label>
            );
          })}
        </div>
      </section>

      {decisionBlockers.length > 0 ? (
        <section className="decision-blocker" role="alert">
          <Icon name="shield" size={24} />
          <div>
            <h2>Разрешение недоступно</h2>
            <ul>
              {decisionBlockers.map((blocker) => (
                <li key={blocker}>
                  {blockerLabels[blocker] ??
                    (blocker === "ACTOR_ID_MISSING"
                      ? "Identity-сессия не содержит проверяемый actor ID."
                      : blocker === "TASK_VERSION_MISSING"
                        ? "У обязательной задачи отсутствует версия."
                        : blocker)}
                </li>
              ))}
            </ul>
          </div>
        </section>
      ) : (
        <form className="entry-form" onSubmit={submit}>
          <div className="entry-form__intro">
            <p className="eyebrow">Контролируемое решение</p>
            <h2>Зафиксировать выбранное значение</h2>
            <p>
              Основание должно объяснять техническую проверку источников, а не
              только повторять выбранное значение.
            </p>
          </div>
          <label className="decision-field">
            <span>Основание разрешения конфликта</span>
            <textarea
              value={draft.reason}
              maxLength={2000}
              rows={5}
              required
              onChange={(event) => change({ reason: event.target.value })}
              placeholder="Опишите сверку исходных страниц, таблиц, единиц и причин выбора."
            />
            <small>{draft.reason.length} / 2000</small>
          </label>
          <label className="decision-field decision-field--confirmation">
            <span>Введите шифр проекта: {project.code}</span>
            <input
              type="text"
              value={draft.projectCode}
              autoComplete="off"
              spellCheck={false}
              required
              onChange={(event) => change({ projectCode: event.target.value })}
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
              Я независимо проверил значения, единицы и точные места в исходных
              документах. Выбор не основан только на confidence, сходстве текста
              или удобстве итоговой цены.
            </span>
          </label>

          {(formError !== null || mutation.isError) && (
            <div className="decision-form__error" role="alert">
              <Icon name="warning" size={18} />
              <span>
                {formError ??
                  (mutation.error instanceof Error
                    ? mutation.error.message
                    : "Не удалось разрешить конфликт")}
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
                : "Выбрать значение и закрыть конфликт"}
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
