import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { InputAndSecretReferences, ProjectInspector, RunSummary } from "./components";
import type { InspectionClient, ProjectInspection, RunInspection } from "./types";

describe("inspection components", () => {
  it("shows secret references without values", () => {
    const project: ProjectInspection = {
      schema_version: "inspection-v1", project: { id: "local-1", name: "demo", source: "local" }, capabilities: { compare: false, rerun: true, cancel: false, annotate: false, open_external_resource: false },
      resources: { recipes: [], fixtures: [], inputs: [], secrets: [{ reference: "secret.openai", type: "api_key", configured: true }], runs: [] },
    };
    render(<InputAndSecretReferences project={project} />);
    expect(screen.getByText("secret.openai")).not.toBeNull();
    expect(screen.queryByText("sk-local-secret")).toBeNull();
  });

  it("renders run summary counts", () => {
    const run: RunInspection = { schema_version: "inspection-v1", run: { id: "run-1", status: "completed", started_at: null, finished_at: null, attempt: 1, source: "local" }, report_summary: { metrics_count: 2, issues_count: 1, charts_count: 0 }, execution_summary: { stage_count: 4, completed_stage_count: 4 }, capabilities: { compare: false, rerun: true, cancel: false, annotate: false, open_external_resource: false } };
    render(<RunSummary run={run} />);
    expect(screen.getByText("2")).not.toBeNull();
    expect(screen.getByText("1")).not.toBeNull();
  });

  it("opens a selected recipe in the shared list and detail workspace", async () => {
    const client: InspectionClient = {
      project: async () => ({ schema_version: "inspection-v1", project: { id: "local-1", name: "demo", source: "local" }, capabilities: { compare: false, rerun: false, cancel: false, annotate: false, open_external_resource: false }, resources: { recipes: [{ id: "recipe.sql", name: "recipe.sql", status: "available" }], fixtures: [], inputs: [], secrets: [], runs: [] } }),
      recipes: async () => ({ items: [] }),
      recipe: async () => ({ recipe: { id: "recipe.sql", name: "recipe.sql", status: "available", source_text: "SELECT 1;" } }),
      fixtures: async () => ({ items: [] }), fixture: async () => ({ fixture: { id: "fixture.py", name: "fixture.py", status: "available" } }),
      inputs: async () => ({ items: [] }), secrets: async () => ({ items: [] }), runs: async () => ({ items: [] }),
      run: async () => { throw new Error("not used"); }, progress: async () => { throw new Error("not used"); }, report: async () => { throw new Error("not used"); }, tables: async () => ({ items: [] }), tableRows: async () => { throw new Error("not used"); }, traces: async () => ({ items: [] }), traceSpans: async () => ({ records: [] }),
    };
    render(<ProjectInspector client={client} projectId="local-1" />);
    await screen.findByRole("heading", { name: "demo" });
    fireEvent.click(screen.getByRole("button", { name: "Recipes" }));
    await waitFor(() => expect(screen.getAllByText("recipe.sql")).toHaveLength(2));
    fireEvent.click(screen.getAllByText("recipe.sql")[0]);
    await waitFor(() => expect(screen.getByDisplayValue("SELECT 1;")).not.toBeNull());
  });
});
