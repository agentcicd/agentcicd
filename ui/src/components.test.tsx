import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { InputAndSecretReferences, ProjectInspector, RunSummary } from "./components";
import { LabelStudioRenderer } from "./label-studio-renderer";
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
      run: async () => { throw new Error("not used"); }, progress: async () => { throw new Error("not used"); }, logs: async () => { throw new Error("not used"); }, graph: async () => { throw new Error("not used"); }, report: async () => { throw new Error("not used"); }, tables: async () => ({ items: [] }), tableRows: async () => { throw new Error("not used"); }, traces: async () => ({ items: [] }), traceSpans: async () => ({ records: [] }),
      annotationRequests: async () => ({ items: [], total: 0 }),
      annotationRequest: async () => { throw new Error("not used"); },
      annotationTasks: async () => ({ request_id: "", tasks: [], total: 0, completed: 0 }),
      annotationTask: async () => { throw new Error("not used"); },
      submitAnnotationReview: async () => { throw new Error("not used"); },
      finalizeAnnotationRequest: async () => { throw new Error("not used"); },
      runtimePools: async () => ({ run_id: "", nodes: [], leases: [] }),
      runtimeRateLimits: async () => ({ run_id: "", leases: [] }),
    };
    render(<ProjectInspector client={client} projectId="local-1" />);
    await screen.findByRole("heading", { name: "demo" });
    fireEvent.click(screen.getByRole("button", { name: "Recipe" }));
    await waitFor(() => expect(screen.getAllByText("recipe.sql")).toHaveLength(2));
    fireEvent.click(screen.getAllByText("recipe.sql")[0]);
    await waitFor(() => expect(screen.getByDisplayValue("SELECT 1;")).not.toBeNull());
  });

  it("renders Label Studio controls and emits annotation results", async () => {
    let latest: Array<Record<string, unknown>> = [];
    render(
      <LabelStudioRenderer
        config={`<View>
  <Text name="prompt" value="$prompt"/>
  <Choices name="quality" toName="prompt" choice="single">
    <Choice value="pass"/>
    <Choice value="fail"/>
  </Choices>
  <Rating name="rating" toName="prompt" maxRating="3"/>
  <TextArea name="notes" toName="prompt" placeholder="Notes"/>
</View>`}
        task={{ id: "task-1", data: { prompt: "Check the answer" } }}
        onChange={(annotation) => {
          latest = annotation.result;
        }}
      />,
    );

    expect(screen.getByText("Check the answer")).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "pass" }));
    fireEvent.click(screen.getByRole("button", { name: "2" }));
    fireEvent.change(screen.getByPlaceholderText("Notes"), { target: { value: "Looks correct" } });

    await waitFor(() => expect(latest.length).toBe(3));
    expect(JSON.stringify(latest)).toContain("pass");
    expect(JSON.stringify(latest)).toContain("Looks correct");
  });

  it("shows invalid Label Studio template errors", () => {
    render(<LabelStudioRenderer config={'<Text name="text"/>'} task={{ id: "task-1", data: {} }} />);
    expect(screen.getByText("Invalid Label Studio template XML")).not.toBeNull();
  });
});
