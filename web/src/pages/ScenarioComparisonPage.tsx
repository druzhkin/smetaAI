import { useMemo, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  executeScenario,
  getProject,
  getScenarioContext,
  newIdempotencyKey,
  type RequestContext,
} from "../api";
import { useAuth } from "../auth";
import { ErrorBlock, LoadingBlock } from "../components/Feedback";
import { Icon } from "../components/Icon";
import { StatusPill } from "../components/StatusPill";
import {
  compactId,
  formatDateTime,
  formatDecimal,
  formatMoney,
} from "../format";
import { Link } from "../navigation";
import {
  validateScenarioExecutionDraft,
  type ScenarioExecutionDraft,
} from "../scenarioWorkflow";
import type {
  RuntimeConfig,
  ScenarioDefinition,
  ScenarioOverride,
} from "../types";

const initialDraft: ScenarioExecutionDraft = {
  reason: "",
  projectCode: "",
  acknowledged: false,
};

const blockerLabels: Record<string, string> = {
  PROJECT_STATE_NOT_ALLOWED: "Состояние проекта не допускает сценарный расчёт.",
  CURRENT_DOCUMENT_SET_MISSING:
    "Не зафиксирован актуальный комплект документации.",
  SCENARIO_POLICY_INTEGRITY_FAILED:
    "Целостность привязанной сценарной политики не подтверждена.",
  SCENARIO_DEFINITIONS_INVALID:
    "Утверждённая политика содержит неприменимое или недоказанное изменение.",
  CURRENT_FIXED_SNAPSHOT_MISSING:
    "Для текущего комплекта нет фиксированного расчётного snapshot.",
  SELECTED_SNAPSHOT_INTEGRITY_FAILED:
    "Выбранный snapshot не прошёл объектную и арифметическую проверку.",
  SCENARIO_RUN_INTEGRITY_FAILED:
    "Хотя бы один сохранённый сценарный результат не воспроизводится.",
};

function blockerLabel(value: string): string {
  return blockerLabels[value] ?? value;
}

function overrideSummary(override: ScenarioOverride): string {
  const changes: string[] = [];
  if (override.quantity !== null) {
    changes.push(`объём → ${formatDecimal(override.quantity)}`);
  }
  if (override.unit_rate !== null) {
    changes.push(`ставка → ${formatDecimal(override.unit_rate)}`);
  }
  for (const [factor, value] of Object.entries(override.factor_values)) {
    changes.push(`${factor} → ${formatDecimal(value)}`);
  }
  return changes.join(" · ");
}

function ScenarioCard({
  definition,
  selected,
  onSelect,
}: {
  definition: ScenarioDefinition;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      className={`scenario-definition${selected ? " is-selected" : ""}`}
      type="button"
      aria-pressed={selected}
      onClick={onSelect}
    >
      <span className="scenario-definition__marker" aria-hidden="true">
        {selected ? "✓" : ""}
      </span>
      <span className="scenario-definition__body">
        <span className="eyebrow">{definition.scenario_id}</span>
        <strong>{definition.name}</strong>
        <span className="scenario-definition__version">
          Политика {compactId(definition.scenario_version)}
        </span>
        <span className="scenario-overrides">
          {definition.overrides.map((override) => (
            <span key={override.cost_input_id}>
              <span>
                <b>{override.cost_input_id}</b>
                <em>{overrideSummary(override)}</em>
              </span>
              <small>
                Основание: {override.evidence_or_assumption_id} ·{" "}
                {override.reason}
              </small>
            </span>
          ))}
        </span>
      </span>
    </button>
  );
}

