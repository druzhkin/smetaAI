import { useMemo, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  getManualEvidenceContext,
  getProject,
  newIdempotencyKey,
  recordManualEvidence,
  type RequestContext,
} from "../api";
import { useAuth } from "../auth";
import { ErrorBlock, LoadingBlock } from "../components/Feedback";
import { Icon } from "../components/Icon";
import { StatusPill } from "../components/StatusPill";
import {
  parseManualEvidenceValue,
  validateManualEvidenceEntry,
  type ManualEvidenceEntryDraft,
  type ManualEvidenceValueFormat,
} from "../manualEvidence";
import { Link, useNavigation } from "../navigation";
import type { RuntimeConfig } from "../types";

const valueFormatLabels: Record<ManualEvidenceValueFormat, string> = {
  TEXT: "Текст",
  EXACT_NUMBER: "Точное число",
  BOOLEAN: "Логическое значение",
  JSON: "Структурированный JSON",
};

function localDateTimeValue(): string {
  const now = new Date();
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

function initialDraft(): ManualEvidenceEntryDraft {
  return {
    fieldName: "",
    valueText: "",
    valueFormat: "TEXT",
    unit: "",
    sourcePriority: "10",
    documentRevisionId: "",
    locatorKind: "PDF_PAGE_REGION",
    locator: "",
    page: "",
    table: "",
    sheet: "",
    cellOrRange: "",
    observedAt: localDateTimeValue(),
    reason: "",
    projectCode: "",
    acknowledged: false,
  };
}

export function ManualEvidenceEntryPage({
  config,
  projectId,
}: {
  config: RuntimeConfig;
  projectId: string;
}) {
  const auth = useAuth();
  const queryClient = useQueryClient();
  const { navigate } = useNavigation();
  const [draft, setDraft] = useState<ManualEvidenceEntryDraft>(initialDraft);
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
  const evidenceContextQuery = useQuery({
    queryKey: ["manual-evidence-context", projectId],
    queryFn: ({ signal }) =>
      getManualEvidenceContext(context, projectId, signal),
  });
  const mutation = useMutation({
    mutationFn: (input: {
      value: unknown;
      documentId: string;
      objectHash: string;
      policyVersionId: string;
      idempotencyKey: string;
    }) =>
      recordManualEvidence(context, {
        projectId,
        policyVersionId: input.policyVersionId,
        fieldName: draft.fieldName.trim(),
        value: input.value,
        unit: draft.unit.trim() || null,
        sourcePriority: Number(draft.sourcePriority),
        documentId: input.documentId,
        documentRevisionId: draft.documentRevisionId,
        originalObjectHash: input.objectHash,
        locatorKind: draft.locatorKind.trim(),
        locator: draft.locator.trim(),
        page: draft.page === "" ? null : Number(draft.page),
        table: draft.table.trim() || null,
        sheet: draft.sheet.trim() || null,
        cellOrRange: draft.cellOrRange.trim() || null,
        observedAt: new Date(draft.observedAt).toISOString(),
        reason: draft.reason.trim(),
        idempotencyKey: input.idempotencyKey,
      }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["records", projectId, "EVIDENCE"],
        }),
        queryClient.invalidateQueries({
          queryKey: ["records", projectId, "APPROVALS"],
        }),
        queryClient.invalidateQueries({ queryKey: ["work-items"] }),
        queryClient.invalidateQueries({ queryKey: ["workbench", projectId] }),
      ]);
      navigate(`/projects/${encodeURIComponent(projectId)}/EVIDENCE`, {
        replace: true,
      });
    },
  });

  const change = (patch: Partial<ManualEvidenceEntryDraft>) => {
    setDraft((current) => ({ ...current, ...patch, acknowledged: false }));
    setOperationKey(null);
    setFormError(null);
    mutation.reset();
  };

  if (projectQuery.isPending || evidenceContextQuery.isPending) {
    return (
      <div className="page">
        <LoadingBlock label="Загрузка политики и текущего комплекта документов" />
      </div>
    );
  }
  if (projectQuery.isError || evidenceContextQuery.isError) {
    const failed = projectQuery.isError ? projectQuery : evidenceContextQuery;
    return (
      <div className="page">
        <ErrorBlock
          error={failed.error}
          onRetry={() => {
            void projectQuery.refetch();
            void evidenceContextQuery.refetch();
          }}
        />
      </div>
    );
  }

  const project = projectQuery.data;
  const evidenceContext = evidenceContextQuery.data;
  const selectedDocument = evidenceContext.documents.find(
    (document) => document.document_revision_id === draft.documentRevisionId,
  );
  const validationError = validateManualEvidenceEntry(draft, project.code);

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setFormError(validationError);
    if (validationError !== null || selectedDocument === undefined) {
      return;
    }
    const parsed = parseManualEvidenceValue(draft.valueFormat, draft.valueText);
    if (!parsed.ok) {
      setFormError(parsed.error);
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
      value: parsed.value,
      documentId: selectedDocument.document_id,
      objectHash: selectedDocument.original_object_hash,
      policyVersionId: evidenceContext.policy_version_id,
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
        <Link to={`/projects/${encodeURIComponent(projectId)}/EVIDENCE`}>
          Доказательства
        </Link>
        <span>/</span>
        <span>Ручное наблюдение</span>
      </nav>

      <header className="entry-header">
        <div>
          <p className="eyebrow">
            {project.code} · контролируемая корректировка
          </p>
          <h1>Зафиксировать ручное наблюдение</h1>
          <p>
            Запись останется неподтверждённой до независимой проверки. Исходное
            наблюдение не заменяется и не получает статус VERIFIED напрямую.
          </p>
        </div>
        <div className="entry-header__guard">
          <Icon name="shield" size={22} />
          <span>
            Политика {evidenceContext.policy_version_id} · комплект{" "}
            {evidenceContext.document_set_revision_id} · проверяет роль{" "}
            {evidenceContext.review_role}
          </span>
        </div>
      </header>

      <section className="evidence-basis-strip">
        <div>
          <span>Состояние проекта</span>
          <StatusPill value={evidenceContext.project_state} compact />
        </div>
        <div>
          <span>Документов в подтверждённом комплекте</span>
          <strong>{evidenceContext.documents.length}</strong>
        </div>
        <div>
          <span>Результат после записи</span>
          <StatusPill value="UNVERIFIED" compact />
        </div>
      </section>

      <form className="entry-form manual-evidence-form" onSubmit={submit}>
        <div className="entry-form__intro">
          <p className="eyebrow">Значение</p>
          <h2>Что именно исправляется</h2>
          <p>
            Для чисел используется строковое десятичное представление: браузер
            не имеет права округлять финансовые или количественные данные.
          </p>
        </div>

        <div className="form-grid">
          <label className="decision-field">
            <span>Формализованное имя поля</span>
            <input
              type="text"
              maxLength={300}
              value={draft.fieldName}
              placeholder="pipeline.nominal_diameter"
              onChange={(event) => change({ fieldName: event.target.value })}
              required
            />
          </label>
          <label className="decision-field">
            <span>Формат значения</span>
            <select
              value={draft.valueFormat}
              onChange={(event) =>
                change({
                  valueFormat: event.target.value as ManualEvidenceValueFormat,
                  valueText: event.target.value === "BOOLEAN" ? "true" : "",
                })
              }
            >
              {Object.entries(valueFormatLabels).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <label className="decision-field form-grid__wide">
            <span>Извлечённое значение</span>
            {draft.valueFormat === "BOOLEAN" ? (
              <select
                value={draft.valueText}
                onChange={(event) => change({ valueText: event.target.value })}
              >
                <option value="true">Да / true</option>
                <option value="false">Нет / false</option>
              </select>
            ) : (
              <textarea
                rows={draft.valueFormat === "JSON" ? 7 : 3}
                value={draft.valueText}
                onChange={(event) => change({ valueText: event.target.value })}
                placeholder={
                  draft.valueFormat === "JSON"
                    ? '{"mark":"К9","power_kw":"125.50"}'
                    : draft.valueFormat === "EXACT_NUMBER"
                      ? "500.00"
                      : "DN500"
                }
                required
              />
            )}
          </label>
          <label className="decision-field">
            <span>Единица измерения</span>
            <input
              type="text"
              maxLength={100}
              value={draft.unit}
              placeholder="mm"
              onChange={(event) => change({ unit: event.target.value })}
            />
          </label>
          <label className="decision-field">
            <span>Приоритет источника</span>
            <input
              type="number"
              min={0}
              step={1}
              value={draft.sourcePriority}
              onChange={(event) =>
                change({ sourcePriority: event.target.value })
              }
              required
            />
          </label>
        </div>

        <div className="entry-form__intro entry-form__section">
          <p className="eyebrow">Provenance</p>
          <h2>Где находится исходное значение</h2>
          <p>
            Выбор ограничен редакциями текущего подтверждённого комплекта.
            Сервер повторно проверит document ID и SHA-256 исходного файла.
          </p>
        </div>

        <div className="form-grid">
          <label className="decision-field form-grid__wide">
            <span>Документ и редакция</span>
            <select
              value={draft.documentRevisionId}
              onChange={(event) =>
                change({ documentRevisionId: event.target.value })
              }
              required
            >
              <option value="">Выберите документ</option>
              {evidenceContext.documents.map((document) => (
                <option
                  key={document.document_revision_id}
                  value={document.document_revision_id}
                >
                  {document.title} · ред. {document.revision_label} ·{" "}
                  {document.original_filename}
                </option>
              ))}
            </select>
            {selectedDocument !== undefined && (
              <small className="hash-value">
                SHA-256: {selectedDocument.original_object_hash}
              </small>
            )}
          </label>
          <label className="decision-field">
            <span>Тип локатора</span>
            <select
              value={draft.locatorKind}
              onChange={(event) => change({ locatorKind: event.target.value })}
            >
              <option value="PDF_PAGE_REGION">PDF: область страницы</option>
              <option value="PDF_TABLE_CELL">PDF: ячейка таблицы</option>
              <option value="EXCEL_CELL_RANGE">Excel: ячейка/диапазон</option>
              <option value="DOCUMENT_SECTION">Раздел документа</option>
              <option value="IMAGE_REGION">Область изображения</option>
            </select>
          </label>
          <label className="decision-field">
            <span>Дата и время наблюдения</span>
            <input
              type="datetime-local"
              value={draft.observedAt}
              onChange={(event) => change({ observedAt: event.target.value })}
              required
            />
          </label>
          <label className="decision-field form-grid__wide">
            <span>Точный локатор</span>
            <input
              type="text"
              maxLength={4000}
              value={draft.locator}
              placeholder="page=12;x=0.10;y=0.22;width=0.40;height=0.08"
              onChange={(event) => change({ locator: event.target.value })}
              required
            />
          </label>
          <label className="decision-field">
            <span>Страница</span>
            <input
              type="number"
              min={1}
              step={1}
              value={draft.page}
              onChange={(event) => change({ page: event.target.value })}
            />
          </label>
          <label className="decision-field">
            <span>Таблица</span>
            <input
              type="text"
              maxLength={500}
              value={draft.table}
              onChange={(event) => change({ table: event.target.value })}
            />
          </label>
          <label className="decision-field">
            <span>Лист</span>
            <input
              type="text"
              maxLength={500}
              value={draft.sheet}
              onChange={(event) => change({ sheet: event.target.value })}
            />
          </label>
          <label className="decision-field">
            <span>Ячейка или диапазон</span>
            <input
              type="text"
              maxLength={500}
              value={draft.cellOrRange}
              onChange={(event) => change({ cellOrRange: event.target.value })}
            />
          </label>
        </div>

        <div className="entry-form__intro entry-form__section">
          <p className="eyebrow">Обоснование</p>
          <h2>Почему требуется корректировка</h2>
        </div>
        <label className="decision-field">
          <span>Причина ручного наблюдения</span>
          <textarea
            rows={5}
            maxLength={2000}
            value={draft.reason}
            onChange={(event) => change({ reason: event.target.value })}
            placeholder="Опишите расхождение извлечения и выполненную сверку с исходным документом."
            required
          />
          <small>{draft.reason.length} / 2000</small>
        </label>
        <label className="decision-field decision-field--confirmation">
          <span>Введите шифр проекта: {project.code}</span>
          <input
            type="text"
            autoComplete="off"
            spellCheck={false}
            value={draft.projectCode}
            onChange={(event) => change({ projectCode: event.target.value })}
            required
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
            Я проверил редакцию, SHA-256 исходника, точный локатор, единицу и
            формат значения. Понимаю, что запись требует независимого решения.
          </span>
        </label>

        {(formError !== null || mutation.isError) && (
          <div className="decision-form__error" role="alert">
            <Icon name="warning" size={18} />
            <span>
              {formError ??
                (mutation.error instanceof Error
                  ? mutation.error.message
                  : "Не удалось записать ручное наблюдение")}
            </span>
          </div>
        )}
        <div className="decision-form__actions">
          <Link
            className="button button--secondary"
            to={`/projects/${encodeURIComponent(projectId)}/EVIDENCE`}
          >
            Отмена
          </Link>
          <button
            className="button button--critical"
            type="submit"
            disabled={mutation.isPending}
          >
            {mutation.isPending
              ? "Фиксация наблюдения…"
              : "Записать и направить на проверку"}
          </button>
        </div>
      </form>
    </div>
  );
}
