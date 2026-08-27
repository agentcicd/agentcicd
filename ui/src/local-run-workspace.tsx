import Editor from "@monaco-editor/react";
import { AlertCircle, Boxes, CheckCircle2, ClipboardCheck, KeyRound, Play, SlidersHorizontal } from "lucide-react";
import { useCallback, useEffect, useState, type ReactNode } from "react";

import type { AnnotationRequestRead, AnnotationTaskRead, InspectionClient, InspectionResource, JsonValue, ProjectInspection, ReportContent, RunInspection } from "./types";
import { InspectionShell, type InspectionSection } from "./inspection-shell";
import { LabelStudioRenderer } from "./label-studio-renderer";
import { CommonDetailsComponent, CommonListComponent, ResourceDetailHeader, ResourceField, ResourceReadonlyTextarea, ResourceTable, type ResourceTableColumn } from "./resource-workspace";
import { ServiceCard, StatusBadge } from "./service-primitives";
import { RunGraph } from "./run-graph";

const POLL_INTERVAL_MS = 1500;

type LoadState<T> = { value: T | null; error: string | null };
type ResourceKind = "inputs" | "fixtures" | "secrets";
const LOCAL_SECTIONS = new Set<InspectionSection>(["run", "recipe", "inputs", "fixtures", "annotations", "secrets"]);

function usePolling<T>(load: () => Promise<T>, dependencies: readonly unknown[], intervalMs = POLL_INTERVAL_MS): LoadState<T> {
  const [value, setValue] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    const refresh = async () => {
      try {
        const nextValue = await load();
        if (active) {
          setValue(nextValue);
          setError(null);
        }
      } catch (reason: unknown) {
        if (active) setError(reason instanceof Error ? reason.message : "Unable to load inspection data.");
      }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), intervalMs);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
    // The caller supplies stable identifiers and a stable inspection client.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, dependencies);

  return { value, error };
}

function text(value: JsonValue | undefined): string {
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  return "-";
}

function ErrorState({ message }: { message: string }) {
  return <div className="flex items-center gap-2 rounded-md border border-red-200 bg-red-50 p-4 text-sm text-red-700"><AlertCircle className="h-4 w-4" />{message}</div>;
}

function LoadingState() {
  return <p className="text-sm text-slate-500">Loading inspection data...</p>;
}

export function LocalRunWorkspace({ client, projectId, runId }: { client: InspectionClient; projectId: string; runId: string | null }) {
  const projectState = usePolling(() => client.project(projectId), [client, projectId]);
  const [section, setSection] = useState<InspectionSection>(() => initialSectionFromLocation());
  const project = projectState.value;
  const selectedRunId = runId ?? project?.resources.runs[0]?.id ?? null;
  const navigateSection = useCallback((nextSection: InspectionSection) => {
    const resolved = LOCAL_SECTIONS.has(nextSection) ? nextSection : "run";
    setSection(resolved);
    if (typeof window !== "undefined") {
      const url = new URL(window.location.href);
      url.searchParams.set("section", resolved);
      window.history.replaceState(null, "", `${url.pathname}${url.search}`);
    }
  }, []);

  if (projectState.error) return <ErrorState message={projectState.error} />;
  if (!project) return <LoadingState />;

  const content = section === "run"
    ? <RunHome client={client} runId={selectedRunId} />
    : section === "recipe"
      ? <RecipeView client={client} projectId={projectId} />
      : section === "annotations"
        ? <AnnotationsView client={client} runId={selectedRunId} />
        : <ResourceView client={client} projectId={projectId} project={project} kind={section === "fixtures" ? "fixtures" : section === "inputs" ? "inputs" : "secrets"} />;

  return <InspectionShell projectName={project.project.name} activeSection={section} onNavigate={navigateSection} headerActions={selectedRunId ? <RunTopStatus client={client} runId={selectedRunId} /> : null}>{content}</InspectionShell>;
}

function initialSectionFromLocation(): InspectionSection {
  if (typeof window === "undefined") return "run";
  const requested = new URL(window.location.href).searchParams.get("section") as InspectionSection | null;
  return requested && LOCAL_SECTIONS.has(requested) ? requested : "run";
}

