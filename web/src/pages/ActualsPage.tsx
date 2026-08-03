import {
  useMemo,
  useRef,
  useState,
  type Dispatch,
  type FormEvent,
  type SetStateAction,
} from "react";
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import {
  compareActualToForecast,
  decideActual,
  decideCalibrationExample,
  decideVariance,
  getActualsContext,
  getProject,
  listActualForecastCandidates,
  newIdempotencyKey,
  recordActual,
  type RequestContext,
} from "../api";
import {
  validateActualComparison,
  validateActualDecision,
  validateActualSubmission,
  validateCalibrationDecision,
  validateVarianceDecision,
  varianceReasons,
  mergeActualsContextPages,
  mergeForecastCandidatePages,
  type ActualsAttestation,
  type OperationIdentity,
  resolveOperationIdentity,
} from "../actualsWorkflow";
import { useAuth } from "../auth";
import { ErrorBlock, LoadingBlock } from "../components/Feedback";
import { Icon } from "../components/Icon";
import { StatusPill } from "../components/StatusPill";
import { compactId, formatDateTime, formatDecimal } from "../format";
import { roleLabels } from "../labels";
import { Link } from "../navigation";
import type {
  ActorRole,
  ActualReviewView,
  CalibrationExampleView,
  RuntimeConfig,
  VarianceReason,
  VarianceView,
} from "../types";

const emptyAttestation: ActualsAttestation = {
  reason: "",
  projectCodeConfirmation: "",
  acknowledged: false,
};

const varianceReasonLabels: Record<VarianceReason, string> = {
  SCOPE_CHANGE: "Изменение объёма работ",
  QUANTITY_ERROR: "Ошибка количества",
  PRICE_CHANGE: "Изменение цены",
  SUPPLIER_CHANGE: "Смена поставщика",
  PRODUCTIVITY_VARIANCE: "Отклонение производительности",
  LOGISTICS_VARIANCE: "Логистическое отклонение",
  SCHEDULE_VARIANCE: "Отклонение графика",
  RISK_REALISED: "Реализованный риск",
  DATA_QUALITY: "Качество данных",
  METHODOLOGY_ERROR: "Ошибка методологии",
  OTHER_APPROVED: "Иная утверждённая причина",
};

const blockerLabels: Record<string, string> = {
  TASK_MISSING: "Связанная задача отсутствует",
  TASK_NOT_PENDING: "Задача уже завершена",
  TASK_INTEGRITY_FAILED: "Нарушена целостность задачи",
  FOUR_EYES_ACTUAL_AUTHOR: "Автор факта не может проверить его",
  FOUR_EYES_VARIANCE_CLASSIFIER:
    "Классификатор отклонения не может проверить его",
  FOUR_EYES_TASK_CREATOR: "Создатель задачи не может принять решение",
  ACTUAL_REVIEW_ROLE_REQUIRED: "Требуется назначенная роль проверки факта",
  VARIANCE_REVIEW_ROLE_REQUIRED:
    "Требуется назначенная роль проверки отклонения",
  METHODOLOGY_OWNER_REQUIRED: "Требуется владелец методологии",
  CALIBRATION_FOUR_EYES_REQUIRED:
    "Участник исходной цепочки не может утвердить калибровку",
  ACTUAL_INTEGRITY_FAILED: "Факт не воспроизводится из доказательств",
  VARIANCE_INTEGRITY_FAILED:
    "Отклонение не воспроизводится из факта и прогноза",
  CALIBRATION_INTEGRITY_FAILED: "Калибровочный пример не воспроизводится",
  ACTUAL_NOT_IN_REVIEW: "Факт уже не находится на проверке",
  VARIANCE_NOT_IN_REVIEW: "Отклонение уже не находится на проверке",
  CALIBRATION_NOT_IN_REVIEW:
    "Калибровочный пример уже не находится на проверке",
};

function blockerLabel(value: string): string {
  return blockerLabels[value] ?? value;
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : "Операция не выполнена.";
}

function hasAnyRole(actorRoles: ActorRole[], requiredRoles: ActorRole[]) {
  return requiredRoles.some((role) => actorRoles.includes(role));
}

function stableOperationKey(
  reference: { current: OperationIdentity | null },
  payload: object,
): string {
  const identity = resolveOperationIdentity(
    reference.current,
    payload,
    newIdempotencyKey,
  );
  reference.current = identity;
  return identity.key;
}

function AttestationFields({
  value,
  setValue,
  projectCode,
  acknowledgement,
}: {
  value: ActualsAttestation;
  setValue: Dispatch<SetStateAction<ActualsAttestation>>;
  projectCode: string;
  acknowledgement: string;
}) {
  return (
    <div className="form-grid">
      <label className="field form-grid__wide">
        <span>Основание действия</span>
        <textarea
          rows={4}
          maxLength={2000}
          value={value.reason}
          onChange={(event) =>
            setValue((current) => ({
              ...current,
              reason: event.target.value,
              acknowledged: false,
            }))
          }
        />
      </label>
      <label className="field">
        <span>Точный шифр проекта: {projectCode}</span>
        <input
          value={value.projectCodeConfirmation}
          autoComplete="off"
          onChange={(event) =>
            setValue((current) => ({
              ...current,
              projectCodeConfirmation: event.target.value,
              acknowledged: false,
            }))
          }
        />
      </label>
      <label className="attestation form-grid__wide">
        <input
          type="checkbox"
          checked={value.acknowledged}
          onChange={(event) =>
            setValue((current) => ({
              ...current,
              acknowledged: event.target.checked,
            }))
          }
        />
        <span>{acknowledgement}</span>
      </label>
    </div>
  );
}

function DecisionChoice({
  value,
  onChange,
}: {
  value: "APPROVED" | "REJECTED";
  onChange: (value: "APPROVED" | "REJECTED") => void;
}) {
  return (
    <label className="field">
      <span>Решение</span>
      <select
        value={value}
        onChange={(event) =>
          onChange(event.target.value as "APPROVED" | "REJECTED")
        }
      >
        <option value="APPROVED">Подтвердить</option>
        <option value="REJECTED">Отклонить</option>
      </select>
      <small>
        Исправление выполняется новой superseding-записью, а не редактированием
        истории.
      </small>
    </label>
  );
}

