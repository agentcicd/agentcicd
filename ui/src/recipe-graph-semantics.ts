import * as dagre from "dagre";

import type { RunGraphContent, RunGraphNode } from "./types";

export type RecipeGraphNodeStyle = {
  fill: string;
  stroke: string;
  text: string;
};

export function normalizeRecipeGraphType(type: string) {
  const normalized = String(type || "").toLowerCase();
  if (normalized.startsWith("function")) return normalized;
  if (normalized === "declare_input") return "input";
  if (normalized === "load_table") return "load";
  if (normalized === "create_batch_table" || normalized === "create_stream_table") return "table";
  if (normalized === "save_table") return "save";
  if (normalized === "publish_reports" || normalized === "publish_report") return "publish_report_other";
  return normalized;
}

export function getRecipeGraphNodeStyle(type: string): RecipeGraphNodeStyle {
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

export function getRecipeGraphNodeTypeLabel(type: string) {
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

export function recipeGraphLaneForType(type: string): "input" | "node" | "output" {
  const normalized = normalizeRecipeGraphType(type);
  if (["input", "macro", "secret", "aisystem", "fixture", "image", "fixture_kind", "vault"].includes(normalized)) return "input";
  if (normalized === "save" || normalized === "publish" || normalized.startsWith("publish_") || normalized === "report" || normalized === "annotation_queue") return "output";
  return "node";
}

export function graphBorderColor(status: string) {
  const normalized = status.toLowerCase();
  if (normalized === "failed") return "rgb(248 113 113)";
  if (normalized === "completed" || normalized === "success" || normalized === "skipped" || normalized === "available") return "rgb(34 197 94)";
  if (normalized === "running" || normalized === "in_progress") return "rgb(100 116 139)";
  return "rgb(148 163 184)";
}

export function statusColor(status: string) {
  if (status === "completed") return "#16a34a";
  if (status === "available") return "#16a34a";
  if (status === "running") return "#475569";
  if (status === "failed") return "#dc2626";
  return "#94a3b8";
}

export function buildRecipeDependencyFlowModel(nodes: RunGraphNode[], edges: RunGraphContent["edges"], compact: boolean) {
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
