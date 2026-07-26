import { useMemo, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  decideContractTerm,
  finalizeContractCostImpact,
  getContractContext,
  getProject,
  newIdempotencyKey,
  proposeContractCostImpact,
  submitContractTerm,
  validateContract,
  type RequestContext,
} from "../api";
import { useAuth } from "../auth";
import {
  type ContractAttestation,
  type ContractImpactDraft,
  validateContractDecision,
  validateContractFinalization,
  validateContractImpact,
  validateContractSubmission,
  validateContractValidation,
} from "../contractWorkflow";
import { ErrorBlock, LoadingBlock } from "../components/Feedback";
import { Icon } from "../components/Icon";
import { StatusPill } from "../components/StatusPill";
import { displayValue, formatDateTime } from "../format";
import { Link } from "../navigation";
import type {
  ApprovalDecision,
  ContractTermKind,
  ContractTermReview,
  RuntimeConfig,
} from "../types";

const termLabels: Record<ContractTermKind, string> = {
  COMPLETION_DATES: "Сроки завершения",
  PHASING: "Этапность",
  PENALTIES: "Штрафы",
  RETENTION: "Удержания",
  BID_OR_PERFORMANCE_SECURITY: "Обеспечение заявки / исполнения",
  BANK_GUARANTEE: "Банковская гарантия",
  ADVANCE: "Аванс",
  PAYMENT_DEFERRAL: "Отсрочка платежа",
  WARRANTY: "Гарантийные обязательства",
  FIXED_PRICE: "Фиксированность цены",
  CURRENCY_RISK: "Валютные риски",
  DESIGN_ERROR_LIABILITY: "Ответственность за проектные ошибки",
  MOBILISATION: "Мобилизация",
  DOCUMENTATION: "Требования к документации",
  INDEXATION_LIMITS: "Ограничения индексации",
  ACCEPTANCE_PROCEDURE: "Порядок приёмки",
};

const decisionLabels: Record<ApprovalDecision, string> = {
  APPROVED: "Подтвердить условие",
  CHANGES_REQUESTED: "Вернуть на доработку",
  REJECTED: "Отклонить условие",
};

function blankAttestation(): ContractAttestation {
  return {
    reason: "",
    projectCodeConfirmation: "",
    acknowledged: false,
  };
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Операция не выполнена.";
}

