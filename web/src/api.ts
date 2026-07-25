import type {
  ApprovalDecision,
  ApprovalDecisionResult,
  ApprovalState,
  CalculationContext,
  CalculationExecution,
  ConflictResolutionResult,
  ConflictReview,
  DocumentSetView,
  NormalizedPrice,
  PriceDecision,
  PriceItemContext,
  PriceQuoteCandidate,
  PriceQuoteRecord,
  ProjectPortfolioPage,
  ProjectRecordPage,
  ProjectRecordSection,
  ProjectView,
  ProjectWorkbench,
  QuantityChangeContext,
  QuantityExecution,
  QuantityManualChange,
  QuantitySubmission,
  QuarantinedUpload,
  ReleaseAttempt,
  ReleaseGateSet,
  WorkItemPage,
  WorkItemDetail,
} from "./types";

export class ApiError extends Error {
  readonly status: number;
  readonly requestId: string | null;

  constructor(message: string, status: number, requestId: string | null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.requestId = requestId;
  }
}

export interface RequestContext {
  apiBasePath: string;
  authorizationHeaders: () => Record<string, string>;
}

type QueryValue =
  string | number | boolean | readonly string[] | null | undefined;

async function request<T>(
  context: RequestContext,
  path: string,
  query?: Record<string, QueryValue>,
  signal?: AbortSignal,
): Promise<T> {
  const url = new URL(`${context.apiBasePath}${path}`, window.location.origin);
  for (const [key, rawValue] of Object.entries(query ?? {})) {
    if (rawValue === undefined || rawValue === null || rawValue === "") {
      continue;
    }
    const values = Array.isArray(rawValue) ? rawValue : [rawValue];
    for (const value of values) {
      url.searchParams.append(key, String(value));
    }
  }
  const response = await fetch(url, {
    method: "GET",
    credentials: "omit",
    headers: {
      Accept: "application/json",
      ...context.authorizationHeaders(),
    },
    ...(signal === undefined ? {} : { signal }),
  });
  if (!response.ok) {
    let detail = `Запрос завершился с кодом ${response.status}`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") {
        detail = body.detail;
      }
    } catch {
      // The bounded status text is safer than reflecting an arbitrary body.
    }
    throw new ApiError(
      detail,
      response.status,
      response.headers.get("x-request-id"),
    );
  }
  return (await response.json()) as T;
}

async function mutate<T>(
  context: RequestContext,
  path: string,
  options: {
    body: unknown;
    idempotencyKey: string;
  },
): Promise<T> {
  const url = new URL(`${context.apiBasePath}${path}`, window.location.origin);
  const response = await fetch(url, {
    method: "POST",
    credentials: "omit",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      "Idempotency-Key": options.idempotencyKey,
      ...context.authorizationHeaders(),
    },
    body: JSON.stringify(options.body),
  });
  if (!response.ok) {
    let detail = `Запрос завершился с кодом ${response.status}`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") {
        detail = body.detail;
      }
    } catch {
      // Do not reflect an arbitrary non-JSON response into the interface.
    }
    throw new ApiError(
      detail,
      response.status,
      response.headers.get("x-request-id"),
    );
  }
  return (await response.json()) as T;
}

async function mutateForm<T>(
  context: RequestContext,
  path: string,
  options: {
    body: FormData;
    idempotencyKey: string;
  },
): Promise<T> {
  const url = new URL(`${context.apiBasePath}${path}`, window.location.origin);
  const response = await fetch(url, {
    method: "POST",
    credentials: "omit",
    headers: {
      Accept: "application/json",
      "Idempotency-Key": options.idempotencyKey,
      ...context.authorizationHeaders(),
    },
    body: options.body,
  });
  if (!response.ok) {
    let detail = `Запрос завершился с кодом ${response.status}`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") {
        detail = body.detail;
      }
    } catch {
      // Do not reflect an arbitrary non-JSON response into the interface.
    }
    throw new ApiError(
      detail,
      response.status,
      response.headers.get("x-request-id"),
    );
  }
  return (await response.json()) as T;
}

export function newIdempotencyKey(): string {
  if (typeof crypto.randomUUID !== "function") {
    throw new Error(
      "Браузер не поддерживает безопасный идентификатор операции; действие заблокировано",
    );
  }
  return crypto.randomUUID();
}

