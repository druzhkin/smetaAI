import { useMemo, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  confirmDocumentSet,
  getDocumentSet,
  getProject,
  newIdempotencyKey,
  type RequestContext,
} from "../api";
import { useAuth } from "../auth";
import { ErrorBlock, LoadingBlock } from "../components/Feedback";
import { Icon } from "../components/Icon";
import { StatusPill } from "../components/StatusPill";
import { formatDateTime } from "../format";
import {
  validateDocumentSetConfirmationDraft,
  type DocumentSetConfirmationDraft,
} from "../intake";
import { Link, useNavigation } from "../navigation";
import type { RuntimeConfig } from "../types";

const initialDraft: DocumentSetConfirmationDraft = {
  reason: "",
  projectCode: "",
  acknowledged: false,
};

export function DocumentSetConfirmPage({
  config,
  projectId,
  documentSetId,
}: {
  config: RuntimeConfig;
  projectId: string;
  documentSetId: string;
}) {
  const auth = useAuth();
  const queryClient = useQueryClient();
  const { navigate } = useNavigation();
  const [draft, setDraft] =
    useState<DocumentSetConfirmationDraft>(initialDraft);
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
  const documentSetQuery = useQuery({
    queryKey: ["document-set", projectId, documentSetId],
    queryFn: ({ signal }) =>
      getDocumentSet(context, projectId, documentSetId, signal),
  });
  const mutation = useMutation({
    mutationFn: (input: { reason: string; idempotencyKey: string }) =>
      confirmDocumentSet(context, {
        projectId,
        documentSetId,
        reason: input.reason,
        idempotencyKey: input.idempotencyKey,
      }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["project", projectId] }),
        queryClient.invalidateQueries({ queryKey: ["projects"] }),
        queryClient.invalidateQueries({ queryKey: ["workbench", projectId] }),
        queryClient.invalidateQueries({
          queryKey: ["records", projectId, "DOCUMENTS"],
        }),
        queryClient.invalidateQueries({
          queryKey: ["document-set", projectId, documentSetId],
        }),
      ]);
      navigate(`/projects/${encodeURIComponent(projectId)}/DOCUMENTS`, {
        replace: true,
      });
    },
  });

  if (projectQuery.isPending || documentSetQuery.isPending) {
    return (
      <div className="page">
        <LoadingBlock label="Загрузка кандидата комплекта документов" />
      </div>
    );
  }
  if (projectQuery.isError || documentSetQuery.isError) {
    const failed = projectQuery.isError ? projectQuery : documentSetQuery;
    return (
      <div className="page">
        <ErrorBlock
          error={failed.error}
          onRetry={() => {
            void projectQuery.refetch();
            void documentSetQuery.refetch();
          }}
        />
      </div>
    );
  }

  const project = projectQuery.data;
  const documentSet = documentSetQuery.data;
  const hasRole =
    auth.roles.includes("REVIEWER") || auth.roles.includes("APPROVER");
  const actorKnown = auth.actorId !== null;
  const isSubmitter = auth.actorId === documentSet.created_by;
  const isDraft = documentSet.status === "DRAFT";
  const validationError = validateDocumentSetConfirmationDraft(
    draft,
    project.code,
  );
  const decisionBlockers = [
    ...(!hasRole
      ? ["Требуется проектная роль проверяющего или утверждающего."]
      : []),
    ...(!actorKnown
      ? ["Identity-сессия не содержит проверяемый actor ID."]
      : []),
    ...(isSubmitter
      ? ["Создатель кандидата не может подтвердить собственный комплект."]
      : []),
    ...(!isDraft
      ? [`Комплект находится в состоянии ${documentSet.status}, а не DRAFT.`]
      : []),
  ];

  const change = (patch: Partial<DocumentSetConfirmationDraft>) => {
    setDraft((current) => ({ ...current, ...patch, acknowledged: false }));
    setOperationKey(null);
    setFormError(null);
    mutation.reset();
  };
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (decisionBlockers.length > 0) {
      setFormError(decisionBlockers[0] ?? "Подтверждение заблокировано");
      return;
    }
    setFormError(validationError);
    if (validationError !== null) {
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
      reason: draft.reason.trim(),
      idempotencyKey: key,
    });
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
        <Link to={`/projects/${encodeURIComponent(projectId)}/DOCUMENTS`}>
          Документы
        </Link>
        <span>/</span>
        <span>Подтверждение комплекта</span>
      </nav>

      <header className="entry-header">
        <div>
          <p className="eyebrow">{project.code} · четыре глаза</p>
          <h1>Подтвердить актуальный комплект</h1>
          <p>
            Подтверждение фиксирует, какие именно редакции считаются текущими.
            Оно не доказывает полноту состава, качество извлечения или
            готовность расчёта.
          </p>
        </div>
        <div className="entry-header__guard">
          <Icon name="shield" size={22} />
          <span>
            Сервер повторно сверит статус кандидата и точный список текущих
            редакций. Устаревший кандидат будет отклонён.
          </span>
        </div>
      </header>

      <section className="document-set-summary">
        <div className="document-set-summary__header">
          <div>
            <p className="eyebrow">Кандидат комплекта</p>
            <h2>{documentSet.id}</h2>
            <p>
              Создан {formatDateTime(documentSet.created_at)} пользователем{" "}
              <strong>{documentSet.created_by}</strong>
            </p>
          </div>
          <StatusPill value={documentSet.status} />
        </div>
        <dl className="task-facts">
          <div>
            <dt>Manifest SHA-256</dt>
            <dd className="hash-value">{documentSet.manifest_hash}</dd>
          </div>
          <div>
            <dt>Количество редакций</dt>
            <dd>{documentSet.revision_ids.length}</dd>
          </div>
          <div>
            <dt>Текущий комплект проекта</dt>
            <dd>
              {project.current_document_set_revision_id ?? "Не подтверждён"}
            </dd>
          </div>
        </dl>
        <div className="document-set-revisions">
          <span>Редакции кандидата</span>
          <ol>
            {documentSet.revision_ids.map((revisionId) => (
              <li key={revisionId}>{revisionId}</li>
            ))}
          </ol>
        </div>
      </section>

      {decisionBlockers.length > 0 ? (
        <section className="decision-blocker" role="alert">
          <Icon name="shield" size={24} />
          <div>
            <h2>Подтверждение недоступно</h2>
            <ul>
              {decisionBlockers.map((blocker) => (
                <li key={blocker}>{blocker}</li>
              ))}
            </ul>
          </div>
        </section>
      ) : (
        <form className="entry-form" onSubmit={submit}>
          <div className="entry-form__intro">
            <p className="eyebrow">Независимое решение</p>
            <h2>Зафиксировать проверку состава редакций</h2>
            <p>
              Основание должно описывать фактически выполненную сверку с
              реестром, дополнениями и отменёнными документами.
            </p>
          </div>
          <label className="decision-field">
            <span>Основание подтверждения</span>
            <textarea
              value={draft.reason}
              maxLength={2000}
              rows={5}
              required
              onChange={(event) => change({ reason: event.target.value })}
              placeholder="Укажите реестр, письмо, дату и выполненную сверку редакций."
            />
            <small>{draft.reason.length} / 2000</small>
          </label>
          <label className="decision-field decision-field--confirmation">
            <span>Введите шифр проекта: {project.code}</span>
            <input
              type="text"
              value={draft.projectCode}
              autoComplete="off"
              spellCheck={false}
              required
              onChange={(event) => change({ projectCode: event.target.value })}
            />
          </label>
          <label className="decision-acknowledgement">
            <input
              type="checkbox"
              checked={draft.acknowledged}
              onChange={(event) => {
                setDraft((current) => ({
                  ...current,
                  acknowledged: event.target.checked,
                }));
                setOperationKey(null);
                setFormError(null);
                mutation.reset();
              }}
            />
            <span>
              Я независимо сверил список редакций и хеш манифеста. Понимаю, что
              это действие определяет актуальную версию входа, но не отменяет
              проверки комплектности, конфликтов и извлечения.
            </span>
          </label>

          {(formError !== null || mutation.isError) && (
            <div className="decision-form__error" role="alert">
              <Icon name="warning" size={18} />
              <span>
                {formError ??
                  (mutation.error instanceof Error
                    ? mutation.error.message
                    : "Не удалось подтвердить комплект документов")}
              </span>
            </div>
          )}
          <div className="decision-form__actions">
            <Link
              className="button button--secondary"
              to={`/projects/${encodeURIComponent(projectId)}/DOCUMENTS`}
            >
              Отмена
            </Link>
            <button
              className="button button--critical"
              type="submit"
              disabled={mutation.isPending}
            >
              {mutation.isPending
                ? "Фиксация решения…"
                : "Подтвердить текущий комплект"}
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
