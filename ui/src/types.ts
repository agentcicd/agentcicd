export const inspectionSchemaVersion = "inspection-v1" as const;

export type InspectionSource = "local" | "live" | "archive" | "imported";
export type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };

export interface InspectionCapabilities {
  compare: boolean;
  rerun: boolean;
  cancel: boolean;
  annotate: boolean;
  open_external_resource: boolean;
}

export interface InspectionResource {
  id: string;
  name: string;
  status: string;
  details?: Record<string, JsonValue>;
}

export interface ProjectInspection {
  schema_version: typeof inspectionSchemaVersion;
  project: { id: string; name: string; source: InspectionSource; root_label?: string | null };
  resources: {
    recipes: InspectionResource[];
    fixtures: InspectionResource[];
    inputs: Array<Record<string, JsonValue>>;
    secrets: Array<Record<string, JsonValue>>;
    runs: RunReference[];
  };
  capabilities: InspectionCapabilities;
}

export interface RunReference {
  id: string;
  status: string;
  started_at?: string | null;
  finished_at?: string | null;
  attempt?: number;
  source?: InspectionSource;
}

export interface RunInspection {
  schema_version: typeof inspectionSchemaVersion;
  run: Required<RunReference>;
  report_summary: { metrics_count: number; issues_count: number; charts_count: number };
  execution_summary: { stage_count?: number; completed_stage_count?: number; failed_stage_count?: number; row_error_count?: number; cell_error_count?: number };
  capabilities: InspectionCapabilities;
  links?: { self: string };
  project_id?: string;
  project_name?: string;
}

export interface ReportContent {
  schema_version: typeof inspectionSchemaVersion;
  run_id: string;
  metrics: Array<Record<string, JsonValue>>;
  issues: Array<Record<string, JsonValue>>;
  charts: Array<Record<string, JsonValue>>;
  layout_json?: JsonValue;
}

export interface ProgressContent {
  schema_version: typeof inspectionSchemaVersion;
  run_id: string;
  steps: Array<Record<string, JsonValue>>;
  total_steps: number;
  completed_steps: number;
  failed_steps: number;
  running_steps: number;
  pending_steps: number;
}

export interface RunTableRows {
  schema_version: typeof inspectionSchemaVersion;
  run_id: string;
  table_name: string;
  columns: string[];
  rows: Array<Record<string, JsonValue>>;
  page: number;
  page_size: number;
  returned: number;
  total_rows: number | null;
  has_more: boolean;
  format: string;
}

export interface InspectionActions {
  rerun?: (runId: string) => void;
  cancel?: (runId: string) => void;
  compare?: (runId: string) => void;
  annotate?: (runId: string) => void;
  navigate?: (path: string) => void;
}

export interface InspectionClient {
  project(projectId: string): Promise<ProjectInspection>;
  recipes(projectId: string): Promise<{ items: InspectionResource[] }>;
  recipe(projectId: string, recipeId: string): Promise<{ recipe: InspectionResource & { source_text?: string; path?: string } }>;
  fixtures(projectId: string): Promise<{ items: InspectionResource[] }>;
  fixture(projectId: string, fixtureId: string): Promise<{ fixture: InspectionResource & { source_text?: string; path?: string } }>;
  inputs(projectId: string): Promise<{ items: Array<Record<string, JsonValue>> }>;
  secrets(projectId: string): Promise<{ items: Array<Record<string, JsonValue>> }>;
  runs(projectId: string): Promise<{ items: RunReference[] }>;
  run(runId: string): Promise<RunInspection>;
  progress(runId: string): Promise<ProgressContent>;
  report(runId: string): Promise<ReportContent>;
  tables(runId: string): Promise<{ items: Array<Record<string, JsonValue>> }>;
  tableRows(runId: string, tableName: string, page: number, pageSize: number): Promise<RunTableRows>;
  traces(runId: string): Promise<{ items: Array<{ id: string; status: string }> }>;
  traceSpans(runId: string, traceId: string, page: number, pageSize: number): Promise<{ records: Array<Record<string, JsonValue>> }>;
}
