import { useMemo, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  decidePassportFact,
  getPassportContext,
  getProject,
  newIdempotencyKey,
  submitPassportFact,
  validatePassport,
  type RequestContext,
} from "../api";
import { useAuth } from "../auth";
import { ErrorBlock, LoadingBlock } from "../components/Feedback";
import { Icon } from "../components/Icon";
import { StatusPill } from "../components/StatusPill";
import { displayValue, formatDateTime } from "../format";
import { Link } from "../navigation";
import {
  validatePassportDecision,
  validatePassportSubmission,
  validatePassportValidation,
  type PassportSubmissionDraft,
} from "../passportWorkflow";
import type {
  ApprovalDecision,
  PassportFactReview,
  RuntimeConfig,
} from "../types";

interface AttestationDraft {
  reason: string;
  projectCodeConfirmation: string;
  acknowledged: boolean;
}

const blankAttestation: AttestationDraft = {
  reason: "",
  projectCodeConfirmation: "",
  acknowledged: false,
};

const decisionLabels: Record<ApprovalDecision, string> = {
  APPROVED: "Подтвердить факт",
  CHANGES_REQUESTED: "Вернуть на доработку",
  REJECTED: "Отклонить факт",
};

export function PassportPage({
  config,
  projectId,
}: {
  config: RuntimeConfig;
  projectId: string;
}) {
  const auth = useAuth();
  const queryClient = useQueryClient();
  const [fieldName, setFieldName] = useState("");
  const [submission, setSubmission] = useState<PassportSubmissionDraft>({
    ...blankAttestation,
    observationIds: [],
  });
  const [submissionKey, setSubmissionKey] = useState<string | null>(null);
  const [submissionError, setSubmissionError] = useState<string | null>(null);
  const [selectedReviewId, setSelectedReviewId] = useState<string | null>(null);
  const [decision, setDecision] = useState<ApprovalDecision>("APPROVED");
  const [reviewDraft, setReviewDraft] =
    useState<AttestationDraft>(blankAttestation);
  const [reviewKey, setReviewKey] = useState<string | null>(null);
  const [reviewError, setReviewError] = useState<string | null>(null);
  const [validationDraft, setValidationDraft] =
    useState<AttestationDraft>(blankAttestation);
  const [validationKey, setValidationKey] = useState<string | null>(null);
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
  const passportQuery = useQuery({
    queryKey: ["passport-context", projectId, fieldName],
    queryFn: ({ signal }) =>
      getPassportContext(context, projectId, fieldName || undefined, signal),
  });
  const selectedReview =
    passportQuery.data?.facts.find(
      (item) => item.fact.fact_id === selectedReviewId,
    ) ?? null;

  const invalidatePassport = async () => {
    await Promise.all([
      queryClient.invalidateQueries({
        queryKey: ["passport-context", projectId],
      }),
      queryClient.invalidateQueries({
        queryKey: ["records", projectId, "EVIDENCE"],
      }),
      queryClient.invalidateQueries({ queryKey: ["work-items"] }),
      queryClient.invalidateQueries({ queryKey: ["workbench", projectId] }),
    ]);
  };
  const submitMutation = useMutation({
    mutationFn: (idempotencyKey: string) => {
      if (projectQuery.data === undefined || passportQuery.data === undefined) {
        throw new Error("Контекст паспорта не загружен.");
      }
      const error = validatePassportSubmission(
        submission,
        passportQuery.data,
        projectQuery.data.code,
      );
      if (error !== null) {
        throw new Error(error);
      }
      const source = passportQuery.data.evidence_candidates.find(
        (candidate) =>
          candidate.observation.observation_id === submission.observationIds[0],
      );
      if (source === undefined) {
        throw new Error("Выбранное доказательство исчезло из контекста.");
      }
      return submitPassportFact(context, {
        projectId,
        fieldName: passportQuery.data.selected_field_name,
        value: source.observation.value,
        unit: source.observation.unit,
        observationIds: submission.observationIds,
        expectedDocumentSetRevisionId:
          passportQuery.data.document_set_revision_id,
        requirementsVersionId: passportQuery.data.requirements_version_id,
        reason: submission.reason.trim(),
        idempotencyKey,
      });
    },
    onSuccess: async (fact) => {
      setSubmission({
        ...blankAttestation,
        observationIds: [],
      });
      setSubmissionKey(null);
      setSubmissionError(null);
      setSelectedReviewId(fact.fact_id);
      await invalidatePassport();
    },
  });
  const decisionMutation = useMutation({
    mutationFn: (idempotencyKey: string) => {
      if (projectQuery.data === undefined || selectedReview === null) {
        throw new Error("Неизменяемый контекст проверки не выбран.");
      }
      const error = validatePassportDecision(
        reviewDraft,
        selectedReview,
        projectQuery.data.code,
      );
      if (error !== null) {
        throw new Error(error);
      }
      return decidePassportFact(context, {
        projectId,
        factId: selectedReview.fact.fact_id,
        decision,
        expectedFactUpdatedAt: selectedReview.fact.updated_at,
        expectedTaskUpdatedAt: selectedReview.task_updated_at,
        reason: reviewDraft.reason.trim(),
        idempotencyKey,
      });
    },
    onSuccess: async () => {
      setReviewDraft(blankAttestation);
      setReviewKey(null);
      setReviewError(null);
      setSelectedReviewId(null);
      await invalidatePassport();
    },
  });
  const validationMutation = useMutation({
    mutationFn: (idempotencyKey: string) => {
      if (projectQuery.data === undefined) {
        throw new Error("Проект не загружен.");
      }
      const error = validatePassportValidation(
        validationDraft,
        projectQuery.data.code,
      );
      if (error !== null) {
        throw new Error(error);
      }
      return validatePassport(context, {
        projectId,
        reason: validationDraft.reason.trim(),
        idempotencyKey,
      });
    },
    onSuccess: async () => {
      setValidationDraft(blankAttestation);
      setValidationKey(null);
      setValidationError(null);
      await invalidatePassport();
    },
  });

  if (projectQuery.isError || passportQuery.isError) {
    const failed = projectQuery.isError ? projectQuery : passportQuery;
    return (
      <div className="page">
        <ErrorBlock
          error={failed.error}
          onRetry={() => {
            void projectQuery.refetch();
            void passportQuery.refetch();
          }}
        />
      </div>
    );
  }
  if (projectQuery.isPending || passportQuery.isPending) {
    return (
      <div className="page">
        <LoadingBlock label="Проверка комплекта, политики и фактов паспорта" />
      </div>
    );
  }

  const project = projectQuery.data;
  const passport = passportQuery.data;
  const activeField = passport.selected_field_name;
  const verifiedRequiredFacts = passport.facts.filter(
    (item) =>
      item.fact.status === "VERIFIED" &&
      passport.required_fields.includes(item.fact.field_name),
  ).length;
  const submissionValidation = validatePassportSubmission(
    submission,
    passport,
    project.code,
  );
  const canAuthor =
    [
      "EXTRACTION_IN_PROGRESS",
      "EXTRACTION_REVIEW",
      "BOQ_IN_PROGRESS",
      "BOQ_REVIEW",
    ].includes(project.state) &&
    (auth.roles.includes("TECHNICAL_EXPERT") ||
      auth.roles.includes("REVIEWER"));
  const currentFact = passport.facts.find(
    (item) => item.fact.field_name === activeField,
  );

  const changeSubmission = (patch: Partial<PassportSubmissionDraft>) => {
    setSubmission((current) => ({
      ...current,
      ...patch,
      acknowledged: false,
    }));
    setSubmissionKey(null);
    setSubmissionError(null);
    submitMutation.reset();
  };
  const submitFact = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSubmissionError(submissionValidation);
    if (submissionValidation !== null || !canAuthor) {
      return;
    }
    let key = submissionKey;
    if (key === null) {
      key = newIdempotencyKey();
      setSubmissionKey(key);
    }
    submitMutation.mutate(key);
  };
  const reviewValidation =
    selectedReview === null
      ? "Выберите факт со статусом IN_REVIEW."
      : validatePassportDecision(reviewDraft, selectedReview, project.code);
  const submitDecision = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setReviewError(reviewValidation);
    if (reviewValidation !== null) {
      return;
    }
    let key = reviewKey;
    if (key === null) {
      key = newIdempotencyKey();
      setReviewKey(key);
    }
    decisionMutation.mutate(key);
  };
  const currentValidationError = validatePassportValidation(
    validationDraft,
    project.code,
  );
  const submitValidation = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setValidationError(currentValidationError);
    if (currentValidationError !== null) {
      return;
    }
    let key = validationKey;
    if (key === null) {
      key = newIdempotencyKey();
      setValidationKey(key);
    }
    validationMutation.mutate(key);
  };

  return (
    <div className="page controlled-workflow-page passport-page">
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
        <span>Паспорт проекта</span>
      </nav>

      <header className="entry-header">
        <div>
          <p className="eyebrow">Паспорт проекта · доказуемые факты</p>
          <h1>Контроль исходных параметров</h1>
          <p>
            Значение нельзя ввести вручную на этом экране. Оно переносится
            только из наблюдений текущего подтверждённого комплекта, а решение
            принимает другой пользователь назначенной роли.
          </p>
        </div>
        <div className="entry-header__guard">
          <Icon name="shield" size={22} />
          <span>
            Требования {passport.requirements_version_id} · комплект{" "}
            {passport.document_set_revision_id} · проверяющий{" "}
            {passport.review_role}
          </span>
        </div>
      </header>

      <section className="passport-overview">
        <article>
          <span>Обязательные поля</span>
          <strong>{passport.required_fields.length}</strong>
          <small>{passport.required_fields.join(", ")}</small>
        </article>
        <article>
          <span>Проверенные факты</span>
          <strong>{verifiedRequiredFacts}</strong>
          <small>из {passport.required_fields.length} обязательных</small>
        </article>
        <article
          className={
            passport.validation.findings.length > 0 ? "is-danger" : "is-ok"
          }
        >
          <span>Блокирующие findings</span>
          <strong>{passport.validation.findings.length}</strong>
          <small>
            snapshot{" "}
            {passport.validation.passport.passport_version.slice(0, 12)}
          </small>
        </article>
      </section>

      {passport.validation.findings.length > 0 && (
        <section className="blocker-panel" role="alert">
          <Icon name="warning" size={20} />
          <div>
            <strong>
              Паспорт пока не открывает переход к следующему этапу
            </strong>
            <ul>
              {passport.validation.findings.map((finding) => (
                <li key={`${finding.code}:${finding.entity_ids.join(":")}`}>
                  {finding.message}
                </li>
              ))}
            </ul>
          </div>
        </section>
      )}

      <section className="review-ledger">
        <div className="review-ledger__heading">
          <div>
            <p className="eyebrow">01 · состав паспорта</p>
            <h2>Текущие факты и экспертные задачи</h2>
          </div>
          <StatusPill value={project.state} />
        </div>
        <div className="passport-fact-grid">
          {[...passport.required_fields, ...passport.optional_fields].map(
            (name) => {
              const review = passport.facts.find(
                (item) => item.fact.field_name === name,
              );
              return (
                <article key={name} className="passport-fact-card">
                  <div>
                    <span className="eyebrow">
                      {passport.required_fields.includes(name)
                        ? "Обязательное"
                        : "Дополнительное"}
                      {passport.independently_verified_fields.includes(name)
                        ? " · два источника"
                        : ""}
                    </span>
                    <h3>{name}</h3>
                  </div>
                  {review === undefined ? (
                    <p>Актуальный факт отсутствует.</p>
                  ) : (
                    <>
                      <code>{displayValue(review.fact.value)}</code>
                      <small>
                        {review.fact.observation_ids.length} источн. ·{" "}
                        {formatDateTime(review.fact.updated_at)}
                      </small>
                      <div className="passport-fact-card__status">
                        <StatusPill value={review.fact.status} compact />
                        <StatusPill value={review.task_status} compact />
                      </div>
                      {review.fact.status === "IN_REVIEW" && (
                        <button
                          className="button button--secondary"
                          type="button"
                          onClick={() => {
                            setSelectedReviewId(review.fact.fact_id);
                            setReviewDraft(blankAttestation);
                            setReviewKey(null);
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
            },
          )}
        </div>
      </section>

      <form className="entry-form controlled-form" onSubmit={submitFact}>
        <section className="entry-form__intro">
          <p className="eyebrow">02 · подготовка факта</p>
          <h2>Выбрать совпадающие доказательства</h2>
          <p>
            Для критического поля сервер повторно проверит независимые
            квалификации, методы, организацию адаптера и неизменность
            документного manifest.
          </p>
        </section>
        <label className="field">
          <span>Поле утверждённого паспорта</span>
          <select
            value={activeField}
            onChange={(event) => {
              setFieldName(event.target.value);
              changeSubmission({ observationIds: [] });
            }}
          >
            {[...passport.required_fields, ...passport.optional_fields].map(
              (name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ),
            )}
          </select>
        </label>
        {currentFact !== undefined && (
          <p className="inline-warning">
            Новая запись станет новой ревизией поля и пометит текущий факт{" "}
            {currentFact.fact.fact_id} как superseded.
          </p>
        )}
        {passport.unresolved_conflict_ids.length > 0 && (
          <div className="inline-error" role="alert">
            Неразрешённые конфликты:{" "}
            {passport.unresolved_conflict_ids.join(", ")}. Сначала завершите
            отдельный workflow разрешения.
          </div>
        )}
        <div className="evidence-choice-grid">
          {passport.evidence_candidates.map((candidate) => {
            const observation = candidate.observation;
            const checked = submission.observationIds.includes(
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
                    changeSubmission({
                      observationIds: event.target.checked
                        ? [
                            ...submission.observationIds,
                            observation.observation_id,
                          ]
                        : submission.observationIds.filter(
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
                    <StatusPill value={observation.status} compact />
                  </span>
                  <code>{displayValue(observation.value)}</code>
                  <small>
                    {observation.unit ?? "без единицы"} ·{" "}
                    {observation.location.locator}
                  </small>
                  <small>
                    Домен {candidate.independence_domain ?? "не определён"} ·{" "}
                    {candidate.adapter_qualification_id ?? "без квалификации"}
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
        {passport.evidence_candidates.length === 0 && (
          <div className="empty-state">
            <Icon name="trace" size={22} />
            <strong>Для поля нет доказательств текущего комплекта</strong>
            <p>
              Сначала выполните извлечение, независимую сверку либо оформите
              ручное наблюдение через его отдельный экспертный контур.
            </p>
            <div className="form-actions">
              <Link
                className="button button--secondary"
                to={`/projects/${encodeURIComponent(projectId)}/evidence/reconcile`}
              >
                Независимая сверка
              </Link>
              <Link
                className="button button--secondary"
                to={`/projects/${encodeURIComponent(projectId)}/evidence/manual`}
              >
                Ручное наблюдение
              </Link>
            </div>
          </div>
        )}
        {passport.candidates_truncated && (
          <p className="inline-warning">
            Показаны первые 100 наблюдений. Выпуск нельзя основывать на
            невидимой записи: сузьте доказательный набор.
          </p>
        )}
        <div className="form-grid">
          <label className="field form-grid__wide">
            <span>Основание формирования факта</span>
            <textarea
              rows={4}
              maxLength={2000}
              value={submission.reason}
              onChange={(event) =>
                changeSubmission({ reason: event.target.value })
              }
            />
          </label>
          <label className="field">
            <span>Точный код проекта</span>
            <input
              value={submission.projectCodeConfirmation}
              onChange={(event) =>
                changeSubmission({
                  projectCodeConfirmation: event.target.value,
                })
              }
              autoComplete="off"
            />
          </label>
          <label className="attestation form-grid__wide">
            <input
              type="checkbox"
              checked={submission.acknowledged}
              onChange={(event) =>
                setSubmission((current) => ({
                  ...current,
                  acknowledged: event.target.checked,
                }))
              }
            />
            <span>
              Подтверждаю, что выбранные наблюдения воспроизводят один факт и
              относятся к актуальному комплекту; значение не редактировалось.
            </span>
          </label>
        </div>
        {(submissionError !== null || submitMutation.isError) && (
          <div className="inline-error" role="alert">
            {submissionError ??
              (submitMutation.error instanceof Error
                ? submitMutation.error.message
                : "Факт не создан.")}
          </div>
        )}
        <div className="form-actions">
          <button
            className="button button--primary"
            type="submit"
            disabled={
              submissionValidation !== null ||
              submitMutation.isPending ||
              !canAuthor
            }
          >
            <Icon name="plus" size={16} />
            {submitMutation.isPending ? "Фиксация…" : "Создать ревизию факта"}
          </button>
        </div>
      </form>

      {selectedReview !== null && (
        <form className="entry-form controlled-form" onSubmit={submitDecision}>
          <section className="entry-form__intro">
            <p className="eyebrow">03 · четыре глаза</p>
            <h2>Решение по {selectedReview.fact.field_name}</h2>
            <p>
              Факт и его источники неизменяемы. Автор{" "}
              {selectedReview.fact.created_by}; задача{" "}
              {selectedReview.fact.approval_task_id}; ревизия{" "}
              {formatDateTime(selectedReview.fact.updated_at)}.
            </p>
          </section>
          <div className="source-proof">
            <span>Проверяемое значение</span>
            <code>{displayValue(selectedReview.fact.value)}</code>
            <small>
              Источники: {selectedReview.fact.observation_ids.join(", ")}
            </small>
            <small>
              Независимые leaf sources:{" "}
              {selectedReview.fact.independence_source_ids.join(", ")}
            </small>
          </div>
          {selectedReview.decision_blockers.length > 0 && (
            <div className="inline-error" role="alert">
              Решение заблокировано:{" "}
              {selectedReview.decision_blockers.join(", ")}
            </div>
          )}
          <div className="form-grid">
            <label className="field">
              <span>Экспертное решение</span>
              <select
                value={decision}
                onChange={(event) => {
                  setDecision(event.target.value as ApprovalDecision);
                  setReviewDraft((current) => ({
                    ...current,
                    acknowledged: false,
                  }));
                  setReviewKey(null);
                }}
              >
                {Object.entries(decisionLabels).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label className="field form-grid__wide">
              <span>Основание решения</span>
              <textarea
                rows={4}
                maxLength={2000}
                value={reviewDraft.reason}
                onChange={(event) => {
                  setReviewDraft((current) => ({
                    ...current,
                    reason: event.target.value,
                    acknowledged: false,
                  }));
                  setReviewKey(null);
                }}
              />
            </label>
            <label className="field">
              <span>Точный код проекта</span>
              <input
                value={reviewDraft.projectCodeConfirmation}
                onChange={(event) => {
                  setReviewDraft((current) => ({
                    ...current,
                    projectCodeConfirmation: event.target.value,
                    acknowledged: false,
                  }));
                  setReviewKey(null);
                }}
                autoComplete="off"
              />
            </label>
            <label className="attestation form-grid__wide">
              <input
                type="checkbox"
                checked={reviewDraft.acknowledged}
                onChange={(event) =>
                  setReviewDraft((current) => ({
                    ...current,
                    acknowledged: event.target.checked,
                  }))
                }
              />
              <span>
                Я независимо проверил(а) значение, единицу, локаторы,
                квалификации источников, текущий manifest и отсутствие
                конфликта. Решение относится к точной ревизии выше.
              </span>
            </label>
          </div>
          {(reviewError !== null || decisionMutation.isError) && (
            <div className="inline-error" role="alert">
              {reviewError ??
                (decisionMutation.error instanceof Error
                  ? decisionMutation.error.message
                  : "Решение не сохранено.")}
            </div>
          )}
          <div className="form-actions">
            <button
              className="button button--secondary"
              type="button"
              onClick={() => setSelectedReviewId(null)}
            >
              Закрыть
            </button>
            <button
              className="button button--primary"
              type="submit"
              disabled={reviewValidation !== null || decisionMutation.isPending}
            >
              <Icon name="check" size={16} />
              {decisionMutation.isPending
                ? "Сохранение…"
                : decisionLabels[decision]}
            </button>
          </div>
        </form>
      )}

      <form className="entry-form controlled-form" onSubmit={submitValidation}>
        <section className="entry-form__intro">
          <p className="eyebrow">04 · формальная проверка</p>
          <h2>Зафиксировать результат stage gate</h2>
          <p>
            Проверка пересобирает паспорт из текущих строк и обновляет
            блокирующие findings. Она не превращает неподтверждённые факты в
            проверенные.
          </p>
        </section>
        <div className="form-grid">
          <label className="field form-grid__wide">
            <span>Основание запуска проверки</span>
            <textarea
              rows={3}
              maxLength={2000}
              value={validationDraft.reason}
              onChange={(event) => {
                setValidationDraft((current) => ({
                  ...current,
                  reason: event.target.value,
                  acknowledged: false,
                }));
                setValidationKey(null);
              }}
            />
          </label>
          <label className="field">
            <span>Точный код проекта</span>
            <input
              value={validationDraft.projectCodeConfirmation}
              onChange={(event) => {
                setValidationDraft((current) => ({
                  ...current,
                  projectCodeConfirmation: event.target.value,
                  acknowledged: false,
                }));
                setValidationKey(null);
              }}
              autoComplete="off"
            />
          </label>
          <label className="attestation form-grid__wide">
            <input
              type="checkbox"
              checked={validationDraft.acknowledged}
              onChange={(event) =>
                setValidationDraft((current) => ({
                  ...current,
                  acknowledged: event.target.checked,
                }))
              }
            />
            <span>
              Подтверждаю запуск проверки по текущим фактам без ручного
              подавления обнаруженных блокеров.
            </span>
          </label>
        </div>
        {(validationError !== null || validationMutation.isError) && (
          <div className="inline-error" role="alert">
            {validationError ??
              (validationMutation.error instanceof Error
                ? validationMutation.error.message
                : "Паспорт не проверен.")}
          </div>
        )}
        {validationMutation.data !== undefined && (
          <div className="workflow-result" role="status">
            <Icon
              name={
                validationMutation.data.findings.length === 0
                  ? "check"
                  : "warning"
              }
              size={20}
            />
            <div>
              <strong>
                {validationMutation.data.findings.length === 0
                  ? "Stage gate паспорта выполнен"
                  : "Stage gate остаётся заблокирован"}
              </strong>
              <p>
                Findings: {validationMutation.data.findings.length} · версия{" "}
                {validationMutation.data.passport.passport_version}
              </p>
            </div>
          </div>
        )}
        <div className="form-actions">
          <Link
            className="button button--secondary"
            to={`/projects/${encodeURIComponent(projectId)}/EVIDENCE`}
          >
            К реестру
          </Link>
          <button
            className="button button--primary"
            type="submit"
            disabled={
              currentValidationError !== null || validationMutation.isPending
            }
          >
            <Icon name="refresh" size={16} />
            {validationMutation.isPending
              ? "Проверка…"
              : "Пересобрать stage gate"}
          </button>
        </div>
      </form>
    </div>
  );
}
