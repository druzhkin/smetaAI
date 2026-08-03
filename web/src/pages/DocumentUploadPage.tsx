import { useMemo, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  getDocumentUpload,
  getProject,
  newIdempotencyKey,
  uploadDocument,
  type RequestContext,
} from "../api";
import { useAuth } from "../auth";
import { ErrorBlock, LoadingBlock } from "../components/Feedback";
import { Icon } from "../components/Icon";
import { IntakeManifestResult } from "../components/IntakeManifestResult";
import { StatusPill } from "../components/StatusPill";
import { formatBytes, formatDateTime } from "../format";
import {
  validateDocumentUploadDraft,
  type DocumentUploadDraft,
} from "../intake";
import { Link } from "../navigation";
import type { QuarantineStatus, RuntimeConfig } from "../types";

interface UploadDraft extends DocumentUploadDraft {
  critical: boolean;
  makeCandidateCurrent: boolean;
}

const initialDraft: UploadDraft = {
  logicalKey: "",
  title: "",
  documentType: "TENDER_TERMS",
  revisionLabel: "",
  reason: "",
  file: null,
  critical: true,
  makeCandidateCurrent: true,
  acknowledged: false,
};

const terminalStatuses = new Set<QuarantineStatus>([
  "PROCESSED",
  "REJECTED",
  "PROCESSING_DEAD_LETTERED",
]);

