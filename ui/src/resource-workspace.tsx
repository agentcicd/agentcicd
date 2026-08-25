import { Search } from "lucide-react";
import { Fragment, type ReactNode } from "react";

import { cn, ServiceCard } from "./service-primitives";

export const RESOURCE_LIST_SCROLL_CLASS = "space-y-3 flex-1 min-h-0 overflow-y-auto pr-2";

export function ResourcePanel({ className, children }: { className?: string; children: ReactNode }) {
  return <ServiceCard data-resource-panel="true" className={cn("p-6 flex-1 h-full min-h-0 flex flex-col w-full rounded-none border-slate-200 shadow-sm hover:shadow-sm", className)}>{children}</ServiceCard>;
}

export function ResourceSearchInput({ placeholder, value, onChange }: { placeholder: string; value: string; onChange: (value: string) => void }) {
  return <div className="relative flex-1"><Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" /><input placeholder={placeholder} value={value} onChange={(event) => onChange(event.target.value)} className="h-9 w-full rounded-none border-x-0 border-b border-t-0 border-b-slate-200 bg-transparent pl-10 text-sm shadow-none outline-none placeholder:text-slate-400 focus:border-b-slate-400" /></div>;
}

export function ResourceListToolbar({ search, actions }: { search: ReactNode; actions?: ReactNode }) {
  return <div className="mb-2 flex min-w-0 flex-col gap-3 sm:flex-row sm:items-center"><div className="min-w-0 flex-1">{search}</div>{actions ? <div className="flex min-w-0 flex-wrap items-center justify-start gap-2 sm:justify-end">{actions}</div> : null}</div>;
}

export function CommonListComponent({ search, actions, hasItems, isLoading = false, loadingLabel = "Loading...", emptyState, children, listClassName = RESOURCE_LIST_SCROLL_CLASS, panelClassName }: { search: ReactNode; actions?: ReactNode; hasItems: boolean; isLoading?: boolean; loadingLabel?: string; emptyState?: ReactNode; children: ReactNode; listClassName?: string; panelClassName?: string }) {
  return <div className="flex h-full min-h-0 flex-col"><ResourcePanel className={panelClassName}><ResourceListToolbar search={search} actions={actions} />{hasItems ? <div className={listClassName}>{children}</div> : isLoading ? <div className="m-3 rounded-lg border border-slate-200 bg-white p-4"><p className="text-sm text-gray-500">{loadingLabel}</p></div> : emptyState ?? null}</ResourcePanel></div>;
}

export function ResourceDetailHeader({ title, subtitle, actions }: { title: ReactNode; subtitle?: ReactNode; actions?: ReactNode }) {
  return <div className="mb-4 flex min-w-0 flex-col items-stretch gap-3 sm:flex-row sm:items-start sm:justify-between"><div className="min-w-0 flex-1"><h2 className="text-xl font-semibold text-slate-900">{title}</h2>{subtitle ? <p className="mt-1 text-sm text-slate-500">{subtitle}</p> : null}</div>{actions ? <div className="flex min-w-0 flex-wrap items-center justify-start gap-2 sm:justify-end">{actions}</div> : null}</div>;
}

export function CommonDetailsComponent({ title, subtitle, actions, tabs, children, panelClassName, wrapperClassName = "h-full min-h-0 flex flex-col" }: { title: ReactNode; subtitle?: ReactNode; actions?: ReactNode; tabs?: ReactNode; children: ReactNode; panelClassName?: string; wrapperClassName?: string }) {
  void title;
  void subtitle;
  return <div className={wrapperClassName}><ResourcePanel className={panelClassName}>{tabs || actions ? <div className="mb-4 flex min-w-0 flex-col items-stretch gap-3 lg:flex-row lg:items-start lg:justify-between"><div className="min-w-0 flex-1">{tabs}</div>{actions ? <div className="flex min-w-0 flex-wrap items-center justify-start gap-2 lg:justify-end">{actions}</div> : null}</div> : null}{children}</ResourcePanel></div>;
}

export function CommonFormComponent({ title, subtitle, actions, children, footer, panelClassName, wrapperClassName = "h-full min-h-0 flex flex-col", contentClassName = "flex-1 space-y-6 min-h-0" }: { title: ReactNode; subtitle?: ReactNode; actions?: ReactNode; children: ReactNode; footer?: ReactNode; panelClassName?: string; wrapperClassName?: string; contentClassName?: string }) {
  const hasHeader = Boolean(title || subtitle || actions);
  return <div className={wrapperClassName}><ResourcePanel className={panelClassName}>{hasHeader ? <ResourceDetailHeader title={title} subtitle={subtitle} actions={actions} /> : null}<div className={contentClassName}>{children}</div>{footer ? <div className="mt-auto shrink-0 border-t border-slate-200 pt-4">{footer}</div> : null}</ResourcePanel></div>;
}

export type ResourceTableColumn<T> = { id: string; header: ReactNode; cell: (row: T) => ReactNode; width?: string; align?: "left" | "center" | "right"; hideBelow?: "sm" | "md" | "lg"; className?: string; headerClassName?: string };

