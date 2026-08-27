import { AlertCircle, BarChart3, CheckCircle2, FileText, Play, Table2, X } from "lucide-react";
import { Background, Controls, MarkerType, Position, ReactFlow, type Edge, type Node } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useEffect, useMemo, useState, type ReactNode } from "react";

import {
  buildRecipeDependencyFlowModel,
  getRecipeGraphNodeStyle,
  getRecipeGraphNodeTypeLabel,
  graphBorderColor,
  normalizeRecipeGraphType,
  recipeGraphLaneForType,
  statusColor,
} from "./recipe-graph-semantics";
import { ServiceCard, StatusBadge } from "./service-primitives";
import { ServiceDataTable, ServiceReportContent } from "./service-renderers";
import type { InspectionClient, JsonValue, ReportContent, RunGraphContent, RunGraphNode, RunLogsContent, RunTableRows } from "./types";

type DrawerSelection =
  | { kind: "node"; nodeId: string }
  | { kind: "logs" }
  | { kind: "results" };

export function RunGraph({ client, runId, graph, logs, report }: { client: InspectionClient; runId: string; graph: RunGraphContent; logs: RunLogsContent; report: ReportContent }) {
  const [selection, setSelection] = useState<DrawerSelection | null>(null);
  const selectedNodeId = selection?.kind === "node" ? selection.nodeId : null;
  const selectedNode = selectedNodeId ? graph.nodes.find((node) => node.id === selectedNodeId) ?? null : null;
  const compact = useCompactViewport();
  const flow = useMemo(() => buildFlow(graph, compact), [graph, compact]);
  const hasReport = report.metrics.length > 0 || report.issues.length > 0 || report.charts.length > 0;
  return <ServiceCard className="relative flex h-full min-h-0 flex-col overflow-hidden">
    <div className="flex items-center justify-between border-b border-slate-200 px-5 py-4">
      <div className="flex items-center gap-2"><Play className="h-4 w-4 text-slate-500" /><h2 className="text-sm font-medium text-slate-900">Execution graph</h2></div>
      <div className="flex items-center gap-2">
        {hasReport ? <button type="button" onClick={() => setSelection({ kind: "results" })} className="inline-flex h-8 items-center gap-1.5 rounded-md border border-slate-200 bg-white px-2.5 text-xs font-medium text-slate-700 hover:bg-slate-50"><BarChart3 className="h-3.5 w-3.5" />Results</button> : null}
        <button type="button" onClick={() => setSelection({ kind: "logs" })} className="inline-flex h-8 items-center gap-1.5 rounded-md border border-slate-200 bg-white px-2.5 text-xs font-medium text-slate-700 hover:bg-slate-50"><FileText className="h-3.5 w-3.5" />Logs</button>
      </div>
    </div>
    <div className="block min-h-0 flex-1 overflow-auto bg-[#fbfcfe] p-4 sm:hidden"><MobileGraphList graph={graph} selectedNodeId={selectedNodeId} onSelect={(nodeId) => setSelection({ kind: "node", nodeId })} /></div>
    <div className="hidden min-h-0 flex-1 bg-[#fbfcfe] sm:block"><ReactFlow nodes={flow.nodes} edges={flow.edges} fitView fitViewOptions={{ padding: compact ? 0.08 : 0.16, maxZoom: 1.1 }} minZoom={0.1} maxZoom={2} onNodeClick={(_event, node) => setSelection({ kind: "node", nodeId: node.id })} nodesDraggable={false} nodesConnectable={false} elementsSelectable proOptions={{ hideAttribution: true }}><Background gap={18} size={1} color="#e2e8f0" /><Controls showInteractive={false} /></ReactFlow></div>
    <RunGraphDetailsDrawer client={client} runId={runId} selection={selection} node={selectedNode} logs={logs} report={report} onClose={() => setSelection(null)} />
  </ServiceCard>;
}

function RunGraphDetailsDrawer({ client, runId, selection, node, logs, report, onClose }: { client: InspectionClient; runId: string; selection: DrawerSelection | null; node: RunGraphNode | null; logs: RunLogsContent; report: ReportContent; onClose: () => void }) {
  const open = Boolean(selection);
  const title = selection?.kind === "logs" ? "Logs" : selection?.kind === "results" ? "Results" : node?.label ?? "";
  const subtitle = selection?.kind === "logs" ? `${logs.files.length} log file${logs.files.length === 1 ? "" : "s"}` : selection?.kind === "results" ? "Published results" : node ? node.type.replaceAll("_", " ") : "";
  return <div aria-hidden={!open} className={`fixed inset-0 z-30 transition-colors duration-200 ease-out ${open ? "bg-slate-950/30" : "pointer-events-none bg-transparent"}`} onClick={onClose}>
    <aside className={`absolute inset-y-0 right-0 flex w-full flex-col border-l border-slate-200 bg-white shadow-xl transition-transform duration-200 ease-out md:w-[440px] xl:w-[520px] ${open ? "translate-x-0" : "translate-x-full"}`} onClick={(event) => event.stopPropagation()}>
      <div className="flex items-start justify-between gap-3 border-b border-slate-200 px-4 py-3">
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold text-slate-900">{title}</div>
          {subtitle ? <div className="mt-1 truncate text-xs font-medium uppercase text-slate-500">{subtitle}</div> : null}
        </div>
        <button type="button" className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-slate-500 transition hover:bg-slate-100 hover:text-slate-900" aria-label="Close details" title="Close" onClick={onClose}><X className="h-4 w-4" /></button>
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-4">
        {selection?.kind === "logs" ? <LogDetails logs={logs} /> : selection?.kind === "results" ? <ServiceReportContent metrics={report.metrics} issues={report.issues} charts={report.charts} /> : node ? <GraphNodeDetails client={client} runId={runId} node={node} /> : null}
      </div>
    </aside>
  </div>;
}