export function DocumentUploadPage({
  config,
  projectId,
}: {
  config: RuntimeConfig;
  projectId: string;
}) {
  const auth = useAuth();
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<UploadDraft>(initialDraft);
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
  const mutation = useMutation({
    mutationFn: (input: {
      draft: UploadDraft & { file: File };
      idempotencyKey: string;
    }) =>
      uploadDocument(context, {
        projectId,
        logicalKey: input.draft.logicalKey.trim(),
        title: input.draft.title.trim(),
        documentType: input.draft.documentType.trim(),
        revisionLabel: input.draft.revisionLabel.trim(),
        reason: input.draft.reason.trim(),
        file: input.draft.file,
        critical: input.draft.critical,
        makeCandidateCurrent: input.draft.makeCandidateCurrent,
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
      ]);
    },
  });
  const uploadId = mutation.data?.upload_id ?? "";
  const statusQuery = useQuery({
    queryKey: ["document-upload", projectId, uploadId],
    queryFn: ({ signal }) =>
      getDocumentUpload(context, projectId, uploadId, signal),
    enabled: uploadId !== "",
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status !== undefined && terminalStatuses.has(status)
        ? false
        : 4000;
    },
    refetchIntervalInBackground: false,
  });
  const receipt = statusQuery.data ?? mutation.data;
  const canUpload =
    auth.roles.includes("ESTIMATOR") || auth.roles.includes("TECHNICAL_EXPERT");
  const validationError = validateDocumentUploadDraft(
    draft,
    config.max_upload_bytes,
  );

  const change = (patch: Partial<UploadDraft>) => {
    setDraft((current) => ({ ...current, ...patch, acknowledged: false }));
    setOperationKey(null);
    setFormError(null);
    mutation.reset();
  };
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!canUpload) {
      setFormError("Требуется роль сметчика или технического эксперта");
      return;
    }
    setFormError(validationError);
    if (validationError !== null || draft.file === null) {
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
      draft: { ...draft, file: draft.file },
      idempotencyKey: key,
    });
  };

  if (projectQuery.isPending) {
    return (
      <div className="page">
        <LoadingBlock label="Загрузка проекта" />
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

  return (
    <div className="page">
      <nav className="breadcrumbs" aria-label="Навигационная цепочка">
        <Link to="/">Проекты</Link>
        <span>/</span>
        <Link to={`/projects/${encodeURIComponent(projectId)}`}>
          {projectQuery.data.code}
        </Link>
        <span>/</span>
        <Link to={`/projects/${encodeURIComponent(projectId)}/DOCUMENTS`}>
          Документы
        </Link>
        <span>/</span>
        <span>Загрузка</span>
      </nav>
      <header className="entry-header">
        <div>
          <p className="eyebrow">{projectQuery.data.code} · входной контур</p>
          <h1>Загрузить документ</h1>
          <p>
            Файл сначала попадает в отдельный карантин. До квалифицированного
            malware scan и изолированной обработки он не является
            доказательством.
          </p>
        </div>
        <div className="entry-header__guard">
          <Icon name="shield" size={22} />
          <span>
            Серверный лимит: {formatBytes(config.max_upload_bytes)}. Архивы и
            вложения проверяются отдельно после загрузки.
          </span>
        </div>
      </header>

      {!canUpload ? (
        <section className="decision-blocker" role="alert">
          <Icon name="shield" size={24} />
          <div>
            <h2>Загрузка недоступна</h2>
            <p>Требуется проектная роль сметчика или технического эксперта.</p>
          </div>
        </section>
      ) : receipt !== undefined ? (
        <section className="upload-receipt" aria-live="polite">
          <div className="upload-receipt__header">
            <div>
              <p className="eyebrow">Карантинная запись создана</p>
              <h2>{receipt.original_filename}</h2>
              <p>
                {formatBytes(receipt.size_bytes)} · загружено{" "}
                {formatDateTime(receipt.created_at)}
              </p>
            </div>
            <StatusPill value={receipt.status} />
          </div>
          <dl className="task-facts">
            <div>
              <dt>Upload ID</dt>
              <dd>{receipt.upload_id}</dd>
            </div>
            <div>
              <dt>SHA-256</dt>
              <dd className="hash-value">{receipt.object_hash}</dd>
            </div>
            <div>
              <dt>Malware verdict</dt>
              <dd>{receipt.latest_scan_verdict ?? "Ожидается"}</dd>
            </div>
            <div>
              <dt>Попытки обработки</dt>
              <dd>{receipt.processing_attempts}</dd>
            </div>
            <div>
              <dt>Document revision</dt>
              <dd>{receipt.processed_document_revision_id ?? "Не создана"}</dd>
            </div>
            <div>
              <dt>Код отказа</dt>
              <dd>{receipt.failure_code ?? "Нет"}</dd>
            </div>
          </dl>
          {!terminalStatuses.has(receipt.status) && (
            <div className="upload-receipt__pending">
              <span className="loading-block__bar" />
              <span>
                Статус обновляется автоматически. Отсутствие результата не
                означает успешную проверку.
              </span>
            </div>
          )}
          {(receipt.status === "PROCESSED" || receipt.manifest !== null) && (
            <IntakeManifestResult manifest={receipt.manifest} />
          )}
          {statusQuery.isError && (
            <ErrorBlock
              error={statusQuery.error}
              onRetry={() => void statusQuery.refetch()}
            />
          )}
          <div className="decision-form__actions">
            <Link
              className="button button--secondary"
              to={`/projects/${encodeURIComponent(projectId)}/DOCUMENTS`}
            >
              Открыть реестр документов
            </Link>
            <button
              className="button button--primary"
              type="button"
              onClick={() => {
                setDraft(initialDraft);
                setOperationKey(null);
                setFormError(null);
                mutation.reset();
              }}
            >
              Загрузить другой документ
            </button>
          </div>
        </section>
      ) : (
        <form className="entry-form" onSubmit={submit}>
          <div className="entry-form__intro">
            <p className="eyebrow">Метаданные до обработки</p>
            <h2>Идентифицировать редакцию</h2>
            <p>
              Логический ключ объединяет редакции одного документа. Ошибка в
              ключе создаст ложное разделение истории и должна быть исправлена
              до загрузки.
            </p>
          </div>

          <div className="entry-form__grid">
            <label className="decision-field">
              <span>Логический ключ</span>
              <input
                type="text"
                value={draft.logicalKey}
                maxLength={300}
                required
                spellCheck={false}
                onChange={(event) => change({ logicalKey: event.target.value })}
                placeholder="tender-terms"
              />
              <small>Стабильный ключ без пробелов для всех редакций.</small>
            </label>
            <label className="decision-field">
              <span>Редакция</span>
              <input
                type="text"
                value={draft.revisionLabel}
                maxLength={100}
                required
                onChange={(event) =>
                  change({ revisionLabel: event.target.value })
                }
                placeholder="R1"
              />
              <small>Как указано в штампе или сопроводительном письме.</small>
            </label>
            <label className="decision-field">
              <span>Наименование</span>
              <input
                type="text"
                value={draft.title}
                maxLength={1000}
                required
                onChange={(event) => change({ title: event.target.value })}
                placeholder="Конкурсная документация"
              />
            </label>
            <label className="decision-field">
              <span>Тип документа</span>
              <input
                type="text"
                list="document-types"
                value={draft.documentType}
                maxLength={100}
                required
                spellCheck={false}
                onChange={(event) =>
                  change({ documentType: event.target.value })
                }
              />
              <datalist id="document-types">
                <option value="TENDER_TERMS" />
                <option value="TECHNICAL_SPECIFICATION" />
                <option value="BILL_OF_QUANTITIES" />
                <option value="DESIGN_DOCUMENTATION" />
                <option value="COMMERCIAL_BASIS" />
                <option value="CONTRACT_DRAFT" />
                <option value="ADDENDUM" />
              </datalist>
            </label>
          </div>

          <label className="file-field">
            <input
              type="file"
              required
              onChange={(event) => {
                const file = event.target.files?.item(0) ?? null;
                change({
                  file,
                  title:
                    draft.title === "" && file !== null
                      ? file.name.replace(/\.[^.]+$/, "")
                      : draft.title,
                });
              }}
            />
            <Icon name="trace" size={24} />
            <span>
              <strong>{draft.file?.name ?? "Выберите исходный файл"}</strong>
              <small>
                {draft.file === null
                  ? `Не более ${formatBytes(config.max_upload_bytes)}`
                  : `${formatBytes(draft.file.size)} · ${draft.file.type || "тип не объявлен"}`}
              </small>
            </span>
          </label>

          <label className="decision-field">
            <span>Основание загрузки</span>
            <textarea
              value={draft.reason}
              maxLength={2000}
              rows={4}
              required
              onChange={(event) => change({ reason: event.target.value })}
              placeholder="Источник получения, дата, письмо или номер дополнения."
            />
            <small>{draft.reason.length} / 2000</small>
          </label>

          <div className="upload-flags">
            <label>
              <input
                type="checkbox"
                checked={draft.critical}
                onChange={(event) => change({ critical: event.target.checked })}
              />
              <span>
                <strong>Критический документ</strong>
                <small>
                  Его отсутствие или отказ обработки блокирует расчёт.
                </small>
              </span>
            </label>
            <label>
              <input
                type="checkbox"
                checked={draft.makeCandidateCurrent}
                onChange={(event) =>
                  change({ makeCandidateCurrent: event.target.checked })
                }
              />
              <span>
                <strong>Кандидат в актуальную редакцию</strong>
                <small>
                  Загрузка инвалидирует текущие производные до проверки новой
                  версии.
                </small>
              </span>
            </label>
          </div>

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
              Я сверил логический ключ, редакцию, критичность и назначение
              файла. Понимаю, что загрузка в карантин не подтверждает
              целостность документа.
            </span>
          </label>

          {(formError !== null || mutation.isError) && (
            <div className="decision-form__error" role="alert">
              <Icon name="warning" size={18} />
              <span>
                {formError ??
                  (mutation.error instanceof Error
                    ? mutation.error.message
                    : "Не удалось принять файл в карантин")}
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
                ? "Передача в карантин…"
                : "Загрузить в карантин"}
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
