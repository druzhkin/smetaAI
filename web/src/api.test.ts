import { afterEach, describe, expect, it, vi } from "vitest";

import {
  confirmDocumentSet,
  createProject,
  decideWorkItem,
  uploadDocument,
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
});