export function listProjects(
  context: RequestContext,
  options: {
    query?: string | undefined;
    states?: ApprovalState[] | undefined;
    cursor?: string | undefined;
    limit?: number;
  },
  signal?: AbortSignal,
): Promise<ProjectPortfolioPage> {
  return request(
    context,
    "/projects",
    {
      query: options.query,
      states: options.states,
      cursor: options.cursor,
      limit: options.limit ?? 50,
    },
    signal,
  );
}

export function createProject(
  context: RequestContext,
  input: {
    code: string;
    name: string;
    reason: string;
    idempotencyKey: string;
  },
): Promise<ProjectView> {
  return mutate(context, "/projects", {
    idempotencyKey: input.idempotencyKey,
    body: {
      code: input.code,
      name: input.name,
      reason: input.reason,
    },
  });
}

export function listWorkItems(
  context: RequestContext,
  options: {
    statuses?: string[] | undefined;
    cursor?: string | undefined;
    limit?: number;
  },
  signal?: AbortSignal,
): Promise<WorkItemPage> {
  return request(
    context,
    "/work-items",
    {
      statuses: options.statuses,
      cursor: options.cursor,
      limit: options.limit ?? 50,
    },
    signal,
  );
}

export function getWorkItem(
  context: RequestContext,
  taskId: string,
  signal?: AbortSignal,
): Promise<WorkItemDetail> {
  return request(
    context,
    `/work-items/${encodeURIComponent(taskId)}`,
    undefined,
    signal,
  );
}

export function decideWorkItem(
  context: RequestContext,
  input: {
    projectId: string;
    taskId: string;
    decision: ApprovalDecision;
    reason: string;
    expectedTaskUpdatedAt: string;
    evidenceIds: string[];
    relatedChangeIds?: string[];
    idempotencyKey: string;
  },
): Promise<ApprovalDecisionResult> {
  return mutate(
    context,
    `/projects/${encodeURIComponent(input.projectId)}/approvals/${encodeURIComponent(input.taskId)}/decision`,
    {
      idempotencyKey: input.idempotencyKey,
      body: {
        decision: input.decision,
        reason: input.reason,
        expected_task_updated_at: input.expectedTaskUpdatedAt,
        evidence_ids: input.evidenceIds,
        related_change_ids: input.relatedChangeIds ?? [],
      },
    },
  );
}

export function getConflictReview(
  context: RequestContext,
  projectId: string,
  conflictId: string,
  signal?: AbortSignal,
): Promise<ConflictReview> {
  return request(
    context,
    `/projects/${encodeURIComponent(projectId)}/evidence/conflicts/${encodeURIComponent(conflictId)}`,
    undefined,
    signal,
  );
}

export function resolveConflict(
  context: RequestContext,
  input: {
    projectId: string;
    conflictId: string;
    selectedObservationId: string;
    resolutionReason: string;
    expectedConflictUpdatedAt: string;
    expectedTaskUpdatedAt: string;
    idempotencyKey: string;
  },
): Promise<ConflictResolutionResult> {
  return mutate(
    context,
    `/projects/${encodeURIComponent(input.projectId)}/evidence/conflicts/${encodeURIComponent(input.conflictId)}/resolve`,
    {
      idempotencyKey: input.idempotencyKey,
      body: {
        selected_observation_id: input.selectedObservationId,
        resolution_reason: input.resolutionReason,
        expected_conflict_updated_at: input.expectedConflictUpdatedAt,
        expected_task_updated_at: input.expectedTaskUpdatedAt,
      },
    },
  );
}

export function getWorkbench(
  context: RequestContext,
  projectId: string,
  signal?: AbortSignal,
): Promise<ProjectWorkbench> {
  return request(
    context,
    `/projects/${encodeURIComponent(projectId)}/workbench`,
    undefined,
    signal,
  );
}

export function getProject(
  context: RequestContext,
  projectId: string,
  signal?: AbortSignal,
): Promise<ProjectView> {
  return request(
    context,
    `/projects/${encodeURIComponent(projectId)}`,
    undefined,
    signal,
  );
}