function RunTopStatus({ client, runId }: { client: InspectionClient; runId: string }) {
  const runState = usePolling(() => client.run(runId), [client, runId]);
  if (!runState.value) return null;
  return <StatusBadge status={runState.value.run.status} />;
}

function RunHome({ client, runId }: { client: InspectionClient; runId: string | null }) {
  if (!runId) return <EmptyRunState />;
  const graphState = usePolling(() => client.graph(runId), [client, runId]);
  const reportState = usePolling(() => client.report(runId), [client, runId]);
  const logsState = usePolling(() => client.logs(runId), [client, runId], 5000);
  const runState = usePolling(() => client.run(runId), [client, runId]);

  if (graphState.error || reportState.error || logsState.error || runState.error) return <ErrorState message={graphState.error ?? reportState.error ?? logsState.error ?? runState.error ?? "Unable to load the run."} />;
  if (!graphState.value || !reportState.value || !logsState.value || !runState.value) return <LoadingState />;

  return <main className="flex h-[calc(100vh-7rem)] min-h-[28rem] min-w-0 flex-col gap-4 overflow-hidden">
    <RunResultCard run={runState.value} report={reportState.value} />
    <div className="min-h-0 flex-1">
      <RunGraph client={client} runId={runId} graph={graphState.value} logs={logsState.value} report={reportState.value} />
    </div>
  </main>;
}

function EmptyRunState() {
  return <ServiceCard className="p-6"><div className="flex items-center gap-3"><Play className="h-5 w-5 text-slate-500" /><div><h2 className="text-base font-medium text-slate-900">No runs yet</h2><p className="mt-1 text-sm text-slate-500">Run this project to materialize a live execution graph and evaluation results.</p></div></div></ServiceCard>;
}

function RunResultCard({ run, report }: { run: RunInspection; report: ReportContent }) {
  const execution = run.execution_summary;
  const resultCards: Array<{ label: string; value: string | number }> = [
    { label: "Stages", value: `${execution.completed_stage_count ?? 0}/${execution.stage_count ?? 0}` },
    { label: "Metrics", value: report.metrics.length },
    { label: "Issues", value: report.issues.length },
    { label: "Errors", value: execution.row_error_count ?? 0 },
  ];
  return <ServiceCard className="shrink-0 p-4"><dl className="grid gap-3 sm:grid-cols-4">{resultCards.map((item) => <div key={item.label} className="border-l-2 border-slate-200 pl-3"><dt className="text-xs text-slate-500">{item.label}</dt><dd className="mt-1 text-xl font-semibold text-slate-900">{item.value}</dd></div>)}</dl></ServiceCard>;
}

function AnnotationsView({ client, runId }: { client: InspectionClient; runId: string | null }) {
  const requestsState = usePolling(() => runId ? client.annotationRequests(runId) : Promise.resolve({ items: [], total: 0 }), [client, runId]);
  const [selectedRequestId, setSelectedRequestId] = useState<string | null>(null);
  const requests = requestsState.value?.items ?? [];
  const selectedRequest = requests.find((request) => request.id === selectedRequestId) ?? null;

  if (!runId) return <EmptyRunState />;
  if (requestsState.error) return <ErrorState message={requestsState.error} />;
  if (!requestsState.value) return <LoadingState />;
  if (!requests.length) return <EmptyResourcePage title="No annotations" message="No annotation requests are used by this run." />;

  const columns: ResourceTableColumn<AnnotationRequestRead>[] = [
    {
      id: "request",
      header: "Request",
      cell: (request) => (
        <div className="min-w-0">
          <div className="truncate font-mono text-sm font-medium text-slate-900">{request.id}</div>
          <div className="truncate text-xs text-slate-500">{request.source_table || request.publish_alias || "-"}</div>
        </div>
      ),
    },
    {
      id: "progress",
      header: "Progress",
      width: "5.75rem",
      cell: (request) => <span className="font-mono text-xs text-slate-600">{request.completed_tasks}/{request.total_tasks}</span>,
    },
    {
      id: "status",
      header: "Status",
      width: "7.25rem",
      align: "right",
      cell: (request) => <StatusBadge status={request.status} />,
    },
  ];

  return <main className="grid min-h-[calc(100vh-10rem)] gap-5 xl:grid-cols-[minmax(26rem,0.95fr)_minmax(0,1.6fr)]">
    <CommonListComponent
      search={<div className="text-sm font-medium text-slate-900">Annotation requests</div>}
      hasItems={requests.length > 0}
    >
      <ResourceTable
        rows={requests}
        columns={columns}
        getRowId={(request) => request.id}
        selectedRowId={selectedRequest?.id ?? null}
        onRowOpen={(request) => setSelectedRequestId(request.id)}
        mobileTitle={(request) => request.id}
        mobileSubtitle={(request) => request.source_table}
        mobileMeta={(request) => <StatusBadge status={request.status} />}
      />
    </CommonListComponent>
    <AnnotationRequestDetails client={client} runId={runId} request={selectedRequest} />
  </main>;
}

