import { useMemo, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  evaluatePriceItem,
  getPriceItemContext,
  getPriceQuoteCandidate,
  getProject,
  newIdempotencyKey,
  normalizePriceQuote,
  recordPriceQuoteFromObservation,
  type RequestContext,
} from "../api";
import { useAuth } from "../auth";
import { ErrorBlock, LoadingBlock } from "../components/Feedback";
import { Icon } from "../components/Icon";
import { StatusPill } from "../components/StatusPill";
import { displayValue, formatDecimal, formatMoney } from "../format";
import { Link } from "../navigation";
import {
  normalizationRequirements,
  validateNormalizationDraft,
  validatePriceEvaluationDraft,
  type ControlledAttestation,
  type PriceEvaluationDraft,
  type PriceNormalizationDraft,
} from "../priceWorkflow";
import type {
  CommercialBasis,
  PriceQuoteSummary,
  RuntimeConfig,
} from "../types";

const emptyAttestation = (): ControlledAttestation => ({
  reason: "",
  projectCode: "",
  acknowledged: false,
});

const emptyNormalization = (): PriceNormalizationDraft => ({
  unitConversionId: "",
  fxRateId: "",
  adjustmentIds: [],
  regionAdjustmentId: "",
  partyAdjustmentId: "",
  paymentAdjustmentId: "",
  ...emptyAttestation(),
});

const emptyEvaluation = (): PriceEvaluationDraft => ({
  asOf: "",
  ...emptyAttestation(),
});

function BasisFacts({
  title,
  basis,
}: {
  title: string;
  basis: CommercialBasis;
}) {
  return (
    <section className="price-basis">
      <span>{title}</span>
      <dl>
        <div>
          <dt>Валюта / НДС</dt>
          <dd>
            {basis.currency} · {basis.vat_basis}
            {basis.vat_rate === null ? "" : ` · ${basis.vat_rate}`}
          </dd>
        </div>
        <div>
          <dt>Единица / упаковка</dt>
          <dd>
            {basis.unit} · {formatDecimal(basis.package_quantity)}
          </dd>
        </div>
        <div>
          <dt>Партия</dt>
          <dd>{formatDecimal(basis.party_quantity)}</dd>
        </div>
        <div>
          <dt>Регион</dt>
          <dd>{basis.region}</dd>
        </div>
        <div>
          <dt>Доставка / разгрузка</dt>
          <dd>
            {basis.delivery_included ? "включена" : "не включена"} ·{" "}
            {basis.unloading_included ? "включена" : "не включена"}
          </dd>
        </div>
        <div>
          <dt>Оплата</dt>
          <dd>{basis.payment_terms}</dd>
        </div>
      </dl>
    </section>
  );
}

function ReferenceSelect({
  label,
  required,
  value,
  options,
  onChange,
}: {
  label: string;
  required: boolean;
  value: string;
  options: Record<string, Record<string, unknown>>;
  onChange: (value: string) => void;
}) {
  return (
    <label className="decision-field">
      <span>
        {label} {required ? "· обязательна" : "· не требуется"}
      </span>
      <select
        value={value}
        required={required}
        disabled={!required}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">Не выбрана</option>
        {Object.keys(options).map((id) => (
          <option key={id} value={id}>
            {id}
          </option>
        ))}
      </select>
    </label>
  );
}