function BlockerList({ blockers }: { blockers: string[] }) {
  if (blockers.length === 0) {
    return null;
  }
  return (
    <ul className="actuals-blocker-list">
      {blockers.map((blocker) => (
        <li key={blocker}>{blockerLabel(blocker)}</li>
      ))}
    </ul>
  );
}

export function ActualsPage({
  config,
  projectId,
}: {
  config: RuntimeConfig;
  projectId: string;
}) {
  const auth = useAuth();
  const queryClient = useQueryClient();
  const [metric, setMetric] = useState<string>();
  const [sourceObservationId, setSourceObservationId] = useState("");
  const [recordAttestation, setRecordAttestation] =
    useState<ActualsAttestation>(emptyAttestation);
  const [recordError, setRecordError] = useState<string | null>(null);

  const [actualReviewId, setActualReviewId] = useState<string | null>(null);
  const [actualDecision, setActualDecision] = useState<"APPROVED" | "REJECTED">(
    "APPROVED",
  );
  const [actualAttestation, setActualAttestation] =
    useState<ActualsAttestation>(emptyAttestation);
  const [actualError, setActualError] = useState<string | null>(null);

  const [comparisonActualId, setComparisonActualId] = useState<string | null>(
    null,
  );
  const [forecastId, setForecastId] = useState("");
  const [varianceReason, setVarianceReason] = useState<VarianceReason | "">("");
  const [varianceReasonDetail, setVarianceReasonDetail] = useState("");
  const [comparisonAttestation, setComparisonAttestation] =
    useState<ActualsAttestation>(emptyAttestation);
  const [comparisonError, setComparisonError] = useState<string | null>(null);

  const [varianceReviewId, setVarianceReviewId] = useState<string | null>(null);
  const [varianceDecision, setVarianceDecision] = useState<
    "APPROVED" | "REJECTED"
  >("APPROVED");
  const [varianceAttestation, setVarianceAttestation] =
    useState<ActualsAttestation>(emptyAttestation);
  const [varianceError, setVarianceError] = useState<string | null>(null);

  const [calibrationReviewId, setCalibrationReviewId] = useState<string | null>(
    null,
  );
  const [calibrationDecision, setCalibrationDecision] = useState<
    "APPROVED" | "REJECTED"
  >("APPROVED");
  const [calibrationAttestation, setCalibrationAttestation] =
    useState<ActualsAttestation>(emptyAttestation);
  const [calibrationError, setCalibrationError] = useState<string | null>(null);
  const recordOperation = useRef<OperationIdentity | null>(null);
  const actualDecisionOperation = useRef<OperationIdentity | null>(null);
  const comparisonOperation = useRef<OperationIdentity | null>(null);
  const varianceDecisionOperation = useRef<OperationIdentity | null>(null);
  const calibrationDecisionOperation = useRef<OperationIdentity | null>(null);

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
  const actualsQuery = useInfiniteQuery({
    queryKey: ["actuals-context", projectId, metric ?? null],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam, signal }) =>
      getActualsContext(
        context,
        projectId,
        {
          ...(metric === undefined ? {} : { metric }),
          ...(pageParam === undefined ? {} : { cursor: pageParam }),
          limit: 20,
        },
        signal,
      ),
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
  });

  const actuals = useMemo(
    () => mergeActualsContextPages(actualsQuery.data?.pages ?? []),
    [actualsQuery.data?.pages],
  );
  const selectedActualReview =
    actuals?.records.find(
      (item) => item.record.actual.actual_id === actualReviewId,
    ) ?? null;
  const comparisonReview =
    actuals?.records.find(
      (item) => item.record.actual.actual_id === comparisonActualId,
    ) ?? null;
  const selectedVariance =
    actuals?.variances.find(
      (item) => item.variance_record_id === varianceReviewId,
    ) ?? null;
  const selectedCalibration =
    actuals?.calibration_examples.find(
      (item) => item.example.example_id === calibrationReviewId,
    ) ?? null;
  const forecastQuery = useInfiniteQuery({
    queryKey: ["actual-forecasts", projectId, comparisonActualId],
    initialPageParam: undefined as string | undefined,
    enabled: comparisonActualId !== null,
    queryFn: ({ pageParam, signal }) => {
      if (comparisonActualId === null) {
        throw new Error("Факт для forecast replay не выбран.");
      }
      return listActualForecastCandidates(
        context,
        {
          projectId,
          actualId: comparisonActualId,
          ...(pageParam === undefined ? {} : { cursor: pageParam }),
          limit: 10,
        },
        signal,
      );
    },
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
  });
  const comparisonForecasts = useMemo(
    () =>
      mergeForecastCandidatePages(
        (forecastQuery.data?.pages ?? []).map((page) => page.items),
      ),
    [forecastQuery.data?.pages],
  );

  const invalidate = async () => {
    await Promise.all([
      queryClient.invalidateQueries({
        queryKey: ["actuals-context", projectId],
      }),
      queryClient.invalidateQueries({
        queryKey: ["actual-forecasts", projectId],
      }),
      queryClient.invalidateQueries({
        queryKey: ["records", projectId, "ACTUALS"],
      }),
      queryClient.invalidateQueries({ queryKey: ["work-items"] }),
      queryClient.invalidateQueries({ queryKey: ["workbench", projectId] }),
    ]);
  };

  const recordMutation = useMutation({
    mutationFn: async () => {
      const project = projectQuery.data;
      const current = actuals;
      if (project === undefined || current === undefined) {
        throw new Error("Контекст факта не загружен.");
      }
      const guard = validateActualSubmission(
        { ...recordAttestation, sourceObservationId },
        current,
        project.code,
      );
      if (guard !== null) {
        throw new Error(guard);
      }
      const candidate = current.evidence_candidates.find(
        (item) =>
          item.observation.observation_id === sourceObservationId &&
          item.eligible &&
          item.evidence_value !== null,
      );
      if (candidate === undefined) {
        throw new Error("Выбранное наблюдение больше недоступно.");
      }
      return recordActual(context, {
        projectId,
        metric: current.selected_metric,
        sourceObservationId,
        expectedObservationCreatedAt: candidate.observation_created_at,
        actualsPolicyVersionId: current.policy_version_id,
        reason: recordAttestation.reason.trim(),
        idempotencyKey: stableOperationKey(recordOperation, {
          projectId,
          metric: current.selected_metric,
          sourceObservationId,
          expectedObservationCreatedAt: candidate.observation_created_at,
          actualsPolicyVersionId: current.policy_version_id,
          reason: recordAttestation.reason.trim(),
        }),
      });
    },
    onSuccess: async () => {
      recordOperation.current = null;
      setSourceObservationId("");
      setRecordAttestation(emptyAttestation);
      setRecordError(null);
      await invalidate();
    },
    onError: (error) => setRecordError(message(error)),
  });

  const actualDecisionMutation = useMutation({
    mutationFn: async () => {
      const project = projectQuery.data;
      if (project === undefined || selectedActualReview === null) {
        throw new Error("Выберите актуальную проверку факта.");
      }
      const guard = validateActualDecision(
        actualAttestation,
        selectedActualReview,
        project.code,
      );
      if (guard !== null) {
        throw new Error(guard);
      }
      return decideActual(context, {
        projectId,
        actualId: selectedActualReview.record.actual.actual_id,
        decision: actualDecision,
        expectedActualCreatedAt: selectedActualReview.record.created_at,
        expectedTaskUpdatedAt: selectedActualReview.record.task_updated_at,
        reason: actualAttestation.reason.trim(),
        idempotencyKey: stableOperationKey(actualDecisionOperation, {
          projectId,
          actualId: selectedActualReview.record.actual.actual_id,
          decision: actualDecision,
          expectedActualCreatedAt: selectedActualReview.record.created_at,
          expectedTaskUpdatedAt: selectedActualReview.record.task_updated_at,
          reason: actualAttestation.reason.trim(),
        }),
      });
    },
    onSuccess: async () => {
      actualDecisionOperation.current = null;
      setActualReviewId(null);
      setActualAttestation(emptyAttestation);
      setActualError(null);
      await invalidate();
    },
    onError: (error) => setActualError(message(error)),
  });

  const comparisonMutation = useMutation({
    mutationFn: async () => {
      const project = projectQuery.data;
      const current = actuals;
      if (
        project === undefined ||
        current === undefined ||
        comparisonReview === null
      ) {
        throw new Error("Выберите проверенный факт для сравнения.");
      }
      const guard = validateActualComparison(
        {
          ...comparisonAttestation,
          actualId: comparisonReview.record.actual.actual_id,
          forecastId,
          varianceReason,
          varianceReasonDetail,
        },
        current,
        comparisonForecasts,
        project.code,
      );
      if (guard !== null) {
        throw new Error(guard);
      }
      const selectedForecast = comparisonForecasts.find(
        (item) =>
          item.actual_id === comparisonReview.record.actual.actual_id &&
          item.forecast.forecast_id === forecastId,
      );
      if (selectedForecast === undefined) {
        throw new Error("Выпущенный прогноз больше недоступен.");
      }
      return compareActualToForecast(context, {
        projectId,
        actualId: comparisonReview.record.actual.actual_id,
        forecastId,
        releasedByDecisionId: selectedForecast.released_by_decision_id,
        varianceReason: varianceReason as VarianceReason,
        varianceReasonDetail: varianceReasonDetail.trim(),
        expectedActualCreatedAt: comparisonReview.record.created_at,
        actualsPolicyVersionId: current.policy_version_id,
        reason: comparisonAttestation.reason.trim(),
        idempotencyKey: stableOperationKey(comparisonOperation, {
          projectId,
          actualId: comparisonReview.record.actual.actual_id,
          forecastId,
          releasedByDecisionId: selectedForecast.released_by_decision_id,
          varianceReason,
          varianceReasonDetail: varianceReasonDetail.trim(),
          expectedActualCreatedAt: comparisonReview.record.created_at,
          actualsPolicyVersionId: current.policy_version_id,
          reason: comparisonAttestation.reason.trim(),
        }),
      });
    },
    onSuccess: async () => {
      comparisonOperation.current = null;
      setComparisonActualId(null);
      setForecastId("");
      setVarianceReason("");
      setVarianceReasonDetail("");
      setComparisonAttestation(emptyAttestation);
      setComparisonError(null);
      await invalidate();
    },
    onError: (error) => setComparisonError(message(error)),
  });

  const varianceDecisionMutation = useMutation({
    mutationFn: async () => {
      const project = projectQuery.data;
      if (project === undefined || selectedVariance === null) {
        throw new Error("Выберите отклонение для независимой проверки.");
      }
      const guard = validateVarianceDecision(
        varianceAttestation,
        selectedVariance,
        project.code,
      );
      if (guard !== null) {
        throw new Error(guard);
      }
      return decideVariance(context, {
        projectId,
        varianceId: selectedVariance.variance_record_id,
        decision: varianceDecision,
        expectedVarianceCreatedAt: selectedVariance.created_at,
        expectedTaskUpdatedAt: selectedVariance.task_updated_at,
        reason: varianceAttestation.reason.trim(),
        idempotencyKey: stableOperationKey(varianceDecisionOperation, {
          projectId,
          varianceId: selectedVariance.variance_record_id,
          decision: varianceDecision,
          expectedVarianceCreatedAt: selectedVariance.created_at,
          expectedTaskUpdatedAt: selectedVariance.task_updated_at,
          reason: varianceAttestation.reason.trim(),
        }),
      });
    },
    onSuccess: async () => {
      varianceDecisionOperation.current = null;
      setVarianceReviewId(null);
      setVarianceAttestation(emptyAttestation);
      setVarianceError(null);
      await invalidate();
    },
    onError: (error) => setVarianceError(message(error)),
  });

  const calibrationDecisionMutation = useMutation({
    mutationFn: async () => {
      const project = projectQuery.data;
      if (project === undefined || selectedCalibration === null) {
        throw new Error("Выберите калибровочный пример.");
      }
      const guard = validateCalibrationDecision(
        calibrationAttestation,
        selectedCalibration,
        project.code,
      );
      if (guard !== null) {
        throw new Error(guard);
      }
      return decideCalibrationExample(context, {
        projectId,
        exampleId: selectedCalibration.example.example_id,
        decision: calibrationDecision,
        expectedExampleCreatedAt: selectedCalibration.created_at,
        expectedTaskUpdatedAt: selectedCalibration.task_updated_at,
        reason: calibrationAttestation.reason.trim(),
        idempotencyKey: stableOperationKey(calibrationDecisionOperation, {
          projectId,
          exampleId: selectedCalibration.example.example_id,
          decision: calibrationDecision,
          expectedExampleCreatedAt: selectedCalibration.created_at,
          expectedTaskUpdatedAt: selectedCalibration.task_updated_at,
          reason: calibrationAttestation.reason.trim(),
        }),
      });
    },
    onSuccess: async () => {
      calibrationDecisionOperation.current = null;
      setCalibrationReviewId(null);
      setCalibrationAttestation(emptyAttestation);
      setCalibrationError(null);
      await invalidate();
    },
    onError: (error) => setCalibrationError(message(error)),
  });

  if (projectQuery.isError || actualsQuery.isError) {
    const failed = projectQuery.isError ? projectQuery : actualsQuery;
    return (
      <div className="page">
        <ErrorBlock
          error={failed.error}
          onRetry={() => void failed.refetch()}
        />
      </div>
    );
  }
  if (projectQuery.data === undefined || actuals === undefined) {
    return (
      <div className="page">
        <LoadingBlock label="Загрузка контура факта и калибровки" />
      </div>
    );
  }

  const project = projectQuery.data;
  const canRecord = hasAnyRole(auth.roles, actuals.record_roles);
  const canClassify = hasAnyRole(auth.roles, actuals.variance_classifier_roles);
  const currentFacts = actuals.records.filter((item) => item.record.is_current);
  const verifiedFacts = currentFacts.filter(
    (item) => item.record.actual.verified,
  );
  const verifiedMetricKeys = new Set(
    verifiedFacts.map((item) => item.record.actual.metric),
  );
  const missingRequiredMetrics = actuals.required_metric_keys.filter(
    (key) => !verifiedMetricKeys.has(key),
  );
  const pendingCalibrations = actuals.calibration_examples.filter(
    (item) => item.task_status === "PENDING",
  );
  const activeMetric =
    actuals.metric_definitions.find(
      (definition) => definition.metric === actuals.selected_metric,
    ) ?? actuals.metric_definitions[0];

  const submitRecord = (event: FormEvent) => {
    event.preventDefault();
    setRecordError(null);
    recordMutation.mutate();
  };
  const submitActualDecision = (event: FormEvent) => {
    event.preventDefault();
    setActualError(null);
    actualDecisionMutation.mutate();
  };
  const submitComparison = (event: FormEvent) => {
    event.preventDefault();
    setComparisonError(null);
    comparisonMutation.mutate();
  };
  const submitVarianceDecision = (event: FormEvent) => {
    event.preventDefault();
    setVarianceError(null);
    varianceDecisionMutation.mutate();
  };
  const submitCalibrationDecision = (event: FormEvent) => {
    event.preventDefault();
    setCalibrationError(null);
    calibrationDecisionMutation.mutate();
  };

  return (
    <div className="page controlled-workflow-page actuals-page">
      <nav className="breadcrumbs" aria-label="Навигационная цепочка">
        <Link to="/">Проекты</Link>
        <span>/</span>
        <Link to={`/projects/${encodeURIComponent(projectId)}`}>
          {project.code}
        </Link>
        <span>/</span>
        <Link to={`/projects/${encodeURIComponent(projectId)}/ACTUALS`}>
          Факт и калибровка
        </Link>
        <span>/</span>
        <span>Управляемый контур</span>
      </nav>

      <header className="entry-header actuals-hero">
        <div>
          <p className="eyebrow">Контур 08 · после выпуска расчёта</p>
          <h1>Факт → отклонение → калибровка</h1>
          <p>
            Финансовые значения здесь не вводятся. Факт выбирается из
            проверенного наблюдения, отклонение пересчитывается сервером по
            выпущенному snapshot, а калибровочный пример требует отдельного
            решения владельца методологии.
          </p>
        </div>
        <div className="actuals-policy-seal" aria-label="Утверждённая политика">
          <span>POLICY LOCK</span>
          <strong>{compactId(actuals.policy_version_id)}</strong>
          <small>{compactId(actuals.policy_content_hash)}</small>
        </div>
      </header>

      <section
        className="actuals-flow"
        aria-label="Последовательность контроля"
      >
        <article>
          <span>01</span>
          <strong>Доказанный факт</strong>
          <small>{roleLabels[actuals.actual_review_role]} · four-eyes</small>
        </article>
        <Icon name="arrow" size={18} />
        <article>
          <span>02</span>
          <strong>Причина отклонения</strong>
          <small>{roleLabels[actuals.variance_review_role]} · replay</small>
        </article>
        <Icon name="arrow" size={18} />
        <article>
          <span>03</span>
          <strong>Учебный пример</strong>
          <small>
            {roleLabels[actuals.calibration_approval_role]} · approval
          </small>
        </article>
      </section>

      <section className="passport-overview" aria-label="Состояние контура">
        <article>
          <span>Текущих фактов</span>
          <strong>{currentFacts.length}</strong>
          <small>{actuals.metric_definitions.length} метрик в политике</small>
        </article>
        <article
          className={
            missingRequiredMetrics.length === 0 ? "is-ok" : "is-danger"
          }
        >
          <span>Проверено обязательных</span>
          <strong>
            {actuals.required_metric_keys.length -
              missingRequiredMetrics.length}
          </strong>
          <small>из {actuals.required_metric_keys.length}</small>
        </article>
        <article
          className={pendingCalibrations.length === 0 ? "is-ok" : "is-danger"}
        >
          <span>Калибровка ожидает</span>
          <strong>{pendingCalibrations.length}</strong>
          <small>решений владельца методологии</small>
        </article>
      </section>

      {missingRequiredMetrics.length > 0 && (
        <section className="blocker-panel" role="alert">
          <Icon name="warning" size={20} />
          <div>
            <strong>Обязательный проверенный факт неполон</strong>
            <p>
              Отсутствуют метрики: {missingRequiredMetrics.join(", ")}. Это не
              разрешает считать отсутствие записи нулевым фактом.
            </p>
          </div>
        </section>
      )}

      <section className="actuals-metric-rail" aria-label="Метрики политики">
        <div>
          <p className="eyebrow">Метрики утверждённой политики</p>
          <strong>Выберите доказательный поток</strong>
        </div>
        <div className="actuals-metric-rail__items">
          {actuals.metric_definitions.map((definition) => {
            const verified = verifiedMetricKeys.has(definition.metric);
            return (
              <button
                key={definition.metric}
                className={
                  definition.metric === actuals.selected_metric
                    ? "is-active"
                    : ""
                }
                type="button"
                onClick={() => {
                  setMetric(definition.metric);
                  setSourceObservationId("");
                  setRecordError(null);
                }}
              >
                <span>{definition.metric}</span>
                <small>{definition.forecast_basis}</small>
                <StatusPill value={verified ? "VERIFIED" : "MISSING"} compact />
              </button>
            );
          })}
        </div>
      </section>

      <form className="entry-form controlled-form" onSubmit={submitRecord}>
        <section className="entry-form__intro">
          <p className="eyebrow">01 · зафиксировать факт без ручной суммы</p>
          <h2>{actuals.selected_metric}</h2>
          <p>
            Поле источника: {activeMetric?.evidence_field_name ?? "—"} ·
            сущность {activeMetric?.entity_type ?? "—"} · допустимые единицы{" "}
            {activeMetric?.allowed_units.join(", ") || "не объявлены"}.
          </p>
        </section>
        <div className="actuals-role-line">
          <span>Регистрация</span>
          <strong>
            {actuals.record_roles.map((role) => roleLabels[role]).join(" · ")}
          </strong>
          <StatusPill value={canRecord ? "ROLE_ALLOWED" : "BLOCKED"} compact />
        </div>
        <div className="evidence-choice-grid">
          {actuals.evidence_candidates.map((candidate) => {
            const value = candidate.evidence_value;
            return (
              <label
                className={`evidence-choice ${!candidate.eligible ? "is-blocked" : ""}`}
                key={candidate.observation.observation_id}
              >
                <input
                  type="radio"
                  name="actual-source"
                  checked={
                    sourceObservationId === candidate.observation.observation_id
                  }
                  disabled={!candidate.eligible}
                  onChange={() => {
                    setSourceObservationId(
                      candidate.observation.observation_id,
                    );
                    setRecordError(null);
                  }}
                />
                <span className="evidence-choice__body">
                  <span className="evidence-choice__heading">
                    <strong>
                      {value?.actual_key ?? "Некорректный payload"}
                    </strong>
                    <StatusPill value={candidate.observation.status} compact />
                  </span>
                  <span className="actuals-value">
                    {value === null
                      ? "—"
                      : `${formatDecimal(value.value)} ${value.unit}`}
                  </span>
                  <small>
                    {value?.source_class ?? "Источник не классифицирован"} ·{" "}
                    {value?.occurred_on ?? "дата отсутствует"}
                  </small>
                  <small>
                    Наблюдение {compactId(candidate.observation.observation_id)}{" "}
                    · версия {formatDateTime(candidate.observation_created_at)}
                  </small>
                  <BlockerList blockers={candidate.blockers} />
                </span>
              </label>
            );
          })}
        </div>
        {actuals.evidence_candidates.length === 0 && (
          <div className="empty-state">
            <Icon name="trace" size={22} />
            <strong>Нет наблюдений для выбранной метрики</strong>
            <p>
              Требуется квалифицированный фактический источник. Пустой список не
              является нулевым значением.
            </p>
          </div>
        )}
        {actuals.candidates_truncated && (
          <p className="inline-warning">
            Показаны первые 100 кандидатов. Операция по неполной выборке требует
            уточнения источника и не должна выполняться автоматически.
          </p>
        )}
        <AttestationFields
          value={recordAttestation}
          setValue={setRecordAttestation}
          projectCode={project.code}
          acknowledgement="Подтверждаю выбор неизменённого серверного наблюдения; значение, единица, дата и класс источника вручную не вводились."
        />
        {recordError !== null && (
          <div className="inline-error" role="alert">
            {recordError}
          </div>
        )}
        <div className="form-actions">
          <button
            className="button button--primary"
            type="submit"
            disabled={recordMutation.isPending || !canRecord}
          >
            <Icon name="plus" size={16} />
            {recordMutation.isPending ? "Фиксация…" : "Создать ревизию факта"}
          </button>
        </div>
      </form>

      <section className="review-ledger actuals-ledger">
        <div className="review-ledger__heading">
          <div>
            <p className="eyebrow">02 · неизменяемый журнал фактов</p>
            <h2>Проверка и привязка к выпущенному прогнозу</h2>
          </div>
          <StatusPill value={actuals.project_state} />
        </div>
        <div className="actuals-ledger-grid">
          {actuals.records.map((review) => {
            const record = review.record;
            const fact = record.actual;
            const hasVariance = review.has_classified_variance;
            return (
              <article
                className={`actuals-ledger-card ${record.is_current ? "" : "is-superseded"}`}
                key={fact.actual_id}
              >
                <header>
                  <div>
                    <span className="eyebrow">{fact.metric}</span>
                    <h3>{record.actual_key}</h3>
                  </div>
                  <div className="passport-fact-card__status">
                    <StatusPill value={fact.status} compact />
                    <StatusPill value={record.task_status} compact />
                  </div>
                </header>
                <div className="actuals-ledger-card__value">
                  <strong>{formatDecimal(fact.value)}</strong>
                  <span>{fact.unit}</span>
                </div>
                <dl>
                  <div>
                    <dt>Дата факта</dt>
                    <dd>{fact.occurred_on}</dd>
                  </div>
                  <div>
                    <dt>Источник</dt>
                    <dd>{fact.source_class ?? "—"}</dd>
                  </div>
                  <div>
                    <dt>Автор</dt>
                    <dd>{record.created_by}</dd>
                  </div>
                  <div>
                    <dt>Leaf evidence</dt>
                    <dd>{record.source_leaf_ids.length}</dd>
                  </div>
                </dl>
                {record.supersedes_actual_id !== null && (
                  <small>
                    Supersede {compactId(record.supersedes_actual_id)}
                  </small>
                )}
                <BlockerList blockers={review.decision_blockers} />
                <div className="actuals-ledger-card__actions">
                  {record.task_status === "PENDING" && (
                    <button
                      className="button button--secondary"
                      type="button"
                      disabled={!review.decision_allowed}
                      onClick={() => {
                        setActualReviewId(fact.actual_id);
                        setActualAttestation(emptyAttestation);
                        setActualError(null);
                      }}
                    >
                      <Icon name="shield" size={15} />
                      Проверить факт
                    </button>
                  )}
                  {fact.verified && !hasVariance && record.is_current && (
                    <button
                      className="button button--secondary"
                      type="button"
                      disabled={!canClassify}
                      onClick={() => {
                        setComparisonActualId(fact.actual_id);
                        setForecastId("");
                        setVarianceReason("");
                        setVarianceReasonDetail("");
                        setComparisonAttestation(emptyAttestation);
                        setComparisonError(null);
                      }}
                    >
                      <Icon name="trace" size={15} />
                      Сравнить с forecast
                    </button>
                  )}
                </div>
              </article>
            );
          })}
        </div>
        {actuals.records.length === 0 && (
          <div className="empty-state">
            <strong>Фактические записи ещё не созданы</strong>
            <p>Выберите квалифицированное наблюдение выше.</p>
          </div>
        )}
      </section>

      {selectedActualReview !== null && (
        <form
          className="entry-form controlled-form"
          onSubmit={submitActualDecision}
        >
          <section className="entry-form__intro">
            <p className="eyebrow">03 · отдельная проверка факта</p>
            <h2>{selectedActualReview.record.actual_key}</h2>
            <p>
              Автор {selectedActualReview.record.created_by} · задача{" "}
              {compactId(selectedActualReview.record.approval_task_id)} · роль{" "}
              {roleLabels[selectedActualReview.assigned_role]}.
            </p>
          </section>
          <div className="source-proof">
            <span>Неизменяемая доказательная основа</span>
            <code>
              {selectedActualReview.record.actual.source_observation_id}
            </code>
            <small>
              Leaf sources:{" "}
              {selectedActualReview.record.source_leaf_ids.join(", ") || "—"}
            </small>
            <small>
              Project outcomes:{" "}
              {selectedActualReview.record.project_outcome_evidence_ids.join(
                ", ",
              ) || "—"}
            </small>
          </div>
          <BlockerList blockers={selectedActualReview.decision_blockers} />
          <DecisionChoice
            value={actualDecision}
            onChange={(value) => {
              setActualDecision(value);
              setActualAttestation((current) => ({
                ...current,
                acknowledged: false,
              }));
            }}
          />
          <AttestationFields
            value={actualAttestation}
            setValue={setActualAttestation}
            projectCode={project.code}
            acknowledgement="Подтверждаю независимую сверку источника, leaf lineage, проектного outcome и утверждённой версии actuals policy."
          />
          {actualError !== null && (
            <div className="inline-error" role="alert">
              {actualError}
            </div>
          )}
          <div className="form-actions">
            <button
              className="button button--secondary"
              type="button"
              onClick={() => setActualReviewId(null)}
            >
              Закрыть
            </button>
            <button
              className="button button--primary"
              type="submit"
              disabled={
                actualDecisionMutation.isPending ||
                !selectedActualReview.decision_allowed
              }
            >
              <Icon name="shield" size={16} />
              {actualDecisionMutation.isPending
                ? "Проверка…"
                : "Зафиксировать решение"}
            </button>
          </div>
        </form>
      )}

      {comparisonReview !== null && (
        <form
          className="entry-form controlled-form"
          onSubmit={submitComparison}
        >
          <section className="entry-form__intro">
            <p className="eyebrow">04 · серверный forecast replay</p>
            <h2>Классифицировать отклонение</h2>
            <p>
              UI отправит только идентификатор выпущенного прогноза и причину.
              Абсолютное и относительное отклонения вычисляются Decimal-логикой
              backend и затем отдельно проверяются.
            </p>
          </section>
          <div className="actuals-comparison">
            <article>
              <span>Проверенный факт</span>
              <strong>
                {formatDecimal(comparisonReview.record.actual.value)}{" "}
                {comparisonReview.record.actual.unit}
              </strong>
              <small>{comparisonReview.record.actual.actual_id}</small>
            </article>
            <Icon name="arrow" size={20} />
            <label className="field">
              <span>Выпущенный прогноз</span>
              <select
                value={forecastId}
                disabled={forecastQuery.isPending || forecastQuery.isError}
                onChange={(event) => {
                  setForecastId(event.target.value);
                  setComparisonAttestation((current) => ({
                    ...current,
                    acknowledged: false,
                  }));
                }}
              >
                <option value="">Выберите snapshot forecast</option>
                {comparisonForecasts.map((candidate) => (
                  <option
                    key={candidate.forecast.forecast_id}
                    value={candidate.forecast.forecast_id}
                  >
                    {formatDecimal(candidate.forecast.value)}{" "}
                    {candidate.forecast.unit} ·{" "}
                    {compactId(candidate.forecast.snapshot_id)}
                  </option>
                ))}
              </select>
            </label>
          </div>
          {forecastQuery.isPending && (
            <LoadingBlock label="Проверка выпущенных forecast snapshots" />
          )}
          {forecastQuery.isError && (
            <div className="inline-error" role="alert">
              {message(forecastQuery.error)}
            </div>
          )}
          {!forecastQuery.isPending &&
            !forecastQuery.isError &&
            comparisonForecasts.length === 0 && (
              <p className="inline-warning">
                Ни один выпущенный forecast не прошёл серверное воспроизведение.
                Классификация отклонения заблокирована.
              </p>
            )}
          {forecastQuery.hasNextPage && (
            <div className="form-actions">
              <button
                className="button button--secondary"
                type="button"
                disabled={forecastQuery.isFetchingNextPage}
                onClick={() => void forecastQuery.fetchNextPage()}
              >
                {forecastQuery.isFetchingNextPage
                  ? "Проверка…"
                  : "Показать более ранние forecasts"}
              </button>
            </div>
          )}
          <div className="form-grid">
            <label className="field">
              <span>Причина по закрытой классификации</span>
              <select
                value={varianceReason}
                onChange={(event) => {
                  setVarianceReason(event.target.value as VarianceReason | "");
                  setComparisonAttestation((current) => ({
                    ...current,
                    acknowledged: false,
                  }));
                }}
              >
                <option value="">Выберите причину</option>
                {varianceReasons.map((reason) => (
                  <option key={reason} value={reason}>
                    {varianceReasonLabels[reason]}
                  </option>
                ))}
              </select>
            </label>
            <label className="field form-grid__wide">
              <span>Объяснение классификации</span>
              <textarea
                rows={4}
                maxLength={4000}
                value={varianceReasonDetail}
                onChange={(event) => {
                  setVarianceReasonDetail(event.target.value);
                  setComparisonAttestation((current) => ({
                    ...current,
                    acknowledged: false,
                  }));
                }}
              />
            </label>
          </div>
          <AttestationFields
            value={comparisonAttestation}
            setValue={setComparisonAttestation}
            projectCode={project.code}
            acknowledgement="Подтверждаю соответствие сущности, метрики и единицы; арифметику не подменял и использую только выпущенный snapshot из серверного контекста."
          />
          {comparisonError !== null && (
            <div className="inline-error" role="alert">
              {comparisonError}
            </div>
          )}
          <div className="form-actions">
            <button
              className="button button--secondary"
              type="button"
              onClick={() => setComparisonActualId(null)}
            >
              Закрыть
            </button>
            <button
              className="button button--primary"
              type="submit"
              disabled={
                comparisonMutation.isPending ||
                !canClassify ||
                forecastId === "" ||
                forecastQuery.isError
              }
            >
              <Icon name="trace" size={16} />
              {comparisonMutation.isPending
                ? "Пересчёт…"
                : "Создать отклонение"}
            </button>
          </div>
        </form>
      )}

      <section className="review-ledger actuals-ledger actuals-ledger--variance">
        <div className="review-ledger__heading">
          <div>
            <p className="eyebrow">05 · реестр отклонений</p>
            <h2>Причина, арифметика и независимое решение</h2>
          </div>
          <span className="actuals-ledger__count">
            {actuals.variances.length}
          </span>
        </div>
        <div className="actuals-ledger-grid">
          {actuals.variances.map((item) => (
            <VarianceCard
              key={item.variance_record_id}
              item={item}
              onReview={() => {
                setVarianceReviewId(item.variance_record_id);
                setVarianceAttestation(emptyAttestation);
                setVarianceError(null);
              }}
            />
          ))}
        </div>
        {actuals.variances.length === 0 && (
          <div className="empty-state">
            <strong>Отклонения ещё не классифицированы</strong>
            <p>
              Сначала требуется текущий проверенный факт и выпущенный forecast.
            </p>
          </div>
        )}
      </section>

      {selectedVariance !== null && (
        <form
          className="entry-form controlled-form"
          onSubmit={submitVarianceDecision}
        >
          <section className="entry-form__intro">
            <p className="eyebrow">06 · four-eyes отклонения</p>
            <h2>{varianceReasonLabels[selectedVariance.variance.reason]}</h2>
            <p>
              Проверяющий {roleLabels[selectedVariance.assigned_role]} должен
              независимо воспроизвести released forecast, факт и Decimal
              arithmetic.
            </p>
          </section>
          <div className="source-proof">
            <span>Классификация</span>
            <strong>{selectedVariance.variance.reason_detail}</strong>
            <small>
              Forecast {selectedVariance.variance.forecast_id} · actual{" "}
              {selectedVariance.variance.actual_id}
            </small>
          </div>
          <BlockerList blockers={selectedVariance.decision_blockers} />
          <DecisionChoice
            value={varianceDecision}
            onChange={(value) => {
              setVarianceDecision(value);
              setVarianceAttestation((current) => ({
                ...current,
                acknowledged: false,
              }));
            }}
          />
          <AttestationFields
            value={varianceAttestation}
            setValue={setVarianceAttestation}
            projectCode={project.code}
            acknowledgement="Подтверждаю независимое воспроизведение прогноза, факта, причины и точной Decimal-арифметики отклонения."
          />
          {varianceError !== null && (
            <div className="inline-error" role="alert">
              {varianceError}
            </div>
          )}
          <div className="form-actions">
            <button
              className="button button--secondary"
              type="button"
              onClick={() => setVarianceReviewId(null)}
            >
              Закрыть
            </button>
            <button
              className="button button--primary"
              type="submit"
              disabled={
                varianceDecisionMutation.isPending ||
                !selectedVariance.decision_allowed
              }
            >
              <Icon name="shield" size={16} />
              {varianceDecisionMutation.isPending
                ? "Проверка…"
                : "Зафиксировать решение"}
            </button>
          </div>
        </form>
      )}

      <section className="review-ledger actuals-ledger actuals-ledger--calibration">
        <div className="review-ledger__heading">
          <div>
            <p className="eyebrow">07 · методологический шлюз</p>
            <h2>Калибровочные примеры не становятся истиной автоматически</h2>
          </div>
          <StatusPill
            value={pendingCalibrations.length === 0 ? "NO_PENDING" : "PENDING"}
          />
        </div>
        <div className="actuals-ledger-grid">
          {actuals.calibration_examples.map((item) => (
            <CalibrationCard
              key={item.example.example_id}
              item={item}
              onReview={() => {
                setCalibrationReviewId(item.example.example_id);
                setCalibrationAttestation(emptyAttestation);
                setCalibrationError(null);
              }}
            />
          ))}
        </div>
        {actuals.calibration_examples.length === 0 && (
          <div className="empty-state">
            <strong>Калибровочных примеров нет</strong>
            <p>Они создаются только после отдельного утверждения отклонения.</p>
          </div>
        )}
      </section>

      {selectedCalibration !== null && (
        <form
          className="entry-form controlled-form actuals-calibration-form"
          onSubmit={submitCalibrationDecision}
        >
          <section className="entry-form__intro">
            <p className="eyebrow">08 · решение владельца методологии</p>
            <h2>{selectedCalibration.example.metric}</h2>
            <p>
              Одобрение разрешает использовать этот пример в управляемом наборе
              калибровки. Оно не доказывает точность модели и не разрешает
              выпуск цены.
            </p>
          </section>
          <div className="actuals-calibration-target">
            <span>Target из проверенного факта</span>
            <strong>
              {formatDecimal(selectedCalibration.example.target_value)}{" "}
              {selectedCalibration.example.unit}
            </strong>
            <small>
              snapshot {selectedCalibration.example.features_snapshot_id}
            </small>
          </div>
          <BlockerList blockers={selectedCalibration.decision_blockers} />
          <DecisionChoice
            value={calibrationDecision}
            onChange={(value) => {
              setCalibrationDecision(value);
              setCalibrationAttestation((current) => ({
                ...current,
                acknowledged: false,
              }));
            }}
          />
          <AttestationFields
            value={calibrationAttestation}
            setValue={setCalibrationAttestation}
            projectCode={project.code}
            acknowledgement="Подтверждаю методологическую применимость примера, независимость от авторов факта/классификации и воспроизводимость всей provenance-цепочки."
          />
          {calibrationError !== null && (
            <div className="inline-error" role="alert">
              {calibrationError}
            </div>
          )}
          <div className="form-actions">
            <button
              className="button button--secondary"
              type="button"
              onClick={() => setCalibrationReviewId(null)}
            >
              Закрыть
            </button>
            <button
              className="button button--primary"
              type="submit"
              disabled={
                calibrationDecisionMutation.isPending ||
                !selectedCalibration.decision_allowed
              }
            >
              <Icon name="shield" size={16} />
              {calibrationDecisionMutation.isPending
                ? "Проверка…"
                : "Зафиксировать решение"}
            </button>
          </div>
        </form>
      )}
      {actualsQuery.hasNextPage && (
        <div className="load-more">
          <button
            className="button button--secondary"
            type="button"
            disabled={actualsQuery.isFetchingNextPage}
            onClick={() => void actualsQuery.fetchNextPage()}
          >
            {actualsQuery.isFetchingNextPage
              ? "Загрузка журналов…"
              : "Показать ещё факты, отклонения и калибровки"}
          </button>
        </div>
      )}
    </div>
  );
}

