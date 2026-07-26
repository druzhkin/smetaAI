import { useMemo, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  finalizeNomenclatureAnalogue,
  getNomenclatureReview,
  getProject,
  newIdempotencyKey,
  proposeNomenclatureAnalogue,
  type RequestContext,
} from "../api";
import { useAuth } from "../auth";
import { ErrorBlock, LoadingBlock } from "../components/Feedback";
import { Icon } from "../components/Icon";
import { StatusPill } from "../components/StatusPill";
import {
  validateAnalogueFinalization,
  validateAnalogueProposal,
  type AnalogueClass,
} from "../controlledWorkflows";
import { displayValue } from "../format";
import { Link, useNavigation } from "../navigation";
import type { RuntimeConfig } from "../types";

export function NomenclatureReviewPage({
  config,
  projectId,
  matchId,
}: {
  config: RuntimeConfig;
  projectId: string;
  matchId: string;
}) {
  const auth = useAuth();
  const queryClient = useQueryClient();
  const { navigate } = useNavigation();
  const [analogueClass, setAnalogueClass] = useState<AnalogueClass>(
    "FUNCTIONAL_ANALOGUE",
  );
  const [reason, setReason] = useState("");
  const [projectCodeConfirmation, setProjectCodeConfirmation] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [operationKey, setOperationKey] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
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
    queryKey: ["nomenclature-review", projectId, matchId],
    queryFn: ({ signal }) =>
      getNomenclatureReview(context, projectId, matchId, signal),
  });
  const mutation = useMutation({
    mutationFn: (input: {
      action: "PROPOSE" | "FINALIZE";
      idempotencyKey: string;
    }) => {
      if (projectQuery.data === undefined || reviewQuery.data === undefined) {
        throw new Error("Контекст номенклатурного решения не загружен.");
      }
      const attestation = {
        reason,
        projectCodeConfirmation,
        acknowledged,
      };
      if (input.action === "PROPOSE") {
        const validation = validateAnalogueProposal(
          { ...attestation, analogueClass },
          reviewQuery.data,
          projectQuery.data.code,
        );
        if (validation !== null) {
          throw new Error(validation);
        }
        return proposeNomenclatureAnalogue(context, {
          projectId,
          matchId,
          analogueClass,
          reason: reason.trim(),
          idempotencyKey: input.idempotencyKey,
        });
      }
      const validation = validateAnalogueFinalization(
        attestation,
        reviewQuery.data,
        projectQuery.data.code,
      );
      if (validation !== null) {
        throw new Error(validation);
      }
      return finalizeNomenclatureAnalogue(context, {
        projectId,
        matchId,
        reason: reason.trim(),
        idempotencyKey: input.idempotencyKey,
      });
    },
    onSuccess: async (result, input) => {
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["records", projectId, "BOQ_SCOPE"],
        }),
        queryClient.invalidateQueries({ queryKey: ["work-items"] }),
        queryClient.invalidateQueries({
          queryKey: ["workbench", projectId],
        }),
      ]);
      if (input.action === "PROPOSE") {
        navigate(
          `/projects/${encodeURIComponent(projectId)}/nomenclature/${encodeURIComponent(result.match.match_id)}/review`,
          { replace: true },
        );
        setReason("");
        setProjectCodeConfirmation("");
        setAcknowledged(false);
        setOperationKey(null);
        await queryClient.invalidateQueries({
          queryKey: ["nomenclature-review", projectId, result.match.match_id],
        });
      } else {
        navigate(
          `/projects/${encodeURIComponent(projectId)}/pricing/items/${encodeURIComponent(result.match.source_item_id)}`,
          { replace: true },
        );
      }
    },
  });

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
  if (projectQuery.isPending || reviewQuery.isPending) {
    return (
      <div className="page">
        <LoadingBlock label="Загрузка критических атрибутов и согласований" />
      </div>
    );
  }
  const project = projectQuery.data;
  const review = reviewQuery.data;
  const match = review.match.match;
  const isProposal =
    review.match.status !== "IN_REVIEW" &&
    match.match_class === "TECHNICALLY_UNACCEPTABLE";
  const isFinalization = review.match.status === "IN_REVIEW";
  const validation = isProposal
    ? validateAnalogueProposal(
        {
          analogueClass,
          reason,
          projectCodeConfirmation,
          acknowledged,
        },
        review,
        project.code,
      )
    : isFinalization
      ? validateAnalogueFinalization(
          { reason, projectCodeConfirmation, acknowledged },
          review,
          project.code,
        )
      : "Для текущего класса нет ручного действия.";
  const reset = () => {
    setAcknowledged(false);
    setOperationKey(null);
    setFormError(null);
    mutation.reset();
  };
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setFormError(validation);
    if (validation !== null) {
      return;
    }
    let key = operationKey;
    if (key === null) {
      key = newIdempotencyKey();
      setOperationKey(key);
    }
    mutation.mutate({
      action: isProposal ? "PROPOSE" : "FINALIZE",
      idempotencyKey: key,
    });
  };
  const attributes = Array.from(
    new Set([
      ...match.required_critical_attributes,
      ...Object.keys(match.source_attributes),
      ...Object.keys(match.canonical_attributes),
    ]),
  ).sort();

  return (
    <div className="page controlled-workflow-page">
      <nav className="breadcrumbs" aria-label="Навигационная цепочка">
        <Link to="/">Проекты</Link>
        <span>/</span>
        <Link to={`/projects/${encodeURIComponent(projectId)}`}>
          {project.code}
        </Link>
        <span>/</span>
        <Link to={`/projects/${encodeURIComponent(projectId)}/BOQ_SCOPE`}>
          Номенклатура
        </Link>
        <span>/</span>
        <span>{match.source_item_id}</span>
      </nav>

      <header className="entry-header">
        <div>
          <p className="eyebrow">Техническая классификация</p>
          <h1>{match.source_item_id}</h1>
          <p>
            Каноническая позиция: {match.canonical_item_id ?? "не определена"}.
            Решение хранит полный набор исходных и каталожных атрибутов.
          </p>
        </div>
        <div className="entry-header__guard">
          <Icon name="shield" size={22} />
          <span>
            <StatusPill value={match.match_class} compact /> Каталог{" "}
            {review.match.catalog_version_id}
          </span>
        </div>
      </header>

      <section className="review-ledger">
        <div className="review-ledger__heading">
          <div>
            <p className="eyebrow">Детерминированная матрица</p>
            <h2>Критические атрибуты</h2>
          </div>
          <StatusPill value={review.match.status} />
        </div>
        <div
          className="attribute-table"
          role="table"
          aria-label="Сравнение критических атрибутов"
        >
          <div className="attribute-table__head" role="row">
            <span role="columnheader">Атрибут</span>
            <span role="columnheader">Источник</span>
            <span role="columnheader">Каталог</span>
            <span role="columnheader">Результат</span>
          </div>
          {attributes.map((attribute) => {
            const source = match.source_attributes[attribute] ?? null;
            const canonical = match.canonical_attributes[attribute] ?? null;
            const missing = match.missing_attributes.includes(attribute);
            const mismatched = match.mismatched_attributes.includes(attribute);
            return (
              <div key={attribute} role="row">
                <strong role="rowheader" data-label="Атрибут">
                  {attribute}
                </strong>
                <span role="cell" data-label="Источник">
                  {source ?? "нет данных"}
                </span>
                <span role="cell" data-label="Каталог">
                  {canonical ?? "нет данных"}
                </span>
                <span
                  role="cell"
                  data-label="Результат"
                  className={
                    missing || mismatched ? "danger-text" : "positive-text"
                  }
                >
                  {missing ? "MISSING" : mismatched ? "MISMATCH" : "EXACT"}
                </span>
              </div>
            );
          })}
        </div>
        <div className="source-proof">
          <span>Исходное наблюдение</span>
          <strong>{review.source_attributes_observation_id}</strong>
          <code>{displayValue(review.source_observation.value)}</code>
          <small>{review.source_observation.location.locator}</small>
        </div>
      </section>

      {review.approval_task_statuses &&
        Object.keys(review.approval_task_statuses).length > 0 && (
          <section className="review-ledger">
            <div className="review-ledger__heading">
              <div>
                <p className="eyebrow">Четыре глаза</p>
                <h2>Согласования аналога</h2>
              </div>
              <span>{review.equivalence_rule_version_id}</span>
            </div>
            <div className="compact-ledger">
              {Object.entries(review.approval_task_statuses).map(
                ([taskId, status]) => (
                  <div key={taskId}>
                    <strong>{taskId}</strong>
                    <StatusPill value={status} compact />
                    <Link to={`/tasks/${encodeURIComponent(taskId)}`}>
                      Открыть задачу
                    </Link>
                  </div>
                ),
              )}
            </div>
          </section>
        )}

      {match.match_class === "EXACT" && review.match.status === "VERIFIED" && (
        <section className="workflow-result">
          <Icon name="check" size={20} />
          <div>
            <strong>Точное соответствие подтверждено</strong>
            <p>Ручное решение не требуется. Можно переходить к ценам.</p>
          </div>
          <Link
            className="button button--primary"
            to={`/projects/${encodeURIComponent(projectId)}/pricing/items/${encodeURIComponent(match.source_item_id)}`}
          >
            Ценовой контур
          </Link>
        </section>
      )}

      {match.match_class === "INSUFFICIENT_DATA" && (
        <section className="blocker-panel" role="alert">
          <Icon name="warning" size={20} />
          <div>
            <strong>Недостаточно критических атрибутов</strong>
            <p>
              Аналог не может быть предложен. Требуется новое проверенное
              доказательство атрибутов.
            </p>
          </div>
        </section>
      )}

      {(isProposal || isFinalization) && (
        <form className="entry-form controlled-form" onSubmit={submit}>
          <section className="entry-form__intro">
            <p className="eyebrow">
              {isProposal ? "Предложение аналога" : "Финализация"}
            </p>
            <h2>
              {isProposal
                ? "Классифицировать допустимость замены"
                : "Зафиксировать завершённые согласования"}
            </h2>
          </section>
          <div className="form-grid">
            {isProposal && (
              <label className="field">
                <span>Класс аналога</span>
                <select
                  value={analogueClass}
                  onChange={(event) => {
                    setAnalogueClass(event.target.value as AnalogueClass);
                    reset();
                  }}
                >
                  <option value="FUNCTIONAL_ANALOGUE">
                    FUNCTIONAL_ANALOGUE
                  </option>
                  <option value="CONDITIONALLY_ACCEPTABLE_ANALOGUE">
                    CONDITIONALLY_ACCEPTABLE_ANALOGUE
                  </option>
                </select>
              </label>
            )}
            <label className="field form-grid__wide">
              <span>
                {isProposal
                  ? "Техническое основание предложения"
                  : "Основание финализации"}
              </span>
              <textarea
                rows={4}
                maxLength={2000}
                value={reason}
                onChange={(event) => {
                  setReason(event.target.value);
                  reset();
                }}
              />
            </label>
            <label className="field">
              <span>Точный код проекта</span>
              <input
                value={projectCodeConfirmation}
                onChange={(event) => {
                  setProjectCodeConfirmation(event.target.value);
                  reset();
                }}
                autoComplete="off"
              />
            </label>
            <label className="attestation form-grid__wide">
              <input
                type="checkbox"
                checked={acknowledged}
                onChange={(event) => setAcknowledged(event.target.checked)}
              />
              <span>
                {isProposal
                  ? "Подтверждаю, что каждый mismatch разрешён утверждённым equivalence rule; решение всё равно требует независимого согласования."
                  : "Подтверждаю наличие всех обязательных независимых approval records и неизменность технической основы."}
              </span>
            </label>
          </div>
          {isFinalization && review.finalization_blockers.length > 0 && (
            <div className="inline-warning">
              {review.finalization_blockers.join(", ")}
            </div>
          )}
          {(formError !== null || mutation.isError) && (
            <div className="inline-error" role="alert">
              {formError ??
                (mutation.error instanceof Error
                  ? mutation.error.message
                  : "Решение не сохранено.")}
            </div>
          )}
          <div className="form-actions">
            <Link
              className="button button--secondary"
              to={`/projects/${encodeURIComponent(projectId)}/BOQ_SCOPE`}
            >
              Назад
            </Link>
            <button
              className="button button--primary"
              type="submit"
              disabled={validation !== null || mutation.isPending}
            >
              <Icon name={isProposal ? "arrow" : "check"} size={16} />
              {mutation.isPending
                ? "Фиксация…"
                : isProposal
                  ? "Создать задачи согласования"
                  : "Финализировать аналог"}
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