function AnnotationRequestDetails({ client, runId, request }: { client: InspectionClient; runId: string; request: AnnotationRequestRead | null }) {
  const tasksState = usePolling(() => request ? client.annotationTasks(runId, request.id) : Promise.resolve({ request_id: "", tasks: [], total: 0, completed: 0 }), [client, runId, request?.id]);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const tasks = tasksState.value?.tasks ?? [];
  const selectedTask = tasks.find((task) => task.task_id === (selectedTaskId ?? tasks[0]?.task_id)) ?? null;

  if (!request) {
    return <CommonDetailsComponent title="Select an annotation request" subtitle="Choose a request to review tasks."><ResourceDetailHeader title="Select an annotation request" subtitle="Choose a request to review tasks." /></CommonDetailsComponent>;
  }
  if (tasksState.error) return <ErrorState message={tasksState.error} />;
  if (!tasksState.value) return <LoadingState />;

  return <CommonDetailsComponent title={request.id} subtitle="Annotation request">
    <ResourceDetailHeader title={<span className="flex items-center gap-2"><ClipboardCheck className="h-4 w-4" />{request.id}</span>} subtitle={request.instructions || "Annotation request"} actions={<StatusBadge status={request.status} />} />
    <div className="space-y-5">
      <AnnotationRequestSummary request={request} />
      <div>
        <div className="mb-2 flex items-center justify-between gap-2">
          <p className="text-sm font-medium text-slate-900">Tasks</p>
          <span className="text-xs text-slate-500">{tasksState.value.completed}/{tasksState.value.total} labeled</span>
        </div>
        {tasks.length ? (
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {tasks.map((task) => (
              <button
                key={task.task_id}
                type="button"
                onClick={() => setSelectedTaskId(task.task_id)}
                className={`rounded-md border px-3 py-2 text-left transition ${selectedTask?.task_id === task.task_id ? "border-slate-500 bg-slate-50" : "border-slate-200 bg-white hover:bg-slate-50"}`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate font-mono text-xs text-slate-800">{task.task_id}</span>
                  <StatusBadge status={task.status} showIcon={false} />
                </div>
                <p className="mt-1 text-xs text-slate-500">{task.review_count} review{task.review_count === 1 ? "" : "s"}</p>
              </button>
            ))}
          </div>
        ) : <p className="rounded-md border border-slate-200 bg-slate-50 p-4 text-sm text-slate-500">No annotation tasks.</p>}
      </div>
      {selectedTask ? <AnnotationTaskReview client={client} runId={runId} request={request} task={selectedTask} /> : null}
    </div>
  </CommonDetailsComponent>;
}

function AnnotationRequestSummary({ request }: { request: AnnotationRequestRead }) {
  const rows: Array<[string, string]> = [
    ["Source table", request.source_table || "-"],
    ["Publish alias", request.publish_alias || "-"],
    ["Progress", `${request.completed_tasks}/${request.total_tasks}`],
    ["Reviewers per task", String(request.reviewers_per_task)],
    ["Consensus", request.consensus],
    ["Results path", request.results_path],
  ];
  return <dl className="grid gap-3 rounded-md border border-slate-200 bg-slate-50 p-4 text-xs sm:grid-cols-2">{rows.map(([label, value]) => <div key={label} className="min-w-0"><dt className="text-slate-500">{label}</dt><dd className="mt-1 truncate font-mono text-slate-800">{value}</dd></div>)}</dl>;
}

function AnnotationTaskReview({ client, runId, request, task }: { client: InspectionClient; runId: string; request: AnnotationRequestRead; task: AnnotationTaskRead }) {
  const [reviewerId, setReviewerId] = useState("local.reviewer");
  const [annotationResult, setAnnotationResult] = useState<Record<string, JsonValue>>({ result: [] });
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const submit = async () => {
    try {
      setError(null);
      await client.submitAnnotationReview(runId, request.id, task.task_id, { reviewer_id: reviewerId, result: annotationResult });
      setMessage("Review saved.");
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "Unable to save review.");
    }
  };
  const finalize = async () => {
    try {
      setError(null);
      await client.finalizeAnnotationRequest(runId, request.id);
      setMessage("Annotation finalized.");
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "Unable to finalize annotation request.");
    }
  };
  return <section className="space-y-4 border-t border-slate-100 pt-5">
    <div className="flex items-center justify-between gap-3">
      <div className="min-w-0">
        <p className="font-mono text-xs text-slate-500">{task.task_id}</p>
        <h3 className="mt-1 text-sm font-medium text-slate-900">Review task</h3>
      </div>
      <StatusBadge status={task.status} />
    </div>
    <div className="rounded-md border border-slate-200 bg-white p-4">
      <LabelStudioRenderer
        config={request.template_snapshot}
        task={{ id: task.task_id, data: task.data }}
        onChange={(annotation) => setAnnotationResult(toJsonRecord(annotation))}
      />
    </div>
    <div className="grid gap-4 lg:grid-cols-[minmax(0,16rem)_minmax(0,1fr)]">
      <label className="block text-xs text-slate-500">Reviewer<input value={reviewerId} onChange={(event) => setReviewerId(event.target.value)} className="mt-1 w-full rounded-md border border-slate-300 bg-white px-3 py-2 font-mono text-xs text-slate-800" /></label>
      <label className="block text-xs text-slate-500">Result payload<textarea value={JSON.stringify(annotationResult, null, 2)} readOnly rows={7} className="mt-1 w-full resize-y rounded-md border border-slate-300 bg-slate-50 px-3 py-2 font-mono text-xs text-slate-800" /></label>
    </div>
    <div className="flex flex-wrap items-center gap-2">
      <button type="button" onClick={() => void submit()} className="inline-flex items-center gap-1.5 rounded-md border border-slate-300 bg-white px-3 py-2 text-xs font-medium text-slate-800 hover:bg-slate-50"><CheckCircle2 className="h-3.5 w-3.5" />Save review</button>
      <button type="button" onClick={() => void finalize()} className="rounded-md bg-slate-900 px-3 py-2 text-xs font-medium text-white hover:bg-slate-700">Finalize request</button>
      {message ? <p className="text-xs text-slate-500">{message}</p> : null}
      {error ? <p className="text-xs text-red-600">{error}</p> : null}
    </div>
  </section>;
}

