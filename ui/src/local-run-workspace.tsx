import { AlertCircle, Boxes, CheckCircle2, ClipboardCheck, FileText, Gauge, KeyRound, Play, SlidersHorizontal, Table2 } from "lucide-react";
import { Background, Controls, MarkerType, Position, ReactFlow, type Edge, type Node } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import * as dagre from "dagre";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";

import type { AnnotationRequestRead, AnnotationTaskRead, InspectionClient, InspectionResource, JsonValue, ProgressContent, ProjectInspection, ReportContent, RunGraphContent, RunGraphNode, RunInspection, RunLogsContent } from "./types";
import { InspectionShell, type InspectionSection } from "./inspection-shell";
import { LabelStudioRenderer } from "./label-studio-renderer";
import { CommonDetailsComponent, CommonListComponent, ResourceDetailHeader, ResourceField, ResourceReadonlyTextarea, ResourceSearchInput, ResourceTable, type ResourceTableColumn } from "./resource-workspace";
import { ServiceCard, StatusBadge } from "./service-primitives";
import { ServiceDataTable, ServiceReportContent } from "./service-renderers";

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
    ? <RunHome client={client} project={project} runId={selectedRunId} />
    : section === "recipe"
      ? <RecipeView client={client} projectId={projectId} />
      : section === "annotations"
        ? <AnnotationsView client={client} runId={selectedRunId} />
        : <ResourceView client={client} projectId={projectId} project={project} kind={section === "fixtures" ? "fixtures" : section === "inputs" ? "inputs" : "secrets"} />;

  return <InspectionShell projectName={project.project.name} activeSection={section} onNavigate={navigateSection}>{content}</InspectionShell>;
}

function initialSectionFromLocation(): InspectionSection {
  if (typeof window === "undefined") return "run";
  const requested = new URL(window.location.href).searchParams.get("section") as InspectionSection | null;
  return requested && LOCAL_SECTIONS.has(requested) ? requested : "run";
}

function RunHome({ client, project, runId }: { client: InspectionClient; project: ProjectInspection; runId: string | null }) {
  if (!runId) return <EmptyRunState />;
  const runState = usePolling(() => client.run(runId), [client, runId]);
  const graphState = usePolling(() => client.graph(runId), [client, runId]);
  const reportState = usePolling(() => client.report(runId), [client, runId]);
  const progressState = usePolling(() => client.progress(runId), [client, runId]);
  const logsState = usePolling(() => client.logs(runId), [client, runId], 5000);

  if (runState.error || graphState.error || reportState.error || progressState.error || logsState.error) return <ErrorState message={runState.error ?? graphState.error ?? reportState.error ?? progressState.error ?? logsState.error ?? "Unable to load the run."} />;
  if (!runState.value || !graphState.value || !reportState.value || !progressState.value || !logsState.value) return <LoadingState />;

  return <main className="flex min-h-[calc(100vh-10rem)] min-w-0 flex-col gap-5">
    <RunHeader project={project} run={runState.value} />
    <div className="grid gap-5 xl:grid-cols-[minmax(0,1.8fr)_minmax(19rem,0.8fr)]">
      <RunGraph graph={graphState.value} />
      <div className="space-y-5">
        <RunResults run={runState.value} report={reportState.value} />
        <RuntimeControlPanel client={client} runId={runId} />
      </div>
    </div>
    <div className="grid gap-5 xl:grid-cols-2">
      <RunProgressPanel progress={progressState.value} />
      <RunLogsPanel logs={logsState.value} />
    </div>
    <RunReport report={reportState.value} />
  </main>;
}

function EmptyRunState() {
  return <ServiceCard className="p-6"><div className="flex items-center gap-3"><Play className="h-5 w-5 text-slate-500" /><div><h2 className="text-base font-medium text-slate-900">No runs yet</h2><p className="mt-1 text-sm text-slate-500">Run this project to materialize a live execution graph and evaluation results.</p></div></div></ServiceCard>;
}

function RunHeader({ project, run }: { project: ProjectInspection; run: RunInspection }) {
  return <header className="flex flex-col gap-3 border-b border-slate-200 pb-4 sm:flex-row sm:items-start sm:justify-between">
    <div className="min-w-0"><p className="text-xs font-medium uppercase text-slate-500">Run</p><h1 className="mt-1 truncate text-xl font-semibold text-slate-900">{project.project.name}</h1><p className="mt-1 truncate font-mono text-xs text-slate-500">{run.run.id}</p></div>
    <StatusBadge status={run.run.status} className="self-start" />
  </header>;
}

