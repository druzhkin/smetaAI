import { useMemo, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  createBoqLine,
  getBoqAuthoringContext,
  getProject,
  newIdempotencyKey,
  type RequestContext,
} from "../api";
import { useAuth } from "../auth";
import { ErrorBlock, LoadingBlock } from "../components/Feedback";
import { Icon } from "../components/Icon";
import { validateBoqLine, type BoqLineDraft } from "../controlledWorkflows";
import { displayValue } from "../format";
import { Link, useNavigation } from "../navigation";
import type {
  BoqCostComponent,
  CostBasisKind,
  CostCategory,
  RuntimeConfig,
} from "../types";

const categories: CostCategory[] = [
  "LABOUR",
  "PLANT",
  "MATERIAL",
  "SUBCONTRACT",
  "LOGISTICS",
  "MOBILISATION",
  "CONTRACT_FINANCE",
  "RISK",
  "OVERHEAD",
  "PROFIT",
  "TAX",
];
const basisKinds: CostBasisKind[] = [
  "MARKET",
  "NORMATIVE",
  "APPROVED_ASSUMPTION",
  "RISK_MODEL",
  "DERIVED_MODEL",
];

function emptyComponent(): BoqCostComponent {
  return {
    semantic_key: "",
    category: "MATERIAL",
    basis_kind: "MARKET",
    sign: 1,
    factor_ids: [],
  };
}