function toJsonRecord(value: unknown): Record<string, JsonValue> {
  return JSON.parse(JSON.stringify(value ?? {})) as Record<string, JsonValue>;
}

function RecipeView({ client, projectId }: { client: InspectionClient; projectId: string }) {
  const recipeState = usePolling(() => client.recipe(projectId, "recipe.sql"), [client, projectId], 5000);
  if (recipeState.error) return <ErrorState message={recipeState.error} />;
  if (!recipeState.value) return <LoadingState />;
  const recipe = recipeState.value.recipe;
  return <main className="min-h-[calc(100vh-10rem)]"><ServiceCard className="overflow-hidden"><div className="h-[calc(100vh-9rem)] min-h-[32rem]"><Editor height="100%" defaultLanguage="sql" theme="vs" value={recipe.source_text ?? ""} options={{ readOnly: true, minimap: { enabled: false }, fontSize: 14, scrollBeyondLastLine: false, scrollbar: { alwaysConsumeMouseWheel: false } }} /></div></ServiceCard></main>;
}

function ResourceView({ client, projectId, project, kind }: { client: InspectionClient; projectId: string; project: ProjectInspection; kind: ResourceKind }) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const resources = kind === "fixtures" ? project.resources.fixtures : kind === "inputs" ? project.resources.inputs.map(inputToResource) : project.resources.secrets.map(secretToResource);
  const selected = resources.find((resource) => resource.id === selectedId) ?? null;
  const title = kind === "fixtures" ? "Fixtures" : kind === "inputs" ? "Inputs" : "Secrets";
  const columns: ResourceTableColumn<InspectionResource>[] = [
    { id: "name", header: "Name", cell: (resource) => <span className="font-medium text-slate-900">{resource.name}</span> },
  ];
  if (!resources.length) return <EmptyResourcePage title={`No ${title.toLowerCase()}`} message={`No ${title.toLowerCase()} are used by this recipe.`} />;
  return <main className={`grid min-h-[calc(100vh-10rem)] gap-5 ${selected ? "xl:grid-cols-[minmax(20rem,0.8fr)_minmax(0,1.7fr)]" : ""}`}><CommonListComponent search={<div className="text-sm font-medium text-slate-900">{title}</div>} hasItems={resources.length > 0}><ResourceTable rows={resources} columns={columns} getRowId={(resource) => resource.id} selectedRowId={selectedId} onRowOpen={(resource) => setSelectedId(resource.id)} mobileTitle={(resource) => resource.name} /></CommonListComponent>{selected ? <ResourceDetails client={client} projectId={projectId} resource={selected} kind={kind} /> : null}</main>;
}

