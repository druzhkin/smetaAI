import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";

import {
  ApiError,
  createProject,
  getDocumentUpload,
  newIdempotencyKey,
  uploadDocument,
  type RequestContext,
} from "../api";
import { Icon } from "../components/Icon";
import { formatBytes } from "../format";
import { Link } from "../navigation";
import type {
  ProjectView,
  QuarantinedUpload,
  QuarantineStatus,
  RuntimeConfig,
} from "../types";

type ImportPhase = "FORM" | "CREATING" | "UPLOADING" | "RECEIVED";
type FilePhase = "WAITING" | "UPLOADING" | "RECEIVED" | "FAILED";

interface ImportFile {
  id: string;
  file: File;
  phase: FilePhase;
  receipt: QuarantinedUpload | null;
  error: string | null;
}

interface ImportFileForValidation {
  file: File;
}

interface OperationKeys {
  project: string;
  uploads: string[];
}

const terminalStatuses = new Set<QuarantineStatus>([
  "PROCESSED",
  "REJECTED",
  "PROCESSING_DEAD_LETTERED",
]);

const statusCopy: Record<QuarantineStatus, string> = {
  QUARANTINED: "Принят в карантин, ожидает антивирусной проверки",
  CLEAN: "Проверен, ожидает изолированной обработки",
  REJECTED: "Отклонён: обнаружена угроза",
  SCAN_FAILED: "Антивирусная проверка завершилась ошибкой",
  PROCESSING: "Комплект разбирается",
  PROCESSED: "Структурная обработка завершена",
  PROCESSING_FAILED: "Обработка завершилась ошибкой",
  PROCESSING_DEAD_LETTERED: "Обработка остановлена после повторных ошибок",
};

function errorMessage(error: unknown): string {
  if (error instanceof ApiError && error.status === 401) {
    return "Операторский код неверен или доступ к загрузке отключён";
  }
  return error instanceof Error ? error.message : "Неизвестная ошибка";
}

function projectCodeIsValid(value: string): boolean {
  const normalized = value.trim();
  return (
    normalized.length > 0 && normalized.length <= 128 && !/\s/.test(normalized)
  );
}

export function validatePublicProjectImport(input: {
  enabled: boolean;
  accessKey: string;
  projectCode: string;
  projectName: string;
  reason: string;
  files: ImportFileForValidation[];
  acknowledged: boolean;
  maxUploadBytes: number;
}): string | null {
  if (!input.enabled) return "Серверный контур загрузки ещё не включён";
  if (!input.accessKey) return "Введите операторский код";
  if (!projectCodeIsValid(input.projectCode)) {
    return "Укажите шифр проекта без пробелов, не длиннее 128 символов";
  }
  if (!input.projectName.trim() || input.projectName.trim().length > 500) {
    return "Укажите наименование проекта, не длиннее 500 символов";
  }
  if (!input.reason.trim() || input.reason.trim().length > 2000) {
    return "Укажите основание загрузки, не длиннее 2000 символов";
  }
  if (input.files.length === 0) return "Выберите архив или файлы проекта";
  const oversized = input.files.find(
    (item) => item.file.size > input.maxUploadBytes,
  );
  if (oversized !== undefined) {
    return `${oversized.file.name}: файл превышает серверный лимит`;
  }
  if (!input.acknowledged) {
    return "Подтвердите, что выбран актуальный комплект проекта";
  }
  return null;
}

