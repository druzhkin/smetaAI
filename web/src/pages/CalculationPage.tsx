import { useMemo, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  executeCurrentCalculation,
  getCalculationContext,
  newIdempotencyKey,
  type RequestContext,
} from "../api";
import { useAuth } from "../auth";
import {
  calculationBasisId,
  validateCalculationExecutionDraft,
  type CalculationExecutionDraft,
} from "../calculationWorkflow";
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
import type {
  AtomicCostInput,
  CalculationCandidate,
  RuntimeConfig,
} from "../types";

const initialDraft: CalculationExecutionDraft = {
  reason: "",
  projectCode: "",
  acknowledged: false,
};

const categoryLabels: Record<string, string> = {
  LABOUR: "Работы",
  MACHINERY: "Машины",
  MATERIAL: "Материалы",
  SUBCONTRACT: "Субподряд",
  LOGISTICS: "Логистика",
  MOBILISATION: "Мобилизация",
  CONTRACT_FINANCE: "Финансовые условия",
  RISK: "Риск-резерв",
  OTHER: "Прочее",
};

function basisKind(input: AtomicCostInput): string {
  if (input.source_observation_id !== null) return "Рыночная цена";
  if (input.normative_rate_id !== null) return "Норматив";
  if (input.approved_assumption_id !== null) return "Допущение";
  if (input.risk_reserve_id !== null) return "Риск-модель";
  if (input.derived_cost_model_id !== null) return "Производная модель";
  return "Нет основания";
}

function CandidateInputs({ candidate }: { candidate: CalculationCandidate }) {
  return (
    <section className="calculation-candidate">
      <div className="calculation-candidate__header">
        <div>
          <p className="eyebrow">Серверный кандидат</p>
          <h2>Атомарные входы без ручного ввода сумм</h2>
          <p>
            Каждая ставка восстановлена из проверенного основания. Браузер не
            умножает значения и не передаёт собственный финансовый итог.
          </p>
        </div>
        <span className="record-count">
          {candidate.inputs.length} компонентов
        </span>
      </div>

      <dl className="calculation-policy-grid">
        <div>
          <dt>Calculation model</dt>
          <dd>{candidate.calculation_model_version_id}</dd>
        </div>
        <div>
          <dt>Документный комплект</dt>
          <dd>{candidate.document_set_revision_id}</dd>
        </div>
        <div>
          <dt>Валюта / округление</dt>
          <dd>
            {candidate.policy.currency} · {candidate.policy.rounding_mode} ·{" "}
            {candidate.policy.line_rounding_scale}/
            {candidate.policy.total_rounding_scale}
          </dd>
        </div>
        <div>
          <dt>Допуск независимого пересчёта</dt>
          <dd>{formatDecimal(candidate.policy.independent_tolerance)}</dd>
        </div>
      </dl>

      <div className="calculation-inputs">
        {candidate.inputs.map((input, index) => {
          const basisId = calculationBasisId(input);
          return (
            <article className="calculation-input" key={input.cost_input_id}>
              <div className="calculation-input__index">
                {String(index + 1).padStart(2, "0")}
              </div>
              <div className="calculation-input__body">
                <div className="calculation-input__title">
                  <div>
                    <span>
                      {categoryLabels[input.category] ?? input.category}
                    </span>
                    <h3>{input.semantic_key}</h3>
                    <p>
                      {input.wbs_node_id} · {input.line_id}
                    </p>
                  </div>
                  <StatusPill value={basisId === null ? "BLOCKED" : "BOUND"} />
                </div>
                <dl>
                  <div>
                    <dt>Проверенный объём</dt>
                    <dd>
                      {formatDecimal(input.quantity)} {input.unit}
                    </dd>
                  </div>
                  <div>
                    <dt>Ставка основания</dt>
                    <dd>
                      {formatMoney(input.unit_rate, input.currency)} /{" "}
                      {input.unit}
                    </dd>
                  </div>
                  <div>
                    <dt>Тип основания</dt>
                    <dd>{basisKind(input)}</dd>
                  </div>
                  <div>
                    <dt>Источник</dt>
                    <dd className="hash-value">
                      {basisId === null ? "не определён" : basisId}
                    </dd>
                  </div>
                  <div>
                    <dt>Знак</dt>
                    <dd>{input.sign === 1 ? "+1" : "−1"}</dd>
                  </div>
                  <div>
                    <dt>Контролируемые коэффициенты</dt>
                    <dd>
                      {input.factors.length === 0
                        ? "не применяются"
                        : input.factors
                            .map(
                              (factor) =>
                                `${factor.factor_id} × ${formatDecimal(factor.value)}`,
                            )
                            .join("; ")}
                    </dd>
                  </div>
                </dl>
              </div>
            </article>
          );
        })}
      </div>

      <div className="candidate-hash">
        <span>SHA-256 кандидата</span>
        <code>{candidate.candidate_hash}</code>
      </div>
    </section>
  );
}