export function ScenarioComparisonPage({
  config,
  projectId,
}: {
  config: RuntimeConfig;
  projectId: string;
}) {
  const auth = useAuth();
  const queryClient = useQueryClient();
  const [requestedSnapshotId, setRequestedSnapshotId] = useState<string | null>(
    null,
  );
  const [requestedScenarioKey, setRequestedScenarioKey] = useState<
    string | null
  >(null);
  const [draft, setDraft] = useState<ScenarioExecutionDraft>(initialDraft);
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
  const scenarioQuery = useQuery({
    queryKey: ["scenario-context", projectId, requestedSnapshotId],
    queryFn: ({ signal }) =>
      getScenarioContext(
        requestContext,
        projectId,
        requestedSnapshotId ?? undefined,
        signal,
      ),
  });
  const mutation = useMutation({
    mutationFn: (input: {
      snapshotId: string;
      scenarioKey: string;
      reason: string;
      idempotencyKey: string;
    }) =>
      executeScenario(requestContext, {
        projectId,
        ...input,
      }),
    onSuccess: async () => {
      setOperationKey(null);
      setDraft(initialDraft);
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["scenario-context", projectId],
        }),
        queryClient.invalidateQueries({
          queryKey: ["records", projectId, "CALCULATION"],
        }),
        queryClient.invalidateQueries({ queryKey: ["workbench", projectId] }),
      ]);
    },
  });

  if (projectQuery.isPending || scenarioQuery.isPending) {
    return (
      <div className="page">
        <LoadingBlock label="Проверка сценарной политики и snapshot" />
      </div>
    );
  }
  if (projectQuery.isError) {
    return (
      <div className="page">
        <ErrorBlock
          error={projectQuery.error}
          onRetry={() => void projectQuery.refetch()}
        />
      </div>
    );
  }
  if (scenarioQuery.isError) {
    return (
      <div className="page">
        <ErrorBlock
          error={scenarioQuery.error}
          onRetry={() => void scenarioQuery.refetch()}
        />
      </div>
    );
  }

  const project = projectQuery.data;
  const scenarioContext = scenarioQuery.data;
  const selectedSnapshotId = scenarioContext.selected_snapshot_id;
  const selectedSnapshot =
    scenarioContext.snapshots.find(
      (snapshot) => snapshot.snapshot_id === selectedSnapshotId,
    ) ?? null;
  const selectedScenario =
    scenarioContext.definitions.find(
      (definition) => definition.scenario_id === requestedScenarioKey,
    ) ??
    scenarioContext.definitions[0] ??
    null;
  const hasScenarioRole =
    auth.roles.includes("ESTIMATOR") ||
    auth.roles.includes("REVIEWER") ||
    auth.roles.includes("APPROVER") ||
    auth.roles.includes("METHODOLOGY_OWNER");
  const decisionBlockers = [
    ...scenarioContext.blockers,
    ...(!hasScenarioRole ? ["Требуется проектная роль расчётчика."] : []),
  ];
  const validationError = validateScenarioExecutionDraft(
    draft,
    project.code,
    selectedSnapshotId,
    selectedScenario?.scenario_id ?? null,
    decisionBlockers,
  );

  const resetOperation = () => {
    setOperationKey(null);
    setFormError(null);
    mutation.reset();
  };
  const selectSnapshot = (snapshotId: string) => {
    setRequestedSnapshotId(snapshotId);
    setRequestedScenarioKey(null);
    setDraft(initialDraft);
    resetOperation();
  };
  const selectScenario = (scenarioKey: string) => {
    setRequestedScenarioKey(scenarioKey);
    setDraft((current) => ({ ...current, acknowledged: false }));
    resetOperation();
  };
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setFormError(validationError);
    if (
      validationError !== null ||
      selectedSnapshotId === null ||
      selectedScenario === null
    ) {
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
      snapshotId: selectedSnapshotId,
      scenarioKey: selectedScenario.scenario_id,
      reason: draft.reason.trim(),
      idempotencyKey: key,
    });
  };

  return (
    <div className="page scenario-page">
      <nav className="breadcrumbs" aria-label="Навигационная цепочка">
        <Link to="/">Проекты</Link>
        <span>/</span>
        <Link to={`/projects/${encodeURIComponent(projectId)}`}>
          {project.code}
        </Link>
        <span>/</span>
        <Link to={`/projects/${encodeURIComponent(projectId)}/CALCULATION`}>
          Расчёт
        </Link>
        <span>/</span>
        <span>Сценарии</span>
      </nav>

      <header className="entry-header">
        <div>
          <p className="eyebrow">Сценарный контур · {project.code}</p>
          <h1>Сравнение управляемых сценариев</h1>
          <p>
            Сервер применяет только утверждённые изменения к фиксированному
            snapshot, заново считает каждую строку и выполняет независимую
            валидацию. Значения из браузера в расчёт не принимаются.
          </p>
        </div>
        <div className="records-header__actions">
          <Link
            className="button button--secondary"
            to={`/projects/${encodeURIComponent(projectId)}/CALCULATION/records`}
          >
            <Icon name="trace" size={16} />
            История
          </Link>
          <StatusPill value={scenarioContext.project_state} />
        </div>
      </header>

      <section className="scenario-basis">
        <div className="controlled-section-heading">
          <div>
            <p className="eyebrow">Неизменяемая база</p>
            <h2>Snapshot и версия политики</h2>
          </div>
          {scenarioQuery.isFetching && <span>Перепроверка…</span>}
        </div>
        <div className="scenario-basis__grid">
          <label>
            <span>Фиксированный snapshot</span>
            <select
              value={selectedSnapshotId ?? ""}
              onChange={(event) => selectSnapshot(event.target.value)}
              disabled={scenarioContext.snapshots.length === 0}
            >
              {scenarioContext.snapshots.map((snapshot) => (
                <option key={snapshot.snapshot_id} value={snapshot.snapshot_id}>
                  {snapshot.integrity_valid ? "Проверен" : "Повреждён"} ·{" "}
                  {formatDateTime(snapshot.created_at)} ·{" "}
                  {formatMoney(snapshot.grand_total, snapshot.currency)}
                </option>
              ))}
            </select>
          </label>
          <dl>
            <div>
              <dt>SHA-256 snapshot</dt>
              <dd>{selectedSnapshot?.snapshot_hash ?? "—"}</dd>
            </div>
            <div>
              <dt>Политика сценариев</dt>
              <dd>{scenarioContext.scenario_policy_version_id ?? "—"}</dd>
            </div>
            <div>
              <dt>Комплект документов</dt>
              <dd>{scenarioContext.current_document_set_revision_id ?? "—"}</dd>
            </div>
            <div>
              <dt>Независимый пересчёт базы</dt>
              <dd>
                {selectedSnapshot?.independent_validation_passed === true
                  ? "Сошёлся"
                  : "Не подтверждён"}
              </dd>
            </div>
          </dl>
        </div>
        {scenarioContext.snapshots_truncated && (
          <p className="scenario-note">
            Показаны 50 последних snapshot. Более ранний можно открыть по
            точному идентификатору через API.
          </p>
        )}
      </section>

      <section className="scenario-library">
        <div className="controlled-section-heading">
          <div>
            <p className="eyebrow">Утверждённая библиотека</p>
            <h2>Выберите сценарий</h2>
          </div>
          <span className="record-count">
            {scenarioContext.definitions.length}{" "}
            {scenarioContext.definitions.length === 1
              ? "сценарий"
              : "сценариев"}
          </span>
        </div>
        {scenarioContext.definitions.length === 0 ? (
          <p className="empty-state">
            В проверенной политике нет доступных сценариев.
          </p>
        ) : (
          <div className="scenario-definitions">
            {scenarioContext.definitions.map((definition) => (
              <ScenarioCard
                key={definition.scenario_id}
                definition={definition}
                selected={
                  definition.scenario_id === selectedScenario?.scenario_id
                }
                onSelect={() => selectScenario(definition.scenario_id)}
              />
            ))}
          </div>
        )}
      </section>

      <section className="scenario-comparisons">
        <div className="controlled-section-heading">
          <div>
            <p className="eyebrow">Воспроизводимые результаты</p>
            <h2>База → сценарий → отклонение</h2>
          </div>
          <span className="record-count">
            {scenarioContext.comparisons.length}{" "}
            {scenarioContext.comparisons.length === 1 ? "запуск" : "запусков"}
          </span>
        </div>
        {scenarioContext.comparisons.length === 0 ? (
          <p className="empty-state">
            Для выбранного snapshot и текущей политики запусков пока нет.
          </p>
        ) : (
          <div className="scenario-results">
            {scenarioContext.comparisons.map((comparison) => (
              <article
                className={`scenario-result${
                  comparison.integrity_valid ? "" : " scenario-result--invalid"
                }`}
                key={comparison.scenario_run_id}
              >
                <header>
                  <div>
                    <p className="eyebrow">{comparison.scenario_key}</p>
                    <h3>{comparison.scenario_name}</h3>
                  </div>
                  <StatusPill
                    value={
                      comparison.integrity_valid
                        ? comparison.status
                        : "INTEGRITY_FAILED"
                    }
                  />
                </header>
                <div className="scenario-result__money">
                  <div>
                    <span>База</span>
                    <strong>
                      {formatMoney(
                        comparison.base_grand_total,
                        comparison.currency,
                      )}
                    </strong>
                  </div>
                  <span className="scenario-result__arrow" aria-hidden="true">
                    →
                  </span>
                  <div>
                    <span>Сценарий</span>
                    <strong>
                      {formatMoney(
                        comparison.scenario_grand_total,
                        comparison.currency,
                      )}
                    </strong>
                  </div>
                  <div className="scenario-result__delta">
                    <span>Отклонение</span>
                    <strong>
                      {formatMoney(
                        comparison.absolute_delta,
                        comparison.currency,
                      )}
                    </strong>
                    <small>
                      {formatDecimal(comparison.relative_delta_percent)} %
                    </small>
                  </div>
                </div>
                <dl>
                  <div>
                    <dt>Независимый пересчёт</dt>
                    <dd>
                      {comparison.independent_validation_passed === true
                        ? "Сошёлся"
                        : "Не подтверждён"}
                    </dd>
                  </div>
                  <div>
                    <dt>Исполнитель</dt>
                    <dd>{comparison.executed_by ?? "—"}</dd>
                  </div>
                  <div>
                    <dt>Время</dt>
                    <dd>{formatDateTime(comparison.created_at)}</dd>
                  </div>
                  <div>
                    <dt>Run</dt>
                    <dd>{comparison.scenario_run_id}</dd>
                  </div>
                </dl>
                {!comparison.integrity_valid && (
                  <p className="scenario-result__error" role="alert">
                    {comparison.integrity_error}
                  </p>
                )}
              </article>
            ))}
          </div>
        )}
        {scenarioContext.comparisons_truncated && (
          <p className="scenario-note">
            Показаны 100 последних запусков для выбранного snapshot и текущей
            политики.
          </p>
        )}
      </section>

      {mutation.isSuccess && (
        <section className="success-panel" role="status">
          <Icon name="check" size={24} />
          <div>
            <h2>Сценарный результат зафиксирован</h2>
            <p>
              Итог{" "}
              <strong>
                {formatMoney(
                  mutation.data.result.primary.grand_total,
                  mutation.data.result.primary.currency,
                )}
              </strong>
              ; независимый пересчёт{" "}
              {mutation.data.result.independent.passed
                ? "сошёлся"
                : "не сошёлся"}
              .
            </p>
          </div>
        </section>
      )}

      {decisionBlockers.length > 0 ? (
        <section className="decision-blocker" role="alert">
          <Icon name="shield" size={24} />
          <div>
            <h2>Новый сценарный запуск заблокирован</h2>
            <ul>
              {decisionBlockers.map((blocker) => (
                <li key={blocker}>{blockerLabel(blocker)}</li>
              ))}
            </ul>
          </div>
        </section>
      ) : (
        <form className="entry-form scenario-form" onSubmit={submit}>
          <div className="entry-form__intro">
            <p className="eyebrow">Контрольная команда</p>
            <h2>Выполнить выбранный сценарий</h2>
            <p>
              В команду попадут только идентификаторы проверенного snapshot и
              сценария. Все изменения ставок, объёмов и коэффициентов сервер
              прочитает из утверждённой политики.
            </p>
          </div>
          <div className="scenario-command-summary">
            <div>
              <span>Snapshot</span>
              <strong>{selectedSnapshotId}</strong>
            </div>
            <div>
              <span>Сценарий</span>
              <strong>{selectedScenario?.scenario_id ?? "—"}</strong>
            </div>
          </div>
          <label>
            Основание запуска
            <textarea
              value={draft.reason}
              maxLength={2000}
              rows={4}
              onChange={(event) => {
                setDraft((current) => ({
                  ...current,
                  reason: event.target.value,
                  acknowledged: false,
                }));
                resetOperation();
              }}
            />
          </label>
          <label>
            Введите шифр проекта: {project.code}
            <input
              value={draft.projectCode}
              maxLength={128}
              autoComplete="off"
              onChange={(event) => {
                setDraft((current) => ({
                  ...current,
                  projectCode: event.target.value,
                  acknowledged: false,
                }));
                resetOperation();
              }}
            />
          </label>
          <label className="attestation">
            <input
              type="checkbox"
              checked={draft.acknowledged}
              onChange={(event) => {
                setDraft((current) => ({
                  ...current,
                  acknowledged: event.target.checked,
                }));
                resetOperation();
              }}
            />
            <span>
              Я проверил выбранный snapshot, версию сценарной политики и
              допущения; понимаю, что невоспроизводимый результат блокирует
              использование сценария.
            </span>
          </label>
          {(formError ?? (mutation.isError ? mutation.error.message : null)) !==
            null && (
            <p className="form-error" role="alert">
              {formError ?? (mutation.isError ? mutation.error.message : null)}
            </p>
          )}
          <button
            className="button button--primary"
            type="submit"
            disabled={mutation.isPending}
          >
            <Icon name="calculator" size={17} />
            {mutation.isPending ? "Пересчёт и проверка…" : "Запустить сценарий"}
          </button>
        </form>
      )}
    </div>
  );
}
