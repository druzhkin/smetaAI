import {
  useMemo,
  useState,
  type Dispatch,
  type FormEvent,
  type SetStateAction,
} from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  calculateRiskReserve,
  decideRiskItem,
  getProject,
  getRiskContext,
  newIdempotencyKey,
  submitRiskItem,
  type RequestContext,
} from "../api";
import { useAuth } from "../auth";
import { ErrorBlock, LoadingBlock } from "../components/Feedback";
import { Icon } from "../components/Icon";
import { StatusPill } from "../components/StatusPill";
import { displayValue, formatDateTime, formatMoney } from "../format";
import { Link } from "../navigation";
import {
  validateRiskCalculation,
  validateRiskDecision,
  validateRiskSubmission,
  type RiskAttestation,
} from "../riskWorkflow";
import type { RiskContext, RiskItemReview, RuntimeConfig } from "../types";

const emptyAttestation: RiskAttestation = {
  reason: "",
  projectCodeConfirmation: "",
  acknowledged: false,
};

function message(error: unknown): string {
  return error instanceof Error ? error.message : "Операция не выполнена.";
}

function AttestationFields({
  value,
  setValue,
  projectCode,
  acknowledgement,
}: {
  value: RiskAttestation;
  setValue: Dispatch<SetStateAction<RiskAttestation>>;
  projectCode: string;
  acknowledgement: string;
}) {
  return (
    <div className="form-grid">
      <label className="field form-grid__wide">
        <span>Основание действия</span>
        <textarea
          rows={4}
          maxLength={2000}
          value={value.reason}
          onChange={(event) =>
            setValue((current) => ({
              ...current,
              reason: event.target.value,
              acknowledged: false,
            }))
          }
        />
      </label>
      <label className="field">
        <span>Точный шифр проекта: {projectCode}</span>
        <input
          value={value.projectCodeConfirmation}
          autoComplete="off"
          onChange={(event) =>
            setValue((current) => ({
              ...current,
              projectCodeConfirmation: event.target.value,
              acknowledged: false,
            }))
          }
        />
      </label>
      <label className="attestation form-grid__wide">
        <input
          type="checkbox"
          checked={value.acknowledged}
          onChange={(event) =>
            setValue((current) => ({
              ...current,
              acknowledged: event.target.checked,
            }))
          }
        />
        <span>{acknowledgement}</span>
      </label>
    </div>
  );
}

function currentReview(
  context: RiskContext,
  riskKey: string,
): RiskItemReview | undefined {
  return context.items.find(
    (review) => review.item.risk_key === riskKey && review.item.is_current,
  );
}

