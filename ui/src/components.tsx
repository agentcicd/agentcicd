import { AlertCircle, BookOpen, Boxes, KeyRound, Play, RefreshCw, ShieldCheck, Table2, XCircle } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import type { InspectionActions, InspectionClient, InspectionResource, JsonValue, ProjectInspection, RunInspection } from "./types";
import { ServiceCard, StatusBadge } from "./service-primitives";
import { ServiceDataTable, ServiceReportContent, ServiceTraceWaterfall } from "./service-renderers";
import { InspectionShell, type InspectionSection } from "./inspection-shell";
import { CommonDetailsComponent, CommonListComponent, ResourceDetailHeader, ResourceField, ResourceReadonlyTextarea, ResourceSearchInput, ResourceTable, type ResourceTableColumn } from "./resource-workspace";

function useLoaded<T>(load: () => Promise<T>, dependencies: readonly unknown[], intervalMs?: number): { value: T | null; error: string | null } {
  const [value, setValue] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    const refresh = () => load().then((item) => {
      if (active) { setValue(item); setError(null); }
    }).catch((reason: unknown) => {
      if (active) setError(reason instanceof Error ? reason.message : "Unable to load inspection data.");
    });
    void refresh();
    const timer = intervalMs ? window.setInterval(() => void refresh(), intervalMs) : undefined;
    return () => { active = false; if (timer) window.clearInterval(timer); };
  // Consumers supply stable client/id dependencies.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, dependencies);
  return { value, error };
}

function Status({ value }: { value: string }) { return <StatusBadge status={value} />; }
function ErrorState({ message }: { message: string }) { return <p className="ac-error"><AlertCircle size={16} />{message}</p>; }
function Loading() { return <p className="ac-muted">Loading inspection data...</p>; }
function text(value: JsonValue | undefined): string { return typeof value === "string" || typeof value === "number" || typeof value === "boolean" ? String(value) : "-"; }

export function RecipeInspector({ client, projectId, recipeId }: { client: InspectionClient; projectId: string; recipeId: string }) {
  const { value, error } = useLoaded(() => client.recipe(projectId, recipeId), [client, projectId, recipeId]);
  if (error) return <ErrorState message={error} />;
  if (!value) return <Loading />;
  return <ServiceCard className="p-4"><h2 className="mb-3 flex items-center gap-2 text-base font-medium text-slate-900"><BookOpen size={18} />{value.recipe.name}</h2><pre>{value.recipe.source_text || "No recipe source is available."}</pre></ServiceCard>;
}

export function FixtureInspector({ client, projectId, fixtureId }: { client: InspectionClient; projectId: string; fixtureId: string }) {
  const { value, error } = useLoaded(() => client.fixture(projectId, fixtureId), [client, projectId, fixtureId]);
  if (error) return <ErrorState message={error} />;
  if (!value) return <Loading />;
  return <ServiceCard className="p-4"><h2 className="mb-3 flex items-center gap-2 text-base font-medium text-slate-900"><Boxes size={18} />{value.fixture.name}</h2><Status value={value.fixture.status} /><pre>{value.fixture.source_text || "No fixture source is available."}</pre></ServiceCard>;
}

export function InputAndSecretReferences({ project }: { project: ProjectInspection }) {
  return <div className="ac-grid ac-grid--two">
    <ServiceCard className="p-4"><h2 className="mb-3 text-base font-medium text-slate-900">Inputs</h2><KeyValueRows items={project.resources.inputs} empty="No configured inputs." /></ServiceCard>
    <ServiceCard className="p-4"><h2 className="mb-3 flex items-center gap-2 text-base font-medium text-slate-900"><KeyRound size={18} />Secret references</h2><KeyValueRows items={project.resources.secrets} empty="No configured secret references." /></ServiceCard>
  </div>;
}

function KeyValueRows({ items, empty }: { items: Array<Record<string, JsonValue>>; empty: string }) {
  if (!items.length) return <p className="ac-muted">{empty}</p>;
  return <div className="divide-y divide-slate-100">{items.map((item, index) => <div className="flex items-center justify-between gap-3 px-1 py-2 text-sm" key={`${text(item.name)}-${index}`}>
    <strong>{text(item.name) !== "-" ? text(item.name) : text(item.reference)}</strong><span>{text(item.type)}</span><span>{text(item.value_preview) === "-" ? text(item.configured) : text(item.value_preview)}</span>
  </div>)}</div>;
}

