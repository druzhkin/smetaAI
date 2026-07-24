import type {
  ApprovalState,
  ProjectPortfolioPage,
  ProjectRecordPage,
  ProjectRecordSection,
  ProjectView,
  ProjectWorkbench,
  WorkItemPage,
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
