import { BookOpen, Boxes, ClipboardCheck, KeyRound, Menu, Play, SlidersHorizontal, X } from "lucide-react";
import { useState, type ComponentType, type ReactNode } from "react";

export type InspectionSection = "run" | "recipe" | "inputs" | "fixtures" | "annotations" | "secrets" | "overview" | "recipes" | "runs";

type InspectionShellProps = {
  projectName: string;
  activeSection: InspectionSection;
  onNavigate: (section: InspectionSection) => void;
  children: ReactNode;
};

const navigation: Array<{ id: InspectionSection; label: string; icon: typeof Play }> = [
  { id: "run", label: "Home", icon: Play },
  { id: "recipe", label: "Recipe", icon: BookOpen },
  { id: "inputs", label: "Inputs", icon: SlidersHorizontal },
  { id: "fixtures", label: "Fixtures", icon: Boxes },
  { id: "annotations", label: "Annotations", icon: ClipboardCheck },
  { id: "secrets", label: "Secrets", icon: KeyRound },
];

export function ServiceSidebarNavigationItem({ label, icon: Icon, active, onClick }: { label: string; icon: ComponentType<{ className?: string }>; active: boolean; onClick: () => void }) {
  return <button onClick={onClick} className={`relative flex w-full items-center gap-3 rounded-lg px-3 py-2 text-sm transition-all ${active ? "bg-slate-100 text-slate-900" : "text-gray-600 hover:bg-gray-50 hover:text-gray-900"}`}>
    {active ? <span className="absolute left-0 top-1/2 h-8 w-1 -translate-y-1/2 rounded-r-full bg-slate-700" /> : null}<Icon className={`h-4 w-4 ${active ? "text-slate-700" : ""}`} />{label}
  </button>;
}

export function InspectionShell({ projectName, activeSection, onNavigate, children }: InspectionShellProps) {
  const [mobileOpen, setMobileOpen] = useState(false);
  const sideNav = <nav className="flex-1 space-y-1 p-4">{navigation.map((item) => <ServiceSidebarNavigationItem key={item.id} label={item.label} icon={item.icon} active={item.id === activeSection} onClick={() => { onNavigate(item.id); setMobileOpen(false); }} />)}</nav>;
  return <div className="flex min-h-screen bg-slate-50 text-slate-900">
    <aside className="hidden w-64 shrink-0 flex-col border-r border-gray-200 bg-white shadow-sm lg:flex"><div className="border-b border-gray-200 bg-slate-50 p-6"><h1 className="text-lg font-normal">agent<span className="font-light text-emerald-700">CICD</span></h1></div>{sideNav}</aside>
    {mobileOpen ? <div className="fixed inset-0 z-20 bg-slate-950/30 lg:hidden" onClick={() => setMobileOpen(false)}><aside className="h-full w-64 bg-white shadow-xl" onClick={(event) => event.stopPropagation()}><div className="flex items-center justify-between border-b border-gray-200 bg-slate-50 p-5"><h1 className="text-lg">agent<span className="font-light text-emerald-700">CICD</span></h1><button className="rounded-md p-2 hover:bg-slate-200" onClick={() => setMobileOpen(false)} aria-label="Close navigation"><X className="h-4 w-4" /></button></div>{sideNav}</aside></div> : null}
    <main className="min-w-0 flex-1"><header className="flex h-16 items-center gap-3 border-b border-gray-200 bg-white px-4 lg:px-6"><button className="rounded-md p-2 hover:bg-slate-100 lg:hidden" onClick={() => setMobileOpen(true)} aria-label="Open navigation"><Menu className="h-5 w-5" /></button><div className="min-w-0"><p className="truncate text-sm font-medium">{projectName}</p><p className="text-xs text-slate-500">Evaluation inspection</p></div></header><div className="min-h-[calc(100vh-4rem)] p-4 lg:p-6">{children}</div></main>
  </div>;
}
