import { CheckCircle2, Clock, Loader2, XCircle } from "lucide-react";
import type { ComponentProps, ReactNode } from "react";
import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...values: ClassValue[]): string {
  return twMerge(clsx(values));
}

export function ServiceCard({ className, ...props }: ComponentProps<"section">) {
  return <section className={cn("min-w-0 rounded-lg border border-slate-200 bg-white", className)} {...props} />;
}

export function ServiceBadge({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={cn("inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-xs font-medium", className)}>{children}</span>;
}

export function StatusBadge({ status, showIcon = true, className }: { status?: string; showIcon?: boolean; className?: string }) {
  const normalized = status?.trim().toLowerCase() || "unknown";
  const presentation = statusPresentation(normalized, status);
  return <ServiceBadge className={cn(presentation.className, className)}>{showIcon ? presentation.icon : null}{presentation.label}</ServiceBadge>;
}

function statusPresentation(normalized: string, original?: string): { label: string; className: string; icon: ReactNode } {
  if (["success", "completed", "active", "available"].includes(normalized)) return { label: normalized === "available" ? "Available" : normalized[0].toUpperCase() + normalized.slice(1), className: "border-green-200 bg-green-50 text-green-700", icon: <CheckCircle2 className="h-3 w-3" /> };
  if (["failed", "error"].includes(normalized)) return { label: normalized === "error" ? "Error" : "Failed", className: "border-red-200 bg-red-50 text-red-700", icon: <XCircle className="h-3 w-3" /> };
  if (["running", "in_progress", "in-progress", "in progress"].includes(normalized)) return { label: "Running", className: "border-slate-200 bg-slate-50 text-slate-700", icon: <Loader2 className="h-3 w-3 animate-spin" /> };
  if (normalized === "waiting_for_annotation") return { label: "Waiting for annotation", className: "border-amber-200 bg-amber-50 text-amber-700", icon: <Clock className="h-3 w-3 animate-pulse" /> };
  if (["cancelling", "canceling"].includes(normalized)) return { label: "Cancelling", className: "border-orange-200 bg-orange-50 text-orange-700", icon: <Loader2 className="h-3 w-3 animate-spin" /> };
  if (["queued", "creating"].includes(normalized)) return { label: "Creating", className: "border-yellow-200 bg-yellow-50 text-yellow-700", icon: <Clock className="h-3 w-3 animate-pulse" /> };
  if (["cancelled", "canceled", "inactive", "deleted"].includes(normalized)) return { label: normalized === "inactive" ? "Inactive" : normalized[0].toUpperCase() + normalized.slice(1), className: "border-gray-200 bg-gray-50 text-gray-700", icon: <XCircle className="h-3 w-3" /> };
  return { label: original || "Unknown", className: "border-gray-200 bg-gray-50 text-gray-700", icon: <Clock className="h-3 w-3" /> };
}
