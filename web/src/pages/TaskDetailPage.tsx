import { useMemo, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  decideWorkItem,
  getWorkItem,
  newIdempotencyKey,
  type RequestContext,
} from "../api";
import { useAuth } from "../auth";
import { ErrorBlock, LoadingBlock } from "../components/Feedback";
import { Icon } from "../components/Icon";
import { StatusPill } from "../components/StatusPill";
import {
  parseIdentifierList,
  validateDecisionDraft,
  type DecisionDraft,
} from "../decision";
import { formatDateTime } from "../format";
import { roleLabels, taskLabels } from "../labels";
import { Link } from "../navigation";
import type {
  ApprovalDecision,
  ProjectRecordSection,
  RuntimeConfig,
} from "../types";

const decisionLabels: Record<ApprovalDecision, string> = {
  APPROVED: "Утвердить",
  CHANGES_REQUESTED: "Вернуть на доработку",
  REJECTED: "Отклонить",
};

const blockerLabels: Record<string, string> = {
  TASK_NOT_PENDING: "Задача уже завершена и не может быть решена повторно.",
  DEDICATED_WORKFLOW_REQUIRED:
    "Для этой задачи требуется специализированная проверка исходных данных.",
  FOUR_EYES_TASK_CREATOR:
    "Создатель обязательной задачи не может принять решение по ней.",
  FOUR_EYES_CHANGE_AUTHOR:
    "Автор критического ручного изменения не может утвердить его.",
  MANUAL_CHANGE_MISSING:
    "Связанная запись ручного изменения отсутствует. Решение заблокировано.",
};

function sectionForEntity(entityType: string): ProjectRecordSection {
  const value = entityType.toLowerCase();
  if (/(quote|price|rfq|nomenclature|analogue)/.test(value)) {
    return "PRICING";
  }
  if (/(boq|quantity|scope|work)/.test(value)) {
    return "BOQ_SCOPE";
  }
  if (/(contract|risk|logistic|mobil|finance)/.test(value)) {
    return "CONTRACT_RISK";
  }
  if (/(actual|variance|calibration)/.test(value)) {
    return "ACTUALS";
  }
  if (/(evidence|observation|conflict|passport)/.test(value)) {
    return "EVIDENCE";
  }
  return "APPROVALS";
}

const initialDraft: DecisionDraft = {
  decision: "CHANGES_REQUESTED",
  reason: "",
  evidenceText: "",
  acknowledged: false,
  approvalProjectCode: "",
};

