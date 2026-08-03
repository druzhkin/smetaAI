import { useDeferredValue, useMemo, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  assessNomenclature,
  getNomenclatureContext,
  getProject,
  newIdempotencyKey,
  type RequestContext,
} from "../api";
import { useAuth } from "../auth";
import { ErrorBlock, LoadingBlock } from "../components/Feedback";
import { Icon } from "../components/Icon";
import {
  validateNomenclatureAssessment,
  type NomenclatureDraft,
} from "../controlledWorkflows";
import { Link, useNavigation } from "../navigation";
import type { RuntimeConfig } from "../types";

export function NomenclatureAssessmentPage({
  config,
  projectId,
}: {
  config: RuntimeConfig;
  projectId: string;
}) {
  const auth = useAuth();
  const queryClient = useQueryClient();
  const { navigate } = useNavigation();
  const [catalogQuery, setCatalogQuery] = useState("");
  const deferredCatalogQuery = useDeferredValue(catalogQuery.trim());
  const [draft, setDraft] = useState<NomenclatureDraft>({
    sourceItemId: "",
    canonicalItemId: "",
    sourceAttributesObservationId: "",
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
  const nomenclatureQuery = useQuery({
    queryKey: [
      "nomenclature-context",
      projectId,
      deferredCatalogQuery,
      draft.sourceItemId,
      "technical_attributes",
    ],
    queryFn: ({ signal }) =>
      getNomenclatureContext(
        context,
        projectId,
        {
          ...(deferredCatalogQuery === ""
            ? {}
            : { catalogQuery: deferredCatalogQuery }),
          evidenceFieldName: "technical_attributes",
          ...(draft.sourceItemId === ""
            ? {}
            : { sourceItemId: draft.sourceItemId }),
        },
        signal,
      ),
  });
  const mutation = useMutation({
    mutationFn: (idempotencyKey: string) => {
      if (
        projectQuery.data === undefined ||
        nomenclatureQuery.data === undefined
      ) {
        throw new Error("Контекст номенклатуры не загружен.");
      }
      const validation = validateNomenclatureAssessment(
        draft,
        nomenclatureQuery.data,
        projectQuery.data.code,
      );
      if (validation !== null) {
        throw new Error(validation);
      }
      return assessNomenclature(context, {
        projectId,
        sourceItemId: draft.sourceItemId,
        canonicalItemId: draft.canonicalItemId,
        sourceAttributesObservationId: draft.sourceAttributesObservationId,
        reason: draft.reason.trim(),
        idempotencyKey,
      });
    },
    onSuccess: async (match) => {
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["records", projectId, "BOQ_SCOPE"],
        }),
        queryClient.invalidateQueries({
          queryKey: ["workbench", projectId],
        }),
      ]);
      navigate(
        `/projects/${encodeURIComponent(projectId)}/nomenclature/${encodeURIComponent(match.match.match_id)}/review`,
        { replace: true },
      );
    },
  });

  if (projectQuery.isError || nomenclatureQuery.isError) {
    const failed = projectQuery.isError ? projectQuery : nomenclatureQuery;
    return (
      <div className="page">
        <ErrorBlock
          error={failed.error}
          onRetry={() => {
            void projectQuery.refetch();
            void nomenclatureQuery.refetch();
          }}
        />
      </div>
    );
  }
  if (projectQuery.isPending || nomenclatureQuery.isPending) {
    return (
      <div className="page">
        <LoadingBlock label="Загрузка контролируемого каталога и атрибутов" />
      </div>
    );
  }
  const project = projectQuery.data;
  const nomenclature = nomenclatureQuery.data;
  const selectedSourceItem = nomenclature.source_items.find(
    (item) => item.source_item_id === draft.sourceItemId,
  );
  const selectedCatalogItem = nomenclature.catalog_items.find(
    (item) => item.canonical_item_id === draft.canonicalItemId,
  );
  const selectedEvidence = nomenclature.evidence_candidates.find(
    (item) =>
      item.observation.observation_id === draft.sourceAttributesObservationId,
  );
  const validation = validateNomenclatureAssessment(
    draft,
    nomenclature,
    project.code,
  );
  const change = (patch: Partial<NomenclatureDraft>) => {
    setDraft((current) => ({
      ...current,
      ...patch,
      acknowledged: false,
    }));
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
        <span>Номенклатура</span>
      </nav>

      <header className="entry-header">
        <div>
          <p className="eyebrow">Контур 05 · техническая номенклатура</p>
          <h1>Сопоставить критические атрибуты</h1>
          <p>
            Система сравнит каждый обязательный атрибут детерминированно.
            Текстовое сходство не участвует в решении; недостающие данные и
            несовпадения получают разные классы.
          </p>
        </div>
        <div className="entry-header__guard">
          <Icon name="shield" size={22} />
          <span>
            Каталог {nomenclature.catalog_version_id} и комплект{" "}
            {nomenclature.document_set_revision_id} выбраны сервером.
          </span>
        </div>
      </header>

      <form className="entry-form controlled-form" onSubmit={submit}>
        <section className="entry-form__intro">
          <p className="eyebrow">01 · позиция BoQ</p>
          <h2>Выберите позицию из текущего проверенного ВОР</h2>
          <p>
            Список сформирован сервером. Ручной ввод идентификатора исключён,
            чтобы характеристики одной позиции нельзя было применить к другой.
          </p>
        </section>
        {nomenclature.source_items.length === 0 ? (
          <p className="inline-error" role="alert">
            Нет текущих проверенных компонентов ВОР. Сопоставление
            заблокировано.
          </p>
        ) : (
          <div className="evidence-choice-grid">
            {nomenclature.source_items.map((item) => (
              <label className="evidence-choice" key={item.source_item_id}>
                <input
                  type="radio"
                  name="nomenclature-source-item"
                  checked={draft.sourceItemId === item.source_item_id}
                  onChange={() =>
                    change({
                      sourceItemId: item.source_item_id,
                      sourceAttributesObservationId: "",
                      canonicalItemId: "",
                    })
                  }
                />
                <span className="evidence-choice__body">
                  <strong>{item.description}</strong>
                  <span>
                    {item.line_key} · {item.work_code} · {item.unit}
                  </span>
                  <small>Semantic key: {item.source_item_id}</small>
                </span>
              </label>
            ))}
          </div>
        )}

        <section className="entry-form__section">
          <div className="entry-form__intro">
            <p className="eyebrow">02 · техническое доказательство</p>
            <h2>Выберите проверенный набор атрибутов</h2>
          </div>
          {draft.sourceItemId === "" ? (
            <p className="inline-warning">
              Сначала выберите позицию ВОР. Сервер покажет только
              доказательства, привязанные именно к ней.
            </p>
          ) : nomenclature.evidence_candidates.length === 0 ? (
            <p className="inline-error" role="alert">
              Для выбранной позиции нет проверенного связанного набора
              технических характеристик. Сопоставление заблокировано.
            </p>
          ) : (
            <div className="evidence-choice-grid">
              {nomenclature.evidence_candidates.map((candidate) => (
                <label
                  className="evidence-choice"
                  key={candidate.observation.observation_id}
                >
                  <input
                    type="radio"
                    name="nomenclature-evidence"
                    checked={
                      draft.sourceAttributesObservationId ===
                      candidate.observation.observation_id
                    }
                    onChange={() =>
                      change({
                        sourceAttributesObservationId:
                          candidate.observation.observation_id,
                      })
                    }
                  />
                  <span className="evidence-choice__body">
                    <strong>{candidate.observation.observation_id}</strong>
                    <span className="attribute-chips">
                      {Object.entries(candidate.attributes).map(
                        ([key, value]) => (
                          <span key={key}>
                            {key}: {value}
                          </span>
                        ),
                      )}
                    </span>
                    <small>
                      {candidate.observation.method} ·{" "}
                      {candidate.observation.location.locator}
                    </small>
                  </span>
                </label>
              ))}
            </div>
          )}
        </section>

        <section className="entry-form__section">
          <div className="controlled-section-heading">
            <div>
              <p className="eyebrow">03 · утверждённый каталог</p>
              <h2>Выберите каноническую позицию</h2>
            </div>
            <label className="search-field compact-search">
              <Icon name="search" size={16} />
              <span className="sr-only">Поиск по каталогу</span>
              <input
                type="search"
                value={catalogQuery}
                placeholder="ID или атрибут"
                onChange={(event) => setCatalogQuery(event.target.value)}
              />
            </label>
          </div>
          <div className="catalog-choice-grid">
            {nomenclature.catalog_items.map((item) => (
              <label className="catalog-choice" key={item.canonical_item_id}>
                <input
                  type="radio"
                  name="catalog-item"
                  checked={draft.canonicalItemId === item.canonical_item_id}
                  onChange={() =>
                    change({ canonicalItemId: item.canonical_item_id })
                  }
                />
                <span>
                  <strong>{item.canonical_item_id}</strong>
                  <small>
                    Критические: {item.critical_attributes.join(", ")}
                  </small>
                  <span className="attribute-chips">
                    {Object.entries(item.attributes).map(([key, value]) => (
                      <span key={key}>
                        {key}: {value}
                      </span>
                    ))}
                  </span>
                  {draft.sourceItemId !== "" && (
                    <span className="attribute-chips">
                      {item.retrieval_exact_identifier && (
                        <span>точный ID упомянут</span>
                      )}
                      {item.retrieval_matched_critical_attributes.map(
                        (attribute) => (
                          <span key={`critical-${attribute}`}>
                            найдено значение: {attribute}
                          </span>
                        ),
                      )}
                      {item.retrieval_matched_terms.length > 0 ? (
                        <span>
                          термины: {item.retrieval_matched_terms.join(", ")}
                        </span>
                      ) : (
                        <span>лексических совпадений нет</span>
                      )}
                    </span>
                  )}
                  {item.critical_price && (
                    <em>Критичная цена по методологии</em>
                  )}
                </span>
              </label>
            ))}
          </div>
          {selectedSourceItem !== undefined && (
            <p className="inline-warning">
              Кандидаты ранжированы только по буквальным терминам наименования «
              {selectedSourceItem.description}». Это не подтверждает техническую
              эквивалентность; решение строится только по проверенным
              критическим характеристикам. {nomenclature.retrieval_notice}
            </p>
          )}
          {nomenclature.catalog_items_truncated && (
            <p className="inline-warning">
              Результат ограничен 100 позициями. Уточните поиск.
            </p>
          )}
        </section>

        {selectedCatalogItem !== undefined &&
          selectedEvidence !== undefined && (
            <section className="attribute-comparison">
              <div className="controlled-section-heading">
                <div>
                  <p className="eyebrow">Предпросмотр</p>
                  <h2>Критические атрибуты</h2>
                </div>
                <span>Решение всё равно повторно рассчитает сервер</span>
              </div>
              <div className="attribute-table">
                <div className="attribute-table__head">
                  <span>Атрибут</span>
                  <span>Источник</span>
                  <span>Каталог</span>
                  <span>Предварительно</span>
                </div>
                {selectedCatalogItem.critical_attributes.map((attribute) => {
                  const sourceValue =
                    selectedEvidence.attributes[attribute] ?? null;
                  const catalogValue =
                    selectedCatalogItem.attributes[attribute] ?? null;
                  const matches =
                    sourceValue !== null &&
                    catalogValue !== null &&
                    sourceValue.trim().toLocaleLowerCase() ===
                      catalogValue.trim().toLocaleLowerCase();
                  return (
                    <div key={attribute}>
                      <strong>{attribute}</strong>
                      <span>{sourceValue ?? "нет данных"}</span>
                      <span>{catalogValue ?? "нет данных"}</span>
                      <span
                        className={matches ? "positive-text" : "danger-text"}
                      >
                        {sourceValue === null
                          ? "INSUFFICIENT_DATA"
                          : matches
                            ? "EXACT"
                            : "MISMATCH"}
                      </span>
                    </div>
                  );
                })}
              </div>
            </section>
          )}

        <section className="entry-form__section">
          <div className="form-grid">
            <label className="field form-grid__wide">
              <span>Основание сопоставления</span>
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
                Подтверждаю выбор исходной и канонической позиции; сходство
                наименований не считается техническим соответствием.
              </span>
            </label>
          </div>
        </section>
        {(formError !== null || mutation.isError) && (
          <div className="inline-error" role="alert">
            {formError ??
              (mutation.error instanceof Error
                ? mutation.error.message
                : "Сопоставление не выполнено.")}
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
            <Icon name="trace" size={16} />
            {mutation.isPending ? "Сопоставление…" : "Сопоставить атрибуты"}
          </button>
        </div>
      </form>
    </div>
  );
}
