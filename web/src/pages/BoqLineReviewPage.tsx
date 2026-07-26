import { useMemo, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  getBoqLineReview,
  getProject,
  newIdempotencyKey,
  verifyBoqLine,
  type RequestContext,
} from "../api";
import { useAuth } from "../auth";
import { ErrorBlock, LoadingBlock } from "../components/Feedback";
import { Icon } from "../components/Icon";
import { StatusPill } from "../components/StatusPill";
import { validateBoqVerification } from "../controlledWorkflows";
import { displayValue, formatDateTime } from "../format";
import { Link, useNavigation } from "../navigation";
import type { RuntimeConfig } from "../types";

export function BoqLineReviewPage({
  config,
  projectId,
  lineId,
}: {
  config: RuntimeConfig;
  projectId: string;
  lineId: string;
}) {
  const auth = useAuth();
  const queryClient = useQueryClient();
  const { navigate } = useNavigation();
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
    queryKey: ["boq-line-review", projectId, lineId],
    queryFn: ({ signal }) =>
      getBoqLineReview(context, projectId, lineId, signal),
  });
  const mutation = useMutation({
    mutationFn: (idempotencyKey: string) => {
      if (projectQuery.data === undefined || reviewQuery.data === undefined) {
        throw new Error("Контекст технической проверки не загружен.");
      }
      const validation = validateBoqVerification(
        { reason, projectCodeConfirmation, acknowledged },
        reviewQuery.data,
        projectQuery.data.code,
      );
      if (validation !== null) {
        throw new Error(validation);
      }
      return verifyBoqLine(context, {
        projectId,
        lineId,
        expectedLineUpdatedAt: reviewQuery.data.line.updated_at,
        reason: reason.trim(),
        idempotencyKey,
      });
    },
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["records", projectId, "BOQ_SCOPE"],
        }),
        queryClient.invalidateQueries({
          queryKey: ["workbench", projectId],
        }),
      ]);
      navigate(`/projects/${encodeURIComponent(projectId)}/BOQ_SCOPE`, {
        replace: true,
      });
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
        <LoadingBlock label="Загрузка неизменяемой строки и её доказательств" />
      </div>
    );
  }

  const project = projectQuery.data;
  const review = reviewQuery.data;
  const validation = validateBoqVerification(
    { reason, projectCodeConfirmation, acknowledged },
    review,
    project.code,
  );
  const resetDecision = () => {
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
    mutation.mutate(key);
  };

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
          BoQ и состав работ
        </Link>
        <span>/</span>
        <span>Проверка строки</span>
      </nav>

      <header className="entry-header">
        <div>
          <p className="eyebrow">Четыре глаза · структура BoQ</p>
          <h1>{review.line.description}</h1>
          <p>
            Проверяющий подтверждает код работы, единицу, WBS, полный план
            компонентов и точные источники. Значения строки на этом экране
            неизменяемы.
          </p>
        </div>
        <div className="entry-header__guard">
          <Icon name="shield" size={22} />
          <span>
            Автор: {review.created_by}. Проверка тем же пользователем
            блокируется сервером.
          </span>
        </div>
      </header>

      <section className="review-ledger">
        <div className="review-ledger__heading">
          <div>
            <p className="eyebrow">Строка {review.line.line_key}</p>
            <h2>
              {review.line.work_code} · {review.line.unit}
            </h2>
          </div>
          <StatusPill value={review.line.status} />
        </div>
        <dl className="task-facts">
          <div>
            <dt>WBS</dt>
            <dd>{review.line.wbs_node_id}</dd>
          </div>
          <div>
            <dt>Комплект документов</dt>
            <dd>{review.document_set_revision_id}</dd>
          </div>
          <div>
            <dt>Версия строки</dt>
            <dd>{formatDateTime(review.line.updated_at)}</dd>
          </div>
          <div>
            <dt>Критичное количество</dt>
            <dd>{review.line.critical_quantity ? "Да" : "Нет"}</dd>
          </div>
        </dl>

        <div className="review-columns">
          <section>
            <p className="eyebrow">Компоненты стоимости</p>
            <div className="compact-ledger">
              {review.line.cost_components.map((component) => (
                <div key={component.semantic_key}>
                  <strong>{component.semantic_key}</strong>
                  <span>
                    {component.category} · {component.basis_kind} · знак{" "}
                    {component.sign}
                  </span>
                  <small>
                    Факторы:{" "}
                    {component.factor_ids.length > 0
                      ? component.factor_ids.join(", ")
                      : "нет"}
                  </small>
                </div>
              ))}
            </div>
          </section>
          <section>
            <p className="eyebrow">Поддерживающие наблюдения</p>
            <div className="compact-ledger">
              {review.evidence_observations.map((observation) => (
                <div key={observation.observation_id}>
                  <strong>{observation.observation_id}</strong>
                  <code>{displayValue(observation.value)}</code>
                  <small>
                    {observation.method} · {observation.location.locator}
                  </small>
                </div>
              ))}
            </div>
          </section>
        </div>
      </section>

      {review.verification_blockers.length > 0 && (
        <section className="blocker-panel" role="alert">
          <Icon name="warning" size={20} />
          <div>
            <strong>Строка не может быть подтверждена</strong>
            <ul>
              {review.verification_blockers.map((blocker) => (
                <li key={blocker}>{blocker}</li>
              ))}
            </ul>
          </div>
        </section>
      )}

      <form className="entry-form controlled-form" onSubmit={submit}>
        <section className="entry-form__intro">
          <p className="eyebrow">Решение проверяющего</p>
          <h2>Подтвердить точную ревизию строки</h2>
        </section>
        <div className="form-grid">
          <label className="field form-grid__wide">
            <span>Основание технической проверки</span>
            <textarea
              rows={4}
              maxLength={2000}
              value={reason}
              onChange={(event) => {
                setReason(event.target.value);
                resetDecision();
              }}
            />
          </label>
          <label className="field">
            <span>Точный код проекта</span>
            <input
              value={projectCodeConfirmation}
              onChange={(event) => {
                setProjectCodeConfirmation(event.target.value);
                resetDecision();
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
              Подтверждаю независимую проверку источников, WBS, единицы и
              полного ожидаемого состава компонентов стоимости.
            </span>
          </label>
        </div>
        {(formError !== null || mutation.isError) && (
          <div className="inline-error" role="alert">
            {formError ??
              (mutation.error instanceof Error
                ? mutation.error.message
                : "Строка не подтверждена.")}
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
            <Icon name="check" size={16} />
            {mutation.isPending ? "Проверка…" : "Подтвердить строку"}
          </button>
        </div>
      </form>
    </div>
  );
}