export function ProjectInspector({ client, projectId, onSelectRun }: { client: InspectionClient; projectId: string; onSelectRun?: (runId: string) => void }) {
  const { value: project, error } = useLoaded(() => client.project(projectId), [client, projectId], 3000);
  const [section, setSection] = useState<InspectionSection>("overview");
  if (error) return <ErrorState message={error} />;
  if (!project) return <Loading />;
  const content = section === "overview"
    ? <ProjectOverview project={project} onNavigate={setSection} />
    : <ProjectResourceWorkspace client={client} projectId={projectId} project={project} section={section} onSelectRun={onSelectRun} />;
  return <InspectionShell projectName={project.project.name} activeSection={legacySectionNavigation(section)} onNavigate={(nextSection) => setSection(legacySectionSelection(nextSection))}>{content}</InspectionShell>;
}

function legacySectionNavigation(section: InspectionSection): InspectionSection {
  if (section === "recipes") return "recipe";
  if (section === "runs") return "run";
  return section;
}

function legacySectionSelection(section: InspectionSection): InspectionSection {
  if (section === "recipe") return "recipes";
  if (section === "run") return "runs";
  return section;
}

function ProjectOverview({ project, onNavigate }: { project: ProjectInspection; onNavigate: (section: InspectionSection) => void }) {
  const summary: Array<{ section: InspectionSection; label: string; count: number; icon: JSX.Element }> = [
    { section: "recipes", label: "Recipes", count: project.resources.recipes.length, icon: <BookOpen size={18} /> },
    { section: "fixtures", label: "Fixtures", count: project.resources.fixtures.length, icon: <Boxes size={18} /> },
    { section: "runs", label: "Runs", count: project.resources.runs.length, icon: <Play size={18} /> },
  ];
  return <main className="space-y-6"><header className="flex items-start justify-between"><div><p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Project overview</p><h1 className="mt-1 text-2xl font-medium">{project.project.name}</h1></div><Status value={project.project.source} /></header><div className="grid gap-4 md:grid-cols-3">{summary.map((item) => <button key={item.section} onClick={() => onNavigate(item.section)} className="rounded-lg border border-slate-200 bg-white p-5 text-left transition hover:border-slate-300 hover:bg-slate-50"><div className="flex items-center justify-between text-slate-600">{item.icon}<span className="text-2xl font-semibold text-slate-900">{item.count}</span></div><p className="mt-4 text-sm font-medium text-slate-900">{item.label}</p></button>)}</div><InputAndSecretReferences project={project} /></main>;
}

type ProjectResourceKind = Exclude<InspectionSection, "overview">;
type ProjectResourceRow = InspectionResource & { kind: ProjectResourceKind; source?: string; value?: JsonValue };

function ProjectResourceWorkspace({ client, projectId, project, section, onSelectRun }: { client: InspectionClient; projectId: string; project: ProjectInspection; section: ProjectResourceKind; onSelectRun?: (runId: string) => void }) {
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const rows = useMemo((): ProjectResourceRow[] => {
    if (section === "recipes") return project.resources.recipes.map((item) => ({ ...item, kind: section }));
    if (section === "fixtures") return project.resources.fixtures.map((item) => ({ ...item, kind: section }));
    if (section === "runs") return project.resources.runs.map((item) => ({ id: item.id, name: item.id, status: item.status, details: { started_at: item.started_at ?? null, finished_at: item.finished_at ?? null, attempt: item.attempt ?? null }, kind: section }));
    const entries = section === "inputs" ? project.resources.inputs : project.resources.secrets;
    return entries.map((item, index) => ({ id: `${section}:${text(item.name) !== "-" ? text(item.name) : index}`, name: text(item.name) !== "-" ? text(item.name) : text(item.reference), status: text(item.configured) === "true" ? "configured" : "available", details: item, kind: section, source: text(item.path), value: item.value_preview }));
  }, [project, section]);
  const filteredRows = rows.filter((row) => `${row.name} ${row.status}`.toLowerCase().includes(query.toLowerCase()));
  const selected = filteredRows.find((row) => row.id === selectedId) ?? null;
  const title = section[0].toUpperCase() + section.slice(1);
  const columns: ResourceTableColumn<ProjectResourceRow>[] = [
    { id: "name", header: "Name", cell: (row) => <span className="font-medium text-slate-900">{row.name}</span> },
    { id: "status", header: "Status", cell: (row) => <Status value={row.status} />, width: "9rem", align: "right" },
  ];
  return <div className="grid min-h-[calc(100vh-10rem)] gap-6 xl:grid-cols-[minmax(20rem,0.8fr)_minmax(0,1.7fr)]"><CommonListComponent search={<ResourceSearchInput placeholder={`Search ${title.toLowerCase()}`} value={query} onChange={setQuery} />} hasItems={filteredRows.length > 0} emptyState={<div className="p-6 text-sm text-slate-500">No {title.toLowerCase()} found.</div>}><ResourceTable rows={filteredRows} columns={columns} getRowId={(row) => row.id} selectedRowId={selectedId} onRowOpen={(row) => { setSelectedId(row.id); if (row.kind === "runs") onSelectRun?.(row.id); }} mobileTitle={(row) => row.name} mobileMeta={(row) => <Status value={row.status} />} /></CommonListComponent><ProjectResourceDetail client={client} projectId={projectId} row={selected} /></div>;
}

