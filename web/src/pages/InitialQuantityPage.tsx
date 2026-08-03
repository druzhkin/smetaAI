import { useMemo, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  attachImportedQuantity,
  getInitialQuantityContext,
  getProject,
  newIdempotencyKey,
  type RequestContext,
} from "../api";
import { useAuth } from "../auth";
import { ErrorBlock, LoadingBlock } from "../components/Feedback";
import { Icon } from "../components/Icon";
import { StatusPill } from "../components/StatusPill";
import { formatDateTime } from "../format";
import { Link, useNavigation } from "../navigation";
import type { RuntimeConfig } from "../types";

export function InitialQuantityPage({
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
  const requestContext = useMemo<RequestContext>(
    () => ({
      apiBasePath: config.api_base_path,
      authorizationHeaders: auth.authorizationHeaders,
    }),
    [auth.authorizationHeaders, config.api_base_path],
  );
  const projectQuery = useQuery({
    queryKey: ["project", projectId],
    queryFn: ({ signal }) => getProject(requestContext, projectId, signal),
  });
  const quantityQuery = useQuery({
    queryKey: ["initial-quantity", projectId, lineId],
    queryFn: ({ signal }) =>
      getInitialQuantityContext(requestContext, projectId, lineId, signal),
  });
  const mutation = useMutation({
    mutationFn: (idempotencyKey: string) => {
      if (quantityQuery.data === undefined) {
        throw new Error("Контекст количества не загружен.");
      }
      const evidence = quantityQuery.data.evidence_candidates[0];
      if (!quantityQuery.data.recording_allowed || evidence === undefined) {
        throw new Error("Первичное количество заблокировано сервером.");
      }
      return attachImportedQuantity(requestContext, {
        projectId,
        lineId,
        sourceObservationId: evidence.observation.observation_id,
        expectedSourceObservationHash: evidence.observation_hash,
        expectedLineUpdatedAt: quantityQuery.data.line.updated_at,
        reason: reason.trim(),
        idempotencyKey,
      });
    },
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["initial-quantity", projectId, lineId],
        }),
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

  if (projectQuery.isError || quantityQuery.isError) {
    const failed = projectQuery.isError ? projectQuery : quantityQuery;
    return (
      <div className="page">
        <ErrorBlock
          error={failed.error}
          onRetry={() => {
            void projectQuery.refetch();
            void quantityQuery.refetch();
          }}
        />
      </div>
    );
  }
  if (projectQuery.isPending || quantityQuery.isPending) {
    return (
      <div className="page">
        <LoadingBlock label="Проверка доказательства количества и политики" />
      </div>
    );
  }

  const project = projectQuery.data;
  const quantity = quantityQuery.data;
  const evidence = quantity.evidence_candidates[0];
  const validation = (() => {
    if (!quantity.recording_allowed) {
      return `BLOCKED: ${quantity.recording_blockers.join(", ")}`;
    }
    if (evidence === undefined || quantity.evidence_candidates.length !== 1) {
      return "Требуется ровно одно проверенное доказательство количества.";
    }
    if (reason.trim().length < 10) {
      return "Опишите основание прикрепления не короче 10 символов.";
    }
    if (projectCodeConfirmation !== project.code) {
      return "Введите точный код проекта.";
    }
    if (!acknowledged) {
      return "Подтвердите использование неизменяемого серверного значения.";
    }
    return null;
  })();
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
    const key = operationKey ?? newIdempotencyKey();
    setOperationKey(key);
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
        <span>Первичное количество</span>
      </nav>

      <header className="entry-header">
        <div>
          <p className="eyebrow">ВОР · проверенное количество</p>
          <h1>{quantity.line.description}</h1>
          <p>
            Число, единица и источник получены с сервера. Браузер передаёт
            только идентификатор доказательства и подтверждение оператора.
          </p>
        </div>
        <div className="entry-header__guard">
          <Icon name="shield" size={22} />
          <span>
            Критичное количество или отсутствие независимого покрытия блокирует
            операцию.
          </span>
        </div>
      </header>

      <section className="review-ledger">
        <div className="review-ledger__heading">
          <div>
            <p className="eyebrow">Строка {quantity.line.line_key}</p>
            <h2>
              {quantity.line.work_code} · {quantity.line.unit}
            </h2>
          </div>
          <StatusPill value={quantity.line.status} />
        </div>
        <dl className="task-facts">
          <div>
            <dt>Источник</dt>
            <dd>{quantity.source_item_id ?? "не связан"}</dd>
          </div>
          <div>
            <dt>Политика количества</dt>
            <dd>{quantity.quantity_policy_version_id ?? "отсутствует"}</dd>
          </div>
          <div>
            <dt>Версия строки</dt>
            <dd>{formatDateTime(quantity.line.updated_at)}</dd>
          </div>
          <div>
            <dt>Текущее количество</dt>
            <dd>
              {quantity.current_quantity_id === null
                ? "не записано"
                : `${quantity.current_quantity_id} · ${quantity.current_quantity_status}`}
            </dd>
          </div>
        </dl>
        {evidence !== undefined && (
          <div className="compact-ledger">
            <div>
              <strong>
                {evidence.value} {evidence.unit}
              </strong>
              <span>{evidence.observation.observation_id}</span>
              <small>{evidence.observation.location.locator}</small>
            </div>
          </div>
        )}
      </section>

      {quantity.recording_blockers.length > 0 && (
        <section className="blocker-panel" role="alert">
          <Icon name="warning" size={20} />
          <div>
            <strong>Количество нельзя прикрепить</strong>
            <ul>
              {quantity.recording_blockers.map((blocker) => (
                <li key={blocker}>{blocker}</li>
              ))}
            </ul>
          </div>
        </section>
      )}

      <form className="entry-form controlled-form" onSubmit={submit}>
        <section className="entry-form__intro">
          <p className="eyebrow">Фиксация первичного количества</p>
          <h2>Использовать проверенное серверное значение</h2>
        </section>
        <div className="form-grid">
          <label className="field form-grid__wide">
            <span>Основание прикрепления</span>
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
              Подтверждаю использование точного проверенного значения без
              ручного изменения числа, единицы, округления или коэффициента.
            </span>
          </label>
        </div>
        {(formError !== null || mutation.isError) && (
          <div className="inline-error" role="alert">
            {formError ??
              (mutation.error instanceof Error
                ? mutation.error.message
                : "Количество не прикреплено.")}
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
            {mutation.isPending ? "Фиксация…" : "Прикрепить количество"}
          </button>
        </div>
      </form>
    </div>
  );
}
