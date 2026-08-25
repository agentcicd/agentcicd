import { inspectionSchemaVersion, type InspectionClient, type JsonValue, type ProgressContent, type ProjectInspection, type ReportContent, type RunInspection, type RunTableRows, type InspectionResource, type RunReference } from "./types";

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
  public report(runId: string) { return this.get<ReportContent>(`/runs/${encodeURIComponent(runId)}/report`); }
  public tables(runId: string) { return this.get<{ items: Array<Record<string, JsonValue>> }>(`/runs/${encodeURIComponent(runId)}/tables`); }
  public tableRows(runId: string, tableName: string, page: number, pageSize: number) {
    return this.get<RunTableRows>(`/runs/${encodeURIComponent(runId)}/tables/${encodeURIComponent(tableName)}/rows?page=${page}&page_size=${pageSize}`);
  }
  public traces(runId: string) { return this.get<{ items: Array<{ id: string; status: string }> }>(`/runs/${encodeURIComponent(runId)}/traces`); }
  public traceSpans(runId: string, traceId: string, page: number, pageSize: number) { return this.get<{ records: Array<Record<string, JsonValue>> }>(`/runs/${encodeURIComponent(runId)}/traces/${encodeURIComponent(traceId)}/spans?page=${page}&page_size=${pageSize}`); }

  private async get<T>(path: string): Promise<T> {
    const base = new URL(this.baseUrl, window.location.origin);
    const request = new URL(path, window.location.origin);
    base.pathname = `${base.pathname.replace(/\/$/, "")}${request.pathname}`;
    request.searchParams.forEach((value, key) => base.searchParams.set(key, value));
    const response = await fetch(`${base.pathname}${base.search}`, { credentials: "same-origin", ...this.requestInit });
    if (!response.ok) throw new Error(`Inspection request failed (${response.status}).`);
    return validateInspectionEnvelope(await response.json() as JsonValue) as T;
  }
}