function ProjectResourceDetail({ client, projectId, row }: { client: InspectionClient; projectId: string; row: ProjectResourceRow | null }) {
  const result = useLoaded(async () => {
    if (!row || row.kind === "runs" || row.kind === "inputs" || row.kind === "secrets") return null;
    return row.kind === "recipes" ? client.recipe(projectId, row.id) : client.fixture(projectId, row.id);
  }, [client, projectId, row?.id, row?.kind]);
  if (!row) return <CommonDetailsComponent title="Select a resource" subtitle="Choose an item from the list to inspect its configuration and source."><ResourceDetailHeader title="Select a resource" subtitle="Choose an item from the list to inspect its configuration and source." /></CommonDetailsComponent>;
  const source = result.value && "recipe" in result.value ? result.value.recipe.source_text : result.value && "fixture" in result.value ? result.value.fixture.source_text : undefined;
  const details = row.details ?? {};
  return <CommonDetailsComponent title={row.name} subtitle={row.kind === "secrets" ? "Secret reference" : row.kind} actions={<Status value={row.status} />}><ResourceDetailHeader title={row.name} subtitle={row.kind === "secrets" ? "Secret reference" : row.kind} /><div className="space-y-6">{result.error ? <ErrorState message={result.error} /> : null}<div className="grid gap-4 sm:grid-cols-2">{Object.entries(details).map(([key, value]) => <ResourceField key={key} label={key.replaceAll("_", " ")}><p className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-700">{text(value)}</p></ResourceField>)}</div>{source ? <ResourceField label="Source"><ResourceReadonlyTextarea value={source} rows={18} /></ResourceField> : null}</div></CommonDetailsComponent>;
}

export function RunSummary({ run }: { run: RunInspection }) {
  const execution = run.execution_summary;
  return <section className="ac-summary"><div><span>Metrics</span><strong>{run.report_summary.metrics_count}</strong></div><div><span>Issues</span><strong>{run.report_summary.issues_count}</strong></div><div><span>Stages</span><strong>{execution.completed_stage_count ?? 0}/{execution.stage_count ?? "?"}</strong></div><div><span>Row errors</span><strong>{execution.row_error_count ?? 0}</strong></div></section>;
}

export function ExecutionTimeline({ client, runId }: { client: InspectionClient; runId: string }) {
  const { value, error } = useLoaded(() => client.progress(runId), [client, runId], 1500);
  if (error) return <ErrorState message={error} />;
  if (!value) return <Loading />;
  return <ServiceCard className="p-4"><h2 className="mb-3 text-base font-medium text-slate-900">Execution</h2>{value.steps.length ? <ol className="divide-y divide-slate-100">{value.steps.map((step, index) => <li className="flex items-center gap-3 py-2 text-sm" key={`${text(step.id)}-${index}`}><Status value={text(step.status)} /><span>{text(step.name) !== "-" ? text(step.name) : text(step.stage)}</span></li>)}</ol> : <p className="text-sm text-slate-500">Waiting for execution progress.</p>}</ServiceCard>;
}

export function ChartView({ charts }: { charts: Array<Record<string, JsonValue>> }) {
  if (!charts.length) return <p className="ac-muted">No charts were generated.</p>;
  return <div className="ac-list">{charts.map((chart, index) => <pre key={index}>{JSON.stringify(chart, null, 2)}</pre>)}</div>;
}

export function ReportView({ client, runId }: { client: InspectionClient; runId: string }) {
  const { value, error } = useLoaded(() => client.report(runId), [client, runId], 3000);
  if (error) return <ErrorState message={error} />;
  if (!value) return <Loading />;
  return <section className="space-y-4"><h2 className="flex items-center gap-2 text-base font-medium text-slate-900"><ShieldCheck size={18} />Report</h2><ServiceReportContent metrics={value.metrics} issues={value.issues} charts={value.charts} /></section>;
}

export function StageEvidenceView({ client, runId }: { client: InspectionClient; runId: string }) {
  const { value: tables, error } = useLoaded(() => client.tables(runId), [client, runId], 3000);
  const [selected, setSelected] = useState<string | null>(null);
  if (error) return <ErrorState message={error} />;
  if (!tables) return <Loading />;
  return <section className="space-y-3"><h2 className="flex items-center gap-2 text-base font-medium text-slate-900"><Table2 size={18} />Evidence</h2><ServiceDataTable rows={tables.items} preferredColumns={["name", "row_count", "row_error_count", "cell_error_count", "status"]} />{selected && <TableRows client={client} runId={runId} tableName={selected} />}</section>;
}

function TableRows({ client, runId, tableName }: { client: InspectionClient; runId: string; tableName: string }) {
  const { value, error } = useLoaded(() => client.tableRows(runId, tableName, 1, 25), [client, runId, tableName]);
  if (error) return <ErrorState message={error} />;
  if (!value) return <Loading />;
  return <ServiceDataTable rows={value.rows} preferredColumns={value.columns} />;
}

export function TraceView({ client, runId }: { client: InspectionClient; runId: string }) {
  const { value, error } = useLoaded(() => client.traces(runId), [client, runId], 3000);
  const [selected, setSelected] = useState<string | null>(null);
  const records = useLoaded(() => selected ? client.traceSpans(runId, selected, 1, 100) : Promise.resolve(null), [client, runId, selected]);
  if (error || records.error) return <ErrorState message={error || records.error || "Unable to load traces."} />;
  if (!value) return <Loading />;
  return <section className="space-y-3"><h2 className="text-base font-medium text-slate-900">Traces</h2><div className="flex flex-wrap gap-2">{value.items.map((item) => <button className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-700 hover:bg-slate-100" onClick={() => setSelected(item.id)} key={item.id}>{item.id}</button>)}</div>{selected && records.value ? <ServiceTraceWaterfall records={records.value.records} /> : null}</section>;
}

export function RunInspector({ client, runId, actions }: { client: InspectionClient; runId: string; actions?: InspectionActions }) {
  const { value: run, error } = useLoaded(() => client.run(runId), [client, runId], 1500);
  if (error) return <ErrorState message={error} />;
  if (!run) return <Loading />;
  const navigate = (section: InspectionSection) => {
    if (section === "runs") return;
    if (run.project_id) window.location.assign(`/projects/${encodeURIComponent(run.project_id)}/`);
  };
  return <InspectionShell projectName={run.project_name || "AgentCICD"} activeSection="runs" onNavigate={navigate}><main className="space-y-6"><header className="flex items-start justify-between"><div><p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Evaluation run</p><h1 className="mt-1 text-2xl font-medium">{run.run.id}</h1></div><div className="flex items-center gap-2"><Status value={run.run.status} />{run.capabilities.rerun && actions?.rerun && <button title="Rerun" onClick={() => actions.rerun?.(runId)}><RefreshCw size={16} /></button>}{run.capabilities.cancel && actions?.cancel && <button title="Cancel" onClick={() => actions.cancel?.(runId)}><XCircle size={16} /></button>}</div></header><RunSummary run={run} /><div className="grid gap-6 xl:grid-cols-2"><ExecutionTimeline client={client} runId={runId} /><ReportView client={client} runId={runId} /></div><StageEvidenceView client={client} runId={runId} /><TraceView client={client} runId={runId} /></main></InspectionShell>;
}
