import { StrictMode, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import { HttpInspectionClient, LocalRunWorkspace } from "../src";

function currentPath(): { projectId: string | null; runId: string | null } {
  const match = window.location.pathname.match(/^\/(projects|runs)\/([^/]+)/);
  if (!match) return { projectId: null, runId: null };
  return match[1] === "projects" ? { projectId: decodeURIComponent(match[2]), runId: null } : { projectId: null, runId: decodeURIComponent(match[2]) };
}

function App() {
  const client = useMemo(() => new HttpInspectionClient(), []);
  const [{ projectId, runId }, setLocation] = useState(currentPath);
  useEffect(() => {
    const onPopState = () => setLocation(currentPath());
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);
  if (runId) return <RunRoute client={client} runId={runId} />;
  if (projectId) return <LocalRunWorkspace client={client} projectId={projectId} runId={null} />;
  return <main className="ac-inspector"><p className="ac-error">This URL does not identify an inspection project or run.</p></main>;
}

function RunRoute({ client, runId }: { client: HttpInspectionClient; runId: string }) {
  const [projectId, setProjectId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    void client.run(runId).then((run) => {
      if (active) setProjectId(run.project_id ?? null);
    }).catch((reason: unknown) => {
      if (active) setError(reason instanceof Error ? reason.message : "Unable to load the run.");
    });
    return () => { active = false; };
  }, [client, runId]);

  if (error) return <main className="ac-inspector"><p className="ac-error">{error}</p></main>;
  if (!projectId) return <main className="ac-inspector"><p className="ac-muted">Loading run inspection...</p></main>;
  return <LocalRunWorkspace client={client} projectId={projectId} runId={runId} />;
}

createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
