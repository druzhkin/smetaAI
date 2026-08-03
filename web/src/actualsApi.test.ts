import { afterEach, describe, expect, it, vi } from "vitest";

import {
  compareActualToForecast,
  decideActual,
  decideCalibrationExample,
  decideVariance,
  getActualsContext,
  listActualForecastCandidates,
  recordActual,
  type RequestContext,
} from "./api";

const context: RequestContext = {
  apiBasePath: "/v1",
  authorizationHeaders: () => ({ Authorization: "Bearer test-token" }),
};

afterEach(() => {
  vi.unstubAllGlobals();
});

function ok(body: unknown = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("actuals API bindings", () => {
  it("loads the exact policy metric context", async () => {
    const fetchMock = vi.fn().mockResolvedValue(ok());
    vi.stubGlobal("fetch", fetchMock);

    await getActualsContext(context, "project/1", {
      metric: "unit_rate",
      cursor: "context-page-2",
      limit: 20,
    });

    const [url, init] = fetchMock.mock.calls[0] as [URL, RequestInit];
    expect(url.pathname).toBe("/v1/projects/project%2F1/actuals/context");
    expect(url.searchParams.get("metric")).toBe("unit_rate");
    expect(url.searchParams.get("cursor")).toBe("context-page-2");
    expect(url.searchParams.get("limit")).toBe("20");
    expect(init.method).toBe("GET");
  });

  it("loads released forecasts lazily for one exact actual", async () => {
    const fetchMock = vi.fn().mockResolvedValue(ok());
    vi.stubGlobal("fetch", fetchMock);

    await listActualForecastCandidates(context, {
      projectId: "project/1",
      actualId: "actual/1",
      cursor: "forecast-page-2",
      limit: 10,
    });

    const [url, init] = fetchMock.mock.calls[0] as [URL, RequestInit];
    expect(url.pathname).toBe(
      "/v1/projects/project%2F1/actuals/actual%2F1/forecast-candidates",
    );
    expect(url.searchParams.get("cursor")).toBe("forecast-page-2");
    expect(url.searchParams.get("limit")).toBe("10");
    expect(init.method).toBe("GET");
  });

  it("binds fact creation to evidence, observation version, and policy", async () => {
    const fetchMock = vi.fn().mockResolvedValue(ok());
    vi.stubGlobal("fetch", fetchMock);

    await recordActual(context, {
      projectId: "project-1",
      metric: "unit_rate",
      sourceObservationId: "observation-1",
      expectedObservationCreatedAt: "2026-07-29T08:00:00Z",
      actualsPolicyVersionId: "actuals-policy-7",
      reason: "Controlled invoice selected",
      idempotencyKey: "actual-operation-1",
    });

    const [url, init] = fetchMock.mock.calls[0] as [URL, RequestInit];
    expect(url.pathname).toBe("/v1/projects/project-1/actuals");
    expect(init.headers).toMatchObject({
      Authorization: "Bearer test-token",
      "Idempotency-Key": "actual-operation-1",
    });
    expect(JSON.parse(String(init.body))).toEqual({
      draft: {
        metric: "unit_rate",
        source_observation_id: "observation-1",
        expected_observation_created_at: "2026-07-29T08:00:00Z",
      },
      actuals_policy_version_id: "actuals-policy-7",
      reason: "Controlled invoice selected",
    });
  });

  it("binds every decision to entity and task optimistic versions", async () => {
    const fetchMock = vi.fn().mockImplementation(async () => ok());
    vi.stubGlobal("fetch", fetchMock);

    await decideActual(context, {
      projectId: "project-1",
      actualId: "actual/1",
      decision: "REJECTED",
      expectedActualCreatedAt: "2026-07-29T08:00:00Z",
      expectedTaskUpdatedAt: "2026-07-29T08:01:00Z",
      reason: "Evidence linkage failed review",
      idempotencyKey: "actual-decision-1",
    });
    await decideVariance(context, {
      projectId: "project-1",
      varianceId: "variance/1",
      decision: "APPROVED",
      expectedVarianceCreatedAt: "2026-07-29T08:02:00Z",
      expectedTaskUpdatedAt: "2026-07-29T08:03:00Z",
      reason: "Cause and arithmetic independently reproduced",
      idempotencyKey: "variance-decision-1",
    });
    await decideCalibrationExample(context, {
      projectId: "project-1",
      exampleId: "calibration/1",
      decision: "REJECTED",
      expectedExampleCreatedAt: "2026-07-29T08:04:00Z",
      expectedTaskUpdatedAt: "2026-07-29T08:05:00Z",
      reason: "Methodology scope mismatch",
      idempotencyKey: "calibration-decision-1",
    });

    const calls = fetchMock.mock.calls as [URL, RequestInit][];
    expect(calls.map(([url]) => url.pathname)).toEqual([
      "/v1/projects/project-1/actuals/actual%2F1/decision",
      "/v1/projects/project-1/actuals/variances/variance%2F1/decision",
      "/v1/projects/project-1/calibration/calibration%2F1/decision",
    ]);
    expect(JSON.parse(String(calls[0]![1].body))).toEqual({
      command: {
        decision: "REJECTED",
        expected_actual_created_at: "2026-07-29T08:00:00Z",
        expected_task_updated_at: "2026-07-29T08:01:00Z",
      },
      reason: "Evidence linkage failed review",
    });
    expect(JSON.parse(String(calls[1]![1].body))).toEqual({
      command: {
        decision: "APPROVED",
        expected_variance_created_at: "2026-07-29T08:02:00Z",
        expected_task_updated_at: "2026-07-29T08:03:00Z",
      },
      reason: "Cause and arithmetic independently reproduced",
    });
    expect(JSON.parse(String(calls[2]![1].body))).toEqual({
      command: {
        decision: "REJECTED",
        expected_example_created_at: "2026-07-29T08:04:00Z",
        expected_task_updated_at: "2026-07-29T08:05:00Z",
      },
      reason: "Methodology scope mismatch",
    });
  });

  it("submits classification only against a released forecast and policy", async () => {
    const fetchMock = vi.fn().mockResolvedValue(ok());
    vi.stubGlobal("fetch", fetchMock);

    await compareActualToForecast(context, {
      projectId: "project-1",
      actualId: "actual-1",
      forecastId: "forecast-1",
      releasedByDecisionId: "release-decision-1",
      varianceReason: "PRICE_CHANGE",
      varianceReasonDetail: "Supplier indexation evidenced by purchase order",
      expectedActualCreatedAt: "2026-07-29T08:00:00Z",
      actualsPolicyVersionId: "actuals-policy-7",
      reason: "Classify released forecast against verified fact",
      idempotencyKey: "comparison-operation-1",
    });

    const [url, init] = fetchMock.mock.calls[0] as [URL, RequestInit];
    expect(url.pathname).toBe(
      "/v1/projects/project-1/actuals/actual-1/compare",
    );
    expect(JSON.parse(String(init.body))).toEqual({
      command: {
        forecast_id: "forecast-1",
        released_by_decision_id: "release-decision-1",
        reason: "PRICE_CHANGE",
        reason_detail: "Supplier indexation evidenced by purchase order",
        expected_actual_created_at: "2026-07-29T08:00:00Z",
        actuals_policy_version_id: "actuals-policy-7",
      },
      reason: "Classify released forecast against verified fact",
    });
  });
});