function RunResults({ run, report }: { run: RunInspection; report: ReportContent }) {
  const execution = run.execution_summary;
  const resultCards: Array<{ label: string; value: string | number }> = [
    { label: "Stages", value: `${execution.completed_stage_count ?? 0}/${execution.stage_count ?? 0}` },
    { label: "Metrics", value: report.metrics.length },
    { label: "Issues", value: report.issues.length },
    { label: "Errors", value: execution.row_error_count ?? 0 },
  ];
  return <ServiceCard className="p-5"><div className="flex items-center gap-2"><Table2 className="h-4 w-4 text-slate-500" /><h2 className="text-sm font-medium text-slate-900">Run results</h2></div><dl className="mt-5 grid grid-cols-2 gap-3">{resultCards.map((item) => <div key={item.label} className="border-l-2 border-slate-200 pl-3"><dt className="text-xs text-slate-500">{item.label}</dt><dd className="mt-1 text-xl font-semibold text-slate-900">{item.value}</dd></div>)}</dl><div className="mt-6 border-t border-slate-100 pt-4"><p className="text-xs text-slate-500">Updates read from the run directory.</p></div></ServiceCard>;
}

function RunProgressPanel({ progress }: { progress: ProgressContent }) {
  const rows = progress.steps.map((step, index) => ({
    id: text(step.id) !== "-" ? text(step.id) : `${text(step.step_type)}:${text(step.step_name)}:${index}`,
    type: text(step.step_type).replaceAll("_", " "),
    name: text(step.step_name),
    status: text(step.status),
    row_count: step.row_count,
    error: step.error,
  }));
  return <ServiceCard className="p-5">
    <div className="mb-4 flex items-center justify-between gap-3">
      <div className="flex items-center gap-2"><Play className="h-4 w-4 text-slate-500" /><h2 className="text-sm font-medium text-slate-900">Progress</h2></div>
      <span className="hidden text-xs text-slate-500 sm:inline">{progress.completed_steps}/{progress.total_steps} completed</span>
    </div>
    {rows.length ? <div className="divide-y divide-slate-100 rounded-md border border-slate-200">{rows.map((row) => <div key={row.id} className="grid gap-3 px-3 py-2 text-sm sm:grid-cols-[minmax(0,1fr)_9rem_auto] sm:items-center"><div className="min-w-0"><p className="truncate font-medium text-slate-900">{row.name}</p><p className="truncate text-xs text-slate-500">{row.type}{row.row_count !== undefined ? ` - ${text(row.row_count)}` : ""}</p>{row.error !== undefined && row.error !== null ? <p className="mt-1 truncate text-xs text-red-600">{text(row.error)}</p> : null}</div><span className="hidden font-mono text-xs text-slate-500 sm:block">{row.id}</span><StatusBadge status={row.status} /></div>)}</div> : <p className="rounded-md border border-slate-200 bg-slate-50 p-4 text-sm text-slate-500">Waiting for execution progress.</p>}
  </ServiceCard>;
}

function RunLogsPanel({ logs }: { logs: RunLogsContent }) {
  return <ServiceCard className="p-5">
    <div className="mb-4 flex items-center justify-between gap-3">
      <div className="flex items-center gap-2"><FileText className="h-4 w-4 text-slate-500" /><h2 className="text-sm font-medium text-slate-900">Logs</h2></div>
      <span className="text-xs text-slate-500">{logs.files.length} files</span>
    </div>
    {logs.files.length ? <div className="mb-4 grid gap-2 sm:grid-cols-2">{logs.files.slice(0, 6).map((file) => <div key={file.path} className="min-w-0 rounded-md border border-slate-200 bg-slate-50 px-3 py-2"><p className="truncate font-mono text-xs text-slate-800">{file.name}</p><p className="mt-1 text-xs text-slate-500">{file.size_bytes} bytes</p></div>)}</div> : null}
    <pre className="max-h-72 overflow-auto rounded-md border border-slate-200 bg-slate-950 p-3 text-xs leading-5 text-slate-100">{logs.text || "No log text available."}</pre>
  </ServiceCard>;
}