export function uploadDocument(
  context: RequestContext,
  input: {
    projectId: string;
    logicalKey: string;
    title: string;
    documentType: string;
    revisionLabel: string;
    reason: string;
    file: File;
    critical: boolean;
    makeCandidateCurrent: boolean;
    idempotencyKey: string;
  },
): Promise<QuarantinedUpload> {
  const body = new FormData();
  body.set("logical_key", input.logicalKey);
  body.set("title", input.title);
  body.set("document_type", input.documentType);
  body.set("revision_label", input.revisionLabel);
  body.set("reason", input.reason);
  body.set("critical", String(input.critical));
  body.set("make_candidate_current", String(input.makeCandidateCurrent));
  body.set("upload", input.file, input.file.name);
  return mutateForm(
    context,
    `/projects/${encodeURIComponent(input.projectId)}/documents`,
    { body, idempotencyKey: input.idempotencyKey },
  );
}

export function getDocumentUpload(
  context: RequestContext,
  projectId: string,
  uploadId: string,
  signal?: AbortSignal,
): Promise<QuarantinedUpload> {
  return request(
    context,
    `/projects/${encodeURIComponent(projectId)}/document-uploads/${encodeURIComponent(uploadId)}`,
    undefined,
    signal,
  );
}

export function getDocumentSet(
  context: RequestContext,
  projectId: string,
  documentSetId: string,
  signal?: AbortSignal,
): Promise<DocumentSetView> {
  return request(
    context,
    `/projects/${encodeURIComponent(projectId)}/document-sets/${encodeURIComponent(documentSetId)}`,
    undefined,
    signal,
  );
}

export function confirmDocumentSet(
  context: RequestContext,
  input: {
    projectId: string;
    documentSetId: string;
    reason: string;
    idempotencyKey: string;
  },
): Promise<ProjectView> {
  return mutate(
    context,
    `/projects/${encodeURIComponent(input.projectId)}/document-set/confirm`,
    {
      idempotencyKey: input.idempotencyKey,
      body: {
        candidate_document_set_revision_id: input.documentSetId,
        reason: input.reason,
      },
    },
  );
}

export function listRecords(
  context: RequestContext,
  projectId: string,
  section: ProjectRecordSection,
  options: {
    query?: string | undefined;
    statuses?: string[] | undefined;
    currentOnly?: boolean;
    cursor?: string | undefined;
    limit?: number;
  },
  signal?: AbortSignal,
): Promise<ProjectRecordPage> {
  return request(
    context,
    `/projects/${encodeURIComponent(projectId)}/records`,
    {
      section,
      query: options.query,
      statuses: options.statuses,
      current_only: options.currentOnly ?? false,
      cursor: options.cursor,
      limit: options.limit ?? 50,
    },
    signal,
  );
}

export function getQuantityChangeContext(
  context: RequestContext,
  projectId: string,
  lineId: string,
  signal?: AbortSignal,
): Promise<QuantityChangeContext> {
  return request(
    context,
    `/projects/${encodeURIComponent(projectId)}/boq/lines/${encodeURIComponent(lineId)}/quantity-change-context`,
    undefined,
    signal,
  );
}

export function proposeQuantityChange(
  context: RequestContext,
  input: {
    projectId: string;
    lineId: string;
    submission: QuantitySubmission;
    reason: string;
    idempotencyKey: string;
  },
): Promise<QuantityManualChange> {
  return mutate(
    context,
    `/projects/${encodeURIComponent(input.projectId)}/boq/lines/${encodeURIComponent(input.lineId)}/quantity-change-proposals`,
    {
      idempotencyKey: input.idempotencyKey,
      body: {
        submission: input.submission,
        reason: input.reason,
      },
    },
  );
}

export function getQuantityManualChange(
  context: RequestContext,
  projectId: string,
  changeId: string,
  signal?: AbortSignal,
): Promise<QuantityManualChange> {
  return request(
    context,
    `/projects/${encodeURIComponent(projectId)}/manual-changes/${encodeURIComponent(changeId)}`,
    undefined,
    signal,
  );
}

export function applyQuantityManualChange(
  context: RequestContext,
  input: {
    projectId: string;
    changeId: string;
    reason: string;
    idempotencyKey: string;
  },
): Promise<QuantityExecution> {
  return mutate(
    context,
    `/projects/${encodeURIComponent(input.projectId)}/manual-changes/${encodeURIComponent(input.changeId)}/apply`,
    {
      idempotencyKey: input.idempotencyKey,
      body: { reason: input.reason },
    },
  );
}

export function getPriceItemContext(
  context: RequestContext,
  projectId: string,
  itemId: string,
  signal?: AbortSignal,
): Promise<PriceItemContext> {
  return request(
    context,
    `/projects/${encodeURIComponent(projectId)}/pricing/items/${encodeURIComponent(itemId)}/context`,
    undefined,
    signal,
  );
}