export function TaskDetailPage({
  config,
  taskId,
}: {
  config: RuntimeConfig;
  taskId: string;
}) {
  const auth = useAuth();
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<DecisionDraft>(initialDraft);
  const [formError, setFormError] = useState<string | null>(null);
  const context = useMemo<RequestContext>(
    () => ({
      apiBasePath: config.api_base_path,
      authorizationHeaders: auth.authorizationHeaders,
    }),
    [auth.authorizationHeaders, config.api_base_path],
  );
  const query = useQuery({
    queryKey: ["work-item", taskId],
    queryFn: ({ signal }) => getWorkItem(context, taskId, signal),
  });
  const mutation = useMutation({
    mutationFn: (input: {
      decision: ApprovalDecision;
      reason: string;
      evidenceIds: string[];
      expectedTaskUpdatedAt: string;
      projectId: string;
      idempotencyKey: string;
    }) =>
      decideWorkItem(context, {
        projectId: input.projectId,
        taskId,
        decision: input.decision,
        reason: input.reason,
        expectedTaskUpdatedAt: input.expectedTaskUpdatedAt,
        evidenceIds: input.evidenceIds,
        idempotencyKey: input.idempotencyKey,
      }),
    onSuccess: async (result) => {
      setFormError(null);
      setDraft(initialDraft);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["work-item", taskId] }),
        queryClient.invalidateQueries({ queryKey: ["work-items"] }),
        queryClient.invalidateQueries({
          queryKey: ["workbench", query.data?.item.project_id],
        }),
        queryClient.invalidateQueries({
          queryKey: ["records", query.data?.item.project_id, "APPROVALS"],
        }),
      ]);
      await queryClient.refetchQueries({
        queryKey: ["work-item", result.task_id],
      });
    },
  });

  if (query.isPending) {
    return (
      <div className="page">
        <LoadingBlock label="Загрузка экспертной задачи" />
      </div>
    );
  }
  if (query.isError) {
    return (
      <div className="page">
        <ErrorBlock error={query.error} onRetry={() => void query.refetch()} />
      </div>
    );
  }

  const detail = query.data;
  const entitySection = sectionForEntity(detail.item.entity_type);
  const entityHref =
    detail.item.entity_type === "manual_change"
      ? `/projects/${encodeURIComponent(detail.item.project_id)}/manual-changes/${encodeURIComponent(detail.item.entity_id)}`
      : detail.item.entity_type === "passport_fact"
        ? `/projects/${encodeURIComponent(detail.item.project_id)}/passport/manage`
        : detail.item.entity_type === "contract_term"
          ? `/projects/${encodeURIComponent(detail.item.project_id)}/contract/manage`
          : `/projects/${encodeURIComponent(detail.item.project_id)}/${entitySection}`;
  const evidenceIds = parseIdentifierList(draft.evidenceText);
  const validationError = validateDecisionDraft(draft, detail.project.code);
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setFormError(validationError);
    if (validationError !== null || !detail.decision_allowed) {
      return;
    }
    let idempotencyKey: string;
    try {
      idempotencyKey = newIdempotencyKey();
    } catch (error) {
      setFormError(
        error instanceof Error
          ? error.message
          : "Не удалось создать ключ операции",
      );
      return;
    }
    mutation.mutate({
      decision: draft.decision,
      reason: draft.reason.trim(),
      evidenceIds,
      expectedTaskUpdatedAt: detail.item.updated_at,
      projectId: detail.item.project_id,
      idempotencyKey,
    });
  };
  const appendEvidence = (identifier: string) => {
    const next = [...new Set([...evidenceIds, identifier])];
    setDraft((current) => ({
      ...current,
      evidenceText: next.join("\n"),
      acknowledged: false,
    }));
    setFormError(null);
  };

  return (
    <div className="page">
      <nav className="breadcrumbs" aria-label="Навигационная цепочка">
        <Link to="/tasks">Мои проверки</Link>
        <span>/</span>
        <Link to={`/projects/${encodeURIComponent(detail.item.project_id)}`}>
          {detail.item.project_code}
        </Link>
        <span>/</span>
        <span>{detail.item.task_id}</span>
      </nav>

      <header className="task-detail-header">
        <div>
          <p className="eyebrow">Экспертное решение · четыре глаза</p>
          <h1>{taskLabels[detail.item.task_type] ?? detail.item.task_type}</h1>
          <p>
            {detail.item.entity_type} · {detail.item.entity_id}
          </p>
        </div>
        <div className="task-detail-header__status">
          <StatusPill value={detail.item.status} />
          <span>
            {detail.item.required
              ? "Обязательная проверка"
              : "Дополнительная проверка"}
          </span>
        </div>
      </header>

      <section className="task-context" aria-labelledby="task-context-title">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Неизменяемый контекст</p>
            <h2 id="task-context-title">Что именно проверяется</h2>
          </div>
          <Link className="text-link" to={entityHref}>
            {detail.item.entity_type === "manual_change"
              ? "Открыть точные before / after"
              : "Открыть связанный реестр"}
            <Icon name="arrow" size={14} />
          </Link>
        </div>
        <dl className="task-facts">
          <div>
            <dt>Проект</dt>
            <dd>
              {detail.project.code} · {detail.project.name}
            </dd>
          </div>
          <div>
            <dt>Целевая сущность</dt>
            <dd>
              {detail.item.entity_type} · {detail.item.entity_id}
            </dd>
          </div>
          <div>
            <dt>Назначенная роль</dt>
            <dd>{roleLabels[detail.item.assigned_role]}</dd>
          </div>
          <div>
            <dt>Автор задачи</dt>
            <dd>{detail.item.created_by ?? "Не указан"}</dd>
          </div>
          <div>
            <dt>Версия политики</dt>
            <dd>{detail.policy_version_id ?? "Не применимо"}</dd>
          </div>
          <div>
            <dt>Версия задачи</dt>
            <dd>{formatDateTime(detail.item.updated_at)}</dd>
          </div>
        </dl>
      </section>

      {detail.decision_blockers.length > 0 && (
        <section className="decision-blocker" role="alert">
          <Icon name="shield" size={24} />
          <div>
            <h2>Решение заблокировано</h2>
            <ul>
              {detail.decision_blockers.map((blocker) => (
                <li key={blocker}>{blockerLabels[blocker] ?? blocker}</li>
              ))}
            </ul>
          </div>
        </section>
      )}

      {detail.item.task_type === "CONFLICT_RESOLUTION" &&
        detail.item.entity_type === "evidence_conflict" && (
          <section className="dedicated-workflow-callout">
            <div>
              <p className="eyebrow">Специализированный workflow</p>
              <h2>Сравнить исходные наблюдения</h2>
              <p>
                Универсальное утверждение запрещено: нужно выбрать конкретное
                исходное значение с проверкой его provenance и версии конфликта.
              </p>
            </div>
            <Link
              className="button button--primary"
              to={`/projects/${encodeURIComponent(detail.item.project_id)}/conflicts/${encodeURIComponent(detail.item.entity_id)}/resolve`}
            >
              Открыть разрешение конфликта
              <Icon name="arrow" size={15} />
            </Link>
          </section>
        )}

      {detail.item.task_type === "MANUAL_EVIDENCE_REVIEW" &&
        detail.item.entity_type === "evidence_observation" && (
          <section className="dedicated-workflow-callout">
            <div>
              <p className="eyebrow">Специализированный workflow</p>
              <h2>Проверить ручное наблюдение и его provenance</h2>
              <p>
                Универсальное утверждение запрещено: необходимо сверить исходную
                редакцию, SHA-256, локатор, единицу и автора записи.
              </p>
            </div>
            <Link
              className="button button--primary"
              to={`/projects/${encodeURIComponent(detail.item.project_id)}/evidence/observations/${encodeURIComponent(detail.item.entity_id)}/review`}
            >
              Открыть независимую проверку
              <Icon name="arrow" size={15} />
            </Link>
          </section>
        )}

      {detail.item.task_type === "PASSPORT_FACT_REVIEW" &&
        detail.item.entity_type === "passport_fact" && (
          <section className="dedicated-workflow-callout">
            <div>
              <p className="eyebrow">Специализированный workflow</p>
              <h2>Сверить факт паспорта и независимые источники</h2>
              <p>
                Универсальное решение запрещено: экран паспорта повторно
                проверяет точное значение, текущий комплект, версию требований,
                квалификации независимых leaf sources и обе optimistic-версии.
              </p>
            </div>
            <Link className="button button--primary" to={entityHref}>
              Открыть проверку паспорта
              <Icon name="arrow" size={15} />
            </Link>
          </section>
        )}

      {detail.item.task_type === "CONTRACT_TERM_REVIEW" &&
        detail.item.entity_type === "contract_term" && (
          <section className="dedicated-workflow-callout">
            <div>
              <p className="eyebrow">Специализированный workflow</p>
              <h2>Сверить договорное условие и его источник</h2>
              <p>
                Универсальное утверждение запрещено. Договорный экран повторно
                проверяет актуальный комплект, утверждённую версию правил,
                точное значение, provenance и optimistic-версии записи и задачи.
              </p>
            </div>
            <Link className="button button--primary" to={entityHref}>
              Открыть проверку договора
              <Icon name="arrow" size={15} />
            </Link>
          </section>
        )}

      {detail.item.task_type === "MANUAL_CHANGE" &&
        detail.item.entity_type === "manual_change" && (
          <section className="dedicated-workflow-callout">
            <div>
              <p className="eyebrow">Обязательная сверка изменения</p>
              <h2>Сравнить точные состояния до и после</h2>
              <p>
                Решение должно относиться к неизменяемой after-записи,
                конкретной версии методики, комплекту документов и указанным
                наблюдениям. Не утверждайте задачу только по краткому названию.
              </p>
            </div>
            <Link className="button button--primary" to={entityHref}>
              Открыть before / after
              <Icon name="arrow" size={15} />
            </Link>
          </section>
        )}

      {detail.decision_allowed && (
        <form className="decision-form" onSubmit={submit}>
          <div className="section-heading">
            <div>
              <p className="eyebrow">Контролируемая запись</p>
              <h2>Зафиксировать решение</h2>
            </div>
            <p>
              После отправки запись попадёт в audit trail. Повтор с тем же
              ключом операции не создаст второе решение.
            </p>
          </div>

          <fieldset className="decision-options">
            <legend>Решение</legend>
            {(
              [
                "CHANGES_REQUESTED",
                "REJECTED",
                "APPROVED",
              ] as ApprovalDecision[]
            ).map((decision) => (
              <label key={decision}>
                <input
                  type="radio"
                  name="decision"
                  value={decision}
                  checked={draft.decision === decision}
                  onChange={() => {
                    setDraft((current) => ({
                      ...current,
                      decision,
                      acknowledged: false,
                      approvalProjectCode:
                        decision === "APPROVED"
                          ? current.approvalProjectCode
                          : "",
                    }));
                    setFormError(null);
                  }}
                />
                <span>
                  <strong>{decisionLabels[decision]}</strong>
                  <small>
                    {decision === "APPROVED"
                      ? "Может снять обязательный hard stop; нужны доказательства и точное подтверждение проекта."
                      : decision === "REJECTED"
                        ? "Фиксирует недопустимость текущего варианта."
                        : "Возвращает предмет проверки автору без утверждения."}
                  </small>
                </span>
              </label>
            ))}
          </fieldset>

          <label className="decision-field">
            <span>Основание решения</span>
            <textarea
              value={draft.reason}
              maxLength={4000}
              rows={5}
              required
              onChange={(event) => {
                setDraft((current) => ({
                  ...current,
                  reason: event.target.value,
                  acknowledged: false,
                }));
                setFormError(null);
              }}
              placeholder="Опишите выполненную проверку, найденные расхождения и основание решения."
            />
            <small>{draft.reason.length} / 4000</small>
          </label>

          <label className="decision-field">
            <span>Идентификаторы проверенных доказательств</span>
            <textarea
              value={draft.evidenceText}
              rows={4}
              onChange={(event) => {
                setDraft((current) => ({
                  ...current,
                  evidenceText: event.target.value,
                  acknowledged: false,
                }));
                setFormError(null);
              }}
              placeholder="По одному observation ID в строке"
            />
            <small>
              Сервер проверит существование каждого observation в этом проекте.
              Для утверждения список обязателен.
            </small>
          </label>

          {detail.candidate_evidence_ids.length > 0 && (
            <div className="evidence-candidates">
              <span>Доказательства из контекста задачи</span>
              <div>
                {detail.candidate_evidence_ids.map((identifier) => (
                  <button
                    key={identifier}
                    type="button"
                    onClick={() => appendEvidence(identifier)}
                  >
                    + {identifier}
                  </button>
                ))}
              </div>
            </div>
          )}

          {draft.decision === "APPROVED" && (
            <label className="decision-field decision-field--confirmation">
              <span>
                Введите шифр проекта <strong>{detail.project.code}</strong>
              </span>
              <input
                type="text"
                value={draft.approvalProjectCode}
                autoComplete="off"
                spellCheck={false}
                onChange={(event) => {
                  setDraft((current) => ({
                    ...current,
                    approvalProjectCode: event.target.value,
                    acknowledged: false,
                  }));
                  setFormError(null);
                }}
              />
              <small>
                Это подтверждение не заменяет backend-проверки и не выпускает
                цену автоматически.
              </small>
            </label>
          )}

          <label className="decision-acknowledgement">
            <input
              type="checkbox"
              checked={draft.acknowledged}
              onChange={(event) => {
                setDraft((current) => ({
                  ...current,
                  acknowledged: event.target.checked,
                }));
                setFormError(null);
              }}
            />
            <span>
              Я проверил целевую сущность, указанные доказательства и понимаю
              влияние решения на обязательные контуры проекта.
            </span>
          </label>

          {(formError !== null || mutation.isError) && (
            <div className="decision-form__error" role="alert">
              <Icon name="warning" size={18} />
              <span>
                {formError ??
                  (mutation.error instanceof Error
                    ? mutation.error.message
                    : "Не удалось записать решение")}
              </span>
            </div>
          )}

          <div className="decision-form__actions">
            <Link className="button button--secondary" to="/tasks">
              Отмена
            </Link>
            <button
              className={
                draft.decision === "APPROVED"
                  ? "button button--critical"
                  : "button button--primary"
              }
              type="submit"
              disabled={mutation.isPending}
            >
              {mutation.isPending
                ? "Фиксация…"
                : `${decisionLabels[draft.decision]} и записать в аудит`}
            </button>
          </div>
        </form>
      )}

      <section
        className="decision-history"
        aria-labelledby="decision-history-title"
      >
        <div className="section-heading">
          <div>
            <p className="eyebrow">Audit trail</p>
            <h2 id="decision-history-title">История решений</h2>
          </div>
          <span>{detail.decisions.length}</span>
        </div>
        {detail.decisions.length === 0 ? (
          <p className="decision-history__empty">
            По этой версии задачи решений ещё нет.
          </p>
        ) : (
          <div className="decision-history__list">
            {detail.decisions.map((decision) => (
              <article key={decision.approval_id}>
                <div>
                  <StatusPill value={decision.decision} compact />
                  <time dateTime={decision.decided_at}>
                    {formatDateTime(decision.decided_at)}
                  </time>
                </div>
                <strong>{decision.decided_by}</strong>
                <p>{decision.reason}</p>
                {decision.evidence_ids.length > 0 && (
                  <ul>
                    {decision.evidence_ids.map((identifier) => (
                      <li key={identifier}>{identifier}</li>
                    ))}
                  </ul>
                )}
              </article>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
