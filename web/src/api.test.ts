import { afterEach, describe, expect, it, vi } from "vitest";

import {
  applyQuantityManualChange,
  assessNomenclature,
  attemptRelease,
  confirmDocumentSet,
  createBoqLine,
  createProject,
  calculateRiskReserve,
  decideRiskItem,
  decideManualEvidence,
  decideWorkItem,
  evaluatePriceItem,
  executeCurrentCalculation,
  executeScenario,
  finalizeNomenclatureAnalogue,
  getBoqAuthoringContext,
  getBoqLineReview,
  getCalculationContext,
  getNomenclatureContext,
  getNomenclatureReview,
  getPriceQuoteCandidate,
  getReconciliationContext,
  getReleaseGates,
  getScenarioContext,
  normalizePriceQuote,
  proposeNomenclatureAnalogue,
  proposeQuantityChange,
  reconcileEvidence,
  recordManualEvidence,
  recordPriceQuoteFromObservation,
  resolveConflict,
  runScopeCompleteness,
  submitRiskItem,
  uploadDocument,
  verifyBoqLine,
  type RequestContext,
} from "./api";

const context: RequestContext = {
  apiBasePath: "/v1",
  authorizationHeaders: () => ({ Authorization: "Bearer test-token" }),
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("controlled mutations", () => {
  it("sends the optimistic version and stable idempotency key", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          approval_id: "approval-1",
          task_id: "task-1",
          decision: "APPROVED",
          decided_by: "reviewer-1",
        }),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await decideWorkItem(context, {
      projectId: "project/with-slash",
      taskId: "task-1",
      decision: "APPROVED",
      reason: "Independent evidence reviewed",
      expectedTaskUpdatedAt: "2026-07-24T17:00:00Z",
      evidenceIds: ["observation-1"],
      idempotencyKey: "operation-1",
    });

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0] as [URL, RequestInit];
    expect(url.pathname).toBe(
      "/v1/projects/project%2Fwith-slash/approvals/task-1/decision",
    );
    expect(init.method).toBe("POST");
    expect(init.headers).toMatchObject({
      Authorization: "Bearer test-token",
      "Idempotency-Key": "operation-1",
      "Content-Type": "application/json",
    });
    expect(JSON.parse(String(init.body))).toEqual({
      decision: "APPROVED",
      reason: "Independent evidence reviewed",
      expected_task_updated_at: "2026-07-24T17:00:00Z",
      evidence_ids: ["observation-1"],
      related_change_ids: [],
    });
  });

  it("sends an auditable project creation command", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: "project-1" }), {
        status: 201,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await createProject(context, {
      code: "TG-2026-041",
      name: "Verified tender",
      reason: "Registered from procurement notice 17",
      idempotencyKey: "project-operation-1",
    });

    const [url, init] = fetchMock.mock.calls[0] as [URL, RequestInit];
    expect(url.pathname).toBe("/v1/projects");
    expect(init.headers).toMatchObject({
      Authorization: "Bearer test-token",
      "Idempotency-Key": "project-operation-1",
      "Content-Type": "application/json",
    });
    expect(JSON.parse(String(init.body))).toEqual({
      code: "TG-2026-041",
      name: "Verified tender",
      reason: "Registered from procurement notice 17",
    });
  });

  it("uploads document bytes without overriding the multipart boundary", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          upload_id: "upload-1",
          project_id: "project-1",
          status: "QUARANTINED",
        }),
        {
          status: 202,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const file = new File(["trusted only after scan"], "terms.txt", {
      type: "text/plain",
    });

    await uploadDocument(context, {
      projectId: "project/1",
      logicalKey: "tender-terms",
      title: "Tender terms",
      documentType: "TENDER_TERMS",
      revisionLabel: "R1",
      reason: "Received through controlled procurement channel",
      file,
      critical: true,
      makeCandidateCurrent: true,
      idempotencyKey: "upload-operation-1",
    });

    const [url, init] = fetchMock.mock.calls[0] as [URL, RequestInit];
    expect(url.pathname).toBe("/v1/projects/project%2F1/documents");
    expect(init.method).toBe("POST");
    expect(init.headers).toMatchObject({
      Authorization: "Bearer test-token",
      Accept: "application/json",
      "Idempotency-Key": "upload-operation-1",
    });
    expect(init.headers).not.toHaveProperty("Content-Type");
    expect(init.body).toBeInstanceOf(FormData);
    const body = init.body as FormData;
    expect(body.get("logical_key")).toBe("tender-terms");
    expect(body.get("critical")).toBe("true");
    expect(body.get("make_candidate_current")).toBe("true");
    expect(body.get("upload")).toBeInstanceOf(File);
    expect((body.get("upload") as File).name).toBe("terms.txt");
  });

  it("binds document-set confirmation to the exact candidate", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          id: "project-1",
          current_document_set_revision_id: "document-set-1",
        }),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await confirmDocumentSet(context, {
      projectId: "project-1",
      documentSetId: "document-set-1",
      reason: "Independently reconciled the revision register",
      idempotencyKey: "document-set-operation-1",
    });

    const [url, init] = fetchMock.mock.calls[0] as [URL, RequestInit];
    expect(url.pathname).toBe("/v1/projects/project-1/document-set/confirm");
    expect(init.headers).toMatchObject({
      Authorization: "Bearer test-token",
      "Idempotency-Key": "document-set-operation-1",
      "Content-Type": "application/json",
    });
    expect(JSON.parse(String(init.body))).toEqual({
      candidate_document_set_revision_id: "document-set-1",
      reason: "Independently reconciled the revision register",
    });
  });

  it("binds conflict resolution to conflict and task versions", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          conflict: { conflict_id: "conflict-1", status: "VERIFIED" },
          verified_observation: { observation_id: "observation-derived" },
        }),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await resolveConflict(context, {
      projectId: "project-1",
      conflictId: "conflict-1",
      selectedObservationId: "observation-2",
      resolutionReason: "Native table checked against signed source",
      expectedConflictUpdatedAt: "2026-07-24T18:00:00Z",
      expectedTaskUpdatedAt: "2026-07-24T18:00:01Z",
      idempotencyKey: "conflict-operation-1",
    });

    const [url, init] = fetchMock.mock.calls[0] as [URL, RequestInit];
    expect(url.pathname).toBe(
      "/v1/projects/project-1/evidence/conflicts/conflict-1/resolve",
    );
    expect(init.headers).toMatchObject({
      Authorization: "Bearer test-token",
      "Idempotency-Key": "conflict-operation-1",
      "Content-Type": "application/json",
    });
    expect(JSON.parse(String(init.body))).toEqual({
      selected_observation_id: "observation-2",
      resolution_reason: "Native table checked against signed source",
      expected_conflict_updated_at: "2026-07-24T18:00:00Z",
      expected_task_updated_at: "2026-07-24T18:00:01Z",
    });
  });

  it("binds risk submission, review, and reserve to exact server context", async () => {
    const fetchMock = vi.fn().mockImplementation(
      async () =>
        new Response(JSON.stringify({}), {
          status: 201,
          headers: { "Content-Type": "application/json" },
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const draft = {
      risk_key: "supplier-delay",
      description: "Supplier delay",
      probability: "0.2",
      impact_min: "10",
      impact_most_likely: "20",
      impact_max: "30",
      currency: "RUB",
      observation_ids: ["observation-1"],
      correlated: false,
      correlation_group: null,
      mitigation_cost_input_id: null,
    };

    await submitRiskItem(context, {
      projectId: "project/1",
      draft,
      expectedDocumentSetRevisionId: "document-set-1",
      riskModelVersionId: "risk-model-1",
      reason: "Bind exact risk evidence",
      idempotencyKey: "risk-submit-operation",
    });
    await decideRiskItem(context, {
      projectId: "project/1",
      riskItemId: "risk/1",
      decision: "APPROVED",
      expectedRiskUpdatedAt: "2026-07-26T10:00:00Z",
      expectedTaskUpdatedAt: "2026-07-26T10:00:01Z",
      reason: "Independently verify the risk evidence",
      idempotencyKey: "risk-review-operation",
    });
    await calculateRiskReserve(context, {
      projectId: "project/1",
      expectedDocumentSetRevisionId: "document-set-1",
      riskModelVersionId: "risk-model-1",
      reason: "Run deterministic independent reserve replay",
      idempotencyKey: "risk-calculate-operation",
    });

    const [submitUrl, submitInit] = fetchMock.mock.calls[0] as [
      URL,
      RequestInit,
    ];
    expect(submitUrl.pathname).toBe("/v1/projects/project%2F1/risks");
    expect(JSON.parse(String(submitInit.body))).toEqual({
      draft,
      expected_document_set_revision_id: "document-set-1",
      risk_model_version_id: "risk-model-1",
      reason: "Bind exact risk evidence",
    });
    const [reviewUrl, reviewInit] = fetchMock.mock.calls[1] as [
      URL,
      RequestInit,
    ];
    expect(reviewUrl.pathname).toBe(
      "/v1/projects/project%2F1/risks/risk%2F1/decision",
    );
    expect(JSON.parse(String(reviewInit.body))).toEqual({
      command: {
        decision: "APPROVED",
        expected_risk_updated_at: "2026-07-26T10:00:00Z",
        expected_task_updated_at: "2026-07-26T10:00:01Z",
      },
      reason: "Independently verify the risk evidence",
    });
    const [calculationUrl, calculationInit] = fetchMock.mock.calls[2] as [
      URL,
      RequestInit,
    ];
    expect(calculationUrl.pathname).toBe(
      "/v1/projects/project%2F1/risks/calculate",
    );
    expect(JSON.parse(String(calculationInit.body))).toEqual({
      expected_document_set_revision_id: "document-set-1",
      risk_model_version_id: "risk-model-1",
      reason: "Run deterministic independent reserve replay",
    });
  });

  it("records manual evidence against the exact governed source basis", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          observation_id: "observation-manual-1",
          status: "UNVERIFIED",
        }),
        {
          status: 201,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await recordManualEvidence(context, {
      projectId: "project/1",
      policyVersionId: "manual-policy-1",
      fieldName: "pipeline.nominal_diameter",
      value: "500.00",
      unit: "mm",
      sourcePriority: 10,
      documentId: "document/1",
      documentRevisionId: "revision/1",
      originalObjectHash: "a".repeat(64),
      locatorKind: "PDF_PAGE_REGION",
      locator: "page=12;x=0.1;y=0.2;width=0.4;height=0.1",
      page: 12,
      table: null,
      sheet: null,
      cellOrRange: null,
      observedAt: "2026-07-24T18:00:00.000Z",
      reason: "Corrected against drawing revision A",
      idempotencyKey: "manual-evidence-operation-1",
    });

    const [url, init] = fetchMock.mock.calls[0] as [URL, RequestInit];
    expect(url.pathname).toBe("/v1/projects/project%2F1/evidence/observations");
    expect(init.headers).toMatchObject({
      "Idempotency-Key": "manual-evidence-operation-1",
    });
    expect(JSON.parse(String(init.body))).toEqual({
      draft: {
        field_name: "pipeline.nominal_diameter",
        value: "500.00",
        unit: "mm",
        method: "MANUAL",
        method_version: "manual-policy-1",
        source_priority: 10,
        location: {
          document_id: "document/1",
          document_revision_id: "revision/1",
          original_object_hash: "a".repeat(64),
          locator_kind: "PDF_PAGE_REGION",
          locator: "page=12;x=0.1;y=0.2;width=0.4;height=0.1",
          page: 12,
          table: null,
          sheet: null,
          cell_or_range: null,
        },
        observed_at: "2026-07-24T18:00:00.000Z",
        confidence: null,
        adapter_qualification_id: null,
        basis_metadata: {},
      },
      reason: "Corrected against drawing revision A",
    });
  });

  it("binds manual evidence review to the exact task version", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          approval_id: "approval-1",
          decision: "APPROVED",
        }),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await decideManualEvidence(context, {
      projectId: "project/1",
      observationId: "observation/1",
      decision: "APPROVED",
      reason: "Independently checked the source page and exact locator",
      expectedTaskUpdatedAt: "2026-07-24T18:00:01Z",
      idempotencyKey: "manual-review-operation-1",
    });

    const [url, init] = fetchMock.mock.calls[0] as [URL, RequestInit];
    expect(url.pathname).toBe(
      "/v1/projects/project%2F1/evidence/observations/observation%2F1/manual-review/decision",
    );
    expect(init.headers).toMatchObject({
      "Idempotency-Key": "manual-review-operation-1",
    });
    expect(JSON.parse(String(init.body))).toEqual({
      decision: "APPROVED",
      reason: "Independently checked the source page and exact locator",
      expected_task_updated_at: "2026-07-24T18:00:01Z",
    });
  });

  it("binds reconciliation and BoQ mutations to governed server context", async () => {
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify({ status: "OK" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await getReconciliationContext(context, "project/1", "pipeline/diameter");
    await reconcileEvidence(context, {
      projectId: "project/1",
      observationIds: ["observation/1", "observation/2"],
      reconciliationVersionId: "reconciliation/r3",
      reason: "Independent extraction methods reconciled",
      idempotencyKey: "reconciliation-operation",
    });
    await getBoqAuthoringContext(context, "project/1", "boq/line");
    await createBoqLine(context, {
      projectId: "project/1",
      lineKey: "WBS-01-L001",
      wbsNodeId: "WBS-01",
      workCode: "PIPE-LAYING",
      description: "Pipeline installation",
      unit: "m",
      evidenceObservationIds: ["observation/boq"],
      costComponents: [
        {
          semantic_key: "material/pipe",
          category: "MATERIAL",
          basis_kind: "MARKET",
          factor_ids: ["waste/factor"],
          sign: 1,
        },
      ],
      criticalQuantity: true,
      reason: "Created from verified BoQ extraction",
      idempotencyKey: "boq-create-operation",
    });
    await getBoqLineReview(context, "project/1", "line/1");
    await verifyBoqLine(context, {
      projectId: "project/1",
      lineId: "line/1",
      expectedLineUpdatedAt: "2026-07-24T18:30:00Z",
      reason: "Independent WBS and evidence review completed",
      idempotencyKey: "boq-verify-operation",
    });
    await runScopeCompleteness(context, {
      projectId: "project/1",
      wbsNodeId: "WBS-01",
      reason: "Run approved scope dependency rules",
      idempotencyKey: "scope-operation",
    });

    const calls = fetchMock.mock.calls as [URL, RequestInit][];
    expect(calls[0]?.[0].pathname).toBe(
      "/v1/projects/project%2F1/evidence/reconciliation-context",
    );
    expect(calls[0]?.[0].searchParams.get("field_name")).toBe(
      "pipeline/diameter",
    );
    expect(calls[1]?.[0].pathname).toBe(
      "/v1/projects/project%2F1/evidence/reconcile",
    );
    expect(JSON.parse(String(calls[1]?.[1].body))).toEqual({
      observation_ids: ["observation/1", "observation/2"],
      reconciliation_version_id: "reconciliation/r3",
      reason: "Independent extraction methods reconciled",
    });
    expect(calls[2]?.[0].pathname).toBe(
      "/v1/projects/project%2F1/boq/authoring-context",
    );
    expect(calls[3]?.[0].pathname).toBe("/v1/projects/project%2F1/boq/lines");
    expect(JSON.parse(String(calls[3]?.[1].body))).toEqual({
      draft: {
        line_key: "WBS-01-L001",
        wbs_node_id: "WBS-01",
        work_code: "PIPE-LAYING",
        description: "Pipeline installation",
        unit: "m",
        evidence_observation_ids: ["observation/boq"],
        cost_components: [
          {
            semantic_key: "material/pipe",
            category: "MATERIAL",
            basis_kind: "MARKET",
            factor_ids: ["waste/factor"],
            sign: 1,
          },
        ],
        critical_quantity: true,
      },
      reason: "Created from verified BoQ extraction",
    });
    expect(calls[4]?.[0].pathname).toBe(
      "/v1/projects/project%2F1/boq/lines/line%2F1/review",
    );
    expect(JSON.parse(String(calls[5]?.[1].body))).toEqual({
      expected_line_updated_at: "2026-07-24T18:30:00Z",
      reason: "Independent WBS and evidence review completed",
    });
    expect(calls[5]?.[1].headers).toMatchObject({
      "Idempotency-Key": "boq-verify-operation",
    });
    expect(calls[6]?.[0].pathname).toBe(
      "/v1/projects/project%2F1/boq/scope-evaluations",
    );
  });

  it("never sends client-calculated nomenclature conclusions", async () => {
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify({ status: "OK" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await getNomenclatureContext(context, "project/1", {
      catalogQuery: "pump/500",
      evidenceFieldName: "technical/attributes",
    });
    await assessNomenclature(context, {
      projectId: "project/1",
      sourceItemId: "material/pipe",
      canonicalItemId: "catalog/pipe-500",
      sourceAttributesObservationId: "observation/attributes",
      reason: "Assess exact governed attributes",
      idempotencyKey: "nomenclature-assess-operation",
    });
    await getNomenclatureReview(context, "project/1", "match/1");
    await proposeNomenclatureAnalogue(context, {
      projectId: "project/1",
      matchId: "match/1",
      analogueClass: "FUNCTIONAL_ANALOGUE",
      reason: "All mismatches covered by equivalence rules",
      idempotencyKey: "nomenclature-propose-operation",
    });
    await finalizeNomenclatureAnalogue(context, {
      projectId: "project/1",
      matchId: "match/2",
      reason: "All independent approval tasks are complete",
      idempotencyKey: "nomenclature-finalize-operation",
    });

    const calls = fetchMock.mock.calls as [URL, RequestInit][];
    expect(calls[0]?.[0].pathname).toBe(
      "/v1/projects/project%2F1/nomenclature/context",
    );
    expect(calls[0]?.[0].searchParams.get("catalog_query")).toBe("pump/500");
    expect(JSON.parse(String(calls[1]?.[1].body))).toEqual({
      draft: {
        source_item_id: "material/pipe",
        canonical_item_id: "catalog/pipe-500",
        source_attributes_observation_id: "observation/attributes",
      },
      reason: "Assess exact governed attributes",
    });
    expect(String(calls[1]?.[1].body)).not.toContain("match_class");
    expect(calls[2]?.[0].pathname).toBe(
      "/v1/projects/project%2F1/nomenclature/match%2F1/review",
    );
    expect(JSON.parse(String(calls[3]?.[1].body))).toEqual({
      command: { analogue_class: "FUNCTIONAL_ANALOGUE" },
      reason: "All mismatches covered by equivalence rules",
    });
    expect(JSON.parse(String(calls[4]?.[1].body))).toEqual({
      reason: "All independent approval tasks are complete",
    });
  });

  it("submits and applies an exact governed quantity revision", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            change_id: "manual-change-1",
            status: "PENDING_APPROVAL",
          }),
          {
            status: 201,
            headers: { "Content-Type": "application/json" },
          },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            quantity: { quantity_id: "quantity-2" },
            validation: { passed: true },
          }),
          {
            status: 201,
            headers: { "Content-Type": "application/json" },
          },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);
    const submission = {
      draft: {
        value: "101.25",
        unit: "m",
        source_observation_ids: ["observation-2"],
        source_priority: 1,
        rounding_scale: 2,
        waste_factor: "0",
        alternative_quantity_ids: [],
        manual_change_id: null,
      },
      formula: null,
      formula_input_observation_ids: {},
    };

    await proposeQuantityChange(context, {
      projectId: "project/1",
      lineId: "line/1",
      submission,
      reason: "Corrected from independently verified source",
      idempotencyKey: "quantity-proposal-operation",
    });
    await applyQuantityManualChange(context, {
      projectId: "project/1",
      changeId: "manual-change/1",
      reason: "Apply the exact independently approved after-state",
      idempotencyKey: "quantity-apply-operation",
    });

    const [proposalUrl, proposalInit] = fetchMock.mock.calls[0] as [
      URL,
      RequestInit,
    ];
    expect(proposalUrl.pathname).toBe(
      "/v1/projects/project%2F1/boq/lines/line%2F1/quantity-change-proposals",
    );
    expect(proposalInit.headers).toMatchObject({
      "Idempotency-Key": "quantity-proposal-operation",
    });
    expect(JSON.parse(String(proposalInit.body))).toEqual({
      submission,
      reason: "Corrected from independently verified source",
    });

    const [applyUrl, applyInit] = fetchMock.mock.calls[1] as [URL, RequestInit];
    expect(applyUrl.pathname).toBe(
      "/v1/projects/project%2F1/manual-changes/manual-change%2F1/apply",
    );
    expect(applyInit.headers).toMatchObject({
      "Idempotency-Key": "quantity-apply-operation",
    });
    expect(JSON.parse(String(applyInit.body))).toEqual({
      reason: "Apply the exact independently approved after-state",
    });
  });

  it("keeps price evidence, normalization references and evaluation exact", async () => {
    const jsonResponse = (value: unknown, status = 200) =>
      new Response(JSON.stringify(value), {
        status,
        headers: { "Content-Type": "application/json" },
      });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({
          source_observation_id: "observation/quote",
          source_origin_id: "supplier-origin",
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({ quote: { quote_id: "quote-1" } }, 201),
      )
      .mockResolvedValueOnce(
        jsonResponse({ normalized_price_id: "normalized-1" }, 201),
      )
      .mockResolvedValueOnce(
        jsonResponse({ decision_id: "decision-1", status: "RFQ_REQUIRED" }),
      );
    vi.stubGlobal("fetch", fetchMock);

    await getPriceQuoteCandidate(
      context,
      "project/1",
      "item/1",
      "observation/quote",
    );
    await recordPriceQuoteFromObservation(context, {
      projectId: "project/1",
      itemId: "item/1",
      sourceObservationId: "observation/quote",
      reason: "Register the exact verified source",
      idempotencyKey: "quote-operation",
    });
    await normalizePriceQuote(context, {
      projectId: "project/1",
      quoteId: "quote-1",
      unitConversionId: "unit-rule-1",
      fxRateId: null,
      adjustmentIds: ["delivery-1"],
      regionAdjustmentId: "region-rule-1",
      partyAdjustmentId: null,
      paymentAdjustmentId: null,
      reason: "Normalize on the approved basis",
      idempotencyKey: "normalize-operation",
    });
    await evaluatePriceItem(context, {
      projectId: "project/1",
      itemId: "item/1",
      asOf: "2026-07-24",
      reason: "Run exact triangulation",
      idempotencyKey: "evaluate-operation",
    });

    const [candidateUrl, candidateInit] = fetchMock.mock.calls[0] as [
      URL,
      RequestInit,
    ];
    expect(candidateUrl.pathname).toBe(
      "/v1/projects/project%2F1/pricing/items/item%2F1/quote-candidates/observation%2Fquote",
    );
    expect(candidateInit.method).toBe("GET");

    const [recordUrl, recordInit] = fetchMock.mock.calls[1] as [
      URL,
      RequestInit,
    ];
    expect(recordUrl.pathname).toBe(
      "/v1/projects/project%2F1/pricing/items/item%2F1/quotes/from-observation",
    );
    expect(recordInit.headers).toMatchObject({
      "Idempotency-Key": "quote-operation",
    });
    expect(JSON.parse(String(recordInit.body))).toEqual({
      source_observation_id: "observation/quote",
      reason: "Register the exact verified source",
    });

    const [normalizeUrl, normalizeInit] = fetchMock.mock.calls[2] as [
      URL,
      RequestInit,
    ];
    expect(normalizeUrl.pathname).toBe(
      "/v1/projects/project%2F1/pricing/normalize",
    );
    expect(JSON.parse(String(normalizeInit.body))).toEqual({
      command: {
        quote_id: "quote-1",
        unit_conversion_id: "unit-rule-1",
        fx_rate_id: null,
        adjustment_ids: ["delivery-1"],
        region_adjustment_id: "region-rule-1",
        party_adjustment_id: null,
        payment_adjustment_id: null,
      },
      reason: "Normalize on the approved basis",
    });

    const [evaluateUrl, evaluateInit] = fetchMock.mock.calls[3] as [
      URL,
      RequestInit,
    ];
    expect(evaluateUrl.pathname).toBe(
      "/v1/projects/project%2F1/pricing/items/item%2F1/evaluate",
    );
    expect(JSON.parse(String(evaluateInit.body))).toEqual({
      as_of: "2026-07-24",
      reason: "Run exact triangulation",
    });
  });

  it("executes only the exact server-generated calculation candidate", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            project: { id: "project/1" },
            candidate: { candidate_hash: "a".repeat(64) },
            blockers: [],
          }),
          {
            status: 200,
            headers: { "Content-Type": "application/json" },
          },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            project: { id: "project/1", state: "INDEPENDENT_VALIDATION" },
            primary: { grand_total: "1250.00", currency: "RUB" },
          }),
          {
            status: 201,
            headers: { "Content-Type": "application/json" },
          },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    await getCalculationContext(context, "project/1");
    await executeCurrentCalculation(context, {
      projectId: "project/1",
      expectedRowVersion: 17,
      candidateHash: "a".repeat(64),
      reason: "Fix the exact server-generated candidate",
      idempotencyKey: "calculation-operation",
    });

    const [contextUrl, contextInit] = fetchMock.mock.calls[0] as [
      URL,
      RequestInit,
    ];
    expect(contextUrl.pathname).toBe(
      "/v1/projects/project%2F1/calculation-context",
    );
    expect(contextInit.method).toBe("GET");

    const [executeUrl, executeInit] = fetchMock.mock.calls[1] as [
      URL,
      RequestInit,
    ];
    expect(executeUrl.pathname).toBe(
      "/v1/projects/project%2F1/calculations/current",
    );
    expect(executeInit.headers).toMatchObject({
      "Idempotency-Key": "calculation-operation",
    });
    expect(JSON.parse(String(executeInit.body))).toEqual({
      expected_row_version: 17,
      candidate_hash: "a".repeat(64),
      reason: "Fix the exact server-generated candidate",
    });
  });

  it("sends only governed scenario identifiers and the auditable reason", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            project_id: "project/1",
            selected_snapshot_id: "snapshot/1",
            definitions: [{ scenario_id: "supplier-stress" }],
            blockers: [],
          }),
          {
            status: 200,
            headers: { "Content-Type": "application/json" },
          },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            scenario_run_id: "scenario-run-1",
            base_snapshot_id: "snapshot/1",
          }),
          {
            status: 201,
            headers: { "Content-Type": "application/json" },
          },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    await getScenarioContext(context, "project/1", "snapshot/1");
    await executeScenario(context, {
      projectId: "project/1",
      snapshotId: "snapshot/1",
      scenarioKey: "supplier-stress",
      reason: "Evaluate the approved supplier stress scenario",
      idempotencyKey: "scenario-operation",
    });

    const [contextUrl, contextInit] = fetchMock.mock.calls[0] as [
      URL,
      RequestInit,
    ];
    expect(contextUrl.pathname).toBe(
      "/v1/projects/project%2F1/scenarios/context",
    );
    expect(contextUrl.searchParams.get("snapshot_id")).toBe("snapshot/1");
    expect(contextInit.method).toBe("GET");

    const [executeUrl, executeInit] = fetchMock.mock.calls[1] as [
      URL,
      RequestInit,
    ];
    expect(executeUrl.pathname).toBe(
      "/v1/projects/project%2F1/scenarios/calculate",
    );
    expect(executeInit.headers).toMatchObject({
      "Idempotency-Key": "scenario-operation",
    });
    expect(JSON.parse(String(executeInit.body))).toEqual({
      command: {
        snapshot_id: "snapshot/1",
        scenario_key: "supplier-stress",
      },
      reason: "Evaluate the approved supplier stress scenario",
    });
  });

  it("binds release to the exact server gate hash and project version", async () => {
    const gateHash = "b".repeat(64);
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            project: { id: "project/1", row_version: 23 },
            decision: { allowed: true, findings: [] },
            gate_hash: gateHash,
          }),
          {
            status: 200,
            headers: { "Content-Type": "application/json" },
          },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            project: { id: "project/1", state: "APPROVED_FOR_BID" },
            decision: { allowed: true, findings: [] },
          }),
          {
            status: 200,
            headers: { "Content-Type": "application/json" },
          },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    await getReleaseGates(context, "project/1");
    await attemptRelease(context, {
      projectId: "project/1",
      target: "bid",
      expectedRowVersion: 23,
      gateHash,
      reason: "Independent complete hard-stop review",
      idempotencyKey: "release-operation",
    });

    const [gateUrl, gateInit] = fetchMock.mock.calls[0] as [URL, RequestInit];
    expect(gateUrl.pathname).toBe("/v1/projects/project%2F1/release-gates");
    expect(gateInit.method).toBe("GET");

    const [releaseUrl, releaseInit] = fetchMock.mock.calls[1] as [
      URL,
      RequestInit,
    ];
    expect(releaseUrl.pathname).toBe("/v1/projects/project%2F1/release/bid");
    expect(releaseInit.headers).toMatchObject({
      "Idempotency-Key": "release-operation",
    });
    expect(JSON.parse(String(releaseInit.body))).toEqual({
      expected_row_version: 23,
      gate_hash: gateHash,
      reason: "Independent complete hard-stop review",
    });
  });
});
