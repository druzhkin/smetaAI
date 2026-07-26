import { useMemo, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  getProject,
  newIdempotencyKey,
  runScopeCompleteness,
  type RequestContext,
} from "../api";
import { useAuth } from "../auth";
import { ErrorBlock, LoadingBlock } from "../components/Feedback";
import { Icon } from "../components/Icon";
import { StatusPill } from "../components/StatusPill";
import { Link } from "../navigation";
import type { RuntimeConfig } from "../types";

export function ScopeCompletenessPage({
  config,
  projectId,
}: {
  config: RuntimeConfig;
  projectId: string;
}) {
  const auth = useAuth();
  const queryClient = useQueryClient();
  const [wbsNodeId, setWbsNodeId] = useState("");
  const [reason, setReason] = useState("");
  const [projectCodeConfirmation, setProjectCodeConfirmation] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
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
  const mutation = useMutation({
    mutationFn: (idempotencyKey: string) =>
      runScopeCompleteness(context, {
        projectId,
        wbsNodeId: wbsNodeId.trim(),
        reason: reason.trim(),
        idempotencyKey,
      }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["records", projectId, "BOQ_SCOPE"],
        }),
        queryClient.invalidateQueries({
          queryKey: ["workbench", projectId],
        }),
      ]);
    },
  });

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
  if (projectQuery.isPending) {
    return (
      <div className="page">
        <LoadingBlock label="Загрузка состояния BoQ review" />
      </div>
    );
  }
  const project = projectQuery.data;
  let validation: string | null = null;
  if (
    wbsNodeId.trim().length < 1 ||
    wbsNodeId !== wbsNodeId.trim() ||
    wbsNodeId.length > 128
  ) {
    validation = "Укажите точный нормализованный идентификатор узла WBS.";
  } else if (reason.trim().length < 10 || reason.trim().length > 2000) {
    validation = "Укажите основание запуска длиной от 10 до 2000 символов.";
  } else if (projectCodeConfirmation.trim() !== project.code) {
    validation = "Введите точный код проекта.";
  } else if (!acknowledged) {
    validation =
      "Подтвердите, что отсутствие строки не считается доказательством неприменимости работы.";
  } else if (project.state !== "BOQ_REVIEW") {
    validation = "Scope Completeness Engine запускается только в BOQ_REVIEW.";
  }
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
    let key = operationKey;
    if (key === null) {
      key = newIdempotencyKey();
      setOperationKey(key);
    }
    mutation.mutate(key);
  };
  const findings =
    mutation.data?.evaluation?.findings ??
    mutation.data?.validation_findings ??
    [];

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
        <span>Проверка полноты</span>
      </nav>

      <header className="entry-header">
        <div>
          <p className="eyebrow">Контур 03 · Scope Completeness Engine</p>
          <h1>Проверить полноту состава работ</h1>
          <p>
            Движок сопоставляет текущие проверенные коды работ с утверждённым
            rule pack и контекстом проекта. Каждая отсутствующая сопутствующая
            работа становится отдельным блокирующим finding.
          </p>
        </div>
        <div className="entry-header__guard">
          <Icon name="warning" size={22} />
          <span>
            Нулевая выдача означает только отсутствие finding по текущей версии
            правил — не общую гарантию полноты за пределами утверждённой
            методологии.
          </span>
        </div>
      </header>

      <form className="entry-form controlled-form" onSubmit={submit}>
        <div className="form-grid">
          <label className="field">
            <span>Узел WBS</span>
            <input
              maxLength={128}
              value={wbsNodeId}
              onChange={(event) => {
                setWbsNodeId(event.target.value);
                reset();
              }}
            />
          </label>
          <label className="field">
            <span>Состояние проекта</span>
            <span className="read-only-field">
              <StatusPill value={project.state} compact />
            </span>
          </label>
          <label className="field form-grid__wide">
            <span>Основание запуска</span>
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
              Подтверждаю: отсутствие позиции в BoQ не доказывает, что работа не
              требуется; каждый finding должен быть добавлен или доказательно
              разрешён.
            </span>
          </label>
        </div>
        {(formError !== null || mutation.isError) && (
          <div className="inline-error" role="alert">
            {formError ??
              (mutation.error instanceof Error
                ? mutation.error.message
                : "Проверка полноты не выполнена.")}
          </div>
        )}
        {mutation.data !== undefined && (
          <section className="scope-result" aria-live="polite">
            <div className="controlled-section-heading">
              <div>
                <p className="eyebrow">Результат</p>
                <h2>
                  {findings.length === 0
                    ? "Finding не обнаружены"
                    : `Обнаружено: ${findings.length}`}
                </h2>
              </div>
              <StatusPill
                value={findings.length === 0 ? "PASSED" : "BLOCKED"}
              />
            </div>
            {findings.length > 0 && (
              <div className="compact-ledger">
                {findings.map((finding) => (
                  <div
                    key={
                      "finding_id" in finding
                        ? finding.finding_id
                        : `${finding.code}:${finding.message}`
                    }
                  >
                    <strong>
                      {"required_work_code" in finding
                        ? finding.required_work_code
                        : finding.code}
                    </strong>
                    <span>
                      {"reason" in finding ? finding.reason : finding.message}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </section>
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
            <Icon name="search" size={16} />
            {mutation.isPending ? "Проверка…" : "Запустить движок"}
          </button>
        </div>
      </form>
    </div>
  );
}
