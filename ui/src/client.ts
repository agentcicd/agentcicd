import { inspectionSchemaVersion, type AnnotationProgress, type AnnotationRequestRead, type AnnotationReviewRead, type AnnotationTaskRead, type InspectionClient, type JsonValue, type PoolLeaseRead, type PoolNodeRead, type ProgressContent, type ProjectInspection, type RateLimitLeaseRead, type ReportContent, type RunGraphContent, type RunInspection, type RunLogsContent, type RunTableRows, type InspectionResource, type RunReference } from "./types";

type JsonObject = Record<string, JsonValue>;

function isJsonObject(value: JsonValue): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function validateInspectionEnvelope(value: JsonValue): JsonObject {
  if (!isJsonObject(value) || value.schema_version !== inspectionSchemaVersion) {
    throw new Error("The server returned an unsupported inspection response.");
  }
  return value;
}

export class HttpInspectionClient implements InspectionClient {
  public constructor(private readonly baseUrl = "/inspection/v1", private readonly requestInit?: RequestInit) {}

  public project(projectId: string) { return this.get<ProjectInspection>(`/projects/${encodeURIComponent(projectId)}`); }
  public recipes(projectId: string) { return this.get<{ items: InspectionResource[] }>(`/projects/${encodeURIComponent(projectId)}/recipes`); }
  public recipe(projectId: string, recipeId: string) { return this.get<{ recipe: InspectionResource & { source_text?: string; path?: string } }>(`/projects/${encodeURIComponent(projectId)}/recipes/${encodeURIComponent(recipeId)}`); }
  public fixtures(projectId: string) { return this.get<{ items: InspectionResource[] }>(`/projects/${encodeURIComponent(projectId)}/fixtures`); }
  public fixture(projectId: string, fixtureId: string) { return this.get<{ fixture: InspectionResource & { source_text?: string; path?: string } }>(`/projects/${encodeURIComponent(projectId)}/fixtures/${encodeURIComponent(fixtureId)}`); }
  public inputs(projectId: string) { return this.get<{ items: Array<Record<string, JsonValue>> }>(`/projects/${encodeURIComponent(projectId)}/inputs`); }
  public secrets(projectId: string) { return this.get<{ items: Array<Record<string, JsonValue>> }>(`/projects/${encodeURIComponent(projectId)}/secrets`); }
  public runs(projectId: string) { return this.get<{ items: RunReference[] }>(`/projects/${encodeURIComponent(projectId)}/runs`); }
  public run(runId: string) { return this.get<RunInspection>(`/runs/${encodeURIComponent(runId)}`); }
  public progress(runId: string) { return this.get<ProgressContent>(`/runs/${encodeURIComponent(runId)}/progress`); }
  public logs(runId: string) { return this.get<RunLogsContent>(`/runs/${encodeURIComponent(runId)}/logs`); }
  public graph(runId: string) { return this.get<RunGraphContent>(`/runs/${encodeURIComponent(runId)}/graph`); }
  public report(runId: string) { return this.get<ReportContent>(`/runs/${encodeURIComponent(runId)}/report`); }
  public tables(runId: string) { return this.get<{ items: Array<Record<string, JsonValue>> }>(`/runs/${encodeURIComponent(runId)}/tables`); }
  public tableRows(runId: string, tableName: string, page: number, pageSize: number) {
    return this.get<RunTableRows>(`/runs/${encodeURIComponent(runId)}/tables/${encodeURIComponent(tableName)}/rows?page=${page}&page_size=${pageSize}`);
  }
  public traces(runId: string) { return this.get<{ items: Array<{ id: string; status: string }> }>(`/runs/${encodeURIComponent(runId)}/traces`); }
  public traceSpans(runId: string, traceId: string, page: number, pageSize: number) { return this.get<{ records: Array<Record<string, JsonValue>> }>(`/runs/${encodeURIComponent(runId)}/traces/${encodeURIComponent(traceId)}/spans?page=${page}&page_size=${pageSize}`); }
  public annotationRequests(runId: string) { return this.get<{ items: AnnotationRequestRead[]; total: number }>(`/runs/${encodeURIComponent(runId)}/annotations/requests`); }
  public annotationRequest(runId: string, requestId: string) { return this.get<{ request: AnnotationRequestRead; progress: AnnotationProgress }>(`/runs/${encodeURIComponent(runId)}/annotations/requests/${encodeURIComponent(requestId)}`); }
  public annotationTasks(runId: string, requestId: string) { return this.get<{ request_id: string; tasks: AnnotationTaskRead[]; total: number; completed: number }>(`/runs/${encodeURIComponent(runId)}/annotations/requests/${encodeURIComponent(requestId)}/tasks`); }
  public annotationTask(runId: string, requestId: string, taskId: string) { return this.get<{ request_id: string; task: AnnotationTaskRead }>(`/runs/${encodeURIComponent(runId)}/annotations/requests/${encodeURIComponent(requestId)}/tasks/${encodeURIComponent(taskId)}`); }
  public submitAnnotationReview(runId: string, requestId: string, taskId: string, payload: { reviewer_id: string; result: Record<string, JsonValue> }) {
    return this.post<{ review: AnnotationReviewRead; progress: AnnotationProgress }>(`/runs/${encodeURIComponent(runId)}/annotations/requests/${encodeURIComponent(requestId)}/tasks/${encodeURIComponent(taskId)}/reviews`, payload);
  }
  public finalizeAnnotationRequest(runId: string, requestId: string) {
    return this.post<{ request_id: string; total_tasks: number; completed_tasks: number; results_path: string }>(`/runs/${encodeURIComponent(runId)}/annotations/requests/${encodeURIComponent(requestId)}/finalize`, {});
  }
  public runtimePools(runId: string) { return this.get<{ run_id: string; nodes: PoolNodeRead[]; leases: PoolLeaseRead[] }>(`/runs/${encodeURIComponent(runId)}/runtime/pools`); }
  public runtimeRateLimits(runId: string) { return this.get<{ run_id: string; leases: RateLimitLeaseRead[] }>(`/runs/${encodeURIComponent(runId)}/runtime/rate-limits`); }

  private async get<T>(path: string): Promise<T> {
    return this.request<T>("GET", path);
  }

  private async post<T>(path: string, payload: JsonValue): Promise<T> {
    return this.request<T>("POST", path, payload);
  }

  private async request<T>(method: "GET" | "POST", path: string, payload?: JsonValue): Promise<T> {
    const base = new URL(this.baseUrl, window.location.origin);
    const request = new URL(path, window.location.origin);
    base.pathname = `${base.pathname.replace(/\/$/, "")}${request.pathname}`;
    request.searchParams.forEach((value, key) => base.searchParams.set(key, value));
    const headers = new Headers(this.requestInit?.headers);
    if (method === "POST") headers.set("Content-Type", "application/json");
    const response = await fetch(`${base.pathname}${base.search}`, {
      credentials: "same-origin",
      ...this.requestInit,
      method,
      headers,
      body: method === "POST" ? JSON.stringify(payload ?? {}) : undefined,
    });
    if (!response.ok) throw new Error(`Inspection request failed (${response.status}).`);
    return validateInspectionEnvelope(await response.json() as JsonValue) as T;
  }
}
