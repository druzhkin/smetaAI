import { afterEach, describe, expect, it, vi } from "vitest";

import { decideWorkItem, type RequestContext } from "./api";

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
});