export function ContractPage({
  config,
  projectId,
}: {
  config: RuntimeConfig;
  projectId: string;
}) {
  const auth = useAuth();
  const queryClient = useQueryClient();
  const [kind, setKind] = useState<ContractTermKind | undefined>();
  const [observationIds, setObservationIds] = useState<string[]>([]);
  const [submission, setSubmission] = useState(blankAttestation);
  const [submissionError, setSubmissionError] = useState<string | null>(null);
  const [reviewId, setReviewId] = useState<string | null>(null);
  const [decision, setDecision] = useState<ApprovalDecision>("APPROVED");
  const [reviewAttestation, setReviewAttestation] = useState(blankAttestation);
  const [reviewError, setReviewError] = useState<string | null>(null);
  const [impactTermId, setImpactTermId] = useState<string | null>(null);
  const [impact, setImpact] = useState<ContractImpactDraft>({
    ...blankAttestation(),
    mode: "NO_DETERMINISTIC_COST",
    noCostReason: "",
    candidateId: "",
  });
  const [impactError, setImpactError] = useState<string | null>(null);
  const [finalizeAttestation, setFinalizeAttestation] =
    useState(blankAttestation);
  const [finalizeError, setFinalizeError] = useState<string | null>(null);
  const [validationAttestation, setValidationAttestation] =
    useState(blankAttestation);
  const [validationError, setValidationError] = useState<string | null>(null);
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
  const contractQuery = useQuery({
    queryKey: ["contract-context", projectId, kind],
    queryFn: ({ signal }) =>
      getContractContext(context, projectId, kind, signal),
  });
  const selectedReview =
    contractQuery.data?.terms.find((item) => item.term.term_id === reviewId) ??
    null;
  const selectedImpact =
    contractQuery.data?.terms.find(
      (item) => item.term.term_id === impactTermId,
    ) ?? null;

  const invalidate = async () => {
    await Promise.all([
      queryClient.invalidateQueries({
        queryKey: ["contract-context", projectId],
      }),
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
      const contract = contractQuery.data;
      if (project === undefined || contract === undefined) {
        throw new Error("Контекст договора не загружен.");
      }
      const draft = { ...submission, observationIds };
      const guard = validateContractSubmission(draft, contract, project.code);
      if (guard !== null) {
        throw new Error(guard);
      }
      const source = contract.evidence_candidates.find(
        (candidate) =>
          candidate.observation.observation_id === observationIds[0],
      );
      if (
        source === undefined ||
        typeof source.observation.value !== "string"
      ) {
        throw new Error(
          "Серверное доказательство не содержит строкового значения условия.",
        );
      }
      return submitContractTerm(context, {
        projectId,
        kind: contract.selected_kind,
        value: source.observation.value,
        observationIds,
        expectedDocumentSetRevisionId: contract.document_set_revision_id,
        rulesVersionId: contract.rules_version_id,
        reason: submission.reason.trim(),
        idempotencyKey: newIdempotencyKey(),
      });
    },
    onSuccess: async (term) => {
      setObservationIds([]);
      setSubmission(blankAttestation());
      setSubmissionError(null);
      setReviewId(term.term_id);
      await invalidate();
    },
    onError: (error) => setSubmissionError(errorMessage(error)),
  });

  const decisionMutation = useMutation({
    mutationFn: async () => {
      const project = projectQuery.data;
      if (project === undefined || selectedReview === null) {
        throw new Error("Выберите неизменяемый контекст проверки.");
      }
      const guard = validateContractDecision(
        reviewAttestation,
        selectedReview,
        decision,
        project.code,
      );
      if (guard !== null) {
        throw new Error(guard);
      }
      return decideContractTerm(context, {
        projectId,
        termId: selectedReview.term.term_id,
        decision,
        expectedTermUpdatedAt: selectedReview.term.updated_at,
        expectedTaskUpdatedAt: selectedReview.task_updated_at,
        reason: reviewAttestation.reason.trim(),
        idempotencyKey: newIdempotencyKey(),
      });
    },
    onSuccess: async (result) => {
      setReviewAttestation(blankAttestation());
      setReviewError(null);
      setReviewId(null);
      if (result.term.verified) {
        setImpactTermId(result.term.term_id);
      }
      await invalidate();
    },
    onError: (error) => setReviewError(errorMessage(error)),
  });

  const impactMutation = useMutation({
    mutationFn: async () => {
      const project = projectQuery.data;
      const contract = contractQuery.data;
      if (
        project === undefined ||
        contract === undefined ||
        selectedImpact === null
      ) {
        throw new Error("Выберите подтверждённое договорное условие.");
      }
      const guard = validateContractImpact(
        impact,
        selectedImpact,
        contract.impact_candidates,
        project.code,
      );
      if (guard !== null) {
        throw new Error(guard);
      }
      if (impact.mode === "NO_DETERMINISTIC_COST") {
        return proposeContractCostImpact(context, {
          projectId,
          termId: selectedImpact.term.term_id,
          command: {
            amount: "0",
            no_cost_reason: impact.noCostReason.trim(),
          },
          expectedTermUpdatedAt: selectedImpact.term.updated_at,
          reason: impact.reason.trim(),
          idempotencyKey: newIdempotencyKey(),
        });
      }
      const candidate = contract.impact_candidates.find(
        (item) => item.derived_cost_model_id === impact.candidateId,
      );
      if (candidate === undefined) {
        throw new Error("Выбранная финансовая модель больше не актуальна.");
      }
      return proposeContractCostImpact(context, {
        projectId,
        termId: selectedImpact.term.term_id,
        command: {
          amount: candidate.amount,
          currency: candidate.currency,
          cost_component_line_id: candidate.cost_component_line_id,
          cost_component_semantic_key: candidate.cost_component_semantic_key,
          derived_cost_model_id: candidate.derived_cost_model_id,
        },
        expectedTermUpdatedAt: selectedImpact.term.updated_at,
        reason: impact.reason.trim(),
        idempotencyKey: newIdempotencyKey(),
      });
    },
    onSuccess: async (term) => {
      setImpact({
        ...blankAttestation(),
        mode: "NO_DETERMINISTIC_COST",
        noCostReason: "",
        candidateId: "",
      });
      setImpactError(null);
      setImpactTermId(term.term_id);
      await invalidate();
    },
    onError: (error) => setImpactError(errorMessage(error)),
  });

  const finalizeMutation = useMutation({
    mutationFn: async () => {
      const project = projectQuery.data;
      if (project === undefined || selectedImpact === null) {
        throw new Error("Выберите условие с согласованным влиянием.");
      }
      const guard = validateContractFinalization(
        finalizeAttestation,
        selectedImpact,
        project.code,
      );
      if (guard !== null) {
        throw new Error(guard);
      }
      return finalizeContractCostImpact(context, {
        projectId,
        termId: selectedImpact.term.term_id,
        expectedTermUpdatedAt: selectedImpact.term.updated_at,
        reason: finalizeAttestation.reason.trim(),
        idempotencyKey: newIdempotencyKey(),
      });
    },
    onSuccess: async () => {
      setFinalizeAttestation(blankAttestation());
      setFinalizeError(null);
      await invalidate();
    },
    onError: (error) => setFinalizeError(errorMessage(error)),
  });

  const validationMutation = useMutation({
    mutationFn: async () => {
      const project = projectQuery.data;
      if (project === undefined) {
        throw new Error("Проект не загружен.");
      }
      const guard = validateContractValidation(
        validationAttestation,
        project.code,
      );
      if (guard !== null) {
        throw new Error(guard);
      }
      return validateContract(context, {
        projectId,
        reason: validationAttestation.reason.trim(),
        idempotencyKey: newIdempotencyKey(),
      });
    },
    onSuccess: async () => {
      setValidationAttestation(blankAttestation());
      setValidationError(null);
      await invalidate();
    },
    onError: (error) => setValidationError(errorMessage(error)),
  });

  if (projectQuery.isError || contractQuery.isError) {
    const failed = projectQuery.isError ? projectQuery : contractQuery;
    return (
      <div className="page">
        <ErrorBlock
          error={failed.error}
          onRetry={() => void failed.refetch()}
        />
      </div>
    );
  }
  if (projectQuery.data === undefined || contractQuery.data === undefined) {
    return (
      <div className="page">
        <LoadingBlock label="Загрузка договорного контура" />
      </div>
    );
  }

  const project = projectQuery.data;
  const contract = contractQuery.data;
  const verifiedCount = contract.terms.filter(
    (item) =>
      contract.required_term_kinds.includes(item.term.kind) &&
      item.term.verified,
  ).length;
  const resolvedCount = contract.terms.filter(
    (item) =>
      contract.required_term_kinds.includes(item.term.kind) &&
      item.term.cost_impact_resolved,
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
  const submitImpact = (event: FormEvent) => {
    event.preventDefault();
    setImpactError(null);
    impactMutation.mutate();
  };
  const submitFinalize = (event: FormEvent) => {
    event.preventDefault();
    setFinalizeError(null);
    finalizeMutation.mutate();
  };
  const submitValidation = (event: FormEvent) => {
    event.preventDefault();
    setValidationError(null);
    validationMutation.mutate();
  };

  return (
    <div className="page controlled-workflow-page contract-page">
      <header className="page-heading">
        <div>
          <span className="eyebrow">Контур 9 · договорные риски</span>
          <h1>Условия договора и стоимостное влияние</h1>
          <p>
            Значение берётся только из серверного доказательства актуального
            комплекта. Подтверждение условия и его стоимостной трактовки
            выполняют независимые пользователи.
          </p>
        </div>
        <div className="page-heading__actions">
          <StatusPill value={contract.project_state} />
          <Link
            className="button button--secondary"
            to={`/projects/${encodeURIComponent(projectId)}/CONTRACT_RISK`}
          >
            Реестр контура
          </Link>
        </div>
      </header>

      <section className="passport-overview" aria-label="Сводка договора">
        <article>
          <span>Обязательных условий</span>
          <strong>{contract.required_term_kinds.length}</strong>
          <small>версия {contract.rules_version_id}</small>
        </article>
        <article
          className={
            verifiedCount === contract.required_term_kinds.length
              ? "is-ok"
              : "is-danger"
          }
        >
          <span>Подтверждено</span>
          <strong>{verifiedCount}</strong>
          <small>с четырьмя глазами</small>
        </article>
        <article
          className={
            resolvedCount === contract.required_term_kinds.length
              ? "is-ok"
              : "is-danger"
          }
        >
          <span>Влияние разрешено</span>
          <strong>{resolvedCount}</strong>
          <small>с источником или допущением</small>
        </article>
        <article
          className={
            contract.validation.findings.length === 0 ? "is-ok" : "is-danger"
          }
        >
          <span>Блокирующих выводов</span>
          <strong>{contract.validation.findings.length}</strong>
          <small>formal validation</small>
        </article>
      </section>

      <section className="section-heading">
        <div>
          <span className="eyebrow">Матрица обязательных условий</span>
          <h2>Текущий договорный паспорт</h2>
        </div>
        <button
          className="button button--secondary"
          type="button"
          onClick={() => void contractQuery.refetch()}
        >
          <Icon name="refresh" size={16} />
          Обновить
        </button>
      </section>
      <div className="passport-fact-grid">
        {contract.required_term_kinds.map((requiredKind) => {
          const review = contract.terms.find(
            (item) => item.term.kind === requiredKind,
          );
          return (
            <article key={requiredKind} className="passport-fact-card">
              <span className="eyebrow">
                {contract.evidence_field_names[requiredKind]}
              </span>
              <h3>{termLabels[requiredKind]}</h3>
              {review === undefined ? (
                <>
                  <p>Условие ещё не сформировано.</p>
                  <StatusPill value="UNVERIFIED" compact />
                </>
              ) : (
                <>
                  <p>{review.term.value}</p>
                  <div className="passport-fact-card__status">
                    <StatusPill
                      value={
                        review.decision_blockers.includes(
                          "TERM_INTEGRITY_FAILED",
                        )
                          ? "BLOCKED"
                          : review.term.verified
                            ? "VERIFIED"
                            : review.task_status
                      }
                      compact
                    />
                    <StatusPill
                      value={
                        review.term.cost_impact_resolved
                          ? "VALIDATED"
                          : "REVIEW_REQUIRED"
                      }
                      compact
                    />
                  </div>
                  <small>
                    Автор: {review.term.created_by} · обновлено{" "}
                    {formatDateTime(review.term.updated_at)}
                  </small>
                  <div className="button-row">
                    {!review.term.verified && (
                      <button
                        className="button button--secondary"
                        type="button"
                        onClick={() => setReviewId(review.term.term_id)}
                      >
                        Проверить
                      </button>
                    )}
                    {review.term.verified &&
                      !review.term.cost_impact_resolved && (
                        <button
                          className="button button--secondary"
                          type="button"
                          onClick={() => setImpactTermId(review.term.term_id)}
                        >
                          Стоимостное влияние
                        </button>
                      )}
                  </div>
                </>
              )}
            </article>
          );
        })}
      </div>

      <form className="entry-form controlled-form" onSubmit={submit}>
        <div className="entry-form__intro">
          <span className="eyebrow">Шаг 1 · сформировать условие</span>
          <h2>Точное значение из доказательства</h2>
          <p>
            Ручного поля значения нет: сервер повторно сверит идентичность,
            редакцию, поле, значение, статус и provenance до листа-источника.
          </p>
        </div>
        <div className="form-grid">
          <label className="field">
            <span>Тип условия</span>
            <select
              value={contract.selected_kind}
              onChange={(event) => {
                setKind(event.target.value as ContractTermKind);
                setObservationIds([]);
              }}
            >
              {contract.required_term_kinds.map((item) => (
                <option key={item} value={item}>
                  {termLabels[item]}
                </option>
              ))}
            </select>
          </label>
          <div className="field form-grid__wide">
            <span>Доказательства актуального комплекта</span>
            <div className="evidence-candidates">
              {contract.evidence_candidates.length === 0 && (
                <span>Подходящие наблюдения не найдены.</span>
              )}
              {contract.evidence_candidates.map((candidate) => {
                const id = candidate.observation.observation_id;
                return (
                  <label key={id}>
                    <input
                      type="checkbox"
                      checked={observationIds.includes(id)}
                      disabled={!candidate.eligible}
                      onChange={(event) =>
                        setObservationIds((current) =>
                          event.target.checked
                            ? [...current, id]
                            : current.filter((item) => item !== id),
                        )
                      }
                    />
                    <span>
                      <strong>
                        {displayValue(candidate.observation.value)}
                      </strong>
                      <small>
                        {candidate.observation.method} ·{" "}
                        {candidate.observation.location.locator} ·{" "}
                        {candidate.observation.status}
                      </small>
                      {!candidate.eligible && (
                        <small>
                          Блокировки: {candidate.blockers.join(", ")}
                        </small>
                      )}
                    </span>
                  </label>
                );
              })}
            </div>
          </div>
          <AttestationFields
            value={submission}
            onChange={setSubmission}
            projectCode={project.code}
          />
        </div>
        {submissionError !== null && (
          <p className="form-error" role="alert">
            {submissionError}
          </p>
        )}
        <div className="button-row">
          <button
            className="button button--primary"
            type="submit"
            disabled={submitMutation.isPending}
          >
            Создать задачу проверки
          </button>
        </div>
      </form>

      {selectedReview !== null && (
        <form className="entry-form controlled-form" onSubmit={submitDecision}>
          <div className="entry-form__intro">
            <span className="eyebrow">Шаг 2 · четыре глаза</span>
            <h2>{termLabels[selectedReview.term.kind]}</h2>
            <p>{selectedReview.term.value}</p>
          </div>
          <div className="form-grid">
            <label className="field">
              <span>Решение</span>
              <select
                value={decision}
                onChange={(event) =>
                  setDecision(event.target.value as ApprovalDecision)
                }
              >
                {Object.entries(decisionLabels).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <div className="field">
              <span>Неизменяемый контекст</span>
              <small>
                задача {selectedReview.term.approval_task_id} ·{" "}
                {selectedReview.task_status}
              </small>
              {selectedReview.decision_blockers.length > 0 && (
                <small>
                  Блокировки: {selectedReview.decision_blockers.join(", ")}
                </small>
              )}
            </div>
            <AttestationFields
              value={reviewAttestation}
              onChange={setReviewAttestation}
              projectCode={project.code}
            />
          </div>
          {reviewError !== null && (
            <p className="form-error" role="alert">
              {reviewError}
            </p>
          )}
          <div className="button-row">
            <button
              className="button button--primary"
              type="submit"
              disabled={decisionMutation.isPending}
            >
              {decisionLabels[decision]}
            </button>
            <button
              className="button button--secondary"
              type="button"
              onClick={() => setReviewId(null)}
            >
              Закрыть
            </button>
          </div>
        </form>
      )}

      {selectedImpact !== null && (
        <>
          {!selectedImpact.term.cost_impact_resolved &&
            selectedImpact.term.cost_impact_proposal === null && (
              <form
                className="entry-form controlled-form"
                onSubmit={submitImpact}
              >
                <div className="entry-form__intro">
                  <span className="eyebrow">Шаг 3 · стоимостная трактовка</span>
                  <h2>{termLabels[selectedImpact.term.kind]}</h2>
                  <p>
                    Ненулевое значение выбирается только из текущей
                    валидированной модели CONTRACT_FINANCE.
                  </p>
                </div>
                <div className="form-grid">
                  <label className="field">
                    <span>Основание</span>
                    <select
                      value={impact.mode}
                      onChange={(event) =>
                        setImpact(
                          event.target.value === "DERIVED_MODEL"
                            ? {
                                ...blankAttestation(),
                                mode: "DERIVED_MODEL",
                                noCostReason: "",
                                candidateId: "",
                              }
                            : {
                                ...blankAttestation(),
                                mode: "NO_DETERMINISTIC_COST",
                                noCostReason: "",
                                candidateId: "",
                              },
                        )
                      }
                    >
                      <option value="NO_DETERMINISTIC_COST">
                        Нет отдельной детерминированной стоимости
                      </option>
                      <option value="DERIVED_MODEL">
                        Проверенная финансовая модель
                      </option>
                    </select>
                  </label>
                  {impact.mode === "NO_DETERMINISTIC_COST" ? (
                    <label className="field form-grid__wide">
                      <span>Явное основание нулевого влияния</span>
                      <textarea
                        value={impact.noCostReason}
                        onChange={(event) =>
                          setImpact({
                            ...impact,
                            noCostReason: event.target.value,
                          })
                        }
                      />
                    </label>
                  ) : (
                    <label className="field form-grid__wide">
                      <span>Модель CONTRACT_FINANCE</span>
                      <select
                        value={impact.candidateId}
                        onChange={(event) =>
                          setImpact({
                            ...impact,
                            candidateId: event.target.value,
                          })
                        }
                      >
                        <option value="">Выберите модель</option>
                        {contract.impact_candidates.map((candidate) => (
                          <option
                            key={candidate.derived_cost_model_id}
                            value={candidate.derived_cost_model_id}
                            disabled={!candidate.eligible}
                          >
                            {candidate.amount} {candidate.currency} ·{" "}
                            {candidate.derived_cost_model_id}
                          </option>
                        ))}
                      </select>
                    </label>
                  )}
                  <AttestationFields
                    value={impact}
                    onChange={(value) => setImpact({ ...impact, ...value })}
                    projectCode={project.code}
                  />
                </div>
                {impactError !== null && (
                  <p className="form-error" role="alert">
                    {impactError}
                  </p>
                )}
                <div className="button-row">
                  <button
                    className="button button--primary"
                    type="submit"
                    disabled={impactMutation.isPending}
                  >
                    Создать обязательное согласование
                  </button>
                </div>
              </form>
            )}

          {selectedImpact.term.cost_impact_proposal !== null &&
            !selectedImpact.term.cost_impact_resolved && (
              <form
                className="entry-form controlled-form"
                onSubmit={submitFinalize}
              >
                <div className="entry-form__intro">
                  <span className="eyebrow">
                    Шаг 4 · завершить после согласования
                  </span>
                  <h2>
                    {selectedImpact.term.cost_impact_proposal.amount}{" "}
                    {selectedImpact.term.cost_impact_proposal.currency ?? ""}
                  </h2>
                  <p>
                    {selectedImpact.term.cost_impact_proposal.no_cost_reason ??
                      selectedImpact.term.cost_impact_proposal
                        .derived_cost_model_id}
                  </p>
                </div>
                <div className="passport-fact-grid">
                  {selectedImpact.term.approval_task_ids.map((taskId) => (
                    <article key={taskId} className="passport-fact-card">
                      <h3>Согласование влияния</h3>
                      <StatusPill
                        value={
                          selectedImpact.term.cost_impact_task_statuses[
                            taskId
                          ] ?? "MISSING"
                        }
                        compact
                      />
                      <Link
                        className="button button--secondary"
                        to={`/tasks/${encodeURIComponent(taskId)}`}
                      >
                        Открыть задачу
                      </Link>
                    </article>
                  ))}
                </div>
                <div className="form-grid">
                  <AttestationFields
                    value={finalizeAttestation}
                    onChange={setFinalizeAttestation}
                    projectCode={project.code}
                  />
                </div>
                {finalizeError !== null && (
                  <p className="form-error" role="alert">
                    {finalizeError}
                  </p>
                )}
                <div className="button-row">
                  <button
                    className="button button--primary"
                    type="submit"
                    disabled={finalizeMutation.isPending}
                  >
                    Зафиксировать согласованное влияние
                  </button>
                  <button
                    className="button button--secondary"
                    type="button"
                    onClick={() => void contractQuery.refetch()}
                  >
                    Обновить статусы задач
                  </button>
                </div>
              </form>
            )}
        </>
      )}

      <form className="entry-form controlled-form" onSubmit={submitValidation}>
        <div className="entry-form__intro">
          <span className="eyebrow">Формальная проверка</span>
          <h2>Пересобрать договорную оценку</h2>
          <p>
            Проверка сохраняет актуальные findings. Нулевое число findings не
            заменяет release gate, который независимо воспроизводит
            доказательства и согласования.
          </p>
        </div>
        <div className="form-grid">
          <AttestationFields
            value={validationAttestation}
            onChange={setValidationAttestation}
            projectCode={project.code}
          />
        </div>
        {validationError !== null && (
          <p className="form-error" role="alert">
            {validationError}
          </p>
        )}
        <div className="button-row">
          <button
            className="button button--primary"
            type="submit"
            disabled={validationMutation.isPending}
          >
            Запустить формальную проверку
          </button>
        </div>
      </form>
    </div>
  );
}

function AttestationFields({
  value,
  onChange,
  projectCode,
}: {
  value: ContractAttestation;
  onChange: (value: ContractAttestation) => void;
  projectCode: string;
}) {
  return (
    <>
      <label className="field form-grid__wide">
        <span>Обоснование</span>
        <textarea
          value={value.reason}
          maxLength={2000}
          onChange={(event) =>
            onChange({ ...value, reason: event.target.value })
          }
        />
      </label>
      <label className="field">
        <span>Шифр проекта: {projectCode}</span>
        <input
          value={value.projectCodeConfirmation}
          autoComplete="off"
          onChange={(event) =>
            onChange({
              ...value,
              projectCodeConfirmation: event.target.value,
            })
          }
        />
      </label>
      <label className="attestation form-grid__wide">
        <input
          type="checkbox"
          checked={value.acknowledged}
          onChange={(event) =>
            onChange({ ...value, acknowledged: event.target.checked })
          }
        />
        <span>
          Подтверждаю, что проверил(а) источник, редакцию и финансовые
          последствия этого действия.
        </span>
      </label>
    </>
  );
}