function PriceQuoteCard({
  quote,
  canNormalize,
  onNormalize,
}: {
  quote: PriceQuoteSummary;
  canNormalize: boolean;
  onNormalize: () => void;
}) {
  return (
    <article className="price-quote-card">
      <div className="price-quote-card__header">
        <div>
          <span>{quote.quote.evidence_class.replaceAll("_", " ")}</span>
          <h3>{formatMoney(quote.quote.amount, quote.quote.basis.currency)}</h3>
          <p>
            {quote.quote.supplier_id ?? "Поставщик не указан"} ·{" "}
            {quote.source_origin_id}
          </p>
        </div>
        <StatusPill value={quote.quote.status} compact />
      </div>
      <dl className="price-quote-facts">
        <div>
          <dt>Дата / действует до</dt>
          <dd>
            {quote.quote.quote_date} ·{" "}
            {quote.quote.valid_until ?? "не подтверждено"}
          </dd>
        </div>
        <div>
          <dt>Доступность / срок</dt>
          <dd>
            {quote.quote.available === true
              ? "доступно"
              : quote.quote.available === false
                ? "недоступно"
                : "не подтверждено"}{" "}
            ·{" "}
            {quote.quote.lead_time_days === null
              ? "срок не подтверждён"
              : `${quote.quote.lead_time_days} дн.`}
          </dd>
        </div>
        <div>
          <dt>Исходное наблюдение</dt>
          <dd>{quote.quote.source_observation_id}</dd>
        </div>
      </dl>
      {quote.normalized_prices.map((normalized) => (
        <div className="normalized-price" key={normalized.normalized_price_id}>
          <span>Сопоставимая цена</span>
          <strong>
            {formatMoney(normalized.amount_per_unit, normalized.currency)} /{" "}
            {normalized.unit}
          </strong>
          <code>{normalized.formula_hash}</code>
        </div>
      ))}
      {canNormalize && quote.normalized_prices.length === 0 && (
        <button
          className="button button--secondary"
          type="button"
          onClick={onNormalize}
        >
          <Icon name="settings" size={15} />
          Нормализовать
        </button>
      )}
    </article>
  );
}