export function BoqAuthoringPage({
  config,
  projectId,
}: {
  config: RuntimeConfig;
  projectId: string;
}) {
  const auth = useAuth();
  const queryClient = useQueryClient();
  const { navigate } = useNavigation();
  const [draft, setDraft] = useState<BoqLineDraft>({
    lineKey: "",
    wbsNodeId: "",
    description: "",
    evidenceObservationIds: [],
    costComponents: [emptyComponent()],
    criticalQuantity: false,
    reason: "",
    projectCodeConfirmation: "",
    acknowledged: false,
  });
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
  const authoringQuery = useQuery({
    queryKey: ["boq-authoring-context", projectId, "boq_line"],
    queryFn: ({ signal }) =>
      getBoqAuthoringContext(context, projectId, "boq_line", signal),
  });
  const mutation = useMutation({
    mutationFn: (idempotencyKey: string) => {
      if (
        projectQuery.data === undefined ||
        authoringQuery.data === undefined
      ) {
        throw new Error("Контекст построения BoQ не загружен.");
      }
      const validation = validateBoqLine(
        draft,
        authoringQuery.data,
        projectQuery.data.code,
      );
      if (validation !== null) {
        throw new Error(validation);
      }
      const selected = authoringQuery.data.evidence_candidates.find(
        (candidate) =>
          candidate.observation.observation_id ===
          draft.evidenceObservationIds[0],
      );
      if (selected === undefined) {
        throw new Error("Серверное доказательство строки больше недоступно.");
      }
      return createBoqLine(context, {
        projectId,
        lineKey: draft.lineKey,
        wbsNodeId: draft.wbsNodeId,
        workCode: selected.work_code,
        description: draft.description,
        unit: selected.unit,
        evidenceObservationIds: draft.evidenceObservationIds,
        costComponents: draft.costComponents,
        criticalQuantity: draft.criticalQuantity,
        reason: draft.reason.trim(),
        idempotencyKey,
      });
    },
    onSuccess: async (line) => {
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["records", projectId, "BOQ_SCOPE"],
        }),
        queryClient.invalidateQueries({
          queryKey: ["workbench", projectId],
        }),
      ]);
      navigate(
        `/projects/${encodeURIComponent(projectId)}/boq-lines/${encodeURIComponent(line.line_id)}/review`,
        { replace: true },
      );
    },
  });

  if (projectQuery.isError || authoringQuery.isError) {
    const failed = projectQuery.isError ? projectQuery : authoringQuery;
    return (
      <div className="page">
        <ErrorBlock
          error={failed.error}
          onRetry={() => {
            void projectQuery.refetch();
            void authoringQuery.refetch();
          }}
        />
      </div>
    );
  }
  if (projectQuery.isPending || authoringQuery.isPending) {
    return (
      <div className="page">
        <LoadingBlock label="Загрузка проверенных строк извлечения для BoQ" />
      </div>
    );
  }

  const project = projectQuery.data;
  const authoring = authoringQuery.data;
  const validation = validateBoqLine(draft, authoring, project.code);
  const selected = authoring.evidence_candidates.find(
    (candidate) =>
      candidate.observation.observation_id === draft.evidenceObservationIds[0],
  );
  const change = (patch: Partial<BoqLineDraft>) => {
    setDraft((current) => ({
      ...current,
      ...patch,
      acknowledged: false,
    }));
    setOperationKey(null);
    setFormError(null);
    mutation.reset();
  };
  const updateComponent = (index: number, patch: Partial<BoqCostComponent>) => {
    change({
      costComponents: draft.costComponents.map((component, itemIndex) =>
        itemIndex === index ? { ...component, ...patch } : component,
      ),
    });
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
        <span>Новая строка</span>
      </nav>

      <header className="entry-header">
        <div>
          <p className="eyebrow">Контур 03 · структура стоимости</p>
          <h1>Сформировать строку BoQ</h1>
          <p>
            Код работы и единица поступают из выбранного проверенного
            наблюдения. План компонентов определяет, какие основания стоимости
            обязаны полностью покрыть строку перед расчётом.
          </p>
        </div>
        <div className="entry-header__guard">
          <Icon name="shield" size={22} />
          <span>
            Созданная строка останется IN_REVIEW до независимой технической
            проверки другим пользователем.
          </span>
        </div>
      </header>

      <form className="entry-form controlled-form" onSubmit={submit}>
        <section className="entry-form__intro">
          <p className="eyebrow">01 · доказательство</p>
          <h2>Выберите согласованную строку извлечения</h2>
        </section>
        <div className="evidence-choice-grid">
          {authoring.evidence_candidates.map((candidate) => (
            <label
              className="evidence-choice"
              key={candidate.observation.observation_id}
            >
              <input
                type="checkbox"
                checked={draft.evidenceObservationIds.includes(
                  candidate.observation.observation_id,
                )}
                onChange={(event) =>
                  change({
                    evidenceObservationIds: event.target.checked
                      ? [
                          ...draft.evidenceObservationIds,
                          candidate.observation.observation_id,
                        ]
                      : draft.evidenceObservationIds.filter(
                          (id) => id !== candidate.observation.observation_id,
                        ),
                  })
                }
              />
              <span className="evidence-choice__body">
                <span className="evidence-choice__heading">
                  <strong>{candidate.work_code}</strong>
                  <span>{candidate.unit}</span>
                </span>
                <code>{displayValue(candidate.observation.value)}</code>
                <small>{candidate.observation.location.locator}</small>
              </span>
            </label>
          ))}
        </div>
        {authoring.candidates_truncated && (
          <p className="inline-warning">
            Показаны первые 100 доказательств. Разделите пакет или уточните поле
            извлечения.
          </p>
        )}

        <section className="entry-form__section">
          <div className="entry-form__intro">
            <p className="eyebrow">02 · структура</p>
            <h2>
              {selected === undefined
                ? "Сначала выберите доказательство"
                : `${selected.work_code} · ${selected.unit}`}
            </h2>
          </div>
          <div className="form-grid">
            <label className="field">
              <span>Стабильный ключ строки</span>
              <input
                maxLength={128}
                value={draft.lineKey}
                onChange={(event) => change({ lineKey: event.target.value })}
              />
            </label>
            <label className="field">
              <span>Узел WBS</span>
              <input
                maxLength={128}
                value={draft.wbsNodeId}
                onChange={(event) => change({ wbsNodeId: event.target.value })}
              />
            </label>
            <label className="field form-grid__wide">
              <span>Описание работы</span>
              <textarea
                rows={3}
                maxLength={2000}
                value={draft.description}
                onChange={(event) =>
                  change({ description: event.target.value })
                }
              />
            </label>
            <label className="attestation form-grid__wide">
              <input
                type="checkbox"
                checked={draft.criticalQuantity}
                onChange={(event) =>
                  change({ criticalQuantity: event.target.checked })
                }
              />
              <span>
                Количество критично для цены или прибыли и требует независимого
                покрытия.
              </span>
            </label>
          </div>
        </section>

        <section className="entry-form__section">
          <div className="controlled-section-heading">
            <div>
              <p className="eyebrow">03 · компоненты стоимости</p>
              <h2>Полный ожидаемый состав строки</h2>
            </div>
            <button
              className="button button--secondary"
              type="button"
              onClick={() =>
                change({
                  costComponents: [...draft.costComponents, emptyComponent()],
                })
              }
            >
              <Icon name="plus" size={15} />
              Добавить компонент
            </button>
          </div>
          <div className="component-editor-list">
            {draft.costComponents.map((component, index) => (
              <fieldset className="component-editor" key={index}>
                <legend>Компонент {index + 1}</legend>
                <div className="form-grid">
                  <label className="field">
                    <span>Semantic key</span>
                    <input
                      maxLength={128}
                      value={component.semantic_key}
                      onChange={(event) =>
                        updateComponent(index, {
                          semantic_key: event.target.value,
                        })
                      }
                    />
                  </label>
                  <label className="field">
                    <span>Категория</span>
                    <select
                      value={component.category}
                      onChange={(event) =>
                        updateComponent(index, {
                          category: event.target.value as CostCategory,
                        })
                      }
                    >
                      {categories.map((value) => (
                        <option key={value} value={value}>
                          {value}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="field">
                    <span>Основание стоимости</span>
                    <select
                      value={component.basis_kind}
                      onChange={(event) =>
                        updateComponent(index, {
                          basis_kind: event.target.value as CostBasisKind,
                        })
                      }
                    >
                      {basisKinds.map((value) => (
                        <option key={value} value={value}>
                          {value}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="field">
                    <span>Знак</span>
                    <select
                      value={String(component.sign)}
                      onChange={(event) =>
                        updateComponent(index, {
                          sign: event.target.value === "-1" ? -1 : 1,
                        })
                      }
                    >
                      <option value="1">+1 · включить</option>
                      <option value="-1">−1 · вычесть</option>
                    </select>
                  </label>
                  <label className="field form-grid__wide">
                    <span>Factor IDs через запятую</span>
                    <input
                      value={component.factor_ids.join(", ")}
                      onChange={(event) =>
                        updateComponent(index, {
                          factor_ids: event.target.value
                            .split(",")
                            .map((value) => value.trim())
                            .filter(Boolean),
                        })
                      }
                    />
                  </label>
                </div>
                {draft.costComponents.length > 1 && (
                  <button
                    className="text-button danger-text"
                    type="button"
                    onClick={() =>
                      change({
                        costComponents: draft.costComponents.filter(
                          (_, itemIndex) => itemIndex !== index,
                        ),
                      })
                    }
                  >
                    Удалить компонент
                  </button>
                )}
              </fieldset>
            ))}
          </div>
        </section>

        <section className="entry-form__section">
          <div className="form-grid">
            <label className="field form-grid__wide">
              <span>Основание формирования строки</span>
              <textarea
                rows={4}
                maxLength={2000}
                value={draft.reason}
                onChange={(event) => change({ reason: event.target.value })}
              />
            </label>
            <label className="field">
              <span>Точный код проекта</span>
              <input
                value={draft.projectCodeConfirmation}
                onChange={(event) =>
                  change({
                    projectCodeConfirmation: event.target.value,
                  })
                }
                autoComplete="off"
              />
            </label>
            <label className="attestation form-grid__wide">
              <input
                type="checkbox"
                checked={draft.acknowledged}
                onChange={(event) =>
                  setDraft((current) => ({
                    ...current,
                    acknowledged: event.target.checked,
                  }))
                }
              />
              <span>
                Подтверждаю, что план компонентов стоимости полный для этой
                строки, а отсутствие компонента не трактуется как отсутствие
                работы.
              </span>
            </label>
          </div>
        </section>

        {(formError !== null || mutation.isError) && (
          <div className="inline-error" role="alert">
            {formError ??
              (mutation.error instanceof Error
                ? mutation.error.message
                : "Строка BoQ не создана.")}
          </div>
        )}
        <div className="form-actions">
          <Link
            className="button button--secondary"
            to={`/projects/${encodeURIComponent(projectId)}/BOQ_SCOPE`}
          >
            Отмена
          </Link>
          <button
            className="button button--primary"
            type="submit"
            disabled={validation !== null || mutation.isPending}
          >
            <Icon name="plus" size={16} />
            {mutation.isPending ? "Фиксация…" : "Создать IN_REVIEW"}
          </button>
        </div>
      </form>
    </div>
  );
}
