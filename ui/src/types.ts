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

export interface RunLogsContent {
  schema_version: typeof inspectionSchemaVersion;
  run_id: string;
  files: Array<{ name: string; path: string; size_bytes: number }>;
  text: string;
}

export interface RunGraphNode {
  id: string;
  type: string;
  label: string;
  status: string;
  details: Record<string, JsonValue>;
}

export interface RunGraphEdge {
  from_id: string;
  to_id: string;
  relation: string;
}

export interface RunGraphContent {
  schema_version: typeof inspectionSchemaVersion;
  run_id: string;
  nodes: RunGraphNode[];
  edges: RunGraphEdge[];
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

export interface AnnotationRequestRead {
  id: string;
  organization_id?: string | null;
  local_project_id?: string | null;
  queue_id: string;
  run_id?: string | null;
  recipe_id?: string | null;
  cluster_id?: string | null;
  source_table: string;
  publish_alias?: string | null;
  instructions?: string | null;
  reviewers_per_task: number;
  reservation_minutes: number;
  consensus: string;
  template_snapshot: string;
  data_path: string;
  reviews_path: string;
  results_path: string;
  manifest_path: string;
  status: string;
  total_tasks: number;
  completed_tasks: number;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface AnnotationTaskRead {
  task_id: string;
  data: Record<string, JsonValue>;
  status: string;
  review_count: number;
}

export interface AnnotationReviewRead {
  task_id: string;
  reviewer_id: string;
  submitted_at: string;
  result: Record<string, JsonValue>;
}

export interface AnnotationProgress {
  request_id: string;
  total_tasks: number;
  completed_tasks: number;
  status: string;
}

export interface PoolNodeRead {
  pool_name: string;
  pool_kind: string;
  node_id: string;
  address?: string | null;
  status: string;
  capacity: number;
  available: number;
  generation: number;
}

export interface PoolLeaseRead {
  lease_id: string;
  pool_name: string;
  pool_kind: string;
  node_id: string;
  manager_id: string;
  worker_slot_id: string;
  address?: string | null;
  request_id: string;
  executor_id: string;
  fixture_id: string;
  status: string;
  acquired_at?: number | null;
  expires_at?: number | null;
  generation: number;
  lease_decision: string;
}

export interface RateLimitLeaseRead {
  lease_id: string;
  key: string;
  max_in_flight: number;
  active_count: number;
  request_id: string;
  acquired_at?: number | null;
  expires_at?: number | null;
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
  logs(runId: string): Promise<RunLogsContent>;
  graph(runId: string): Promise<RunGraphContent>;
  report(runId: string): Promise<ReportContent>;
  tables(runId: string): Promise<{ items: Array<Record<string, JsonValue>> }>;
  tableRows(runId: string, tableName: string, page: number, pageSize: number): Promise<RunTableRows>;
  traces(runId: string): Promise<{ items: Array<{ id: string; status: string }> }>;
  traceSpans(runId: string, traceId: string, page: number, pageSize: number): Promise<{ records: Array<Record<string, JsonValue>> }>;
  annotationRequests(runId: string): Promise<{ items: AnnotationRequestRead[]; total: number }>;
  annotationRequest(runId: string, requestId: string): Promise<{ request: AnnotationRequestRead; progress: AnnotationProgress }>;
  annotationTasks(runId: string, requestId: string): Promise<{ request_id: string; tasks: AnnotationTaskRead[]; total: number; completed: number }>;
  annotationTask(runId: string, requestId: string, taskId: string): Promise<{ request_id: string; task: AnnotationTaskRead }>;
  submitAnnotationReview(runId: string, requestId: string, taskId: string, payload: { reviewer_id: string; result: Record<string, JsonValue> }): Promise<{ review: AnnotationReviewRead; progress: AnnotationProgress }>;
  finalizeAnnotationRequest(runId: string, requestId: string): Promise<{ request_id: string; total_tasks: number; completed_tasks: number; results_path: string }>;
  runtimePools(runId: string): Promise<{ run_id: string; nodes: PoolNodeRead[]; leases: PoolLeaseRead[] }>;
  runtimeRateLimits(runId: string): Promise<{ run_id: string; leases: RateLimitLeaseRead[] }>;
}
