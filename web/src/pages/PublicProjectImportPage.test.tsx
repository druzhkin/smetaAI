import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";
import type { RuntimeConfig } from "../types";
import { validatePublicProjectImport } from "./PublicProjectImportPage";

const config: RuntimeConfig = {
  environment: "development",
  authentication_mode: "PUBLIC_DEMO",
  oidc_authority: null,
  oidc_client_id: null,
  oidc_scope: "openid profile email",
  api_base_path: "/v1",
  application_version: "0.1.0",
  application_build_reference: null,
  max_upload_bytes: 1_000_000,
  showcase_operator_upload_enabled: true,
};

const projectResponse = {
  id: "project-showcase-1",
  code: "PROJECT-001",
  name: "Проект коллег",
  state: "DRAFT",
};

const uploadResponse = {
  upload_id: "upload-1",
  project_id: "project-showcase-1",
  status: "QUARANTINED",
  object_hash: "a".repeat(64),
  size_bytes: 21,
  original_filename: "project.xlsx",
  uploaded_by: "showcase-operator",
  latest_scan_verdict: null,
  latest_scan_report_hash: null,
  processed_document_id: null,
  processed_document_revision_id: null,
  candidate_document_set_revision_id: null,
  manifest: null,
  failure_code: null,
  processing_attempts: 0,
  processing_lease_expires_at: null,
  processing_dead_lettered_at: null,
  created_at: "2026-08-04T10:00:00Z",
  updated_at: "2026-08-04T10:00:00Z",
};

function completeAndSubmitImportForm() {
  fireEvent.change(screen.getByLabelText(/Операторский код/), {
    target: { value: "operator-key-kept-only-in-memory" },
  });
  fireEvent.change(screen.getByLabelText(/Шифр проекта/), {
    target: { value: "PROJECT-001" },
  });
  fireEvent.change(screen.getByLabelText(/Наименование проекта/), {
    target: { value: "Проект коллег" },
  });
  const file = new File(["project workbook bytes"], "project.xlsx", {
    type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  });
  const fileInput =
    document.querySelector<HTMLInputElement>('input[type="file"]');
  expect(fileInput).not.toBeNull();
  if (fileInput === null) return;
  fireEvent.change(fileInput, { target: { files: [file] } });
  fireEvent.change(screen.getByLabelText(/Основание загрузки/), {
    target: { value: "Получено от заказчика 04.08.2026" },
  });
  const confirmation = screen.getByRole("checkbox");
  fireEvent.click(confirmation);
  expect(confirmation).toBeChecked();
  const submitButton = screen.getByRole("button", {
    name: "Создать проект и загрузить комплект",
  });
  const form = submitButton.closest("form");
  expect(form).not.toBeNull();
  if (form !== null) fireEvent.submit(form);
}

beforeEach(() => {
  window.history.pushState({}, "", "/import");
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  window.history.pushState({}, "", "/");
});

describe("public project import", () => {
  it("creates a real draft and submits the original file to quarantine", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        new Response(JSON.stringify(projectResponse), {
          status: 201,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify(uploadResponse), {
          status: 202,
          headers: { "Content-Type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    render(<App config={config} />);
    completeAndSubmitImportForm();

    await waitFor(() => {
      expect(
        screen.getByRole("heading", { name: "Проект коллег" }),
      ).toBeInTheDocument();
    });
    expect(screen.getByText("QUARANTINED")).toBeInTheDocument();
    expect(screen.getByText(`SHA-256 ${"a".repeat(64)}`)).toBeInTheDocument();
    expect(screen.getByText("BLOCKED")).toBeInTheDocument();

    expect(fetchMock).toHaveBeenCalledTimes(2);
    const createOptions = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(createOptions.headers).toMatchObject({
      Authorization: "Bearer operator-key-kept-only-in-memory",
    });
    const uploadOptions = fetchMock.mock.calls[1]?.[1] as RequestInit;
    expect(uploadOptions.headers).toMatchObject({
      Authorization: "Bearer operator-key-kept-only-in-memory",
    });
    expect(uploadOptions.body).toBeInstanceOf(FormData);
    const body = uploadOptions.body as FormData;
    expect(body.get("title")).toBe("project.xlsx");
    expect(body.get("document_type")).toBe("PROJECT_SOURCE_FILE");
  });

  it("retries only the failed upload with the same idempotency key", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        new Response(JSON.stringify(projectResponse), {
          status: 201,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: "Temporary intake failure" }), {
          status: 503,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify(uploadResponse), {
          status: 202,
          headers: { "Content-Type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    render(<App config={config} />);
    completeAndSubmitImportForm();

    const retry = await screen.findByRole("button", {
      name: /Повторить непереданные файлы \(1\)/,
    });
    const firstUploadOptions = fetchMock.mock.calls[1]?.[1] as RequestInit;
    fireEvent.click(retry);

    await waitFor(() => {
      expect(screen.getByText("QUARANTINED")).toBeInTheDocument();
    });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    const retryOptions = fetchMock.mock.calls[2]?.[1] as RequestInit;
    expect(retryOptions.headers).toMatchObject({
      "Idempotency-Key": (firstUploadOptions.headers as Record<string, string>)[
        "Idempotency-Key"
      ],
    });
  });

  it("blocks the form when the server-side operator intake is disabled", () => {
    render(
      <App config={{ ...config, showcase_operator_upload_enabled: false }} />,
    );

    expect(
      screen.getByRole("heading", { name: "Загрузка ещё не включена" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", {
        name: "Создать проект и загрузить комплект",
      }),
    ).not.toBeInTheDocument();
  });

  it("rejects an oversized file before any network request", () => {
    const error = validatePublicProjectImport({
      enabled: true,
      accessKey: "operator-key",
      projectCode: "PROJECT-001",
      projectName: "Проект",
      reason: "Исходный комплект",
      files: [{ file: new File(["too large"], "large.zip") }],
      acknowledged: true,
      maxUploadBytes: 3,
    });

    expect(error).toBe("large.zip: файл превышает серверный лимит");
  });
});