function VarianceCard({
  item,
  onReview,
}: {
  item: VarianceView;
  onReview: () => void;
}) {
  return (
    <article className="actuals-ledger-card">
      <header>
        <div>
          <span className="eyebrow">{item.variance.reason}</span>
          <h3>{compactId(item.variance_record_id)}</h3>
        </div>
        <div className="passport-fact-card__status">
          <StatusPill value={item.variance.status} compact />
          <StatusPill value={item.task_status} compact />
        </div>
      </header>
      <div className="actuals-variance-values">
        <span>
          <small>Абсолютно</small>
          <strong>{formatDecimal(item.variance.absolute_variance)}</strong>
        </span>
        <span>
          <small>Относительная доля</small>
          <strong>{formatDecimal(item.variance.relative_variance)}</strong>
        </span>
      </div>
      <p>{item.variance.reason_detail}</p>
      <small>
        Классификатор {item.variance.classified_by} · snapshot{" "}
        {compactId(item.forecast.snapshot_id)}
      </small>
      <BlockerList blockers={item.decision_blockers} />
      {item.task_status === "PENDING" && (
        <div className="actuals-ledger-card__actions">
          <button
            className="button button--secondary"
            type="button"
            disabled={!item.decision_allowed}
            onClick={onReview}
          >
            <Icon name="shield" size={15} />
            Проверить отклонение
          </button>
        </div>
      )}
    </article>
  );
}

