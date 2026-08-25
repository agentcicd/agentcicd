import { StrictMode, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import { HttpInspectionClient, ProjectInspector, RunInspector } from "../src";

function currentPath(): { projectId: string | null; runId: string | null } {
  const match = window.location.pathname.match(/^\/(projects|runs)\/([^/]+)/);
  if (!match) return { projectId: null, runId: null };
  return match[1] === "projects" ? { projectId: decodeURIComponent(match[2]), runId: null } : { projectId: null, runId: decodeURIComponent(match[2]) };
}

function App() {
  const client = useMemo(() => new HttpInspectionClient(), []);
  const [{ projectId, runId }, setLocation] = useState(currentPath);
  const openRun = (id: string) => {
    window.history.pushState({}, "", `/runs/${encodeURIComponent(id)}/`);
    setLocation({ projectId: null, runId: id });
  };
  if (runId) return <RunInspector client={client} runId={runId} />;
  if (projectId) return <ProjectInspector client={client} projectId={projectId} onSelectRun={openRun} />;
  return <main className="ac-inspector"><p className="ac-error">This URL does not identify an inspection project or run.</p></main>;
}

createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