export function PublicProjectImportPage({ config }: { config: RuntimeConfig }) {
  const [accessKey, setAccessKey] = useState("");
  const [projectCode, setProjectCode] = useState("");
  const [projectName, setProjectName] = useState("");
  const [reason, setReason] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [files, setFiles] = useState<ImportFile[]>([]);
  const [phase, setPhase] = useState<ImportPhase>("FORM");
  const [project, setProject] = useState<ProjectView | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const operationKeys = useRef<OperationKeys | null>(null);

  const context = useMemo<RequestContext>(
    () => ({
      apiBasePath: config.api_base_path,
      authorizationHeaders: () => ({ Authorization: `Bearer ${accessKey}` }),
    }),
    [accessKey, config.api_base_path],
  );

  const validationError = validatePublicProjectImport({
    enabled: config.showcase_operator_upload_enabled,
    accessKey,
    projectCode,
    projectName,
    reason,
    files,
    acknowledged,
    maxUploadBytes: config.max_upload_bytes,
  });

  const resetOperation = () => {
    operationKeys.current = null;
    setFormError(null);
  };

  useEffect(() => {
    if (project === null || phase !== "RECEIVED") return;
    const active = files.filter(
      (item) =>
        item.receipt !== null && !terminalStatuses.has(item.receipt.status),
    );
    if (active.length === 0) return;
    const controller = new AbortController();
    const timer = window.setInterval(() => {
      void Promise.all(
        active.map(async (item) => {
          if (item.receipt === null) return;
          try {
            const receipt = await getDocumentUpload(
              context,
              project.id,
              item.receipt.upload_id,
              controller.signal,
            );
            setFiles((current) =>
              current.map((candidate) =>
                candidate.id === item.id
                  ? { ...candidate, receipt }
                  : candidate,
              ),
            );
          } catch (error) {
            if (!controller.signal.aborted) {
              setFiles((current) =>
                current.map((candidate) =>
                  candidate.id === item.id
                    ? { ...candidate, error: errorMessage(error) }
                    : candidate,
                ),
              );
            }
          }
        }),
      );
    }, 5000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [context, files, phase, project]);

  const uploadFiles = async (
    created: ProjectView,
    selectedIndexes: number[],
  ) => {
    const keys = operationKeys.current;
    if (keys === null) {
      setFormError(
        "Идентификаторы операции утрачены; повторная передача заблокирована",
      );
      return;
    }
    setPhase("UPLOADING");
    for (const index of selectedIndexes) {
      const item = files[index];
      if (item === undefined) continue;
      setFiles((current) =>
        current.map((candidate) =>
          candidate.id === item.id
            ? { ...candidate, phase: "UPLOADING", error: null }
            : candidate,
        ),
      );
      try {
        const receipt = await uploadDocument(context, {
          projectId: created.id,
          logicalKey: `source-file-${String(index + 1).padStart(3, "0")}`,
          title: item.file.name,
          documentType: "PROJECT_SOURCE_FILE",
          revisionLabel: "R1",
          reason: reason.trim(),
          file: item.file,
          critical: true,
          makeCandidateCurrent: true,
          idempotencyKey: keys.uploads[index] ?? newIdempotencyKey(),
        });
        setFiles((current) =>
          current.map((candidate) =>
            candidate.id === item.id
              ? { ...candidate, phase: "RECEIVED", receipt, error: null }
              : candidate,
          ),
        );
      } catch (error) {
        setFiles((current) =>
          current.map((candidate) =>
            candidate.id === item.id
              ? {
                  ...candidate,
                  phase: "FAILED",
                  error: errorMessage(error),
                }
              : candidate,
          ),
        );
      }
    }
    setPhase("RECEIVED");
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setFormError(validationError);
    if (validationError !== null) return;

    try {
      operationKeys.current ??= {
        project: newIdempotencyKey(),
        uploads: files.map(() => newIdempotencyKey()),
      };
    } catch (error) {
      setFormError(errorMessage(error));
      return;
    }

    try {
      setPhase("CREATING");
      const created = await createProject(context, {
        code: projectCode.trim(),
        name: projectName.trim(),
        reason: reason.trim(),
        idempotencyKey: operationKeys.current.project,
      });
      setProject(created);
      await uploadFiles(
        created,
        files.map((_, index) => index),
      );
    } catch (error) {
      setPhase("FORM");
      setFormError(errorMessage(error));
    }
  };

  const acceptedCount = files.filter((item) => item.receipt !== null).length;
  const failedIndexes = files.flatMap((item, index) =>
    item.phase === "FAILED" ? [index] : [],
  );

  return (
    <main className="public-demo public-import">
      <header className="public-demo__topbar">
        <Link
          className="public-demo__brand"
          to="/"
          aria-label="СметаИИ — к примеру"
        >
          <span>СИ</span>
          <strong>СметаИИ</strong>
        </Link>
        <nav aria-label="Разделы импорта">
          <a href="#package">Комплект</a>
          <a href="#status">Статус</a>
        </nav>
        <div className="public-demo__mode">
          <Icon name="shield" size={16} />
          <span>Операторский импорт</span>
        </div>
      </header>

      <section className="public-import__hero">
        <div>
          <p className="eyebrow">Новый расчётный проект</p>
          <h1>Загрузите весь комплект одним действием</h1>
          <p>
            Архив или отдельные документы будут сохранены по исходным байтам,
            получат SHA-256 и реальные статусы карантина. Непроверенный файл не
            попадёт в расчёт.
          </p>
        </div>
        <aside>
          <span>Поддерживаемый вход</span>
          <strong>ZIP · XLSX · PDF · DOCX</strong>
          <p>
            Также принимаются XLSM, CSV, TXT, изображения и проектные форматы.
            Формат без квалифицированного обработчика будет явно заблокирован.
          </p>
        </aside>
      </section>

      <section className="public-import__body" id="package">
        {!config.showcase_operator_upload_enabled ? (
          <div className="public-import__disabled" role="alert">
            <Icon name="warning" size={28} />
            <div>
              <p className="eyebrow">Серверная готовность</p>
              <h2>Загрузка ещё не включена</h2>
              <p>
                Операторский ключ или постоянное хранилище не настроены. Форма
                не будет изображать успешную загрузку без серверного приёма.
              </p>
            </div>
            <Link className="button button--secondary" to="/">
              Вернуться к примеру
            </Link>
          </div>
        ) : project !== null ? (
          <section
            className="public-import__result"
            id="status"
            aria-live="polite"
          >
            <header>
              <div>
                <p className="eyebrow">Проект зарегистрирован</p>
                <h2>{project.name}</h2>
                <p>
                  {project.code} · принято файлов: {acceptedCount} из{" "}
                  {files.length}
                </p>
              </div>
              <span className="public-import__blocked">BLOCKED</span>
            </header>

            <div className="public-import__truth">
              <Icon name="shield" size={22} />
              <p>
                Проект создан, но сметная цена не выпускается: файлы должны
                пройти антивирусную и структурную обработку, затем — сбор и
                проверку ценовых оснований.
              </p>
            </div>

            <div className="public-import__files">
              {files.map((item) => (
                <article key={item.id}>
                  <div className="public-import__file-icon">
                    <Icon
                      name={item.phase === "FAILED" ? "warning" : "trace"}
                      size={19}
                    />
                  </div>
                  <div>
                    <strong>{item.file.name}</strong>
                    <span>
                      {formatBytes(item.file.size)} ·{" "}
                      {item.receipt === null
                        ? item.phase === "FAILED"
                          ? "не принят"
                          : "передача"
                        : statusCopy[item.receipt.status]}
                    </span>
                    {item.receipt !== null && (
                      <code>SHA-256 {item.receipt.object_hash}</code>
                    )}
                    {item.error !== null && <em>{item.error}</em>}
                  </div>
                  <b>{item.receipt?.status ?? item.phase}</b>
                </article>
              ))}
            </div>

            <div className="public-import__actions">
              <Link className="button button--secondary" to="/">
                Открыть пример расчёта
              </Link>
              {failedIndexes.length > 0 && (
                <button
                  className="button button--public"
                  type="button"
                  disabled={phase === "UPLOADING"}
                  onClick={() => void uploadFiles(project, failedIndexes)}
                >
                  Повторить непереданные файлы ({failedIndexes.length})
                </button>
              )}
              <button
                className="button button--public"
                type="button"
                onClick={() => window.location.reload()}
              >
                Загрузить другой проект
              </button>
            </div>
          </section>
        ) : (
          <form
            className="public-import__form"
            onSubmit={(event) => void submit(event)}
          >
            <header>
              <div>
                <p className="eyebrow">01 / Идентификация</p>
                <h2>Проект и исходные файлы</h2>
              </div>
              <span>Данные не публикуются в открытой витрине</span>
            </header>

            <div className="public-import__grid">
              <label>
                <span>Операторский код</span>
                <input
                  type="password"
                  value={accessKey}
                  autoComplete="off"
                  required
                  disabled={project !== null}
                  onChange={(event) => {
                    setAccessKey(event.target.value);
                    resetOperation();
                  }}
                  placeholder="Вставьте код доступа"
                />
                <small>Код хранится только в памяти этой вкладки.</small>
              </label>
              <label>
                <span>Шифр проекта</span>
                <input
                  type="text"
                  value={projectCode}
                  maxLength={128}
                  required
                  spellCheck={false}
                  onChange={(event) => {
                    setProjectCode(event.target.value);
                    resetOperation();
                  }}
                  placeholder="4527946"
                />
                <small>Без пробелов; уникальный для проекта.</small>
              </label>
              <label className="public-import__wide">
                <span>Наименование проекта</span>
                <input
                  type="text"
                  value={projectName}
                  maxLength={500}
                  required
                  onChange={(event) => {
                    setProjectName(event.target.value);
                    resetOperation();
                  }}
                  placeholder="Строительство производственного корпуса"
                />
              </label>
            </div>

            <label className="public-import__drop">
              <input
                type="file"
                multiple
                required
                onChange={(event) => {
                  const selected = Array.from(event.target.files ?? []).map(
                    (file, index): ImportFile => ({
                      id: `${file.name}:${file.size}:${file.lastModified}:${index}`,
                      file,
                      phase: "WAITING",
                      receipt: null,
                      error: null,
                    }),
                  );
                  setFiles(selected);
                  if (projectName === "" && selected[0] !== undefined) {
                    setProjectName(
                      selected[0].file.name.replace(/\.[^.]+$/, ""),
                    );
                  }
                  resetOperation();
                }}
              />
              <span className="public-import__drop-icon">
                <Icon name="trace" size={28} />
              </span>
              <span>
                <strong>
                  {files.length === 0
                    ? "Выберите архив или все файлы проекта"
                    : `Выбрано файлов: ${files.length}`}
                </strong>
                <small>
                  Каждый файл — до {formatBytes(config.max_upload_bytes)}.
                  Исходные имена и байты сохраняются без изменения.
                </small>
              </span>
            </label>

            {files.length > 0 && (
              <div className="public-import__selection">
                {files.map((item) => (
                  <span key={item.id}>
                    <strong>{item.file.name}</strong>
                    <small>{formatBytes(item.file.size)}</small>
                  </span>
                ))}
              </div>
            )}

            <label className="public-import__reason">
              <span>Основание загрузки</span>
              <textarea
                value={reason}
                maxLength={2000}
                rows={4}
                required
                onChange={(event) => {
                  setReason(event.target.value);
                  resetOperation();
                }}
                placeholder="Откуда получен комплект, дата и версия документации"
              />
              <small>{reason.length} / 2000</small>
            </label>

            <label className="public-import__ack">
              <input
                type="checkbox"
                checked={acknowledged}
                onChange={(event) => {
                  setAcknowledged(event.target.checked);
                  resetOperation();
                }}
              />
              <span>
                Я выбрал актуальный комплект проекта и понимаю, что загрузка
                файла не означает готовность или безопасность сметной цены.
              </span>
            </label>

            {formError !== null && (
              <div className="public-import__error" role="alert">
                <Icon name="warning" size={19} />
                <span>{formError}</span>
              </div>
            )}

            <footer>
              <Link className="button button--secondary" to="/">
                Отмена
              </Link>
              <button
                className="button button--public"
                type="submit"
                disabled={phase === "CREATING" || phase === "UPLOADING"}
              >
                {phase === "CREATING"
                  ? "Создание проекта…"
                  : phase === "UPLOADING"
                    ? `Передача файлов: ${acceptedCount} / ${files.length}`
                    : "Создать проект и загрузить комплект"}
              </button>
            </footer>
          </form>
        )}
      </section>
    </main>
  );
}