function RunReport({ report }: { report: ReportContent }) {
  return <ServiceCard className="min-h-0 p-5"><h2 className="text-sm font-medium text-slate-900">Published results</h2>{report.metrics.length || report.issues.length || report.charts.length ? <div className="mt-4"><ServiceReportContent metrics={report.metrics} issues={report.issues} charts={report.charts} /></div> : <p className="mt-4 text-sm text-slate-500">Results will appear here as recipe stages publish them.</p>}</ServiceCard>;
}

function AnnotationsView({ client, runId }: { client: InspectionClient; runId: string | null }) {
  const requestsState = usePolling(() => runId ? client.annotationRequests(runId) : Promise.resolve({ items: [], total: 0 }), [client, runId]);
  const [query, setQuery] = useState("");
  const [selectedRequestId, setSelectedRequestId] = useState<string | null>(null);
  const requests = requestsState.value?.items ?? [];
  const filteredRequests = requests.filter((request) => `${request.id} ${request.source_table} ${request.status} ${request.publish_alias ?? ""}`.toLowerCase().includes(query.toLowerCase()));
  const selectedRequest = filteredRequests.find((request) => request.id === (selectedRequestId ?? filteredRequests[0]?.id)) ?? null;

  if (!runId) return <EmptyRunState />;
  if (requestsState.error) return <ErrorState message={requestsState.error} />;
  if (!requestsState.value) return <LoadingState />;

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
      search={<ResourceSearchInput placeholder="Search annotation requests" value={query} onChange={setQuery} />}
      hasItems={filteredRequests.length > 0}
      emptyState={<div className="p-6 text-sm text-slate-500">No annotation requests are waiting for this run.</div>}
    >
      <ResourceTable
        rows={filteredRequests}
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

function RuntimeControlPanel({ client, runId }: { client: InspectionClient; runId: string }) {
  const poolsState = usePolling(() => client.runtimePools(runId), [client, runId]);
  const limitsState = usePolling(() => client.runtimeRateLimits(runId), [client, runId]);
  if (poolsState.error || limitsState.error) return <ErrorState message={poolsState.error ?? limitsState.error ?? "Unable to load runtime controls."} />;
  if (!poolsState.value || !limitsState.value) return <LoadingState />;
  if (!poolsState.value.nodes.length && !limitsState.value.leases.length) return null;
  const poolRows = poolsState.value.nodes.map((node) => ({ ...node }));
  const limitRows = limitsState.value.leases.map((lease) => ({ ...lease }));
  return <ServiceCard className="p-5"><div className="flex items-center gap-2"><Gauge className="h-4 w-4 text-slate-500" /><h2 className="text-sm font-medium text-slate-900">Runtime controls</h2></div><div className="mt-4 space-y-4">{poolRows.length ? <ServiceDataTable rows={poolRows} preferredColumns={["pool_name", "pool_kind", "node_id", "capacity", "available", "status"]} /> : null}{limitRows.length ? <ServiceDataTable rows={limitRows} preferredColumns={["key", "max_in_flight", "active_count"]} /> : null}</div></ServiceCard>;
}

function RunGraph({ graph }: { graph: RunGraphContent }) {
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const selectedNode = graph.nodes.find((node) => node.id === selectedNodeId) ?? null;
  const compact = useCompactViewport();
  const flow = useMemo(() => buildFlow(graph, compact), [graph, compact]);
  return <ServiceCard className="overflow-hidden sm:min-h-[36rem]">
    <div className="flex items-center justify-between border-b border-slate-200 px-5 py-4"><div className="flex items-center gap-2"><Play className="h-4 w-4 text-slate-500" /><h2 className="text-sm font-medium text-slate-900">Execution graph</h2></div><span className="text-xs text-slate-500">{graph.nodes.length} nodes</span></div>
    <div className="block bg-[#fbfcfe] p-4 sm:hidden"><MobileGraphList graph={graph} selectedNodeId={selectedNodeId} onSelect={setSelectedNodeId} /></div>
    <div className="hidden h-[31rem] bg-[#fbfcfe] sm:block"><ReactFlow nodes={flow.nodes} edges={flow.edges} fitView fitViewOptions={{ padding: compact ? 0.08 : 0.16, maxZoom: 1.1 }} minZoom={0.1} maxZoom={2} onNodeClick={(_event, node) => setSelectedNodeId(node.id)} nodesDraggable={false} nodesConnectable={false} elementsSelectable proOptions={{ hideAttribution: true }}><Background gap={18} size={1} color="#e2e8f0" /><Controls showInteractive={false} /></ReactFlow></div>
    {selectedNode ? <GraphNodeDetails node={selectedNode} /> : <div className="truncate border-t border-slate-200 px-5 py-3 text-xs text-slate-500">Select a node to inspect its current state.</div>}
  </ServiceCard>;
}

function GraphNodeDetails({ node }: { node: RunGraphNode }) {
  const detailEntries = Object.entries(node.details).filter(([, value]) => value !== null);
  return <div className="border-t border-slate-200 px-5 py-3"><div className="flex items-center justify-between gap-3"><div className="min-w-0"><p className="truncate text-sm font-medium text-slate-900">{node.label}</p><p className="mt-0.5 text-xs uppercase text-slate-500">{node.type.replaceAll("_", " ")}</p></div><StatusBadge status={node.status} /></div>{detailEntries.length ? <dl className="mt-3 grid gap-2 sm:grid-cols-2">{detailEntries.map(([key, value]) => <div key={key}><dt className="text-xs text-slate-500">{key.replaceAll("_", " ")}</dt><dd className="mt-0.5 truncate font-mono text-xs text-slate-700">{text(value)}</dd></div>)}</dl> : null}</div>;
}

function MobileGraphList({ graph, selectedNodeId, onSelect }: { graph: RunGraphContent; selectedNodeId: string | null; onSelect: (nodeId: string) => void }) {
  return <div className="space-y-2">
    {graph.nodes.map((node) => {
      const style = getRecipeGraphNodeStyle(node.type);
      const selected = node.id === selectedNodeId;
      return <button key={node.id} onClick={() => onSelect(node.id)} className={`w-full rounded-md border px-3 py-3 text-left ${selected ? "ring-2 ring-slate-300" : ""}`} style={{ borderColor: graphBorderColor(node.status), background: style.fill }}>
        <div className="flex items-center justify-between gap-2"><span className="truncate text-xs font-medium uppercase" style={{ color: style.text }}>{graphTypeLabel(node.type)}</span><span className="h-2 w-2 shrink-0 rounded-full" style={{ backgroundColor: statusColor(node.status) }} /></div>
        <p className="mt-1 break-words text-sm font-medium text-slate-900">{node.label}</p>
      </button>;
    })}
  </div>;
}

function useCompactViewport(): boolean {
  const [compact, setCompact] = useState(() => typeof window !== "undefined" ? window.matchMedia("(max-width: 640px)").matches : false);
  useEffect(() => {
    const query = window.matchMedia("(max-width: 640px)");
    const update = () => setCompact(query.matches);
    update();
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  return compact;
}

function buildFlow(graph: RunGraphContent, compact = false): { nodes: Node[]; edges: Edge[] } {
  const { layout, dimensionsById } = buildRecipeDependencyFlowModel(graph.nodes, graph.edges, compact);
  const nodes = graph.nodes.map((node) => {
    const position = layout.node(node.id);
    const dimensions = dimensionsById.get(node.id) ?? { width: 210, height: 72 };
    const style = getRecipeGraphNodeStyle(node.type);
    const borderColor = graphBorderColor(node.status);
    return {
      id: node.id,
      position: { x: position.x - dimensions.width / 2, y: position.y - dimensions.height / 2 },
      sourcePosition: Position.Bottom,
      targetPosition: Position.Top,
      draggable: false,
      selectable: true,
      connectable: false,
      data: { label: <GraphNodeLabel node={node} /> },
      style: {
        width: dimensions.width,
        height: dimensions.height,
        border: `2px solid ${borderColor}`,
        borderRadius: normalizeRecipeGraphType(node.type) === "macro" ? 9999 : 8,
        background: style.fill,
        color: "rgb(15 23 42)",
        boxShadow: "none",
        cursor: "pointer",
        opacity: recipeGraphLaneForType(node.type) === "output" ? 0.82 : 1,
        padding: 0,
      },
    } satisfies Node;
  });
  const nodeById = new Map(graph.nodes.map((node) => [node.id, node]));
  const edges = graph.edges.map((edge, index) => {
    const source = nodeById.get(edge.from_id);
    const target = nodeById.get(edge.to_id);
    const active = source?.status === "running" || target?.status === "running";
    const isInputEdge = recipeGraphLaneForType(source?.type ?? "") === "input";
    return {
      id: `${edge.from_id}-${edge.to_id}-${index}`,
      source: edge.from_id,
      target: edge.to_id,
      type: "smoothstep",
      animated: active,
      style: {
        stroke: isInputEdge ? "#94a3b8" : active ? "#475569" : "#64748b",
        strokeWidth: isInputEdge ? 1.4 : active ? 2.2 : 1.8,
        opacity: isInputEdge ? 0.78 : 1,
      },
      markerEnd: {
        type: MarkerType.ArrowClosed,
        color: isInputEdge ? "#94a3b8" : active ? "#475569" : "#64748b",
        width: isInputEdge ? 12 : 16,
        height: isInputEdge ? 12 : 16,
      },
    } satisfies Edge;
  });
  return { nodes, edges };
}

function GraphNodeLabel({ node }: { node: RunGraphNode }) {
  const presentation = getStatusPresentation(node.status);
  return <div className="relative h-full w-full text-left">
    <span className="absolute left-3 right-14 top-[15px] truncate text-[13px] font-semibold leading-none text-slate-900">{node.label}</span>
    <span className={`absolute right-3 top-1/2 inline-flex h-8 w-8 -translate-y-1/2 items-center justify-center rounded-full border-2 [&>svg]:h-5 [&>svg]:w-5 ${presentation.className}`} title={node.status || "pending"} aria-label={node.status || "pending"}>
      {presentation.icon}
    </span>
    <span className="absolute left-3 right-14 top-[32px] flex min-w-0 items-baseline gap-1.5 leading-none">
      <span className="min-w-0 truncate text-[11px] font-medium uppercase text-slate-500">{getRecipeGraphNodeTypeLabel(node.type)}</span>
    </span>
  </div>;
}

function graphTypeLabel(type: string) {
  return getRecipeGraphNodeTypeLabel(type);
}

function statusColor(status: string) {
  if (status === "completed") return "#16a34a";
  if (status === "available") return "#16a34a";
  if (status === "running") return "#475569";
  if (status === "failed") return "#dc2626";
  return "#94a3b8";
}

type RecipeGraphNodeStyle = {
  fill: string;
  stroke: string;
  text: string;
};

function normalizeRecipeGraphType(type: string) {
  const normalized = String(type || "").toLowerCase();
  if (normalized.startsWith("function")) return normalized;
  if (normalized === "declare_input") return "input";
  if (normalized === "load_table") return "load";
  if (normalized === "create_batch_table" || normalized === "create_stream_table") return "table";
  if (normalized === "save_table") return "save";
  if (normalized === "publish_reports" || normalized === "publish_report") return "publish_report_other";
  return normalized;
}

function getRecipeGraphNodeStyle(type: string): RecipeGraphNodeStyle {
  const normalizedType = normalizeRecipeGraphType(type);
  const map: Record<string, RecipeGraphNodeStyle> = {
    function: { fill: "#F5F3FF", stroke: "#C4B5FD", text: "#5B21B6" },
    function_local: { fill: "#F5F3FF", stroke: "#C4B5FD", text: "#5B21B6" },
    function_reference: { fill: "#F5F3FF", stroke: "#C4B5FD", text: "#5B21B6" },
    function_spark: { fill: "#F5F3FF", stroke: "#C4B5FD", text: "#5B21B6" },
    macro: { fill: "#F1F5F9", stroke: "#CBD5E1", text: "#334155" },
    load: { fill: "#EFF6FF", stroke: "#93C5FD", text: "#1D4ED8" },
    table: { fill: "#ECFDF5", stroke: "#86EFAC", text: "#166534" },
    save: { fill: "#FFF7ED", stroke: "#FDBA74", text: "#C2410C" },
    publish: { fill: "#FFF1F2", stroke: "#FDA4AF", text: "#9F1239" },
    publish_dataset: { fill: "#FFF1F2", stroke: "#FDA4AF", text: "#9F1239" },
    publish_report_metric: { fill: "#FFF1F2", stroke: "#FDA4AF", text: "#9F1239" },
    publish_report_chart: { fill: "#FFF1F2", stroke: "#FDA4AF", text: "#9F1239" },
    publish_report_issue: { fill: "#FEF2F2", stroke: "#FCA5A5", text: "#991B1B" },
    publish_report_example: { fill: "#FFF1F2", stroke: "#FDA4AF", text: "#9F1239" },
    publish_report_other: { fill: "#FFF1F2", stroke: "#FDA4AF", text: "#9F1239" },
    publish_annotation: { fill: "#FFF1F2", stroke: "#FDA4AF", text: "#9F1239" },
    retrieve_annotation: { fill: "#FFFBEB", stroke: "#FCD34D", text: "#92400E" },
    fixture: { fill: "#F0FDFA", stroke: "#5EEAD4", text: "#0F766E" },
    image: { fill: "#F0FDFA", stroke: "#5EEAD4", text: "#0F766E" },
    fixture_kind: { fill: "#F0FDFA", stroke: "#5EEAD4", text: "#0F766E" },
    aisystem: { fill: "#EEF2FF", stroke: "#A5B4FC", text: "#3730A3" },
    secret: { fill: "#FEF3C7", stroke: "#FCD34D", text: "#92400E" },
    vault: { fill: "#FEF3C7", stroke: "#FCD34D", text: "#92400E" },
    input: { fill: "#F0F9FF", stroke: "#7DD3FC", text: "#0369A1" },
    report: { fill: "#FFF1F2", stroke: "#FDA4AF", text: "#9F1239" },
    annotation_queue: { fill: "#FFF1F2", stroke: "#FDA4AF", text: "#9F1239" },
  };
  return map[normalizedType] || { fill: "#F8FAFC", stroke: "#CBD5E1", text: "#334155" };
}

function getRecipeGraphNodeTypeLabel(type: string) {
  const normalizedType = normalizeRecipeGraphType(type);
  const map: Record<string, string> = {
    function: "FUNCTION",
    function_local: "FUNCTION",
    function_reference: "FUNCTION",
    function_spark: "FUNCTION",
    macro: "MACRO",
    load: "LOAD",
    table: "TABLE",
    save: "SAVE",
    publish: "PUBLISH",
    publish_dataset: "PUBLISH DATASET",
    publish_report_metric: "PUBLISH REPORT / METRIC",
    publish_report_chart: "PUBLISH REPORT / CHART",
    publish_report_issue: "PUBLISH REPORT / ISSUE",
    publish_report_example: "PUBLISH REPORT / EXAMPLE",
    publish_report_other: "PUBLISH REPORT",
    publish_annotation: "PUBLISH ANNOTATION",
    retrieve_annotation: "RETRIEVE ANNOTATION",
    annotation_queue: "ANNOTATION QUEUE",
    fixture: "FIXTURE",
    image: "IMAGE",
    fixture_kind: "FIXTURE KIND",
    aisystem: "AI SYSTEM",
    secret: "SECRET",
    vault: "VAULT",
    input: "INPUT",
    report: "REPORT",
  };
  return map[normalizedType] || normalizedType.replaceAll("_", " ").toUpperCase();
}

function recipeGraphLaneForType(type: string): "input" | "node" | "output" {
  const normalized = normalizeRecipeGraphType(type);
  if (["input", "macro", "secret", "aisystem", "fixture", "image", "fixture_kind", "vault"].includes(normalized)) return "input";
  if (normalized === "save" || normalized === "publish" || normalized.startsWith("publish_") || normalized === "report" || normalized === "annotation_queue") return "output";
  return "node";
}

function estimateRecipeGraphNodeDimensions(label: string, type: string) {
  const typeLabel = getRecipeGraphNodeTypeLabel(type);
  const maxCharsPerLine = 34;
  const longestLineChars = Math.max(label.length, typeLabel.length, 12);
  const labelLineCount = Math.max(1, Math.ceil(label.length / maxCharsPerLine));
  const typeLineCount = Math.max(1, Math.ceil(typeLabel.length / maxCharsPerLine));
  const width = Math.max(190, Math.min(440, Math.round(longestLineChars * 7.1 + 34)));
  const height = Math.max(58, 18 + (labelLineCount + typeLineCount) * 18);
  return { width, height };
}

function getRecipeGraphEdgeWeight(relation?: string) {
  if (relation === "depends_on") return 5;
  if (relation === "macro_applied") return 4;
  if (relation === "publish_reports") return 4;
  if (relation?.startsWith("publish_report_")) return 4;
  if (relation === "publish_dataset") return 4;
  if (relation === "function_used") return 2;
  return 1;
}

function buildRecipeDependencyFlowModel(nodes: RunGraphNode[], edges: RunGraphContent["edges"], compact: boolean) {
  const dimensionsById = new Map<string, { width: number; height: number }>();
  nodes.forEach((node) => dimensionsById.set(node.id, estimateRecipeGraphNodeDimensions(node.label, node.type)));

  const graph = new dagre.graphlib.Graph({ multigraph: false, compound: false });
  graph.setGraph({
    rankdir: "TB",
    ranksep: compact ? 72 : 88,
    nodesep: compact ? 52 : 96,
    edgesep: 52,
    marginx: compact ? 16 : 28,
    marginy: compact ? 24 : 28,
    ranker: "network-simplex",
    acyclicer: "greedy",
  });
  graph.setDefaultEdgeLabel(() => ({}));

  const middleNodes = nodes.filter((node) => recipeGraphLaneForType(node.type) === "node");
  middleNodes.forEach((node) => graph.setNode(node.id, dimensionsById.get(node.id) ?? { width: 190, height: 58 }));
  edges.forEach((edge) => {
    if (graph.hasNode(edge.from_id) && graph.hasNode(edge.to_id)) {
      graph.setEdge(edge.from_id, edge.to_id, { weight: getRecipeGraphEdgeWeight(edge.relation), minlen: 1 });
    }
  });
  dagre.layout(graph);

  const positions = new Map<string, { x: number; y: number }>();
  const laneGap = compact ? 120 : 150;
  const columnGap = compact ? 32 : 70;
  const topLane = nodes.filter((node) => recipeGraphLaneForType(node.type) === "input");
  const bottomLane = nodes.filter((node) => recipeGraphLaneForType(node.type) === "output");
  const middleLayout = middleNodes.map((node, index) => {
    const layoutNode = graph.node(node.id) as { x: number; y: number } | undefined;
    return { node, x: layoutNode?.x ?? index * 240, y: layoutNode?.y ?? 0 };
  });
  const middleMinY = middleLayout.length ? Math.min(...middleLayout.map((item) => item.y)) : 0;
  const middleMaxY = middleLayout.length ? Math.max(...middleLayout.map((item) => item.y)) : 0;
  const middleTop = topLane.length ? laneGap : 0;
  middleLayout.forEach((item) => {
    positions.set(item.node.id, { x: item.x, y: middleTop + item.y - middleMinY });
  });

  const middleMaxX = middleLayout.length ? Math.max(...middleLayout.map((item) => item.x)) : 0;
  const laneWidth = Math.max(0, middleMaxX, (Math.max(topLane.length, bottomLane.length) - 1) * 240);
  const layoutLane = (laneNodes: RunGraphNode[], y: number) => {
    const totalWidth = laneNodes.reduce((sum, node) => sum + (dimensionsById.get(node.id)?.width ?? 190), 0) + Math.max(0, laneNodes.length - 1) * columnGap;
    let cursor = (laneWidth - totalWidth) / 2;
    laneNodes.forEach((node) => {
      const dimensions = dimensionsById.get(node.id) ?? { width: 190, height: 58 };
      positions.set(node.id, { x: cursor + dimensions.width / 2, y });
      cursor += dimensions.width + columnGap;
    });
  };
  layoutLane(topLane, 0);
  layoutLane(bottomLane, middleTop + Math.max(0, middleMaxY - middleMinY) + laneGap);

  return {
    layout: {
      node: (id: string) => positions.get(id) ?? { x: 0, y: 0 },
    },
    dimensionsById,
  };
}

function graphBorderColor(status: string) {
  const normalized = status.toLowerCase();
  if (normalized === "failed") return "rgb(248 113 113)";
  if (normalized === "completed" || normalized === "success" || normalized === "skipped" || normalized === "available") return "rgb(34 197 94)";
  if (normalized === "running" || normalized === "in_progress") return "rgb(100 116 139)";
  return "rgb(148 163 184)";
}

function getStatusPresentation(status: string): { className: string; icon: ReactNode } {
  const normalized = status.toLowerCase();
  if (normalized === "failed") return { className: "border-red-200 bg-red-50 text-red-600", icon: <AlertCircle /> };
  if (normalized === "completed" || normalized === "success" || normalized === "skipped" || normalized === "available") return { className: "border-emerald-200 bg-emerald-50 text-emerald-600", icon: <CheckCircle2 /> };
  if (normalized === "running" || normalized === "in_progress") return { className: "border-slate-300 bg-white text-slate-600", icon: <Play /> };
  return { className: "border-slate-200 bg-slate-50 text-slate-400", icon: <span className="h-2.5 w-2.5 rounded-full bg-current" /> };
}

function RecipeView({ client, projectId }: { client: InspectionClient; projectId: string }) {
  const recipeState = usePolling(() => client.recipe(projectId, "recipe.sql"), [client, projectId], 5000);
  if (recipeState.error) return <ErrorState message={recipeState.error} />;
  if (!recipeState.value) return <LoadingState />;
  const recipe = recipeState.value.recipe;
  return <main className="min-h-[calc(100vh-10rem)]"><CommonDetailsComponent title={recipe.name} subtitle={recipe.path}><ResourceDetailHeader title={recipe.name} subtitle={recipe.path} actions={<StatusBadge status={recipe.status} />} /><ResourceField label="recipe.sql"><ResourceReadonlyTextarea value={recipe.source_text} rows={30} /></ResourceField></CommonDetailsComponent></main>;
}

function ResourceView({ client, projectId, project, kind }: { client: InspectionClient; projectId: string; project: ProjectInspection; kind: ResourceKind }) {
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const resources = kind === "fixtures" ? project.resources.fixtures : kind === "inputs" ? project.resources.inputs.map(inputToResource) : project.resources.secrets.map(secretToResource);
  const filtered = resources.filter((resource) => `${resource.name} ${resource.status}`.toLowerCase().includes(query.toLowerCase()));
  const selected = filtered.find((resource) => resource.id === selectedId) ?? null;
  const title = kind === "fixtures" ? "Fixtures" : kind === "inputs" ? "Inputs" : "Secrets";
  const columns: ResourceTableColumn<InspectionResource>[] = [
    { id: "name", header: "Name", cell: (resource) => <span className="font-medium text-slate-900">{resource.name}</span> },
    { id: "status", header: "Status", width: "10rem", align: "right", cell: (resource) => <StatusBadge status={resource.status} /> },
  ];
  return <main className="grid min-h-[calc(100vh-10rem)] gap-5 xl:grid-cols-[minmax(20rem,0.8fr)_minmax(0,1.7fr)]"><CommonListComponent search={<ResourceSearchInput placeholder={`Search ${title.toLowerCase()}`} value={query} onChange={setQuery} />} hasItems={filtered.length > 0} emptyState={<div className="p-6 text-sm text-slate-500">No {title.toLowerCase()} are used by this recipe.</div>}><ResourceTable rows={filtered} columns={columns} getRowId={(resource) => resource.id} selectedRowId={selectedId} onRowOpen={(resource) => setSelectedId(resource.id)} mobileTitle={(resource) => resource.name} mobileMeta={(resource) => <StatusBadge status={resource.status} />} /></CommonListComponent><ResourceDetails client={client} projectId={projectId} resource={selected} kind={kind} /></main>;
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
  if (!resource) return <CommonDetailsComponent title="Select a resource" subtitle="Choose an item from the list to inspect it."><ResourceDetailHeader title="Select a resource" subtitle="Choose an item from the list to inspect it." /></CommonDetailsComponent>;
  if (fixtureState.error) return <ErrorState message={fixtureState.error} />;
  const details = resource.details ?? {};
  const fixture = fixtureState.value?.fixture;
  const icon: ReactNode = kind === "fixtures" ? <Boxes className="h-4 w-4" /> : kind === "inputs" ? <SlidersHorizontal className="h-4 w-4" /> : <KeyRound className="h-4 w-4" />;
  const subtitle = kind === "fixtures" ? "Recipe fixture" : kind === "inputs" ? "Recipe input" : "Recipe secret";
  return <CommonDetailsComponent title={resource.name} subtitle={subtitle}><ResourceDetailHeader title={<span className="flex items-center gap-2">{icon}{resource.name}</span>} subtitle={subtitle} actions={<StatusBadge status={resource.status} />} /><div className="space-y-5"><div className="grid gap-4 sm:grid-cols-2">{Object.entries(details).map(([key, value]) => <ResourceField key={key} label={key.replaceAll("_", " ")}><p className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 font-mono text-sm text-slate-700">{Array.isArray(value) ? value.join(", ") : text(value)}</p></ResourceField>)}</div>{fixture?.source_text ? <ResourceField label="Source"><ResourceReadonlyTextarea value={fixture.source_text} rows={22} /></ResourceField> : null}</div></CommonDetailsComponent>;
}
