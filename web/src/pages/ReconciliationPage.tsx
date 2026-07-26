import { useMemo, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  getProject,
  getReconciliationContext,
  newIdempotencyKey,
  reconcileEvidence,
  type RequestContext,
} from "../api";
import { useAuth } from "../auth";
import { ErrorBlock, LoadingBlock } from "../components/Feedback";
import { Icon } from "../components/Icon";
import { StatusPill } from "../components/StatusPill";
import {
  validateReconciliation,
  type ReconciliationDraft,
} from "../controlledWorkflows";
import { displayValue, formatDateTime } from "../format";
import { Link } from "../navigation";
import type { RuntimeConfig } from "../types";

export function ReconciliationPage({
  config,
  projectId,
}: {
  config: RuntimeConfig;
  projectId: string;
}) {
  const auth = useAuth();
  const queryClient = useQueryClient();
  const [fieldName, setFieldName] = useState("");
  const [draft, setDraft] = useState<ReconciliationDraft>({
    observationIds: [],
    reason: "",
    projectCodeConfirmation: "",
    acknowledged: false,
  });
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
  const reconciliationQuery = useQuery({
    queryKey: ["reconciliation-context", projectId, fieldName],
    queryFn: ({ signal }) =>
      getReconciliationContext(
        context,
        projectId,
        fieldName || undefined,
        signal,
      ),
  });
  const mutation = useMutation({
    mutationFn: (idempotencyKey: string) => {
      if (
        projectQuery.data === undefined ||
        reconciliationQuery.data === undefined
      ) {
        throw new Error("Контекст сверки не загружен.");
      }
      const validation = validateReconciliation(
        draft,
        reconciliationQuery.data,
        projectQuery.data.code,
      );
      if (validation !== null) {
        throw new Error(validation);
      }
      return reconcileEvidence(context, {
        projectId,
        observationIds: draft.observationIds,
        reconciliationVersionId:
          reconciliationQuery.data.reconciliation_version_id,
        reason: draft.reason.trim(),
        idempotencyKey,
      });
    },
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["records", projectId, "EVIDENCE"],
        }),
        queryClient.invalidateQueries({ queryKey: ["work-items"] }),
        queryClient.invalidateQueries({
          queryKey: ["workbench", projectId],
        }),
        reconciliationQuery.refetch(),
      ]);
    },
  });

  if (projectQuery.isError || reconciliationQuery.isError) {
    const failed = projectQuery.isError ? projectQuery : reconciliationQuery;
    return (
      <div className="page">
        <ErrorBlock
          error={failed.error}
          onRetry={() => {
            void projectQuery.refetch();
            void reconciliationQuery.refetch();
          }}
        />
      </div>
    );
  }
  if (projectQuery.isPending || reconciliationQuery.isPending) {
    return (
      <div className="page">
        <LoadingBlock label="Загрузка независимых источников и правила сверки" />
      </div>
    );
  }

  const project = projectQuery.data;
  const reconciliation = reconciliationQuery.data;
  const validation = validateReconciliation(
    draft,
    reconciliation,
    project.code,
  );
  const changeDraft = (patch: Partial<ReconciliationDraft>) => {
    setDraft((current) => ({
      ...current,
      ...patch,
      acknowledged: false,
    }));
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
        <Link to={`/projects/${encodeURIComponent(projectId)}/EVIDENCE`}>
          Доказательства
        </Link>
        <span>/</span>
        <span>Независимая сверка</span>
      </nav>

      <header className="entry-header">
        <div>
          <p className="eyebrow">Контур 02 · независимое извлечение</p>
          <h1>Сверка наблюдений</h1>
          <p>
            Совпадение принимается только между разными квалифицированными
            методами и независимыми доменами. Расхождение создаёт Conflict и
            отдельную задачу, а не усреднённое значение.
          </p>
        </div>
        <div className="entry-header__guard">
          <Icon name="shield" size={22} />
          <span>
            Правило {reconciliation.reconciliation_version_id} и комплект{" "}
            {reconciliation.document_set_revision_id} выбраны сервером.
          </span>
        </div>
      </header>

      <form className="entry-form controlled-form" onSubmit={submit}>
        <section className="entry-form__intro">
          <p className="eyebrow">01 · поле</p>
          <h2>Выберите один тип извлечённого значения</h2>
          <p>
            Сервер показывает только необработанные наблюдения текущего
            подтверждённого комплекта документов.
          </p>
        </section>
        <label className="field">
          <span>Поле доказательства</span>
          <input
            list="reconciliation-field-names"
            maxLength={300}
            value={fieldName}
            onChange={(event) => {
              setFieldName(event.target.value);
              changeDraft({ observationIds: [] });
            }}
          />
          <datalist id="reconciliation-field-names">
            {reconciliation.available_field_names.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </datalist>
        </label>
        {reconciliation.field_names_truncated && (
          <p className="inline-warning">
            Список подсказок ограничен серверным пределом. Точное имя поля можно
            ввести вручную.
          </p>
        )}

        {fieldName !== "" && (
          <section className="entry-form__section">
            <div className="entry-form__intro">
              <p className="eyebrow">02 · источники</p>
              <h2>Отметьте независимые наблюдения</h2>
            </div>
            <div className="evidence-choice-grid">
              {reconciliation.candidates.map((candidate) => {
                const observation = candidate.observation;
                const checked = draft.observationIds.includes(
                  observation.observation_id,
                );
                return (
                  <label
                    className={`evidence-choice ${!candidate.eligible ? "is-blocked" : ""}`}
                    key={observation.observation_id}
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      disabled={!candidate.eligible}
                      onChange={(event) =>
                        changeDraft({
                          observationIds: event.target.checked
                            ? [
                                ...draft.observationIds,
                                observation.observation_id,
                              ]
                            : draft.observationIds.filter(
                                (id) => id !== observation.observation_id,
                              ),
                        })
                      }
                    />
                    <span className="evidence-choice__body">
                      <span className="evidence-choice__heading">
                        <strong>
                          {observation.method} · {observation.method_version}
                        </strong>
                        <StatusPill
                          value={candidate.eligible ? "VERIFIED" : "BLOCKED"}
                          compact
                        />
                      </span>
                      <code>{displayValue(observation.value)}</code>
                      <small>
                        Домен: {candidate.independence_domain ?? "не определён"}{" "}
                        · квалификация:{" "}
                        {candidate.adapter_qualification_id ?? "отсутствует"}
                      </small>
                      <small>
                        {observation.location.locator} ·{" "}
                        {formatDateTime(observation.observed_at)}
                      </small>
                      {candidate.blockers.length > 0 && (
                        <small className="danger-text">
                          {candidate.blockers.join(", ")}
                        </small>
                      )}
                    </span>
                  </label>
                );
              })}
            </div>
            {reconciliation.candidates_truncated && (
              <p className="inline-warning">
                Показаны первые 100 записей. Сузьте поле или обработайте пакет
                частями.
              </p>
            )}
          </section>
        )}

        <section className="entry-form__section">
          <div className="form-grid">
            <label className="field form-grid__wide">
              <span>Основание сверки</span>
              <textarea
                rows={4}
                maxLength={2000}
                value={draft.reason}
                onChange={(event) =>
                  changeDraft({ reason: event.target.value })
                }
              />
            </label>
            <label className="field">
              <span>Точный код проекта</span>
              <input
                value={draft.projectCodeConfirmation}
                onChange={(event) =>
                  changeDraft({
                    projectCodeConfirmation: event.target.value,
                  })
                }
                autoComplete="off"
              />
            </label>
            <label className="attestation form-grid__wide">
              <input
                type="checkbox"
                checked={draft.acknowledged}
                onChange={(event) =>
                  setDraft((current) => ({
                    ...current,
                    acknowledged: event.target.checked,
                  }))
                }
              />
              <span>
                Подтверждаю, что выбраны именно независимые источники одного
                параметра; расхождение нельзя объединять автоматически.
              </span>
            </label>
          </div>
        </section>

        {(formError !== null || mutation.isError) && (
          <div className="inline-error" role="alert">
            {formError ??
              (mutation.error instanceof Error
                ? mutation.error.message
                : "Сверка не выполнена.")}
          </div>
        )}
        {mutation.data !== undefined && (
          <div className="workflow-result" role="status">
            <Icon
              name={mutation.data.conflict === null ? "check" : "warning"}
              size={20}
            />
            <div>
              <strong>
                {mutation.data.conflict === null
                  ? "Независимое совпадение подтверждено"
                  : "Зафиксирован конфликт"}
              </strong>
              <p>
                {mutation.data.conflict === null
                  ? `Создано производное наблюдение ${mutation.data.verified_observation_id}.`
                  : `Conflict ${mutation.data.conflict.conflict_id} передан другому проверяющему.`}
              </p>
            </div>
          </div>
        )}
        <div className="form-actions">
          <Link
            className="button button--secondary"
            to={`/projects/${encodeURIComponent(projectId)}/EVIDENCE`}
          >
            Отмена
          </Link>
          <button
            className="button button--primary"
            type="submit"
            disabled={validation !== null || mutation.isPending}
          >
            <Icon name="trace" size={16} />
            {mutation.isPending ? "Сверка…" : "Выполнить сверку"}
          </button>
        </div>
      </form>
    </div>
  );
}