export function CalculationPage({
  config,
  projectId,
}: {
  config: RuntimeConfig;
  projectId: string;
}) {
  const auth = useAuth();
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<CalculationExecutionDraft>(initialDraft);
  const [operationKey, setOperationKey] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const requestContext = useMemo<RequestContext>(
    () => ({
      apiBasePath: config.api_base_path,
      authorizationHeaders: auth.authorizationHeaders,
    }),
    [auth.authorizationHeaders, config.api_base_path],
  );
  const contextQuery = useQuery({
    queryKey: ["calculation-context", projectId],
    queryFn: ({ signal }) =>
      getCalculationContext(requestContext, projectId, signal),
  });
  const mutation = useMutation({
    mutationFn: (input: {
      candidate: CalculationCandidate;
      reason: string;
      idempotencyKey: string;
    }) =>
      executeCurrentCalculation(requestContext, {
        projectId,
        expectedRowVersion: input.candidate.project_row_version,
        candidateHash: input.candidate.candidate_hash,
        reason: input.reason,
        idempotencyKey: input.idempotencyKey,
      }),
    onSuccess: async () => {
      setOperationKey(null);
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["calculation-context", projectId],
        }),
        queryClient.invalidateQueries({ queryKey: ["project", projectId] }),
        queryClient.invalidateQueries({ queryKey: ["workbench", projectId] }),
        queryClient.invalidateQueries({ queryKey: ["projects"] }),
        queryClient.invalidateQueries({
          queryKey: ["records", projectId, "CALCULATION"],
        }),
      ]);
    },
  });

  if (contextQuery.isPending) {
    return (
      <div className="page">
        <LoadingBlock label="Проверка расчётного контекста" />
      </div>
    );
  }
  if (contextQuery.isError) {
    return (
      <div className="page">
        <ErrorBlock
          error={contextQuery.error}
          onRetry={() => void contextQuery.refetch()}
        />
      </div>
    );
  }

  const context = contextQuery.data;
  const candidate = context.candidate;
  const hasEstimatorRole = auth.roles.includes("ESTIMATOR");
  const decisionBlockers = [
    ...context.blockers,
    ...(!hasEstimatorRole ? ["Требуется проектная роль сметчика."] : []),
  ];
  const validationError = validateCalculationExecutionDraft(
    draft,
    context.project.code,
    candidate !== null,
    decisionBlockers,
  );

  const resetOperation = () => {
    setOperationKey(null);
    setFormError(null);
    mutation.reset();
  };
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setFormError(validationError);
    if (validationError !== null || candidate === null) {
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
      candidate,
      reason: draft.reason.trim(),
      idempotencyKey: key,
    });
  };

  return (
    <div className="page calculation-page">
      <nav className="breadcrumbs" aria-label="Навигационная цепочка">
        <Link to="/">Проекты</Link>
        <span>/</span>
        <Link to={`/projects/${encodeURIComponent(projectId)}`}>
          {context.project.code}
        </Link>
        <span>/</span>
        <span>Расчёт</span>
      </nav>

      <header className="entry-header">
        <div>
          <p className="eyebrow">06 · {context.project.code}</p>
          <h1>Расчёт и независимая валидация</h1>
          <p>
            Итог строится только из серверно восстановленных атомарных входов.
            Сохранённый snapshot содержит входы, политику, основной результат и
            независимый пересчёт.
          </p>
        </div>
        <div className="records-header__actions">
          <Link
            className="button button--secondary"
            to={`/projects/${encodeURIComponent(projectId)}/scenarios`}
          >
            <Icon name="refresh" size={16} />
            Сценарии
          </Link>
          <Link
            className="button button--secondary"
            to={`/projects/${encodeURIComponent(projectId)}/release`}
          >
            <Icon name="shield" size={16} />
            Допуск
          </Link>
          <Link
            className="button button--secondary"
            to={`/projects/${encodeURIComponent(projectId)}/CALCULATION/records`}
          >
            <Icon name="trace" size={16} />
            История
          </Link>
          <StatusPill value={context.project.state} />
        </div>
      </header>

      {context.latest_fixed_calculation !== null && (
        <section
          className={`fixed-calculation ${
            context.latest_fixed_calculation.integrity_valid
              ? "fixed-calculation--valid"
              : "fixed-calculation--invalid"
          }`}
        >
          <div>
            <p className="eyebrow">Последний фиксированный snapshot</p>
            <h2>
              {context.latest_fixed_calculation.integrity_valid
                ? formatMoney(
                    context.latest_fixed_calculation.grand_total,
                    context.latest_fixed_calculation.currency,
                  )
                : "Целостность не подтверждена"}
            </h2>
            <p>
              {context.latest_fixed_calculation.integrity_valid
                ? `Независимый пересчёт ${
                    context.latest_fixed_calculation
                      .independent_validation_passed
                      ? "сошёлся"
                      : "не сошёлся"
                  }. Создан ${formatDateTime(
                    context.latest_fixed_calculation.created_at,
                  )}.`
                : context.latest_fixed_calculation.integrity_error}
            </p>
          </div>
          <dl>
            <div>
              <dt>Snapshot</dt>
              <dd>{context.latest_fixed_calculation.snapshot_id}</dd>
            </div>
            <div>
              <dt>Run</dt>
              <dd>{context.latest_fixed_calculation.calculation_run_id}</dd>
            </div>
            <div>
              <dt>SHA-256</dt>
              <dd className="hash-value">
                {context.latest_fixed_calculation.snapshot_hash}
              </dd>
            </div>
          </dl>
        </section>
      )}

      {candidate !== null && <CandidateInputs candidate={candidate} />}

      {mutation.isSuccess && (
        <section className="success-panel" role="status">
          <Icon name="check" size={24} />
          <div>
            <h2>Snapshot зафиксирован</h2>
            <p>
              Основной итог{" "}
              <strong>
                {formatMoney(
                  mutation.data.primary.grand_total,
                  mutation.data.primary.currency,
                )}
              </strong>
              ; независимый пересчёт{" "}
              {mutation.data.independent.passed ? "сошёлся" : "заблокирован"}.
            </p>
          </div>
        </section>
      )}

      {decisionBlockers.length > 0 ? (
        <section className="decision-blocker" role="alert">
          <Icon name="shield" size={24} />
          <div>
            <h2>Запуск расчёта недоступен</h2>
            <ul>
              {decisionBlockers.map((blocker) => (
                <li key={blocker}>{blocker}</li>
              ))}
            </ul>
          </div>
        </section>
      ) : (
        <form className="entry-form calculation-form" onSubmit={submit}>
          <div className="entry-form__intro">
            <p className="eyebrow">Контрольная команда</p>
            <h2>Зафиксировать расчёт и независимый пересчёт</h2>
            <p>
              Команда передаёт только hash кандидата и основание. Сервер заново
              собирает входы перед созданием snapshot.
            </p>
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
            Введите шифр проекта: {context.project.code}
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
              Я проверил состав серверного кандидата и понимаю, что
              арифметическое расхождение, устаревший источник или изменение
              контекста блокирует snapshot.
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
            {mutation.isPending
              ? "Фиксация и пересчёт…"
              : "Запустить и зафиксировать"}
          </button>
        </form>
      )}

      {candidate !== null && (
        <p className="calculation-candidate-note">
          Кандидат {compactId(candidate.candidate_hash)} будет принят только при
          неизменной версии проекта {candidate.project_row_version}.
        </p>
      )}
    </div>
  );
}
