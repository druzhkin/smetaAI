import { useMemo, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  applyQuantityManualChange,
  getProject,
  getQuantityManualChange,
  newIdempotencyKey,
  type RequestContext,
} from "../api";
import { useAuth } from "../auth";
import { ErrorBlock, LoadingBlock } from "../components/Feedback";
import { Icon } from "../components/Icon";
import { StatusPill } from "../components/StatusPill";
import { formatDateTime } from "../format";
import { Link } from "../navigation";
import type { RuntimeConfig } from "../types";

export function QuantityManualChangePage({
  config,
  projectId,
  changeId,
}: {
  config: RuntimeConfig;
  projectId: string;
  changeId: string;
}) {
  const auth = useAuth();
  const queryClient = useQueryClient();
  const [reason, setReason] = useState("");
  const [projectCode, setProjectCode] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [operationKey, setOperationKey] = useState<string | null>(null);
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
  const changeQuery = useQuery({
    queryKey: ["quantity-manual-change", projectId, changeId],
    queryFn: ({ signal }) =>
      getQuantityManualChange(context, projectId, changeId, signal),
  });
  const mutation = useMutation({
    mutationFn: (idempotencyKey: string) =>
      applyQuantityManualChange(context, {
        projectId,
        changeId,
        reason: reason.trim(),
        idempotencyKey,
      }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["quantity-manual-change", projectId, changeId],
        }),
        queryClient.invalidateQueries({
          queryKey: ["quantity-change-context", projectId],
        }),
        queryClient.invalidateQueries({ queryKey: ["work-items"] }),
        queryClient.invalidateQueries({ queryKey: ["workbench", projectId] }),
        queryClient.invalidateQueries({
          queryKey: ["records", projectId, "BOQ_SCOPE"],
        }),
      ]);
      setReason("");
      setProjectCode("");
      setAcknowledged(false);
      setFormError(null);
      setOperationKey(null);
    },
  });

  if (projectQuery.isPending || changeQuery.isPending) {
    return (
      <div className="page">
        <LoadingBlock label="Загрузка зарегистрированного изменения" />
      </div>
    );
  }
  if (projectQuery.isError || changeQuery.isError) {
    const failed = projectQuery.isError ? projectQuery : changeQuery;
    return (
      <div className="page">
        <ErrorBlock
          error={failed.error}
          onRetry={() => {
            void projectQuery.refetch();
            void changeQuery.refetch();
          }}
        />
      </div>
    );
  }

  const project = projectQuery.data;
  const change = changeQuery.data;
  const actorIsAuthor = auth.actorId === change.changed_by;
  const applyReady =
    change.status === "APPROVED" || change.status === "APPROVED_BY_POLICY";
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    let error: string | null = null;
    if (!actorIsAuthor) {
      error =
        "Применить запись может только зарегистрированный автор изменения.";
    } else if (!applyReady) {
      error = "Изменение ещё не прошло обязательное согласование.";
    } else if (reason.trim() === "") {
      error = "Укажите основание применения согласованной записи.";
    } else if (reason.length > 2000) {
      error = "Основание применения превышает 2000 символов.";
    } else if (projectCode.trim() !== project.code) {
      error = "Контрольный шифр проекта не совпадает.";
    } else if (!acknowledged) {
      error = "Подтвердите применение именно показанной after-записи.";
    }
    setFormError(error);
    if (error !== null) {
      return;
    }
    let key = operationKey;
    if (key === null) {
      try {
        key = newIdempotencyKey();
      } catch (keyError) {
        setFormError(
          keyError instanceof Error
            ? keyError.message
            : "Не удалось создать ключ операции.",
        );
        return;
      }
      setOperationKey(key);
    }
    mutation.mutate(key);
  };

  return (
    <div className="page">
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
        <span>Ручное изменение</span>
      </nav>

      <header className="entry-header">
        <div>
          <p className="eyebrow">{project.code} · неизменяемая запись</p>
          <h1>Изменение объёма</h1>
          <p>
            Перед применением сравните точное состояние до и после. Сервер не
            принимает от интерфейса повторно набранное значение: он применяет
            сохранённую after-запись и заново проверяет весь контекст.
          </p>
        </div>
        <StatusPill value={change.status} />
      </header>

      <section className="manual-change-summary">
        <dl className="task-facts">
          <div>
            <dt>Автор</dt>
            <dd>{change.changed_by}</dd>
          </div>
          <div>
            <dt>Создано</dt>
            <dd>{formatDateTime(change.changed_at)}</dd>
          </div>
          <div>
            <dt>Предыдущий объём</dt>
            <dd>{change.previous_quantity_id}</dd>
          </div>
          <div>
            <dt>Политика</dt>
            <dd>{change.policy_version_id}</dd>
          </div>
          <div>
            <dt>Комплект документов</dt>
            <dd>{change.document_set_revision_id}</dd>
          </div>
          <div>
            <dt>Критическое</dt>
            <dd>{change.critical ? "Да" : "Нет"}</dd>
          </div>
        </dl>
        <div className="manual-change-reason">
          <span>Основание автора</span>
          <p>{change.reason}</p>
        </div>
        <div className="manual-change-diff">
          <section>
            <span>До</span>
            <pre>{JSON.stringify(change.before, null, 2)}</pre>
          </section>
          <section>
            <span>После</span>
            <pre>{JSON.stringify(change.after, null, 2)}</pre>
          </section>
        </div>
      </section>

      {change.approval_task_id !== null && change.status !== "APPLIED" && (
        <section className="approval-callout">
          <div>
            <p className="eyebrow">Независимое согласование</p>
            <h2>
              {change.status === "PENDING_APPROVAL"
                ? "Ожидается решение проверяющего"
                : `Решение задачи: ${change.approval_task_status ?? "не определено"}`}
            </h2>
          </div>
          <Link
            className="button button--secondary"
            to={`/tasks/${encodeURIComponent(change.approval_task_id)}`}
          >
            Открыть задачу
          </Link>
        </section>
      )}

      {change.status === "APPLIED" ? (
        <section className="applied-change">
          <Icon name="check" size={24} />
          <div>
            <h2>Точная запись применена</h2>
            <p>
              Новый объём: {change.applied_quantity_id}. Применил{" "}
              {change.applied_by}{" "}
              {change.applied_at === null
                ? ""
                : formatDateTime(change.applied_at)}
              .
            </p>
          </div>
        </section>
      ) : applyReady && actorIsAuthor ? (
        <form className="entry-form" onSubmit={submit}>
          <div className="entry-form__intro">
            <p className="eyebrow">Атомарное применение</p>
            <h2>Применить согласованную after-запись</h2>
            <p>
              При изменении текущего объёма, комплекта документов, методики,
              задачи или доказательств операция будет заблокирована.
            </p>
          </div>
          <label className="decision-field">
            <span>Основание применения</span>
            <textarea
              value={reason}
              maxLength={2000}
              rows={4}
              required
              onChange={(event) => {
                setReason(event.target.value);
                setOperationKey(null);
                setFormError(null);
                mutation.reset();
              }}
            />
            <small>{reason.length} / 2000</small>
          </label>
          <label className="decision-field decision-field--confirmation">
            <span>Введите шифр проекта: {project.code}</span>
            <input
              type="text"
              value={projectCode}
              autoComplete="off"
              spellCheck={false}
              required
              onChange={(event) => {
                setProjectCode(event.target.value);
                setOperationKey(null);
                setFormError(null);
                mutation.reset();
              }}
            />
          </label>
          <label className="decision-acknowledgement">
            <input
              type="checkbox"
              checked={acknowledged}
              onChange={(event) => {
                setAcknowledged(event.target.checked);
                setOperationKey(null);
                setFormError(null);
                mutation.reset();
              }}
            />
            <span>
              Я повторно сравнил before и after и применяю именно показанную
              неизменяемую запись.
            </span>
          </label>
          {(formError !== null || mutation.isError) && (
            <div className="decision-form__error" role="alert">
              <Icon name="warning" size={18} />
              <span>
                {formError ??
                  (mutation.error instanceof Error
                    ? mutation.error.message
                    : "Не удалось применить изменение.")}
              </span>
            </div>
          )}
          <div className="decision-form__actions">
            <Link
              className="button button--secondary"
              to={`/projects/${encodeURIComponent(projectId)}/BOQ_SCOPE`}
            >
              Не применять
            </Link>
            <button
              className="button button--critical"
              type="submit"
              disabled={mutation.isPending}
            >
              {mutation.isPending
                ? "Независимая перепроверка…"
                : "Применить точную after-запись"}
            </button>
          </div>
        </form>
      ) : (
        <section className="decision-blocker" role="alert">
          <Icon name="shield" size={24} />
          <div>
            <h2>Применение недоступно</h2>
            <p>
              {actorIsAuthor
                ? "Дождитесь обязательного согласования или устраните нарушение целостности."
                : "Применить изменение может только его автор; проверяющий обязан оставаться независимым."}
            </p>
          </div>
        </section>
      )}
    </div>
  );
}