export function getPriceQuoteCandidate(
  context: RequestContext,
  projectId: string,
  itemId: string,
  sourceObservationId: string,
  signal?: AbortSignal,
): Promise<PriceQuoteCandidate> {
  return request(
    context,
    `/projects/${encodeURIComponent(projectId)}/pricing/items/${encodeURIComponent(itemId)}/quote-candidates/${encodeURIComponent(sourceObservationId)}`,
    undefined,
    signal,
  );
}

export function recordPriceQuoteFromObservation(
  context: RequestContext,
  input: {
    projectId: string;
    itemId: string;
    sourceObservationId: string;
    reason: string;
    idempotencyKey: string;
  },
): Promise<PriceQuoteRecord> {
  return mutate(
    context,
    `/projects/${encodeURIComponent(input.projectId)}/pricing/items/${encodeURIComponent(input.itemId)}/quotes/from-observation`,
    {
      idempotencyKey: input.idempotencyKey,
      body: {
        source_observation_id: input.sourceObservationId,
        reason: input.reason,
      },
    },
  );
}

export function normalizePriceQuote(
  context: RequestContext,
  input: {
    projectId: string;
    quoteId: string;
    unitConversionId: string | null;
    fxRateId: string | null;
    adjustmentIds: string[];
    regionAdjustmentId: string | null;
    partyAdjustmentId: string | null;
    paymentAdjustmentId: string | null;
    reason: string;
    idempotencyKey: string;
  },
): Promise<NormalizedPrice> {
  return mutate(
    context,
    `/projects/${encodeURIComponent(input.projectId)}/pricing/normalize`,
    {
      idempotencyKey: input.idempotencyKey,
      body: {
        command: {
          quote_id: input.quoteId,
          unit_conversion_id: input.unitConversionId,
          fx_rate_id: input.fxRateId,
          adjustment_ids: input.adjustmentIds,
          region_adjustment_id: input.regionAdjustmentId,
          party_adjustment_id: input.partyAdjustmentId,
          payment_adjustment_id: input.paymentAdjustmentId,
        },
        reason: input.reason,
      },
    },
  );
}

export function evaluatePriceItem(
  context: RequestContext,
  input: {
    projectId: string;
    itemId: string;
    asOf: string;
    reason: string;
    idempotencyKey: string;
  },
): Promise<PriceDecision> {
  return mutate(
    context,
    `/projects/${encodeURIComponent(input.projectId)}/pricing/items/${encodeURIComponent(input.itemId)}/evaluate`,
    {
      idempotencyKey: input.idempotencyKey,
      body: {
        as_of: input.asOf,
        reason: input.reason,
      },
    },
  );
}

export function getCalculationContext(
  context: RequestContext,
  projectId: string,
  signal?: AbortSignal,
): Promise<CalculationContext> {
  return request(
    context,
    `/projects/${encodeURIComponent(projectId)}/calculation-context`,
    undefined,
    signal,
  );
}

export function executeCurrentCalculation(
  context: RequestContext,
  input: {
    projectId: string;
    expectedRowVersion: number;
    candidateHash: string;
    reason: string;
    idempotencyKey: string;
  },
): Promise<CalculationExecution> {
  return mutate(
    context,
    `/projects/${encodeURIComponent(input.projectId)}/calculations/current`,
    {
      idempotencyKey: input.idempotencyKey,
      body: {
        expected_row_version: input.expectedRowVersion,
        candidate_hash: input.candidateHash,
        reason: input.reason,
      },
    },
  );
}

export function getReleaseGates(
  context: RequestContext,
  projectId: string,
  signal?: AbortSignal,
): Promise<ReleaseGateSet> {
  return request(
    context,
    `/projects/${encodeURIComponent(projectId)}/release-gates`,
    undefined,
    signal,
  );
}

export function attemptRelease(
  context: RequestContext,
  input: {
    projectId: string;
    target: "bid" | "internal";
    expectedRowVersion: number;
    gateHash: string;
    reason: string;
    idempotencyKey: string;
  },
): Promise<ReleaseAttempt> {
  return mutate(
    context,
    `/projects/${encodeURIComponent(input.projectId)}/release/${input.target}`,
    {
      idempotencyKey: input.idempotencyKey,
      body: {
        expected_row_version: input.expectedRowVersion,
        gate_hash: input.gateHash,
        reason: input.reason,
      },
    },
  );
}
