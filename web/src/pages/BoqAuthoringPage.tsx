import { useMemo, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  createBoqLine,
  getBoqAuthoringContext,
  getBoqSpreadsheetCandidates,
  getProject,
  newIdempotencyKey,
  proposeBoqSpreadsheetMapping,
  proposeBoqSpreadsheetQuantity,
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

interface SpreadsheetMappingDraft {
  sourceObservationId: string;
  workCode: string;
  description: string;
  unit: string;
  reason: string;
  projectCodeConfirmation: string;
  acknowledged: boolean;
}

const emptyMappingDraft: SpreadsheetMappingDraft = {
  sourceObservationId: "",
  workCode: "",
  description: "",
  unit: "",
  reason: "",
  projectCodeConfirmation: "",
  acknowledged: false,
};

interface SpreadsheetQuantityDraft {
  sourceObservationId: string;
  reason: string;
  projectCodeConfirmation: string;
  acknowledged: boolean;
}

const emptyQuantityDraft: SpreadsheetQuantityDraft = {
  sourceObservationId: "",
  reason: "",
  projectCodeConfirmation: "",
  acknowledged: false,
};

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
  const [mappingDraft, setMappingDraft] =
    useState<SpreadsheetMappingDraft>(emptyMappingDraft);
  const [mappingOperationKey, setMappingOperationKey] = useState<string | null>(
    null,
  );
  const [mappingProposedAt, setMappingProposedAt] = useState<string | null>(
    null,
  );
  const [mappingFormError, setMappingFormError] = useState<string | null>(null);
  const [quantityDraft, setQuantityDraft] =
    useState<SpreadsheetQuantityDraft>(emptyQuantityDraft);
  const [quantityOperationKey, setQuantityOperationKey] = useState<
    string | null
  >(null);
  const [quantityProposedAt, setQuantityProposedAt] = useState<string | null>(
    null,
  );
  const [quantityFormError, setQuantityFormError] = useState<string | null>(
    null,
  );
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
  const spreadsheetQuery = useQuery({
    queryKey: ["boq-spreadsheet-candidates", projectId],
    queryFn: ({ signal }) =>
      getBoqSpreadsheetCandidates(context, projectId, signal),
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
  const mappingMutation = useMutation({
    mutationFn: ({
      idempotencyKey,
      proposedAt,
    }: {
      idempotencyKey: string;
      proposedAt: string;
    }) => {
      const source = spreadsheetQuery.data?.candidates.find(
        (candidate) =>
          candidate.source_observation.observation_id ===
          mappingDraft.sourceObservationId,
      );
      if (source === undefined || !source.proposal_allowed) {
        throw new Error("Импортированная строка недоступна для сопоставления.");
      }
      return proposeBoqSpreadsheetMapping(context, {
        projectId,
        observationId: source.source_observation.observation_id,
        workCode: mappingDraft.workCode.trim(),
        description: mappingDraft.description.trim(),
        unit: mappingDraft.unit.trim(),
        expectedSourceObservationHash: source.source_observation_hash,
        proposedAt,
        reason: mappingDraft.reason.trim(),
        idempotencyKey,
      });
    },
    onSuccess: async (proposal) => {
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["boq-spreadsheet-candidates", projectId],
        }),
        queryClient.invalidateQueries({
          queryKey: ["workbench", projectId],
        }),
      ]);
      navigate(
        `/projects/${encodeURIComponent(projectId)}/evidence/observations/${encodeURIComponent(proposal.observation_id)}/review`,
      );
    },
  });
  const quantityMutation = useMutation({
    mutationFn: ({
      idempotencyKey,
      proposedAt,
    }: {
      idempotencyKey: string;
      proposedAt: string;
    }) => {
      const source = spreadsheetQuery.data?.candidates.find(
        (candidate) =>
          candidate.source_observation.observation_id ===
          quantityDraft.sourceObservationId,
      );
      if (source === undefined || !source.quantity_proposal_allowed) {
        throw new Error("Количество недоступно для независимой проверки.");
      }
      return proposeBoqSpreadsheetQuantity(context, {
        projectId,
        observationId: source.source_observation.observation_id,
        expectedSourceObservationHash: source.source_observation_hash,
        proposedAt,
        reason: quantityDraft.reason.trim(),
        idempotencyKey,
      });
    },
    onSuccess: async (proposal) => {
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["boq-spreadsheet-candidates", projectId],
        }),
        queryClient.invalidateQueries({
          queryKey: ["workbench", projectId],
        }),
      ]);
      navigate(
        `/projects/${encodeURIComponent(projectId)}/evidence/observations/${encodeURIComponent(proposal.observation_id)}/review`,
      );
    },
  });

  if (
    projectQuery.isError ||
    authoringQuery.isError ||
    spreadsheetQuery.isError
  ) {
    const failed = projectQuery.isError
      ? projectQuery
      : authoringQuery.isError
        ? authoringQuery
        : spreadsheetQuery;
    return (
      <div className="page">
        <ErrorBlock
          error={failed.error}
          onRetry={() => {
            void projectQuery.refetch();
            void authoringQuery.refetch();
            void spreadsheetQuery.refetch();
          }}
        />
      </div>
    );
  }
  if (
    projectQuery.isPending ||
    authoringQuery.isPending ||
    spreadsheetQuery.isPending
  ) {
    return (
      <div className="page">
        <LoadingBlock label="Загрузка проверенных строк извлечения для BoQ" />
      </div>
    );
  }

  const project = projectQuery.data;
  const authoring = authoringQuery.data;
  const spreadsheet = spreadsheetQuery.data;
  const validation = validateBoqLine(draft, authoring, project.code);
  const selected = authoring.evidence_candidates.find(
    (candidate) =>
      candidate.observation.observation_id === draft.evidenceObservationIds[0],
  );
  const selectedSpreadsheetRow = spreadsheet.candidates.find(
    (candidate) =>
      candidate.source_observation.observation_id ===
      mappingDraft.sourceObservationId,
  );
  const selectedQuantityRow = spreadsheet.candidates.find(
    (candidate) =>
      candidate.source_observation.observation_id ===
      quantityDraft.sourceObservationId,
  );
  const mappingValidation = (() => {
    if (selectedSpreadsheetRow === undefined) {
      return "Выберите импортированную строку.";
    }
    if (!selectedSpreadsheetRow.proposal_allowed) {
      return `Сопоставление заблокировано: ${selectedSpreadsheetRow.proposal_blockers.join(", ")}.`;
    }
    if (
      !mappingDraft.workCode.trim() ||
      !mappingDraft.description.trim() ||
      !mappingDraft.unit.trim()
    ) {
      return "Укажите код работы, каноническое описание и единицу.";
    }
    if (
      mappingDraft.workCode !== mappingDraft.workCode.trim() ||
      mappingDraft.description !== mappingDraft.description.trim() ||
      mappingDraft.unit !== mappingDraft.unit.trim()
    ) {
      return "Поля сопоставления не должны содержать пробелы по краям.";
    }
    if (mappingDraft.reason.trim().length < 10) {
      return "Опишите основание сопоставления не короче 10 символов.";
    }
    if (mappingDraft.projectCodeConfirmation !== project.code) {
      return "Введите точный код проекта.";
    }
    if (!mappingDraft.acknowledged) {
      return "Подтвердите ограниченный состав сопоставления.";
    }
    return null;
  })();
  const quantityValidation = (() => {
    if (selectedQuantityRow === undefined) {
      return "Выберите импортированную строку с количеством.";
    }
    if (!selectedQuantityRow.quantity_proposal_allowed) {
      return `Проверка количества заблокирована: ${selectedQuantityRow.quantity_proposal_blockers.join(", ")}.`;
    }
    if (quantityDraft.reason.trim().length < 10) {
      return "Опишите проверяемую ячейку и основание не короче 10 символов.";
    }
    if (quantityDraft.projectCodeConfirmation !== project.code) {
      return "Введите точный код проекта.";
    }
    if (!quantityDraft.acknowledged) {
      return "Подтвердите, что числовое значение не редактировалось.";
    }
    return null;
  })();
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
  const changeMapping = (patch: Partial<SpreadsheetMappingDraft>) => {
    setMappingDraft((current) => ({
      ...current,
      ...patch,
      acknowledged: patch.acknowledged ?? false,
    }));
    setMappingOperationKey(null);
    setMappingProposedAt(null);
    setMappingFormError(null);
    mappingMutation.reset();
  };
  const selectSpreadsheetRow = (observationId: string) => {
    const candidate = spreadsheet.candidates.find(
      (item) => item.source_observation.observation_id === observationId,
    );
    if (candidate === undefined) {
      return;
    }
    setMappingDraft({
      ...emptyMappingDraft,
      sourceObservationId: observationId,
      description: candidate.description.slice(0, 2000),
      unit: candidate.unit,
    });
    setMappingOperationKey(null);
    setMappingProposedAt(null);
    setMappingFormError(null);
    mappingMutation.reset();
  };
  const submitMapping = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setMappingFormError(mappingValidation);
    if (mappingValidation !== null) {
      return;
    }
    const key = mappingOperationKey ?? newIdempotencyKey();
    const proposedAt = mappingProposedAt ?? new Date().toISOString();
    setMappingOperationKey(key);
    setMappingProposedAt(proposedAt);
    mappingMutation.mutate({ idempotencyKey: key, proposedAt });
  };
  const changeQuantity = (patch: Partial<SpreadsheetQuantityDraft>) => {
    setQuantityDraft((current) => ({
      ...current,
      ...patch,
      acknowledged: patch.acknowledged ?? false,
    }));
    setQuantityOperationKey(null);
    setQuantityProposedAt(null);
    setQuantityFormError(null);
    quantityMutation.reset();
  };
  const selectQuantityRow = (sourceObservationId: string) => {
    setQuantityDraft({
      ...emptyQuantityDraft,
      sourceObservationId,
    });
    setQuantityOperationKey(null);
    setQuantityProposedAt(null);
    setQuantityFormError(null);
    quantityMutation.reset();
  };
  const submitQuantity = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setQuantityFormError(quantityValidation);
    if (quantityValidation !== null) {
      return;
    }
    const key = quantityOperationKey ?? newIdempotencyKey();
    const proposedAt = quantityProposedAt ?? new Date().toISOString();
    setQuantityOperationKey(key);
    setQuantityProposedAt(proposedAt);
    quantityMutation.mutate({ idempotencyKey: key, proposedAt });
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

      <section className="entry-form controlled-form">
        <div className="entry-form__intro">
          <p className="eyebrow">Импорт XLSX · обязательная проверка</p>
          <h2>Сопоставить строки исходной ведомости</h2>
          <p>
            Здесь подтверждаются только идентичность работы, каноническое
            описание и единица. Количество проходит отдельную проверку и может
            быть прикреплено только после проверки строки BoQ; для критической
            позиции одного источника недостаточно.
          </p>
        </div>
        {spreadsheet.candidates.length === 0 ? (
          <p className="inline-warning">
            Управляемые строки XLSX в текущем комплекте документов отсутствуют.
          </p>
        ) : (
          <div className="evidence-choice-grid">
            {spreadsheet.candidates.map((candidate) => (
              <article
                className="evidence-choice"
                key={candidate.source_observation.observation_id}
              >
                <span className="evidence-choice__body">
                  <span className="evidence-choice__heading">
                    <strong>Позиция {candidate.source_position_id}</strong>
                    <span>
                      {candidate.quantity} {candidate.unit}
                    </span>
                  </span>
                  <span>{candidate.description}</span>
                  {candidate.specification !== null && (
                    <small>{candidate.specification}</small>
                  )}
                  <small>
                    {candidate.worksheet_name}! строка {candidate.row_number} ·{" "}
                    {candidate.source_item_id}
                  </small>
                  {candidate.proposal_observation_id !== null ? (
                    <Link
                      className="text-button"
                      to={`/projects/${encodeURIComponent(projectId)}/evidence/observations/${encodeURIComponent(candidate.proposal_observation_id)}/review`}
                    >
                      Проверка: {candidate.proposal_task_status ?? "неизвестно"}
                    </Link>
                  ) : (
                    <button
                      className="text-button"
                      type="button"
                      disabled={!candidate.proposal_allowed}
                      onClick={() =>
                        selectSpreadsheetRow(
                          candidate.source_observation.observation_id,
                        )
                      }
                    >
                      Подготовить сопоставление
                    </button>
                  )}
                  {!candidate.proposal_allowed &&
                    candidate.proposal_blockers.length > 0 && (
                      <small className="danger-text">
                        BLOCKED: {candidate.proposal_blockers.join(", ")}
                      </small>
                    )}
                  {candidate.quantity_proposal_observation_id !== null ? (
                    <Link
                      className="text-button"
                      to={`/projects/${encodeURIComponent(projectId)}/evidence/observations/${encodeURIComponent(candidate.quantity_proposal_observation_id)}/review`}
                    >
                      Количество:{" "}
                      {candidate.quantity_proposal_task_status ?? "неизвестно"}
                    </Link>
                  ) : (
                    <button
                      className="text-button"
                      type="button"
                      disabled={!candidate.quantity_proposal_allowed}
                      onClick={() =>
                        selectQuantityRow(
                          candidate.source_observation.observation_id,
                        )
                      }
                    >
                      Передать количество на проверку
                    </button>
                  )}
                  {!candidate.quantity_proposal_allowed &&
                    candidate.quantity_proposal_blockers.length > 0 && (
                      <small className="danger-text">
                        Количество BLOCKED:{" "}
                        {candidate.quantity_proposal_blockers.join(", ")}
                      </small>
                    )}
                </span>
              </article>
            ))}
          </div>
        )}
        {spreadsheet.candidates_truncated && (
          <p className="inline-warning">
            Показаны первые 100 строк. Для полной проверки разделите пакет.
          </p>
        )}

        {selectedSpreadsheetRow !== undefined && (
          <form className="entry-form__section" onSubmit={submitMapping}>
            <div className="entry-form__intro">
              <p className="eyebrow">
                Позиция {selectedSpreadsheetRow.source_position_id}
              </p>
              <h2>Предложение специалиста</h2>
            </div>
            <div className="form-grid">
              <label className="field">
                <span>Код работы</span>
                <input
                  maxLength={200}
                  value={mappingDraft.workCode}
                  onChange={(event) =>
                    changeMapping({ workCode: event.target.value })
                  }
                />
              </label>
              <label className="field">
                <span>Каноническая единица</span>
                <input
                  maxLength={100}
                  value={mappingDraft.unit}
                  onChange={(event) =>
                    changeMapping({ unit: event.target.value })
                  }
                />
              </label>
              <label className="field form-grid__wide">
                <span>Каноническое описание</span>
                <textarea
                  rows={3}
                  maxLength={2000}
                  value={mappingDraft.description}
                  onChange={(event) =>
                    changeMapping({ description: event.target.value })
                  }
                />
              </label>
              <label className="field form-grid__wide">
                <span>Почему сопоставление корректно</span>
                <textarea
                  rows={3}
                  maxLength={2000}
                  value={mappingDraft.reason}
                  onChange={(event) =>
                    changeMapping({ reason: event.target.value })
                  }
                />
              </label>
              <label className="field">
                <span>Точный код проекта</span>
                <input
                  value={mappingDraft.projectCodeConfirmation}
                  onChange={(event) =>
                    changeMapping({
                      projectCodeConfirmation: event.target.value,
                    })
                  }
                  autoComplete="off"
                />
              </label>
              <label className="attestation form-grid__wide">
                <input
                  type="checkbox"
                  checked={mappingDraft.acknowledged}
                  onChange={(event) =>
                    changeMapping({ acknowledged: event.target.checked })
                  }
                />
                <span>
                  Подтверждаю, что сопоставляю только работу, описание и
                  единицу; количество и цена требуют отдельных доказательств.
                </span>
              </label>
            </div>
            {(mappingFormError !== null || mappingMutation.isError) && (
              <div className="inline-error" role="alert">
                {mappingFormError ??
                  (mappingMutation.error instanceof Error
                    ? mappingMutation.error.message
                    : "Сопоставление не создано.")}
              </div>
            )}
            <div className="form-actions">
              <button
                className="button button--secondary"
                type="button"
                onClick={() => setMappingDraft(emptyMappingDraft)}
              >
                Отмена
              </button>
              <button
                className="button button--primary"
                type="submit"
                disabled={
                  mappingValidation !== null || mappingMutation.isPending
                }
              >
                <Icon name="shield" size={16} />
                {mappingMutation.isPending
                  ? "Фиксация…"
                  : "Передать на независимую проверку"}
              </button>
            </div>
          </form>
        )}
        {selectedQuantityRow !== undefined && (
          <form className="entry-form__section" onSubmit={submitQuantity}>
            <div className="entry-form__intro">
              <p className="eyebrow">
                Позиция {selectedQuantityRow.source_position_id} · количество
              </p>
              <h2>
                {selectedQuantityRow.quantity} {selectedQuantityRow.unit}
              </h2>
              <p>
                Значение загружено из исходной ячейки и на этом экране не
                редактируется.
              </p>
            </div>
            <div className="form-grid">
              <label className="field form-grid__wide">
                <span>Что проверяется в исходной ведомости</span>
                <textarea
                  rows={3}
                  maxLength={2000}
                  value={quantityDraft.reason}
                  onChange={(event) =>
                    changeQuantity({ reason: event.target.value })
                  }
                />
              </label>
              <label className="field">
                <span>Точный код проекта</span>
                <input
                  value={quantityDraft.projectCodeConfirmation}
                  onChange={(event) =>
                    changeQuantity({
                      projectCodeConfirmation: event.target.value,
                    })
                  }
                  autoComplete="off"
                />
              </label>
              <label className="attestation form-grid__wide">
                <input
                  type="checkbox"
                  checked={quantityDraft.acknowledged}
                  onChange={(event) =>
                    changeQuantity({ acknowledged: event.target.checked })
                  }
                />
                <span>
                  Подтверждаю, что сверяю точную исходную ячейку, единицу и
                  контекст позиции; число не вводилось и не изменялось вручную.
                </span>
              </label>
            </div>
            {(quantityFormError !== null || quantityMutation.isError) && (
              <div className="inline-error" role="alert">
                {quantityFormError ??
                  (quantityMutation.error instanceof Error
                    ? quantityMutation.error.message
                    : "Проверка количества не создана.")}
              </div>
            )}
            <div className="form-actions">
              <button
                className="button button--secondary"
                type="button"
                onClick={() => setQuantityDraft(emptyQuantityDraft)}
              >
                Отмена
              </button>
              <button
                className="button button--primary"
                type="submit"
                disabled={
                  quantityValidation !== null || quantityMutation.isPending
                }
              >
                <Icon name="shield" size={16} />
                {quantityMutation.isPending
                  ? "Фиксация…"
                  : "Передать точное количество на проверку"}
              </button>
            </div>
          </form>
        )}
      </section>

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
                    description:
                      event.target.checked &&
                      typeof candidate.description === "string"
                        ? candidate.description
                        : draft.description,
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
