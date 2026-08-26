import { AlertCircle, CheckCircle2, Play } from "lucide-react";
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
import type { JsonValue, RunGraphContent, RunGraphNode } from "./types";

export function RunGraph({ graph }: { graph: RunGraphContent }) {
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