function CalibrationCard({
  item,
  onReview,
}: {
  item: CalibrationExampleView;
  onReview: () => void;
}) {
  return (
    <article className="actuals-ledger-card actuals-calibration-card">
      <header>
        <div>
          <span className="eyebrow">{item.example.metric}</span>
          <h3>{compactId(item.example.example_id)}</h3>
        </div>
        <div className="passport-fact-card__status">
          <StatusPill
            value={item.approved ? "APPROVED" : item.task_status}
            compact
          />
        </div>
      </header>
      <div className="actuals-ledger-card__value">
        <strong>{formatDecimal(item.example.target_value)}</strong>
        <span>{item.example.unit}</span>
      </div>
      <dl>
        <div>
          <dt>Причина</dt>
          <dd>{varianceReasonLabels[item.example.variance_reason]}</dd>
        </div>
        <div>
          <dt>Features snapshot</dt>
          <dd>{compactId(item.example.features_snapshot_id)}</dd>
        </div>
      </dl>
      <small>
        Approved by {item.approved_by ?? "—"} · policy{" "}
        {compactId(item.policy_version_id)}
      </small>
      <BlockerList blockers={item.decision_blockers} />
      {item.task_status === "PENDING" && (
        <div className="actuals-ledger-card__actions">
          <button
            className="button button--secondary"
            type="button"
            disabled={!item.decision_allowed}
            onClick={onReview}
          >
            <Icon name="shield" size={15} />
            Методологическое решение
          </button>
        </div>
      )}
    </article>
  );
}