function alignClass(align: ResourceTableColumn<unknown>["align"]) { return align === "right" ? "text-right" : align === "center" ? "text-center" : "text-left"; }
function visibilityClass(hideBelow: ResourceTableColumn<unknown>["hideBelow"]) { return hideBelow === "lg" ? "hidden lg:table-cell" : hideBelow === "md" ? "hidden md:table-cell" : hideBelow === "sm" ? "hidden sm:table-cell" : undefined; }

export function ResourceTable<T>({ rows, columns, getRowId, selectedRowId, onRowOpen, rowActions, mobileTitle, mobileSubtitle, mobileMeta, mobileMetaHeader, emptyState, className }: { rows: T[]; columns: ResourceTableColumn<T>[]; getRowId: (row: T) => string; selectedRowId?: string | null; onRowOpen?: (row: T) => void; rowActions?: (row: T) => ReactNode; mobileTitle?: (row: T) => ReactNode; mobileSubtitle?: (row: T) => ReactNode; mobileMeta?: (row: T) => ReactNode; mobileMetaHeader?: ReactNode; emptyState?: ReactNode; className?: string }) {
  if (!rows.length) return <>{emptyState ?? null}</>;
  return <div className={cn("min-h-0 flex-1 overflow-y-auto", className)}><div className="hidden min-w-0 bg-white md:block"><table className="w-full caption-bottom text-sm"><thead className="[&_tr]:border-b-0"><tr className="h-9 rounded-md bg-slate-50 hover:bg-slate-50">{columns.map((column) => <th key={column.id} className={cn("px-3 text-xs font-medium text-slate-500 first:pl-10", alignClass(column.align), visibilityClass(column.hideBelow), column.headerClassName)} style={column.width ? { width: column.width } : undefined}>{column.header}</th>)}{rowActions ? <th className="h-9 w-12 px-3 text-right" /> : null}</tr></thead><tbody className="[&_tr:last-child]:border-b [&_tr:last-child]:border-slate-100">{rows.map((row) => { const rowId = getRowId(row); const selected = selectedRowId === rowId; return <tr key={rowId} className={cn("h-12 border-b border-slate-100 hover:bg-slate-50", onRowOpen && "cursor-pointer", selected && "bg-slate-50 hover:bg-slate-50")} onClick={() => onRowOpen?.(row)}>{columns.map((column) => <td key={column.id} className={cn("px-3 py-2 text-sm text-slate-700 first:pl-10", alignClass(column.align), visibilityClass(column.hideBelow), column.className)}>{column.cell(row)}</td>)}{rowActions ? <td className="w-12 px-3 py-2 text-right" onClick={(event) => event.stopPropagation()}>{rowActions(row)}</td> : null}</tr>; })}</tbody></table></div><div className="bg-white md:hidden"><div className="grid grid-cols-[minmax(0,1fr)_auto] gap-3 rounded-md bg-slate-50 px-3 py-2 text-xs font-medium text-slate-500"><div className="min-w-0 truncate">{columns[0]?.header}</div><div className="justify-self-end whitespace-nowrap">{mobileMetaHeader ?? columns.find((column) => column.id !== columns[0]?.id && column.hideBelow !== "md")?.header ?? ""}</div></div><div className="mt-2">{rows.map((row) => { const rowId = getRowId(row); const selected = selectedRowId === rowId; return <button key={rowId} type="button" className={cn("grid w-full grid-cols-[minmax(0,1fr)_auto] items-center gap-3 border-b border-slate-100 px-3 py-2.5 text-left transition last:border-b-0 hover:bg-slate-50", selected && "bg-slate-50")} onClick={() => onRowOpen?.(row)}><div className="min-w-0"><div className="truncate text-sm font-medium text-slate-900">{mobileTitle ? mobileTitle(row) : columns[0]?.cell(row)}</div>{mobileSubtitle ? <div className="mt-0.5 truncate text-xs text-slate-500">{mobileSubtitle(row)}</div> : null}</div>{mobileMeta ? <div className="shrink-0 justify-self-end">{mobileMeta(row)}</div> : null}</button>; })}</div></div></div>;
}

export function ResourceField({ label, children, className }: { label: string; children: ReactNode; className?: string }) { return <div className={className}><p className="mb-2 text-sm text-slate-600">{label}</p>{children}</div>; }

export function ResourceReadonlyTextarea({ value, rows = 4 }: { value?: string | null; rows?: number }) { return <textarea value={value ?? ""} rows={rows} disabled className="w-full resize-y rounded-md border border-gray-200 bg-slate-50 px-3 py-2 font-mono text-sm text-slate-700 disabled:opacity-100" />; }

export function CommonProgressSteps({ steps, currentStep, compact = false }: { steps: ReadonlyArray<{ id: string; label: string }>; currentStep: number; compact?: boolean }) {
  return <div className={compact ? "px-2" : "mb-5 px-8 pb-2"}><div className={`grid w-full items-center ${compact ? "gap-x-2 text-xs" : "gap-x-3.5 text-sm"}`} style={{ gridTemplateColumns: steps.map((_, index) => index < steps.length - 1 ? "auto minmax(0, 1fr)" : "auto").join(" ") }}>{steps.map((step, index) => <Fragment key={step.id}><span className={`min-w-0 text-center ${index === currentStep ? "font-bold text-slate-950" : "font-normal text-slate-400"}`}>{step.label}</span>{index < steps.length - 1 ? <span aria-hidden="true" className="block h-px w-full bg-slate-200" /> : null}</Fragment>)}</div></div>;
}