function GraphNodeDetails({ client, runId, node }: { client: InspectionClient; runId: string; node: RunGraphNode }) {
  const tableName = tableNameForNode(node);
  const tableRows = useTableRows(client, runId, tableName);
  const detailEntries = Object.entries(node.details).filter(([, value]) => value !== null);
  return <div className="space-y-4">
    <div className="flex items-center justify-between gap-3"><div className="min-w-0"><p className="truncate text-sm font-medium text-slate-900">{node.label}</p><p className="mt-0.5 text-xs uppercase text-slate-500">{node.type.replaceAll("_", " ")}</p></div><StatusBadge status={node.status} /></div>
    {detailEntries.length ? <dl className="grid gap-2 sm:grid-cols-2">{detailEntries.map(([key, value]) => <div key={key}><dt className="text-xs text-slate-500">{key.replaceAll("_", " ")}</dt><dd className="mt-0.5 break-words font-mono text-xs text-slate-700">{text(value)}</dd></div>)}</dl> : null}
    {tableName ? <div className="border-t border-slate-100 pt-4"><div className="mb-2 flex items-center gap-2"><Table2 className="h-4 w-4 text-slate-500" /><h3 className="text-sm font-medium text-slate-900">Rows</h3></div>{tableRows.error ? <p className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700">{tableRows.error}</p> : tableRows.value ? <ServiceDataTable rows={tableRows.value.rows} preferredColumns={tableRows.value.columns} /> : <p className="text-sm text-slate-500">Loading rows...</p>}</div> : null}
  </div>;
}

function LogDetails({ logs }: { logs: RunLogsContent }) {
  return <div>
    <pre className="whitespace-pre-wrap break-words rounded-md border border-slate-200 bg-slate-950 p-3 text-xs leading-5 text-slate-100">{logs.text || "No log text available."}</pre>
  </div>;
}

function useTableRows(client: InspectionClient, runId: string, tableName: string | null): { value: RunTableRows | null; error: string | null } {
  const [value, setValue] = useState<RunTableRows | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!tableName) {
      setValue(null);
      setError(null);
      return;
    }
    let active = true;
    setValue(null);
    setError(null);
    client.tableRows(runId, tableName, 1, 100).then((rows) => {
      if (active) setValue(rows);
    }).catch((reason: unknown) => {
      if (active) setError(reason instanceof Error ? reason.message : "Unable to load rows.");
    });
    return () => {
      active = false;
    };
  }, [client, runId, tableName]);
  return { value, error };
}

function tableNameForNode(node: RunGraphNode): string | null {
  const fromDetails = ["table_name", "table", "name", "source_table"].map((key) => node.details[key]).find((value) => typeof value === "string" && value.trim());
  if (typeof fromDetails === "string") return fromDetails;
  const normalizedType = normalizeRecipeGraphType(node.type);
  if (["table", "load", "retrieve_annotation"].includes(normalizedType) && node.label.trim()) return node.label;
  return null;
}

function MobileGraphList({ graph, selectedNodeId, onSelect }: { graph: RunGraphContent; selectedNodeId: string | null; onSelect: (nodeId: string) => void }) {
  return <div className="space-y-2">
    {graph.nodes.map((node) => {
      const style = getRecipeGraphNodeStyle(node.type);
      const selected = node.id === selectedNodeId;
      return <button key={node.id} onClick={() => onSelect(node.id)} className={`w-full rounded-md border px-3 py-3 text-left ${selected ? "ring-2 ring-slate-300" : ""}`} style={{ borderColor: graphBorderColor(node.status), background: style.fill }}>
        <div className="flex items-center justify-between gap-2"><span className="truncate text-xs font-medium uppercase" style={{ color: style.text }}>{getRecipeGraphNodeTypeLabel(node.type)}</span><span className="h-2 w-2 shrink-0 rounded-full" style={{ backgroundColor: statusColor(node.status) }} /></div>
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
        stroke: isInputEdge ? "#dbe6f0" : active ? "#475569" : "#64748b",
        strokeWidth: isInputEdge ? 1.2 : active ? 2.2 : 1.8,
        opacity: isInputEdge ? 0.72 : 1,
      },
      markerEnd: isInputEdge ? undefined : {
        type: MarkerType.ArrowClosed,
        color: active ? "#475569" : "#64748b",
        width: 16,
        height: 16,
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

function getStatusPresentation(status: string): { className: string; icon: ReactNode } {
  const normalized = status.toLowerCase();
  if (normalized === "failed") return { className: "border-red-200 bg-red-50 text-red-600", icon: <AlertCircle /> };
  if (normalized === "completed" || normalized === "success" || normalized === "skipped" || normalized === "available") return { className: "border-emerald-200 bg-emerald-50 text-emerald-600", icon: <CheckCircle2 /> };
  if (normalized === "running" || normalized === "in_progress") return { className: "border-slate-300 bg-white text-slate-600", icon: <Play /> };
  return { className: "border-slate-200 bg-slate-50 text-slate-400", icon: <span className="h-2.5 w-2.5 rounded-full bg-current" /> };
}

function text(value: JsonValue | undefined): string {
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  return "-";
}