function EmptyResourcePage({ title, message }: { title: string; message: string }) {
  return <main className="flex min-h-[calc(100vh-10rem)] items-center justify-center"><div className="text-center"><h2 className="text-base font-medium text-slate-900">{title}</h2><p className="mt-2 text-sm text-slate-500">{message}</p></div></main>;
}

function secretToResource(secret: Record<string, JsonValue>): InspectionResource {
  const reference = text(secret.reference);
  return { id: reference, name: reference, status: text(secret.configured) === "true" ? "available" : "unknown", details: secret };
}

function inputToResource(input: Record<string, JsonValue>, index: number): InspectionResource {
  const name = text(input.name) !== "-" ? text(input.name) : text(input.reference) !== "-" ? text(input.reference) : `input_${index + 1}`;
  const kind = text(input.kind) !== "-" ? text(input.kind) : text(input.type);
  return { id: `input:${name}`, name, status: "available", details: { ...input, kind } };
}

function ResourceDetails({ client, projectId, resource, kind }: { client: InspectionClient; projectId: string; resource: InspectionResource | null; kind: ResourceKind }) {
  const fixtureState = usePolling(
    () => resource && kind === "fixtures" ? client.fixture(projectId, resource.id) : Promise.resolve(null),
    [client, projectId, resource?.id, kind],
    5000,
  );
  if (!resource) return null;
  if (fixtureState.error) return <ErrorState message={fixtureState.error} />;
  const details = resource.details ?? {};
  const fixture = fixtureState.value?.fixture;
  const icon: ReactNode = kind === "fixtures" ? <Boxes className="h-4 w-4" /> : kind === "inputs" ? <SlidersHorizontal className="h-4 w-4" /> : <KeyRound className="h-4 w-4" />;
  const subtitle = kind === "fixtures" ? "Recipe fixture" : kind === "inputs" ? "Recipe input" : "Recipe secret";
  return <CommonDetailsComponent title={resource.name} subtitle={subtitle}><ResourceDetailHeader title={<span className="flex items-center gap-2">{icon}{resource.name}</span>} subtitle={subtitle} /><div className="space-y-5"><div className="grid gap-4 sm:grid-cols-2">{Object.entries(details).map(([key, value]) => <ResourceField key={key} label={key.replaceAll("_", " ")}><p className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 font-mono text-sm text-slate-700">{Array.isArray(value) ? value.join(", ") : text(value)}</p></ResourceField>)}</div>{fixture?.source_text ? <ResourceField label="Source"><ResourceReadonlyTextarea value={fixture.source_text} rows={22} /></ResourceField> : null}</div></CommonDetailsComponent>;
}