export function RiskPage({
  config,
  projectId,
}: {
  config: RuntimeConfig;
  projectId: string;
}) {
  const auth = useAuth();
  const queryClient = useQueryClient();
  const [riskKey, setRiskKey] = useState<string>();
  const [observationIds, setObservationIds] = useState<string[]>([]);
  const [submission, setSubmission] =
    useState<RiskAttestation>(emptyAttestation);
  const [submissionError, setSubmissionError] = useState<string | null>(null);
  const [reviewId, setReviewId] = useState<string | null>(null);
  const [decision, setDecision] = useState<"APPROVED" | "REJECTED">("APPROVED");
  const [reviewAttestation, setReviewAttestation] =
    useState<RiskAttestation>(emptyAttestation);
  const [reviewError, setReviewError] = useState<string | null>(null);
  const [calculationAttestation, setCalculationAttestation] =
    useState<RiskAttestation>(emptyAttestation);
  const [calculationError, setCalculationError] = useState<string | null>(null);
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
  const riskQuery = useQuery({
    queryKey: ["risk-context", projectId, riskKey],
    queryFn: ({ signal }) =>
      getRiskContext(context, projectId, riskKey, signal),
  });
  const selectedReview =
    riskQuery.data?.items.find((review) => review.item.row_id === reviewId) ??
    null;
  const canAuthor =
    auth.roles.includes("ESTIMATOR") || auth.roles.includes("TECHNICAL_EXPERT");

  const invalidate = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["risk-context", projectId] }),
      queryClient.invalidateQueries({
        queryKey: ["records", projectId, "CONTRACT_RISK"],
      }),
      queryClient.invalidateQueries({ queryKey: ["work-items"] }),
      queryClient.invalidateQueries({ queryKey: ["workbench", projectId] }),
    ]);
  };

  const submitMutation = useMutation({
    mutationFn: async () => {
      const project = projectQuery.data;
      const risk = riskQuery.data;
      if (project === undefined || risk === undefined) {
        throw new Error("Контекст риска не загружен.");
      }
      const guard = validateRiskSubmission(
        { ...submission, observationIds },
        risk,
        project.code,
      );
      if (guard !== null) {
        throw new Error(guard);
      }
      const candidate = risk.evidence_candidates.find(
        (item) =>
          item.observation.observation_id === observationIds[0] &&
          item.draft !== null,
      );
      if (candidate?.draft === null || candidate?.draft === undefined) {
        throw new Error("Серверная структура риска больше недоступна.");
      }
      return submitRiskItem(context, {
        projectId,
        draft: { ...candidate.draft, observation_ids: observationIds },
        expectedDocumentSetRevisionId: risk.document_set_revision_id,
        riskModelVersionId: risk.risk_model_version_id,
        reason: submission.reason.trim(),
        idempotencyKey: newIdempotencyKey(),
      });
    },
    onSuccess: async (item) => {
      setObservationIds([]);
      setSubmission(emptyAttestation);
      setSubmissionError(null);
      setReviewId(item.row_id);
      await invalidate();
    },
    onError: (error) => setSubmissionError(message(error)),
  });

  const decisionMutation = useMutation({
    mutationFn: async () => {
      const project = projectQuery.data;
      if (project === undefined || selectedReview === null) {
        throw new Error("Выберите актуальную задачу проверки риска.");
      }
      const guard = validateRiskDecision(
        reviewAttestation,
        selectedReview,
        project.code,
      );
      if (guard !== null) {
        throw new Error(guard);
      }
      return decideRiskItem(context, {
        projectId,
        riskItemId: selectedReview.item.row_id,
        decision,
        expectedRiskUpdatedAt: selectedReview.item.updated_at,
        expectedTaskUpdatedAt: selectedReview.task_updated_at,
        reason: reviewAttestation.reason.trim(),
        idempotencyKey: newIdempotencyKey(),
      });
    },
    onSuccess: async () => {
      setReviewId(null);
      setReviewAttestation(emptyAttestation);
      setReviewError(null);
      await invalidate();
    },
    onError: (error) => setReviewError(message(error)),
  });

  const calculationMutation = useMutation({
    mutationFn: async () => {
      const project = projectQuery.data;
      const risk = riskQuery.data;
      if (project === undefined || risk === undefined) {
        throw new Error("Контекст риска не загружен.");
      }
      const guard = validateRiskCalculation(
        calculationAttestation,
        risk,
        project.code,
      );
      if (guard !== null) {
        throw new Error(guard);
      }
      return calculateRiskReserve(context, {
        projectId,
        expectedDocumentSetRevisionId: risk.document_set_revision_id,
        riskModelVersionId: risk.risk_model_version_id,
        reason: calculationAttestation.reason.trim(),
        idempotencyKey: newIdempotencyKey(),
      });
    },
    onSuccess: async () => {
      setCalculationAttestation(emptyAttestation);
      setCalculationError(null);
      await invalidate();
    },
    onError: (error) => setCalculationError(message(error)),
  });

  if (projectQuery.isError || riskQuery.isError) {
    const failed = projectQuery.isError ? projectQuery : riskQuery;
    return (
      <div className="page">
        <ErrorBlock
          error={failed.error}
          onRetry={() => void failed.refetch()}
        />
      </div>
    );
  }
  if (projectQuery.data === undefined || riskQuery.data === undefined) {
    return (
      <div className="page">
        <LoadingBlock label="Загрузка управляемого реестра рисков" />
      </div>
    );
  }

  const project = projectQuery.data;
  const risk = riskQuery.data;
  const activeRiskKey = risk.selected_risk_key;
  const activeReview = currentReview(risk, activeRiskKey);
  const verifiedRequired = risk.required_risk_keys.filter(
    (key) => currentReview(risk, key)?.item.risk.status === "VERIFIED",
  ).length;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setSubmissionError(null);
    submitMutation.mutate();
  };
  const submitDecision = (event: FormEvent) => {
    event.preventDefault();
    setReviewError(null);
    decisionMutation.mutate();
  };
  const submitCalculation = (event: FormEvent) => {
    event.preventDefault();
    setCalculationError(null);
    calculationMutation.mutate();
  };

  return (
    <div className="page controlled-workflow-page risk-page">
      <nav className="breadcrumbs" aria-label="Навигационная цепочка">
        <Link to="/">Проекты</Link>
        <span>/</span>
        <Link to={`/projects/${encodeURIComponent(projectId)}`}>
          {project.code}
        </Link>
        <span>/</span>
        <Link to={`/projects/${encodeURIComponent(projectId)}/CONTRACT_RISK`}>
          Договор и риски
        </Link>
        <span>/</span>
        <span>Риск-резерв</span>
      </nav>

      <header className="entry-header">
        <div>
          <p className="eyebrow">Контур 9 · риск исполнения и резерв</p>
          <h1>Управляемый реестр рисков</h1>
          <p>
            Параметры риска нельзя вводить на этом экране. Они переносятся
            только из структурированного наблюдения текущего комплекта,
            проверяются другим пользователем и независимо пересчитываются.
          </p>
        </div>
        <div className="entry-header__guard">
          <Icon name="shield" size={22} />
          <span>
            Модель {risk.risk_model_version_id} · комплект{" "}
            {risk.document_set_revision_id} · проверяющий {risk.review_role}
          </span>
        </div>
      </header>

      <section className="passport-overview" aria-label="Состояние риск-модели">
        <article>
          <span>Обязательных рисков</span>
          <strong>{risk.required_risk_keys.length}</strong>
          <small>минимум реестра: {risk.minimum_risk_items}</small>
        </article>
        <article
          className={
            verifiedRequired === risk.required_risk_keys.length
              ? "is-ok"
              : "is-danger"
          }
        >
          <span>Проверено</span>
          <strong>{verifiedRequired}</strong>
          <small>из {risk.required_risk_keys.length} обязательных</small>
        </article>
        <article
          className={
            risk.calculation_blockers.length === 0 ? "is-ok" : "is-danger"
          }
        >
          <span>Блокеров расчёта</span>
          <strong>{risk.calculation_blockers.length}</strong>
          <small>{risk.calculation_blockers.join(", ") || "нет"}</small>
        </article>
      </section>

      {risk.calculation_blockers.length > 0 && (
        <section className="blocker-panel" role="alert">
          <Icon name="warning" size={20} />
          <div>
            <strong>Риск-резерв пока нельзя использовать в расчёте</strong>
            <ul>
              {risk.calculation_blockers.map((blocker) => (
                <li key={blocker}>{blocker}</li>
              ))}
            </ul>
          </div>
        </section>
      )}

      <section className="review-ledger">
        <div className="review-ledger__heading">
          <div>
            <p className="eyebrow">01 · закрытая модель</p>
            <h2>Объявленные риски и состояния проверки</h2>
          </div>
          <StatusPill value={risk.project_state} />
        </div>
        <div className="passport-fact-grid">
          {risk.risk_keys.map((key) => {
            const review = currentReview(risk, key);
            return (
              <article key={key} className="passport-fact-card">
                <span className="eyebrow">
                  {risk.required_risk_keys.includes(key)
                    ? "Обязательный"
                    : "Дополнительный"}
                  {risk.independently_verified_risk_keys.includes(key)
                    ? " · два источника"
                    : ""}
                </span>
                <h3>{key}</h3>
                <small>{risk.evidence_field_names[key]}</small>
                {review === undefined ? (
                  <p>Актуальная запись отсутствует.</p>
                ) : (
                  <>
                    <p>{review.item.risk.description}</p>
                    <small>
                      P={review.item.risk.probability} ·{" "}
                      {formatMoney(
                        review.item.risk.impact_min,
                        review.item.risk.currency,
                      )}{" "}
                      /{" "}
                      {formatMoney(
                        review.item.risk.impact_most_likely,
                        review.item.risk.currency,
                      )}{" "}
                      /{" "}
                      {formatMoney(
                        review.item.risk.impact_max,
                        review.item.risk.currency,
                      )}
                    </small>
                    <div className="passport-fact-card__status">
                      <StatusPill value={review.item.risk.status} compact />
                      <StatusPill value={review.task_status} compact />
                    </div>
                    {review.item.risk.status === "IN_REVIEW" && (
                      <button
                        className="button button--secondary"
                        type="button"
                        onClick={() => {
                          setReviewId(review.item.row_id);
                          setReviewAttestation(emptyAttestation);
                          setReviewError(null);
                        }}
                      >
                        <Icon name="shield" size={15} />
                        Открыть проверку
                      </button>
                    )}
                  </>
                )}
              </article>
            );
          })}
        </div>
      </section>

      <form className="entry-form controlled-form" onSubmit={submit}>
        <section className="entry-form__intro">
          <p className="eyebrow">02 · доказательная запись</p>
          <h2>Выбрать серверные параметры риска</h2>
          <p>
            UI отправляет идентификаторы наблюдений и серверную структуру без
            редактирования вероятности или суммы. Backend повторно сверит каждое
            поле, версию модели и manifest документов.
          </p>
        </section>
        <label className="field">
          <span>Ключ утверждённой риск-модели</span>
          <select
            value={activeRiskKey}
            onChange={(event) => {
              setRiskKey(event.target.value);
              setObservationIds([]);
              setSubmissionError(null);
            }}
          >
            {risk.risk_keys.map((key) => (
              <option key={key} value={key}>
                {key}
              </option>
            ))}
          </select>
        </label>
        {activeReview !== undefined && (
          <p className="inline-warning">
            Новая запись supersede текущий риск {activeReview.item.row_id}; его
            история и решение останутся неизменяемыми.
          </p>
        )}
        {risk.unresolved_conflict_ids.length > 0 && (
          <div className="inline-error" role="alert">
            Неразрешённые конфликты: {risk.unresolved_conflict_ids.join(", ")}
          </div>
        )}
        <div className="evidence-choice-grid">
          {risk.evidence_candidates.map((candidate) => {
            const observation = candidate.observation;
            const checked = observationIds.includes(observation.observation_id);
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
                    setObservationIds((current) =>
                      event.target.checked
                        ? [...current, observation.observation_id]
                        : current.filter(
                            (id) => id !== observation.observation_id,
                          ),
                    )
                  }
                />
                <span className="evidence-choice__body">
                  <span className="evidence-choice__heading">
                    <strong>
                      {observation.method} · {observation.method_version}
                    </strong>
                    <StatusPill value={observation.status} compact />
                  </span>
                  <code>{displayValue(candidate.draft)}</code>
                  <small>
                    {observation.location.locator} · домен{" "}
                    {candidate.independence_domain ?? "не определён"}
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
        {risk.evidence_candidates.length === 0 && (
          <div className="empty-state">
            <Icon name="trace" size={22} />
            <strong>Нет структурированного наблюдения этого риска</strong>
            <p>
              Требуется извлечение либо отдельный управляемый workflow ручного
              наблюдения. Пустой риск не считается доказательством отсутствия.
            </p>
          </div>
        )}
        {risk.candidates_truncated && (
          <p className="inline-warning">
            Показаны только первые 100 кандидатов. Нельзя принимать решение по
            неполному списку.
          </p>
        )}
        <AttestationFields
          value={submission}
          setValue={setSubmission}
          projectCode={project.code}
          acknowledgement="Подтверждаю выбор неизменённых серверных параметров из актуального комплекта; финансовые значения вручную не вводились."
        />
        {submissionError !== null && (
          <div className="inline-error" role="alert">
            {submissionError}
          </div>
        )}
        <div className="form-actions">
          <button
            className="button button--primary"
            type="submit"
            disabled={submitMutation.isPending || !canAuthor}
          >
            <Icon name="plus" size={16} />
            {submitMutation.isPending ? "Фиксация…" : "Создать ревизию риска"}
          </button>
        </div>
      </form>

      {selectedReview !== null && (
        <form className="entry-form controlled-form" onSubmit={submitDecision}>
          <section className="entry-form__intro">
            <p className="eyebrow">03 · четыре глаза</p>
            <h2>Независимое решение по {selectedReview.item.risk_key}</h2>
            <p>
              Автор {selectedReview.item.created_by}; задача{" "}
              {selectedReview.item.approval_task_id}; версия записи{" "}
              {formatDateTime(selectedReview.item.updated_at)}.
            </p>
          </section>
          <div className="source-proof">
            <span>Неизменяемые параметры</span>
            <code>{displayValue(selectedReview.item.risk)}</code>
            <small>
              Источники: {selectedReview.item.risk.observation_ids.join(", ")}
            </small>
            <small>
              Leaf sources:{" "}
              {selectedReview.item.independence_source_ids.join(", ")}
            </small>
          </div>
          {selectedReview.decision_blockers.length > 0 && (
            <div className="inline-error" role="alert">
              Решение заблокировано:{" "}
              {selectedReview.decision_blockers.join(", ")}
            </div>
          )}
          <label className="field">
            <span>Решение</span>
            <select
              value={decision}
              onChange={(event) => {
                setDecision(event.target.value as "APPROVED" | "REJECTED");
                setReviewAttestation((current) => ({
                  ...current,
                  acknowledged: false,
                }));
              }}
            >
              <option value="APPROVED">Подтвердить</option>
              <option value="REJECTED">Отклонить</option>
            </select>
          </label>
          <AttestationFields
            value={reviewAttestation}
            setValue={setReviewAttestation}
            projectCode={project.code}
            acknowledgement="Подтверждаю сверку точных параметров, источников, версии модели и комплекта документов; я не являюсь автором записи."
          />
          {reviewError !== null && (
            <div className="inline-error" role="alert">
              {reviewError}
            </div>
          )}
          <div className="form-actions">
            <button
              className="button button--secondary"
              type="button"
              onClick={() => setReviewId(null)}
            >
              Закрыть
            </button>
            <button
              className={
                decision === "APPROVED"
                  ? "button button--critical"
                  : "button button--primary"
              }
              type="submit"
              disabled={
                decisionMutation.isPending || !selectedReview.decision_allowed
              }
            >
              {decisionMutation.isPending ? "Фиксация…" : "Записать решение"}
            </button>
          </div>
        </form>
      )}

      <form className="entry-form controlled-form" onSubmit={submitCalculation}>
        <section className="entry-form__intro">
          <p className="eyebrow">04 · независимый пересчёт</p>
          <h2>Зафиксировать версионный риск-резерв</h2>
          <p>
            Сумма вычисляется только backend по утверждённой модели. Второй
            алгоритм независимо повторяет арифметику, а входная сигнатура
            связывает риски, approvals, модель и manifest.
          </p>
        </section>
        {risk.current_calculation !== null && (
          <div className="workflow-result">
            <Icon name="shield" size={20} />
            <div>
              <strong>
                {formatMoney(
                  risk.current_calculation.calculation.expected_reserve,
                  risk.current_calculation.calculation.currency,
                )}
              </strong>
              <p>
                {risk.current_calculation.calculation_id} ·{" "}
                {risk.current_calculation.independent_validation_passed
                  ? "независимый пересчёт сошёлся"
                  : "независимый пересчёт не подтверждён"}
              </p>
              <small>
                input {risk.current_calculation.input_signature} · output{" "}
                {risk.current_calculation.output_hash}
              </small>
            </div>
            <StatusPill value={risk.current_calculation.status} compact />
          </div>
        )}
        <AttestationFields
          value={calculationAttestation}
          setValue={setCalculationAttestation}
          projectCode={project.code}
          acknowledgement="Подтверждаю запуск расчёта только по текущим проверенным рискам и понимаю, что несовпадение независимого пересчёта блокирует выпуск."
        />
        {calculationError !== null && (
          <div className="inline-error" role="alert">
            {calculationError}
          </div>
        )}
        <div className="form-actions">
          <button
            className="button button--critical"
            type="submit"
            disabled={
              calculationMutation.isPending ||
              risk.calculation_blockers.length > 0
            }
          >
            <Icon name="refresh" size={16} />
            {calculationMutation.isPending
              ? "Пересчёт…"
              : "Рассчитать и независимо проверить"}
          </button>
        </div>
      </form>
    </div>
  );
}
