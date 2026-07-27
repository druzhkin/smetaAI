import type {
  ApprovalDecision,
  ApprovalDecisionResult,
  ApprovalState,
  BoqAuthoringContext,
  BoqCostComponent,
  BoqLine,
  BoqLineReview,
  CalculationContext,
  CalculationExecution,
  ConflictResolutionResult,
  ConflictReview,
  ContractContext,
  ContractDecisionResult,
  ContractTerm,
  ContractTermKind,
  ContractValidation,
  DocumentSetView,
  EvidenceObservation,
  ManualEvidenceContext,
  ManualEvidenceDecisionResult,
  ManualEvidenceReview,
  NomenclatureContext,
  NomenclatureMatchClass,
  NomenclatureMatchView,
  NomenclatureReviewContext,
  NormalizedPrice,
  PassportContext,
  PassportDecisionResult,
  PassportFact,
  PassportValidation,
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
  ReconciliationContext,
  ReconciliationOutcome,
  ReleaseAttempt,
  ReleaseGateSet,
  RiskCalculation,
  RiskContext,
  RiskDecisionResult,
  RiskDraft,
  ScopeRun,
  ScenarioContext,
  ScenarioExecution,
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

export function getManualEvidenceContext(
  context: RequestContext,
  projectId: string,
  signal?: AbortSignal,
): Promise<ManualEvidenceContext> {
  return request(
    context,
    `/projects/${encodeURIComponent(projectId)}/evidence/manual/context`,
    undefined,
    signal,
  );
}

export function getPassportContext(
  context: RequestContext,
  projectId: string,
  fieldName?: string,
  signal?: AbortSignal,
): Promise<PassportContext> {
  return request(
    context,
    `/projects/${encodeURIComponent(projectId)}/passport/context`,
    { field_name: fieldName, limit: 100 },
    signal,
  );
}

export function submitPassportFact(
  context: RequestContext,
  input: {
    projectId: string;
    fieldName: string;
    value: unknown;
    unit: string | null;
    observationIds: string[];
    expectedDocumentSetRevisionId: string;
    requirementsVersionId: string;
    reason: string;
    idempotencyKey: string;
  },
): Promise<PassportFact> {
  return mutate(
    context,
    `/projects/${encodeURIComponent(input.projectId)}/passport/facts`,
    {
      idempotencyKey: input.idempotencyKey,
      body: {
        draft: {
          field_name: input.fieldName,
          value: input.value,
          unit: input.unit,
          observation_ids: input.observationIds,
        },
        expected_document_set_revision_id: input.expectedDocumentSetRevisionId,
        requirements_version_id: input.requirementsVersionId,
        reason: input.reason,
      },
    },
  );
}

export function decidePassportFact(
  context: RequestContext,
  input: {
    projectId: string;
    factId: string;
    decision: ApprovalDecision;
    expectedFactUpdatedAt: string;
    expectedTaskUpdatedAt: string;
    reason: string;
    idempotencyKey: string;
  },
): Promise<PassportDecisionResult> {
  return mutate(
    context,
    `/projects/${encodeURIComponent(input.projectId)}/passport/facts/${encodeURIComponent(input.factId)}/decision`,
    {
      idempotencyKey: input.idempotencyKey,
      body: {
        decision: input.decision,
        expected_fact_updated_at: input.expectedFactUpdatedAt,
        expected_task_updated_at: input.expectedTaskUpdatedAt,
        reason: input.reason,
      },
    },
  );
}

export function validatePassport(
  context: RequestContext,
  input: {
    projectId: string;
    reason: string;
    idempotencyKey: string;
  },
): Promise<PassportValidation> {
  return mutate(
    context,
    `/projects/${encodeURIComponent(input.projectId)}/passport/validate`,
    {
      idempotencyKey: input.idempotencyKey,
      body: { reason: input.reason },
    },
  );
}

export function getContractContext(
  context: RequestContext,
  projectId: string,
  kind?: ContractTermKind,
  signal?: AbortSignal,
): Promise<ContractContext> {
  return request(
    context,
    `/projects/${encodeURIComponent(projectId)}/contract/context`,
    { kind, limit: 100 },
    signal,
  );
}

export function submitContractTerm(
  context: RequestContext,
  input: {
    projectId: string;
    kind: ContractTermKind;
    value: string;
    observationIds: string[];
    expectedDocumentSetRevisionId: string;
    rulesVersionId: string;
    reason: string;
    idempotencyKey: string;
  },
): Promise<ContractTerm> {
  return mutate(
    context,
    `/projects/${encodeURIComponent(input.projectId)}/contract/terms`,
    {
      idempotencyKey: input.idempotencyKey,
      body: {
        draft: {
          kind: input.kind,
          value: input.value,
          observation_ids: input.observationIds,
        },
        expected_document_set_revision_id: input.expectedDocumentSetRevisionId,
        rules_version_id: input.rulesVersionId,
        reason: input.reason,
      },
    },
  );
}

export function decideContractTerm(
  context: RequestContext,
  input: {
    projectId: string;
    termId: string;
    decision: ApprovalDecision;
    expectedTermUpdatedAt: string;
    expectedTaskUpdatedAt: string;
    reason: string;
    idempotencyKey: string;
  },
): Promise<ContractDecisionResult> {
  return mutate(
    context,
    `/projects/${encodeURIComponent(input.projectId)}/contract/terms/${encodeURIComponent(input.termId)}/decision`,
    {
      idempotencyKey: input.idempotencyKey,
      body: {
        decision: input.decision,
        expected_term_updated_at: input.expectedTermUpdatedAt,
        expected_task_updated_at: input.expectedTaskUpdatedAt,
        reason: input.reason,
      },
    },
  );
}

export function proposeContractCostImpact(
  context: RequestContext,
  input: {
    projectId: string;
    termId: string;
    command: {
      amount: string;
      currency?: string;
      cost_component_line_id?: string;
      cost_component_semantic_key?: string;
      derived_cost_model_id?: string;
      no_cost_reason?: string;
    };
    expectedTermUpdatedAt: string;
    reason: string;
    idempotencyKey: string;
  },
): Promise<ContractTerm> {
  return mutate(
    context,
    `/projects/${encodeURIComponent(input.projectId)}/contract/terms/${encodeURIComponent(input.termId)}/cost-impact-proposals`,
    {
      idempotencyKey: input.idempotencyKey,
      body: {
        command: input.command,
        expected_term_updated_at: input.expectedTermUpdatedAt,
        reason: input.reason,
      },
    },
  );
}

export function finalizeContractCostImpact(
  context: RequestContext,
  input: {
    projectId: string;
    termId: string;
    expectedTermUpdatedAt: string;
    reason: string;
    idempotencyKey: string;
  },
): Promise<{ term: ContractTerm; validation: ContractValidation }> {
  return mutate(
    context,
    `/projects/${encodeURIComponent(input.projectId)}/contract/terms/${encodeURIComponent(input.termId)}/cost-impact/finalize`,
    {
      idempotencyKey: input.idempotencyKey,
      body: {
        expected_term_updated_at: input.expectedTermUpdatedAt,
        reason: input.reason,
      },
    },
  );
}

export function validateContract(
  context: RequestContext,
  input: {
    projectId: string;
    reason: string;
    idempotencyKey: string;
  },
): Promise<ContractValidation> {
  return mutate(
    context,
    `/projects/${encodeURIComponent(input.projectId)}/contract/validate`,
    {
      idempotencyKey: input.idempotencyKey,
      body: { reason: input.reason },
    },
  );
}

export function getRiskContext(
  context: RequestContext,
  projectId: string,
  riskKey?: string,
  signal?: AbortSignal,
): Promise<RiskContext> {
  return request(
    context,
    `/projects/${encodeURIComponent(projectId)}/risks/context`,
    { risk_key: riskKey, limit: 100 },
    signal,
  );
}

export function submitRiskItem(
  context: RequestContext,
  input: {
    projectId: string;
    draft: RiskDraft;
    expectedDocumentSetRevisionId: string;
    riskModelVersionId: string;
    reason: string;
    idempotencyKey: string;
  },
): Promise<RiskContext["items"][number]["item"]> {
  return mutate(
    context,
    `/projects/${encodeURIComponent(input.projectId)}/risks`,
    {
      idempotencyKey: input.idempotencyKey,
      body: {
        draft: input.draft,
        expected_document_set_revision_id: input.expectedDocumentSetRevisionId,
        risk_model_version_id: input.riskModelVersionId,
        reason: input.reason,
      },
    },
  );
}

export function decideRiskItem(
  context: RequestContext,
  input: {
    projectId: string;
    riskItemId: string;
    decision: "APPROVED" | "REJECTED";
    expectedRiskUpdatedAt: string;
    expectedTaskUpdatedAt: string;
    reason: string;
    idempotencyKey: string;
  },
): Promise<RiskDecisionResult> {
  return mutate(
    context,
    `/projects/${encodeURIComponent(input.projectId)}/risks/${encodeURIComponent(input.riskItemId)}/decision`,
    {
      idempotencyKey: input.idempotencyKey,
      body: {
        command: {
          decision: input.decision,
          expected_risk_updated_at: input.expectedRiskUpdatedAt,
          expected_task_updated_at: input.expectedTaskUpdatedAt,
        },
        reason: input.reason,
      },
    },
  );
}

export function calculateRiskReserve(
  context: RequestContext,
  input: {
    projectId: string;
    expectedDocumentSetRevisionId: string;
    riskModelVersionId: string;
    reason: string;
    idempotencyKey: string;
  },
): Promise<RiskCalculation> {
  return mutate(
    context,
    `/projects/${encodeURIComponent(input.projectId)}/risks/calculate`,
    {
      idempotencyKey: input.idempotencyKey,
      body: {
        expected_document_set_revision_id: input.expectedDocumentSetRevisionId,
        risk_model_version_id: input.riskModelVersionId,
        reason: input.reason,
      },
    },
  );
}

export function recordManualEvidence(
  context: RequestContext,
  input: {
    projectId: string;
    policyVersionId: string;
    fieldName: string;
    value: unknown;
    unit: string | null;
    sourcePriority: number;
    documentId: string;
    documentRevisionId: string;
    originalObjectHash: string;
    locatorKind: string;
    locator: string;
    page: number | null;
    table: string | null;
    sheet: string | null;
    cellOrRange: string | null;
    observedAt: string;
    reason: string;
    idempotencyKey: string;
  },
): Promise<EvidenceObservation> {
  return mutate(
    context,
    `/projects/${encodeURIComponent(input.projectId)}/evidence/observations`,
    {
      idempotencyKey: input.idempotencyKey,
      body: {
        draft: {
          field_name: input.fieldName,
          value: input.value,
          unit: input.unit,
          method: "MANUAL",
          method_version: input.policyVersionId,
          source_priority: input.sourcePriority,
          location: {
            document_id: input.documentId,
            document_revision_id: input.documentRevisionId,
            original_object_hash: input.originalObjectHash,
            locator_kind: input.locatorKind,
            locator: input.locator,
            page: input.page,
            table: input.table,
            sheet: input.sheet,
            cell_or_range: input.cellOrRange,
          },
          observed_at: input.observedAt,
          confidence: null,
          adapter_qualification_id: null,
          basis_metadata: {},
        },
        reason: input.reason,
      },
    },
  );
}

export function getManualEvidenceReview(
  context: RequestContext,
  projectId: string,
  observationId: string,
  signal?: AbortSignal,
): Promise<ManualEvidenceReview> {
  return request(
    context,
    `/projects/${encodeURIComponent(projectId)}/evidence/observations/${encodeURIComponent(observationId)}/manual-review`,
    undefined,
    signal,
  );
}

export function decideManualEvidence(
  context: RequestContext,
  input: {
    projectId: string;
    observationId: string;
    decision: ApprovalDecision;
    reason: string;
    expectedTaskUpdatedAt: string;
    idempotencyKey: string;
  },
): Promise<ManualEvidenceDecisionResult> {
  return mutate(
    context,
    `/projects/${encodeURIComponent(input.projectId)}/evidence/observations/${encodeURIComponent(input.observationId)}/manual-review/decision`,
    {
      idempotencyKey: input.idempotencyKey,
      body: {
        decision: input.decision,
        reason: input.reason,
        expected_task_updated_at: input.expectedTaskUpdatedAt,
      },
    },
  );
}

export function getReconciliationContext(
  context: RequestContext,
  projectId: string,
  fieldName?: string,
  signal?: AbortSignal,
): Promise<ReconciliationContext> {
  return request(
    context,
    `/projects/${encodeURIComponent(projectId)}/evidence/reconciliation-context`,
    { field_name: fieldName, limit: 100 },
    signal,
  );
}

export function reconcileEvidence(
  context: RequestContext,
  input: {
    projectId: string;
    observationIds: string[];
    reconciliationVersionId: string;
    reason: string;
    idempotencyKey: string;
  },
): Promise<ReconciliationOutcome> {
  return mutate(
    context,
    `/projects/${encodeURIComponent(input.projectId)}/evidence/reconcile`,
    {
      idempotencyKey: input.idempotencyKey,
      body: {
        observation_ids: input.observationIds,
        reconciliation_version_id: input.reconciliationVersionId,
        reason: input.reason,
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

export function getBoqAuthoringContext(
  context: RequestContext,
  projectId: string,
  evidenceFieldName: string,
  signal?: AbortSignal,
): Promise<BoqAuthoringContext> {
  return request(
    context,
    `/projects/${encodeURIComponent(projectId)}/boq/authoring-context`,
    { evidence_field_name: evidenceFieldName, limit: 100 },
    signal,
  );
}

export function createBoqLine(
  context: RequestContext,
  input: {
    projectId: string;
    lineKey: string;
    wbsNodeId: string;
    workCode: string;
    description: string;
    unit: string;
    evidenceObservationIds: string[];
    costComponents: BoqCostComponent[];
    criticalQuantity: boolean;
    reason: string;
    idempotencyKey: string;
  },
): Promise<BoqLine> {
  return mutate(
    context,
    `/projects/${encodeURIComponent(input.projectId)}/boq/lines`,
    {
      idempotencyKey: input.idempotencyKey,
      body: {
        draft: {
          line_key: input.lineKey,
          wbs_node_id: input.wbsNodeId,
          work_code: input.workCode,
          description: input.description,
          unit: input.unit,
          evidence_observation_ids: input.evidenceObservationIds,
          cost_components: input.costComponents,
          critical_quantity: input.criticalQuantity,
        },
        reason: input.reason,
      },
    },
  );
}

export function getBoqLineReview(
  context: RequestContext,
  projectId: string,
  lineId: string,
  signal?: AbortSignal,
): Promise<BoqLineReview> {
  return request(
    context,
    `/projects/${encodeURIComponent(projectId)}/boq/lines/${encodeURIComponent(lineId)}/review`,
    undefined,
    signal,
  );
}

export function verifyBoqLine(
  context: RequestContext,
  input: {
    projectId: string;
    lineId: string;
    expectedLineUpdatedAt: string;
    reason: string;
    idempotencyKey: string;
  },
): Promise<BoqLine> {
  return mutate(
    context,
    `/projects/${encodeURIComponent(input.projectId)}/boq/lines/${encodeURIComponent(input.lineId)}/verify`,
    {
      idempotencyKey: input.idempotencyKey,
      body: {
        expected_line_updated_at: input.expectedLineUpdatedAt,
        reason: input.reason,
      },
    },
  );
}

export function runScopeCompleteness(
  context: RequestContext,
  input: {
    projectId: string;
    wbsNodeId: string;
    reason: string;
    idempotencyKey: string;
  },
): Promise<ScopeRun> {
  return mutate(
    context,
    `/projects/${encodeURIComponent(input.projectId)}/boq/scope-evaluations`,
    {
      idempotencyKey: input.idempotencyKey,
      body: {
        wbs_node_id: input.wbsNodeId,
        reason: input.reason,
      },
    },
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

export function getNomenclatureContext(
  context: RequestContext,
  projectId: string,
  options: {
    catalogQuery?: string;
    evidenceFieldName: string;
  },
  signal?: AbortSignal,
): Promise<NomenclatureContext> {
  return request(
    context,
    `/projects/${encodeURIComponent(projectId)}/nomenclature/context`,
    {
      catalog_query: options.catalogQuery,
      evidence_field_name: options.evidenceFieldName,
      limit: 100,
    },
    signal,
  );
}

export function assessNomenclature(
  context: RequestContext,
  input: {
    projectId: string;
    sourceItemId: string;
    canonicalItemId: string;
    sourceAttributesObservationId: string;
    reason: string;
    idempotencyKey: string;
  },
): Promise<NomenclatureMatchView> {
  return mutate(
    context,
    `/projects/${encodeURIComponent(input.projectId)}/nomenclature/assessments`,
    {
      idempotencyKey: input.idempotencyKey,
      body: {
        draft: {
          source_item_id: input.sourceItemId,
          canonical_item_id: input.canonicalItemId,
          source_attributes_observation_id: input.sourceAttributesObservationId,
        },
        reason: input.reason,
      },
    },
  );
}

export function getNomenclatureReview(
  context: RequestContext,
  projectId: string,
  matchId: string,
  signal?: AbortSignal,
): Promise<NomenclatureReviewContext> {
  return request(
    context,
    `/projects/${encodeURIComponent(projectId)}/nomenclature/${encodeURIComponent(matchId)}/review`,
    undefined,
    signal,
  );
}

export function proposeNomenclatureAnalogue(
  context: RequestContext,
  input: {
    projectId: string;
    matchId: string;
    analogueClass: Exclude<
      NomenclatureMatchClass,
      "EXACT" | "TECHNICALLY_UNACCEPTABLE" | "INSUFFICIENT_DATA"
    >;
    reason: string;
    idempotencyKey: string;
  },
): Promise<NomenclatureMatchView> {
  return mutate(
    context,
    `/projects/${encodeURIComponent(input.projectId)}/nomenclature/${encodeURIComponent(input.matchId)}/analogue-proposals`,
    {
      idempotencyKey: input.idempotencyKey,
      body: {
        command: { analogue_class: input.analogueClass },
        reason: input.reason,
      },
    },
  );
}

export function finalizeNomenclatureAnalogue(
  context: RequestContext,
  input: {
    projectId: string;
    matchId: string;
    reason: string;
    idempotencyKey: string;
  },
): Promise<NomenclatureMatchView> {
  return mutate(
    context,
    `/projects/${encodeURIComponent(input.projectId)}/nomenclature/${encodeURIComponent(input.matchId)}/finalize`,
    {
      idempotencyKey: input.idempotencyKey,
      body: { reason: input.reason },
    },
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

export function getScenarioContext(
  context: RequestContext,
  projectId: string,
  snapshotId?: string,
  signal?: AbortSignal,
): Promise<ScenarioContext> {
  return request(
    context,
    `/projects/${encodeURIComponent(projectId)}/scenarios/context`,
    { snapshot_id: snapshotId },
    signal,
  );
}

export function executeScenario(
  context: RequestContext,
  input: {
    projectId: string;
    snapshotId: string;
    scenarioKey: string;
    reason: string;
    idempotencyKey: string;
  },
): Promise<ScenarioExecution> {
  return mutate(
    context,
    `/projects/${encodeURIComponent(input.projectId)}/scenarios/calculate`,
    {
      idempotencyKey: input.idempotencyKey,
      body: {
        command: {
          snapshot_id: input.snapshotId,
          scenario_key: input.scenarioKey,
        },
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
