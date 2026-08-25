import { AlertTriangle, ArrowDown, ArrowUp, ArrowUpDown, BarChart3, ChevronDown, ChevronRight, FileText } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";
import { Bar, BarChart, CartesianGrid, Cell, Legend, Line, LineChart, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { JsonValue } from "./types";
import { ServiceCard, StatusBadge } from "./service-primitives";

type RecordValue = Record<string, JsonValue>;

function isRecord(value: JsonValue | unknown): value is RecordValue {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function text(value: JsonValue | undefined): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}

function flatten(value: RecordValue, prefix = "", output: Record<string, string> = {}): Record<string, string> {
  Object.entries(value).forEach(([key, item]) => {
    if (key === "visible" || key === "hidden" || item === null || item === undefined) return;
    const path = prefix ? `${prefix}.${key}` : key;
    if (isRecord(item)) flatten(item, path, output);
    else output[path] = text(item);
  });
  return output;
}

function columns(rows: RecordValue[], preferred: string[]): string[] {
  const discovered = new Set(rows.flatMap((row) => Object.keys(flatten(row))));
  return [...preferred.filter((item) => discovered.delete(item)), ...[...discovered].sort()];
}

export function ServiceDataTable({ rows, preferredColumns = [] }: { rows: RecordValue[]; preferredColumns?: string[] }) {
  const flattened = useMemo(() => rows.map((row) => flatten(row)), [rows]);
  const headers = useMemo(() => columns(rows, preferredColumns), [rows, preferredColumns]);
  const [sort, setSort] = useState<{ column: string; descending: boolean } | null>(null);
  const ordered = useMemo(() => {
    if (!sort) return flattened;
    return [...flattened].sort((left, right) => {
      const result = (left[sort.column] ?? "").localeCompare(right[sort.column] ?? "", undefined, { numeric: true });
      return sort.descending ? -result : result;
    });
  }, [flattened, sort]);
  if (!rows.length) return <div className="rounded-lg border border-slate-200 bg-slate-50 p-6 text-center text-sm text-slate-500">No rows available.</div>;
  return <ServiceCard className="mt-3 overflow-hidden"><div className="max-w-full overflow-x-auto"><table className="min-w-max text-sm"><thead className="border-b border-slate-200 bg-slate-50 text-left text-xs text-slate-500"><tr>{headers.map((column) => <th className="px-4 py-3 font-medium" key={column}><button className="flex min-w-40 items-center justify-between gap-3" onClick={() => setSort((current) => current?.column === column ? { column, descending: !current.descending } : { column, descending: false })}>{column}<span>{sort?.column === column ? sort.descending ? <ArrowDown className="h-3.5 w-3.5" /> : <ArrowUp className="h-3.5 w-3.5" /> : <ArrowUpDown className="h-3.5 w-3.5 text-slate-300" />}</span></button></th>)}</tr></thead><tbody>{ordered.map((row, rowIndex) => <tr className="border-b border-slate-100 last:border-0" key={rowIndex}>{headers.map((column) => <td className="max-w-96 px-4 py-3 align-top text-slate-700 whitespace-normal" key={column}>{row[column] || "-"}</td>)}</tr>)}</tbody></table></div></ServiceCard>;
}

type ChartRow = Record<string, string | number>;

function chartRows(chart: RecordValue): ChartRow[] {
  const value = chart.data;
  if (!Array.isArray(value)) return [];
  return value.filter(isRecord).map((row) => Object.fromEntries(Object.entries(row).map(([key, item]) => [key, typeof item === "number" || typeof item === "string" ? item : text(item)])));
}

function chartField(chart: RecordValue, keys: string[], fallback: string): string {
  for (const key of keys) { const value = text(chart[key]); if (value) return value; }
  return fallback;
}

export function ServiceReportContent({ description, metrics, issues, charts }: { description?: string; metrics: RecordValue[]; issues: RecordValue[]; charts: RecordValue[] }) {
  return <div className="space-y-6">
    {description ? <ServiceCard className="p-4"><p className="text-xs font-medium uppercase tracking-wide text-slate-500">Description</p><p className="mt-2 whitespace-pre-wrap text-sm text-slate-700">{description}</p></ServiceCard> : null}
    <section><h3 className="text-sm font-medium text-slate-900">Charts</h3>{charts.length ? <div className="mt-3 grid gap-4 lg:grid-cols-2">{charts.map((chart, index) => <ServiceChart key={index} chart={chart} index={index} />)}</div> : <Empty icon={<BarChart3 className="h-5 w-5" />} label="No charts" />}</section>
    <section><h3 className="text-sm font-medium text-slate-900">Metrics <span className="text-slate-500">({metrics.length})</span></h3>{metrics.length ? <ServiceDataTable rows={metrics} preferredColumns={["metric", "metric_name", "value", "metric_value", "score", "result"]} /> : <Empty icon={<FileText className="h-5 w-5" />} label="No metrics" />}</section>
    <section><h3 className="text-sm font-medium text-slate-900">Issues <span className="text-slate-500">({issues.length})</span></h3>{issues.length ? <ServiceDataTable rows={issues} preferredColumns={["issue", "title", "name", "message", "severity", "status"]} /> : <Empty icon={<AlertTriangle className="h-5 w-5" />} label="No issues" />}</section>
  </div>;
}

function Empty({ icon, label }: { icon: ReactNode; label: string }) { return <div className="mt-3 flex flex-col items-center gap-2 rounded-lg border border-slate-200 bg-slate-50 p-6 text-sm text-slate-500">{icon}<span>{label}</span></div>; }

function ServiceChart({ chart, index }: { chart: RecordValue; index: number }) {
  const rows = chartRows(chart);
  const title = chartField(chart, ["title", "name", "chart"], `Chart ${index + 1}`);
  const description = chartField(chart, ["description", "summary"], "");
  const type = chartField(chart, ["type", "chart_type", "visualization"], "bar").toLowerCase();
  const x = chartField(chart, ["x", "x_field", "label_field", "category"], Object.keys(rows[0] ?? {})[0] ?? "label");
  const y = chartField(chart, ["y", "y_field", "value_field", "value"], Object.keys(rows[0] ?? {})[1] ?? "value");
  return <ServiceCard className="p-4"><div className="flex items-start gap-3"><div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-slate-100 text-slate-600"><BarChart3 className="h-4 w-4" /></div><div className="min-w-0"><h4 className="truncate text-sm font-medium text-slate-900">{title}</h4>{description ? <p className="mt-1 text-sm text-slate-600">{description}</p> : null}</div></div>{rows.length ? <div className="mt-4 h-72"><ResponsiveContainer width="100%" height="100%">{type === "line" ? <LineChart data={rows}><CartesianGrid strokeDasharray="3 3" /><XAxis dataKey={x} /><YAxis /><Tooltip /><Legend /><Line dataKey={y} stroke="#64748b" strokeWidth={2} /></LineChart> : type === "pie" || type === "donut" ? <PieChart><Tooltip /><Legend /><Pie data={rows} dataKey={y} nameKey={x} innerRadius={type === "donut" ? 55 : 0}>{rows.map((_, item) => <Cell fill={["#64748b", "#2563eb", "#10b981", "#f59e0b", "#ef4444"][item % 5]} key={item} />)}</Pie></PieChart> : <BarChart data={rows}><CartesianGrid strokeDasharray="3 3" /><XAxis dataKey={x} /><YAxis /><Tooltip /><Legend /><Bar dataKey={y} fill="#64748b" radius={[4, 4, 0, 0]} /></BarChart>}</ResponsiveContainer></div> : <div className="mt-4 rounded-md border border-dashed border-slate-200 bg-slate-50 px-3 py-8 text-center text-sm text-slate-500">No chart rows available.</div>}</ServiceCard>;
}

export function ServiceTraceWaterfall({ records }: { records: RecordValue[] }) {
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  if (!records.length) return <Empty icon={<FileText className="h-5 w-5" />} label="Full trace records are not available yet." />;
  const start = Math.min(...records.map((item, index) => Date.parse(text(item.started_at)) || index));
  const end = Math.max(...records.map((item, index) => (Date.parse(text(item.started_at)) || index) + Number(item.duration_ms || 1)));
  const total = Math.max(1, end - start);
  return <ServiceCard className="overflow-hidden">{records.map((record, index) => { const id = text(record.span_id) || text(record.call_id) || String(index); const duration = Math.max(1, Number(record.duration_ms || 1)); const offset = ((Date.parse(text(record.started_at)) || index) - start) / total * 100; const failed = ["failed", "error"].includes(text(record.status).toLowerCase()); return <div className="grid grid-cols-[minmax(15rem,1fr)_minmax(12rem,2fr)] divide-x divide-gray-100 border-b border-gray-100 last:border-0" key={id}><button className="flex items-center gap-2 px-3 py-2 text-left" onClick={() => setCollapsed((current) => { const next = new Set(current); next.has(id) ? next.delete(id) : next.add(id); return next; })}>{collapsed.has(id) ? <ChevronRight className="h-3.5 w-3.5 text-gray-400" /> : <ChevronDown className="h-3.5 w-3.5 text-gray-400" />}<span className={`h-2 w-2 rounded-full ${failed ? "bg-red-500" : "bg-sky-500"}`} /><span className="truncate text-sm font-medium">{text(record.name) || id}</span><StatusBadge status={text(record.status)} showIcon={false} /></button><div className="relative px-3 py-2"><div className="relative h-4"><span className="absolute top-1/2 -translate-y-1/2 whitespace-nowrap font-mono text-[11px] text-gray-500" style={{ right: `${Math.max(0, 100 - offset)}%`, marginRight: "0.375rem" }}>{duration} ms</span><div className={`h-4 rounded ${failed ? "bg-red-500" : "bg-sky-500"}`} style={{ marginLeft: `${Math.max(0, Math.min(95, offset))}%`, width: `${Math.max(1, Math.min(100, duration / total * 100))}%` }} /></div></div></div>; })}</ServiceCard>;
}
