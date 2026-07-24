import { useEffect, useMemo, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  getProject,
  getQuantityChangeContext,
  newIdempotencyKey,
  proposeQuantityChange,
  type RequestContext,
} from "../api";
import { useAuth } from "../auth";
import { ErrorBlock, LoadingBlock } from "../components/Feedback";
import { Icon } from "../components/Icon";
import { StatusPill } from "../components/StatusPill";
import {
  draftFromQuantityContext,
  validateQuantityChangeDraft,
  type QuantityChangeDraft,
} from "../quantityChange";
import { Link, useNavigation } from "../navigation";
import type { QuantityOperation, RuntimeConfig } from "../types";

const operations: { value: QuantityOperation; label: string }[] = [
  { value: "SUM", label: "Сумма входов" },
  { value: "PRODUCT", label: "Произведение входов" },
  { value: "RECTANGULAR_VOLUME", label: "Прямоугольный объём" },
  { value: "CYLINDER_VOLUME", label: "Объём цилиндра" },
];

export function QuantityChangeProposePage({
  config,
  projectId,
  lineId,
}: {
  config: RuntimeConfig;
  projectId: string;
  lineId: string;
}) {
  const auth = useAuth();
  const queryClient = useQueryClient();
  const { navigate } = useNavigation();
  const [draft, setDraft] = useState<QuantityChangeDraft | null>(null);
  const [loadedQuantityId, setLoadedQuantityId] = useState<string | null>(null);
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
  const quantityQuery = useQuery({
    queryKey: ["quantity-change-context", projectId, lineId],
    queryFn: ({ signal }) =>
      getQuantityChangeContext(context, projectId, lineId, signal),
  });

  useEffect(() => {
    if (
      quantityQuery.data !== undefined &&
      quantityQuery.data.current_quantity_id !== loadedQuantityId
    ) {
      setDraft(draftFromQuantityContext(quantityQuery.data));
      setLoadedQuantityId(quantityQuery.data.current_quantity_id);
      setFormError(null);
      setOperationKey(null);
    }
  }, [loadedQuantityId, quantityQuery.data]);

  const mutation = useMutation({
    mutationFn: (input: {
      draft: QuantityChangeDraft;
      idempotencyKey: string;
    }) => {
      if (quantityQuery.data === undefined || projectQuery.data === undefined) {
        throw new Error("Контекст изменения объёма не загружен.");
      }
      const validation = validateQuantityChangeDraft(
        input.draft,
        quantityQuery.data,
        projectQuery.data.code,
      );
      if (validation.error !== null || validation.submission === null) {
        throw new Error(
          validation.error ?? "Изменение объёма не прошло проверку.",
        );
      }
      return proposeQuantityChange(context, {
        projectId,
        lineId,
        submission: validation.submission,
        reason: input.draft.reason.trim(),
        idempotencyKey: input.idempotencyKey,
      });
    },
    onSuccess: async (change) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["work-items"] }),
        queryClient.invalidateQueries({ queryKey: ["workbench", projectId] }),
        queryClient.invalidateQueries({
          queryKey: ["records", projectId, "BOQ_SCOPE"],
        }),
      ]);
      navigate(
        `/projects/${encodeURIComponent(projectId)}/manual-changes/${encodeURIComponent(change.change_id)}`,
        { replace: true },
      );
    },
  });

  if (projectQuery.isError || quantityQuery.isError) {
    const failed = projectQuery.isError ? projectQuery : quantityQuery;
    return (
      <div className="page">
        <ErrorBlock
          error={failed.error}
          onRetry={() => {
            void projectQuery.refetch();
            void quantityQuery.refetch();
          }}
        />
      </div>
    );
  }
  if (projectQuery.isPending || quantityQuery.isPending || draft === null) {
    return (
      <div className="page">
        <LoadingBlock label="Загрузка текущего объёма и версий методики" />
      </div>
    );
  }

  const project = projectQuery.data;
  const quantityContext = quantityQuery.data;
  const validation = validateQuantityChangeDraft(
    draft,
    quantityContext,
    project.code,
  );
  const change = (patch: Partial<QuantityChangeDraft>) => {
    setDraft((current) =>
      current === null
        ? current
        : { ...current, ...patch, acknowledged: false },
    );
    setOperationKey(null);
    setFormError(null);
    mutation.reset();
  };
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setFormError(validation.error);
    if (validation.error !== null) {
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
            : "Не удалось создать ключ операции.",
        );
        return;
      }
      setOperationKey(key);
    }
    mutation.mutate({ draft, idempotencyKey: key });
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
        <span>Изменение объёма</span>
      </nav>

      <header className="entry-header">
        <div>
          <p className="eyebrow">{project.code} · управляемое изменение</p>
          <h1>{quantityContext.description}</h1>
          <p>
            Изменение не заменяет доказательство. Сервер повторно проверит
            наблюдения, формулу, единицу, текущий комплект документов и
            утверждённые версии правил.
          </p>
        </div>
        <div className="entry-header__guard">
          <Icon name="shield" size={22} />
          <span>
            {quantityContext.critical
              ? `Критическое изменение. После предложения требуется независимое согласование роли ${quantityContext.approval_role ?? "не определена"}.`
              : "Методика классифицирует это изменение как некритическое; точная запись всё равно сохраняется в audit trail."}
          </span>
        </div>
      </header>

      <section className="quantity-context">
        <div className="quantity-context__value">
          <span>Текущий объём</span>
          <strong>
            {quantityContext.current_submission.draft.value}{" "}
            {quantityContext.unit}
          </strong>
          <StatusPill value={quantityContext.current_quantity_status} compact />
        </div>
        <dl className="task-facts">
          <div>
            <dt>Текущая запись</dt>
            <dd>{quantityContext.current_quantity_id}</dd>
          </div>
          <div>
            <dt>Комплект документов</dt>
            <dd>{quantityContext.document_set_revision_id}</dd>
          </div>
          <div>
            <dt>Политика количества</dt>
            <dd>{quantityContext.quantity_policy_version_id}</dd>
          </div>
          <div>
            <dt>Политика ручных изменений</dt>
            <dd>{quantityContext.manual_change_policy_version_id}</dd>
          </div>
        </dl>
      </section>

      <form className="entry-form quantity-change-form" onSubmit={submit}>
        <div className="entry-form__intro">
          <p className="eyebrow">Предлагаемая точная запись</p>
          <h2>Объём, доказательства и вычисление</h2>
          <p>
            Идентификаторы наблюдений должны существовать в этом проекте и быть
            проверенными. Числа вводятся десятичными строками без экспоненты.
          </p>
        </div>

        <div className="quantity-edit-grid">
          <label className="decision-field">
            <span>Количество, {quantityContext.unit}</span>
            <input
              type="text"
              inputMode="decimal"
              value={draft.value}
              required
              onChange={(event) => change({ value: event.target.value })}
            />
          </label>
          <label className="decision-field">
            <span>Приоритет источника</span>
            <input
              type="number"
              min="0"
              step="1"
              value={draft.sourcePriority}
              required
              onChange={(event) =>
                change({ sourcePriority: event.target.value })
              }
            />
          </label>
          <label className="decision-field">
            <span>Знаков округления</span>
            <input
              type="number"
              min="0"
              max="12"
              step="1"
              value={draft.roundingScale}
              required
              onChange={(event) =>
                change({ roundingScale: event.target.value })
              }
            />
          </label>
          <label className="decision-field">
            <span>Коэффициент отхода</span>
            <input
              type="text"
              inputMode="decimal"
              value={draft.wasteFactor}
              required
              onChange={(event) => change({ wasteFactor: event.target.value })}
            />
          </label>
        </div>

        <label className="decision-field">
          <span>Проверенные исходные наблюдения</span>
          <textarea
            value={draft.sourceObservationIds}
            rows={4}
            required
            spellCheck={false}
            onChange={(event) =>
              change({ sourceObservationIds: event.target.value })
            }
            placeholder="Один ID на строку"
          />
        </label>
        <label className="decision-field">
          <span>Альтернативные значения количества</span>
          <textarea
            value={draft.alternativeQuantityIds}
            rows={3}
            spellCheck={false}
            onChange={(event) =>
              change({ alternativeQuantityIds: event.target.value })
            }
            placeholder="Необязательно; один ID на строку"
          />
        </label>

        <fieldset className="quantity-formula">
          <legend>Формула независимого пересчёта</legend>
          <label className="decision-acknowledgement">
            <input
              type="checkbox"
              checked={draft.formulaEnabled}
              onChange={(event) =>
                change({
                  formulaEnabled: event.target.checked,
                  ...(event.target.checked
                    ? {}
                    : { formulaEvidenceJson: "{}" }),
                })
              }
            />
            <span>Количество вычисляется по формуле</span>
          </label>
          {draft.formulaEnabled && (
            <>
              <div className="quantity-edit-grid">
                <label className="decision-field">
                  <span>ID формулы</span>
                  <input
                    type="text"
                    value={draft.formulaId}
                    required
                    onChange={(event) =>
                      change({ formulaId: event.target.value })
                    }
                  />
                </label>
                <label className="decision-field">
                  <span>Операция</span>
                  <select
                    value={draft.formulaOperation}
                    onChange={(event) =>
                      change({
                        formulaOperation: event.target
                          .value as QuantityOperation,
                      })
                    }
                  >
                    {operations.map((operation) => (
                      <option key={operation.value} value={operation.value}>
                        {operation.label}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              <label className="decision-field">
                <span>Отображаемая формула</span>
                <input
                  type="text"
                  value={draft.formulaDisplay}
                  required
                  onChange={(event) =>
                    change({ formulaDisplay: event.target.value })
                  }
                />
              </label>
              <div className="quantity-json-grid">
                <label className="decision-field">
                  <span>Входы формулы — JSON: имя → десятичная строка</span>
                  <textarea
                    value={draft.formulaInputsJson}
                    rows={7}
                    required
                    spellCheck={false}
                    onChange={(event) =>
                      change({ formulaInputsJson: event.target.value })
                    }
                  />
                </label>
                <label className="decision-field">
                  <span>Доказательства — JSON: имя → ID наблюдения</span>
                  <textarea
                    value={draft.formulaEvidenceJson}
                    rows={7}
                    required
                    spellCheck={false}
                    onChange={(event) =>
                      change({ formulaEvidenceJson: event.target.value })
                    }
                  />
                </label>
              </div>
              <p className="quantity-formula__version">
                Утверждённая версия правил:{" "}
                {quantityContext.quantity_formula_rules_version_id ??
                  "не привязана — предложение будет заблокировано"}
              </p>
            </>
          )}
        </fieldset>

        <label className="decision-field">
          <span>Проверяемое основание изменения</span>
          <textarea
            value={draft.reason}
            maxLength={2000}
            rows={5}
            required
            onChange={(event) => change({ reason: event.target.value })}
            placeholder="Укажите документ, лист/ячейку, выполненную сверку и причину изменения."
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
              setDraft({ ...draft, acknowledged: event.target.checked });
              setOperationKey(null);
              setFormError(null);
              mutation.reset();
            }}
          />
          <span>
            Я проверил единицу, точное значение, источники, округление, отходы и
            формулу. Понимаю, что предложение не применяется до прохождения всех
            обязательных согласований.
          </span>
        </label>

        {(formError !== null || mutation.isError) && (
          <div className="decision-form__error" role="alert">
            <Icon name="warning" size={18} />
            <span>
              {formError ??
                (mutation.error instanceof Error
                  ? mutation.error.message
                  : "Не удалось зарегистрировать изменение объёма.")}
            </span>
          </div>
        )}
        <div className="decision-form__actions">
          <Link
            className="button button--secondary"
            to={`/projects/${encodeURIComponent(projectId)}/BOQ_SCOPE`}
          >
            Отмена
          </Link>
          <button
            className="button button--critical"
            type="submit"
            disabled={mutation.isPending}
          >
            {mutation.isPending
              ? "Фиксация предложения…"
              : quantityContext.critical
                ? "Зафиксировать и направить на проверку"
                : "Зафиксировать управляемое изменение"}
          </button>
        </div>
      </form>
    </div>
  );
}