export function PriceItemPage({
  config,
  projectId,
  itemId,
}: {
  config: RuntimeConfig;
  projectId: string;
  itemId: string;
}) {
  const auth = useAuth();
  const queryClient = useQueryClient();
  const [candidateId, setCandidateId] = useState("");
  const [recordDraft, setRecordDraft] =
    useState<ControlledAttestation>(emptyAttestation);
  const [recordError, setRecordError] = useState<string | null>(null);
  const [recordKey, setRecordKey] = useState<string | null>(null);
  const [normalizationQuoteId, setNormalizationQuoteId] = useState<
    string | null
  >(null);
  const [normalizationDraft, setNormalizationDraft] =
    useState<PriceNormalizationDraft>(emptyNormalization);
  const [normalizationError, setNormalizationError] = useState<string | null>(
    null,
  );
  const [normalizationKey, setNormalizationKey] = useState<string | null>(null);
  const [evaluationDraft, setEvaluationDraft] =
    useState<PriceEvaluationDraft>(emptyEvaluation);
  const [evaluationError, setEvaluationError] = useState<string | null>(null);
  const [evaluationKey, setEvaluationKey] = useState<string | null>(null);

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
  const priceQuery = useQuery({
    queryKey: ["price-item-context", projectId, itemId],
    queryFn: ({ signal }) =>
      getPriceItemContext(context, projectId, itemId, signal),
  });
  const candidateMutation = useMutation({
    mutationFn: (sourceObservationId: string) =>
      getPriceQuoteCandidate(context, projectId, itemId, sourceObservationId),
  });

  const refresh = async () => {
    await Promise.all([
      queryClient.invalidateQueries({
        queryKey: ["price-item-context", projectId, itemId],
      }),
      queryClient.invalidateQueries({
        queryKey: ["records", projectId, "PRICING"],
      }),
      queryClient.invalidateQueries({ queryKey: ["workbench", projectId] }),
      queryClient.invalidateQueries({ queryKey: ["project", projectId] }),
      queryClient.invalidateQueries({ queryKey: ["work-items"] }),
    ]);
  };
  const recordMutation = useMutation({
    mutationFn: (idempotencyKey: string) => {
      if (candidateMutation.data === undefined) {
        throw new Error("Сначала загрузите и проверьте исходное наблюдение.");
      }
      return recordPriceQuoteFromObservation(context, {
        projectId,
        itemId,
        sourceObservationId: candidateMutation.data.source_observation_id,
        reason: recordDraft.reason.trim(),
        idempotencyKey,
      });
    },
    onSuccess: async () => {
      await refresh();
      setCandidateId("");
      setRecordDraft(emptyAttestation());
      setRecordError(null);
      setRecordKey(null);
      candidateMutation.reset();
    },
  });
  const normalizeMutation = useMutation({
    mutationFn: (input: {
      command: NonNullable<
        ReturnType<typeof validateNormalizationDraft>["command"]
      >;
      idempotencyKey: string;
    }) =>
      normalizePriceQuote(context, {
        projectId,
        quoteId: input.command.quoteId,
        unitConversionId: input.command.unitConversionId,
        fxRateId: input.command.fxRateId,
        adjustmentIds: input.command.adjustmentIds,
        regionAdjustmentId: input.command.regionAdjustmentId,
        partyAdjustmentId: input.command.partyAdjustmentId,
        paymentAdjustmentId: input.command.paymentAdjustmentId,
        reason: normalizationDraft.reason.trim(),
        idempotencyKey: input.idempotencyKey,
      }),
    onSuccess: async () => {
      await refresh();
      setNormalizationQuoteId(null);
      setNormalizationDraft(emptyNormalization());
      setNormalizationError(null);
      setNormalizationKey(null);
    },
  });
  const evaluateMutation = useMutation({
    mutationFn: (idempotencyKey: string) =>
      evaluatePriceItem(context, {
        projectId,
        itemId,
        asOf: evaluationDraft.asOf,
        reason: evaluationDraft.reason.trim(),
        idempotencyKey,
      }),
    onSuccess: async () => {
      await refresh();
      setEvaluationDraft(emptyEvaluation());
      setEvaluationError(null);
      setEvaluationKey(null);
    },
  });

  if (projectQuery.isPending || priceQuery.isPending) {
    return (
      <div className="page">
        <LoadingBlock label="Загрузка ценовой политики и источников" />
      </div>
    );
  }
  if (projectQuery.isError || priceQuery.isError) {
    const failed = projectQuery.isError ? projectQuery : priceQuery;
    return (
      <div className="page">
        <ErrorBlock
          error={failed.error}
          onRetry={() => {
            void projectQuery.refetch();
            void priceQuery.refetch();
          }}
        />
      </div>
    );
  }

  const project = projectQuery.data;
  const priceContext = priceQuery.data;
  const canRecord = auth.roles.includes("PROCUREMENT");
  const canNormalize =
    auth.roles.includes("PROCUREMENT") || auth.roles.includes("ESTIMATOR");
  const canEvaluate = canNormalize || auth.roles.includes("TECHNICAL_EXPERT");
  const quoteToNormalize =
    normalizationQuoteId === null
      ? undefined
      : priceContext.quotes.find(
          (item) => item.quote.quote_id === normalizationQuoteId,
        );
  const requirements =
    quoteToNormalize === undefined
      ? null
      : normalizationRequirements(
          quoteToNormalize.quote.basis,
          priceContext.target_basis,
        );
  const references = priceContext.normalization_references;
  const updateRecord = (patch: Partial<ControlledAttestation>) => {
    setRecordDraft((current) => ({
      ...current,
      ...patch,
      ...(patch.acknowledged === undefined ? { acknowledged: false } : {}),
    }));
    setRecordError(null);
    setRecordKey(null);
    recordMutation.reset();
  };
  const updateNormalization = (patch: Partial<PriceNormalizationDraft>) => {
    setNormalizationDraft((current) => ({
      ...current,
      ...patch,
      ...(patch.acknowledged === undefined ? { acknowledged: false } : {}),
    }));
    setNormalizationError(null);
    setNormalizationKey(null);
    normalizeMutation.reset();
  };
  const updateEvaluation = (patch: Partial<PriceEvaluationDraft>) => {
    setEvaluationDraft((current) => ({
      ...current,
      ...patch,
      ...(patch.acknowledged === undefined ? { acknowledged: false } : {}),
    }));
    setEvaluationError(null);
    setEvaluationKey(null);
    evaluateMutation.reset();
  };

  const submitCandidate = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const sourceId = candidateId.trim();
    if (sourceId === "") {
      setRecordError("Укажите ID проверенного наблюдения цены.");
      return;
    }
    setRecordError(null);
    setRecordKey(null);
    candidateMutation.mutate(sourceId);
  };
  const submitRecord = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    let error: string | null = null;
    if (recordDraft.reason.trim() === "") {
      error = "Укажите основание регистрации источника.";
    } else if (recordDraft.reason.length > 2000) {
      error = "Основание регистрации превышает 2000 символов.";
    } else if (recordDraft.projectCode.trim() !== project.code) {
      error = "Контрольный шифр проекта не совпадает.";
    } else if (!recordDraft.acknowledged) {
      error = "Подтвердите сверку точного источника.";
    }
    setRecordError(error);
    if (error !== null) {
      return;
    }
    let key = recordKey;
    if (key === null) {
      try {
        key = newIdempotencyKey();
      } catch (keyError) {
        setRecordError(
          keyError instanceof Error
            ? keyError.message
            : "Не удалось создать ключ операции.",
        );
        return;
      }
      setRecordKey(key);
    }
    recordMutation.mutate(key);
  };
  const submitNormalization = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (quoteToNormalize === undefined) {
      setNormalizationError("Выберите зарегистрированное предложение.");
      return;
    }
    const validation = validateNormalizationDraft(
      normalizationDraft,
      quoteToNormalize,
      priceContext,
      project.code,
    );
    setNormalizationError(validation.error);
    if (validation.error !== null || validation.command === null) {
      return;
    }
    let key = normalizationKey;
    if (key === null) {
      try {
        key = newIdempotencyKey();
      } catch (keyError) {
        setNormalizationError(
          keyError instanceof Error
            ? keyError.message
            : "Не удалось создать ключ операции.",
        );
        return;
      }
      setNormalizationKey(key);
    }
    normalizeMutation.mutate({
      command: validation.command,
      idempotencyKey: key,
    });
  };
  const submitEvaluation = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const error = validatePriceEvaluationDraft(evaluationDraft, project.code);
    setEvaluationError(error);
    if (error !== null) {
      return;
    }
    let key = evaluationKey;
    if (key === null) {
      try {
        key = newIdempotencyKey();
      } catch (keyError) {
        setEvaluationError(
          keyError instanceof Error
            ? keyError.message
            : "Не удалось создать ключ операции.",
        );
        return;
      }
      setEvaluationKey(key);
    }
    evaluateMutation.mutate(key);
  };

  return (
    <div className="page price-item-page">
      <nav className="breadcrumbs" aria-label="Навигационная цепочка">
        <Link to="/">Проекты</Link>
        <span>/</span>
        <Link to={`/projects/${encodeURIComponent(projectId)}`}>
          {project.code}
        </Link>
        <span>/</span>
        <Link to={`/projects/${encodeURIComponent(projectId)}/PRICING`}>
          Цены
        </Link>
        <span>/</span>
        <span>{itemId}</span>
      </nav>

      <header className="entry-header">
        <div>
          <p className="eyebrow">{project.code} · коммерческая база</p>
          <h1>{itemId}</h1>
          <p>
            Источник регистрируется без повторного ввода цены. Нормализация
            использует только ссылки из утверждённой политики, а triangulation
            заново проверяет сроки, доступность и независимость происхождения.
          </p>
        </div>
        <div className="entry-header__guard">
          <Icon name="shield" size={22} />
          <span>
            {priceContext.critical_price
              ? "Критическая цена: требуются первичный, независимый рыночный и внутренний/RFQ-контуры."
              : "Состав подтверждений определяется утверждённой ценовой политикой."}
          </span>
        </div>
      </header>

      <section className="price-governance-summary">
        <dl className="task-facts">
          <div>
            <dt>Сопоставление</dt>
            <dd>
              {priceContext.match_class} · {priceContext.match_id}
            </dd>
          </div>
          <div>
            <dt>Каталог</dt>
            <dd>{priceContext.catalog_version_id}</dd>
          </div>
          <div>
            <dt>Ценовая политика</dt>
            <dd>{priceContext.price_policy_version_id}</dd>
          </div>
          <div>
            <dt>Округление нормализации</dt>
            <dd>
              {priceContext.normalization_rounding_mode} ·{" "}
              {priceContext.normalization_rounding_scale} знака(ов)
            </dd>
          </div>
          <div>
            <dt>Комплект документов</dt>
            <dd>
              {priceContext.document_set_revision_id ??
                "не зафиксирован — выпуск заблокирован"}
            </dd>
          </div>
        </dl>
        <div className="technical-attributes">
          <span>Проверенные критические атрибуты</span>
          {Object.entries(priceContext.technical_attributes).map(
            ([key, value]) => (
              <code key={key}>
                {key}: {value}
              </code>
            ),
          )}
        </div>
        <BasisFacts
          title="Целевая коммерческая база"
          basis={priceContext.target_basis}
        />
      </section>

      {priceContext.current_decision !== null && (
        <section className="price-decision-banner">
          <div>
            <p className="eyebrow">Текущее решение</p>
            <h2>{priceContext.current_decision.status}</h2>
            <p>
              {priceContext.current_decision.amount_per_unit === null ||
              priceContext.current_decision.currency === null
                ? "Подтверждённая сопоставимая сумма пока отсутствует."
                : `${formatMoney(
                    priceContext.current_decision.amount_per_unit,
                    priceContext.current_decision.currency,
                  )} / ${priceContext.current_decision.unit ?? "ед."}`}
            </p>
          </div>
          <dl>
            <div>
              <dt>Дата среза</dt>
              <dd>{priceContext.current_decision.as_of ?? "не указана"}</dd>
            </div>
            <div>
              <dt>RFQ</dt>
              <dd>
                {priceContext.current_decision.rfq_request_id ?? "не открыт"}
              </dd>
            </div>
            <div>
              <dt>Согласования</dt>
              <dd>
                {priceContext.current_decision.approval_task_ids.join(", ") ||
                  "не требуются / не созданы"}
              </dd>
            </div>
          </dl>
        </section>
      )}

      <section className="price-workflow-section">
        <div className="section-heading">
          <div>
            <p className="eyebrow">01 · источник</p>
            <h2>Проверить наблюдение цены</h2>
            <p>
              Укажите ID уже проверенного наблюдения. Сервер восстановит из него
              полное предложение и перепроверит квалификацию источника.
            </p>
          </div>
        </div>
        <form className="inline-controlled-form" onSubmit={submitCandidate}>
          <label className="decision-field">
            <span>ID исходного наблюдения</span>
            <input
              type="text"
              value={candidateId}
              onChange={(event) => {
                setCandidateId(event.target.value);
                setRecordError(null);
                setRecordKey(null);
                candidateMutation.reset();
                recordMutation.reset();
              }}
            />
          </label>
          <button
            className="button button--secondary"
            type="submit"
            disabled={candidateMutation.isPending}
          >
            {candidateMutation.isPending ? "Проверка…" : "Загрузить источник"}
          </button>
        </form>
        {recordError !== null && candidateMutation.data === undefined && (
          <div className="decision-form__error" role="alert">
            <Icon name="warning" size={18} />
            <span>{recordError}</span>
          </div>
        )}
        {candidateMutation.isError && (
          <div className="decision-form__error" role="alert">
            <Icon name="warning" size={18} />
            <span>
              {candidateMutation.error instanceof Error
                ? candidateMutation.error.message
                : "Источник не прошёл проверку."}
            </span>
          </div>
        )}
        {candidateMutation.data !== undefined && (
          <div className="price-candidate">
            <div className="price-candidate__header">
              <div>
                <span>Точное предложение из доказательства</span>
                <h3>
                  {formatMoney(
                    candidateMutation.data.draft.amount,
                    candidateMutation.data.draft.basis.currency,
                  )}
                </h3>
                <p>
                  {candidateMutation.data.draft.evidence_class} · origin{" "}
                  {candidateMutation.data.source_origin_id}
                </p>
              </div>
              <StatusPill
                value={
                  candidateMutation.data.draft.available === true
                    ? "AVAILABLE"
                    : "NOT_CONFIRMED"
                }
                compact
              />
            </div>
            <BasisFacts
              title="Исходная коммерческая база"
              basis={candidateMutation.data.draft.basis}
            />
            <dl className="price-quote-facts">
              <div>
                <dt>Поставщик</dt>
                <dd>
                  {candidateMutation.data.draft.supplier_id ?? "не указан"}
                </dd>
              </div>
              <div>
                <dt>Дата / действует до</dt>
                <dd>
                  {candidateMutation.data.draft.quote_date} ·{" "}
                  {candidateMutation.data.draft.valid_until ??
                    "не подтверждено"}
                </dd>
              </div>
              <div>
                <dt>Срок поставки</dt>
                <dd>
                  {candidateMutation.data.draft.lead_time_days === null
                    ? "не подтверждён"
                    : `${candidateMutation.data.draft.lead_time_days} дн.`}
                </dd>
              </div>
            </dl>
            {canRecord ? (
              <form
                className="entry-form compact-entry-form"
                onSubmit={submitRecord}
              >
                <label className="decision-field">
                  <span>Основание регистрации</span>
                  <textarea
                    value={recordDraft.reason}
                    maxLength={2000}
                    rows={3}
                    onChange={(event) =>
                      updateRecord({ reason: event.target.value })
                    }
                  />
                </label>
                <label className="decision-field decision-field--confirmation">
                  <span>Введите шифр проекта: {project.code}</span>
                  <input
                    type="text"
                    value={recordDraft.projectCode}
                    onChange={(event) =>
                      updateRecord({ projectCode: event.target.value })
                    }
                  />
                </label>
                <label className="decision-acknowledgement">
                  <input
                    type="checkbox"
                    checked={recordDraft.acknowledged}
                    onChange={(event) =>
                      updateRecord({ acknowledged: event.target.checked })
                    }
                  />
                  <span>
                    Я сверил показанные атрибуты, сумму, НДС, упаковку, партию,
                    регион, доставку, оплату, доступность и срок действия.
                  </span>
                </label>
                {(recordError !== null || recordMutation.isError) && (
                  <div className="decision-form__error" role="alert">
                    <Icon name="warning" size={18} />
                    <span>
                      {recordError ??
                        (recordMutation.error instanceof Error
                          ? recordMutation.error.message
                          : "Регистрация источника не выполнена.")}
                    </span>
                  </div>
                )}
                <button
                  className="button button--critical"
                  type="submit"
                  disabled={recordMutation.isPending}
                >
                  {recordMutation.isPending
                    ? "Регистрация…"
                    : "Зарегистрировать точный источник"}
                </button>
              </form>
            ) : (
              <p className="permission-note">
                Регистрация доступна роли закупок; вы можете проверить точное
                содержимое источника без изменения данных.
              </p>
            )}
          </div>
        )}
      </section>

      <section className="price-workflow-section">
        <div className="section-heading">
          <div>
            <p className="eyebrow">02 · нормализация</p>
            <h2>Привести к общей коммерческой базе</h2>
            <p>
              Сумма не вводится вручную: сервер пересчитает её из предложения и
              выбранных ссылок текущей политики.
            </p>
          </div>
          <span className="section-count">{priceContext.quotes.length}</span>
        </div>
        <div className="price-quote-grid">
          {priceContext.quotes.map((quote) => (
            <PriceQuoteCard
              key={quote.quote.quote_id}
              quote={quote}
              canNormalize={canNormalize}
              onNormalize={() => {
                setNormalizationQuoteId(quote.quote.quote_id);
                setNormalizationDraft(emptyNormalization());
                setNormalizationError(null);
                setNormalizationKey(null);
                normalizeMutation.reset();
              }}
            />
          ))}
        </div>
        {priceContext.quotes.length === 0 && (
          <p className="permission-note">
            Зарегистрированных источников пока нет. Это не означает отсутствие
            рыночной цены.
          </p>
        )}
        {quoteToNormalize !== undefined && requirements !== null && (
          <form
            className="entry-form normalization-form"
            onSubmit={submitNormalization}
          >
            <div className="entry-form__intro">
              <p className="eyebrow">Точная команда нормализации</p>
              <h2>{quoteToNormalize.quote.quote_id}</h2>
              <p>
                Требуемые несовпадения определены сравнением строковых полей
                исходной и целевой базы; браузер не рассчитывает цену.
              </p>
            </div>
            <div className="price-basis-comparison">
              <BasisFacts
                title="Исходная база"
                basis={quoteToNormalize.quote.basis}
              />
              <BasisFacts
                title="Целевая база"
                basis={priceContext.target_basis}
              />
            </div>
            <div className="quantity-edit-grid">
              <ReferenceSelect
                label="Преобразование единицы"
                required={requirements.unitConversion}
                value={normalizationDraft.unitConversionId}
                options={references.unit_conversions ?? {}}
                onChange={(value) =>
                  updateNormalization({ unitConversionId: value })
                }
              />
              <ReferenceSelect
                label="Валютный курс"
                required={requirements.fxRate}
                value={normalizationDraft.fxRateId}
                options={references.fx_rates ?? {}}
                onChange={(value) => updateNormalization({ fxRateId: value })}
              />
              <ReferenceSelect
                label="Региональная корректировка"
                required={requirements.regionAdjustment}
                value={normalizationDraft.regionAdjustmentId}
                options={references.region_adjustments ?? {}}
                onChange={(value) =>
                  updateNormalization({ regionAdjustmentId: value })
                }
              />
              <ReferenceSelect
                label="Корректировка партии"
                required={requirements.partyAdjustment}
                value={normalizationDraft.partyAdjustmentId}
                options={references.party_adjustments ?? {}}
                onChange={(value) =>
                  updateNormalization({ partyAdjustmentId: value })
                }
              />
              <ReferenceSelect
                label="Корректировка оплаты"
                required={requirements.paymentAdjustment}
                value={normalizationDraft.paymentAdjustmentId}
                options={references.payment_adjustments ?? {}}
                onChange={(value) =>
                  updateNormalization({ paymentAdjustmentId: value })
                }
              />
            </div>
            <fieldset className="normalization-adjustments">
              <legend>Компоненты корректировки стоимости</legend>
              {Object.entries(references.adjustments ?? {}).map(
                ([id, payload]) => (
                  <label key={id}>
                    <input
                      type="checkbox"
                      checked={normalizationDraft.adjustmentIds.includes(id)}
                      onChange={(event) =>
                        updateNormalization({
                          adjustmentIds: event.target.checked
                            ? [...normalizationDraft.adjustmentIds, id]
                            : normalizationDraft.adjustmentIds.filter(
                                (value) => value !== id,
                              ),
                        })
                      }
                    />
                    <span>
                      <strong>{id}</strong>
                      {displayValue(payload)}
                    </span>
                  </label>
                ),
              )}
            </fieldset>
            <label className="decision-field">
              <span>Основание нормализации</span>
              <textarea
                value={normalizationDraft.reason}
                maxLength={2000}
                rows={4}
                onChange={(event) =>
                  updateNormalization({ reason: event.target.value })
                }
              />
            </label>
            <label className="decision-field decision-field--confirmation">
              <span>Введите шифр проекта: {project.code}</span>
              <input
                type="text"
                value={normalizationDraft.projectCode}
                onChange={(event) =>
                  updateNormalization({ projectCode: event.target.value })
                }
              />
            </label>
            <label className="decision-acknowledgement">
              <input
                type="checkbox"
                checked={normalizationDraft.acknowledged}
                onChange={(event) =>
                  updateNormalization({ acknowledged: event.target.checked })
                }
              />
              <span>
                Я проверил исходную и целевую базу и выбрал только применимые
                утверждённые ссылки и доказанные компоненты.
              </span>
            </label>
            {(normalizationError !== null || normalizeMutation.isError) && (
              <div className="decision-form__error" role="alert">
                <Icon name="warning" size={18} />
                <span>
                  {normalizationError ??
                    (normalizeMutation.error instanceof Error
                      ? normalizeMutation.error.message
                      : "Нормализация не выполнена.")}
                </span>
              </div>
            )}
            <div className="decision-form__actions">
              <button
                className="button button--secondary"
                type="button"
                onClick={() => setNormalizationQuoteId(null)}
              >
                Отмена
              </button>
              <button
                className="button button--critical"
                type="submit"
                disabled={normalizeMutation.isPending}
              >
                {normalizeMutation.isPending
                  ? "Независимый пересчёт…"
                  : "Нормализовать из сохранённых входов"}
              </button>
            </div>
          </form>
        )}
      </section>

      {canEvaluate && (
        <section className="price-workflow-section">
          <div className="section-heading">
            <div>
              <p className="eyebrow">03 · triangulation / RFQ</p>
              <h2>Оценить комплект подтверждений</h2>
              <p>
                Будут засчитаны только действующие, доступные, коммерчески
                полные и независимые нормализованные источники.
              </p>
            </div>
          </div>
          <form
            className="entry-form compact-entry-form"
            onSubmit={submitEvaluation}
          >
            <label className="decision-field">
              <span>Дата среза</span>
              <input
                type="date"
                value={evaluationDraft.asOf}
                onChange={(event) =>
                  updateEvaluation({ asOf: event.target.value })
                }
              />
            </label>
            <label className="decision-field">
              <span>Основание оценки</span>
              <textarea
                value={evaluationDraft.reason}
                maxLength={2000}
                rows={4}
                onChange={(event) =>
                  updateEvaluation({ reason: event.target.value })
                }
              />
            </label>
            <label className="decision-field decision-field--confirmation">
              <span>Введите шифр проекта: {project.code}</span>
              <input
                type="text"
                value={evaluationDraft.projectCode}
                onChange={(event) =>
                  updateEvaluation({ projectCode: event.target.value })
                }
              />
            </label>
            <label className="decision-acknowledgement">
              <input
                type="checkbox"
                checked={evaluationDraft.acknowledged}
                onChange={(event) =>
                  updateEvaluation({ acknowledged: event.target.checked })
                }
              />
              <span>
                Я проверил дату среза и понимаю, что недостаток классов,
                независимости или коммерческих условий откроет RFQ либо
                экспертную проверку, а не создаст приблизительную цену.
              </span>
            </label>
            {(evaluationError !== null || evaluateMutation.isError) && (
              <div className="decision-form__error" role="alert">
                <Icon name="warning" size={18} />
                <span>
                  {evaluationError ??
                    (evaluateMutation.error instanceof Error
                      ? evaluateMutation.error.message
                      : "Оценка цены не выполнена.")}
                </span>
              </div>
            )}
            <button
              className="button button--critical"
              type="submit"
              disabled={evaluateMutation.isPending}
            >
              {evaluateMutation.isPending
                ? "Проверка источников…"
                : "Запустить triangulation"}
            </button>
          </form>
        </section>
      )}
    </div>
  );
}
