import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const mocks = vi.hoisted(() => ({ list: vi.fn(), get: vi.fn(), approve: vi.fn(), remove: vi.fn() }));
vi.mock("next-intl", () => ({
  useTranslations:
    () => (key: string, values?: Record<string, unknown>) =>
      values ? `${key} ${JSON.stringify(values)}` : key,
}));
vi.mock("@/lib/team-tool-sources-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/team-tool-sources-api")>();
  return { ...actual, listN8nWorkflows: mocks.list, getN8nWorkflow: mocks.get, approveN8nWorkflow: mocks.approve, removeN8nWorkflow: mocks.remove };
});
import { TeamToolApiError, type TeamToolSourcePublic } from "@/lib/team-tool-sources-api";
import { SourceN8nWorkflowsDialog } from "./source-n8n-workflows-dialog";

const source = { id: "ots_n8n", name: "Sales n8n" } as TeamToolSourcePublic;
const list = {
  items: [{ id: "wfMoM", name: "MoM", description: "Month-over-month sales", updated_at: null, listed: true, approval: null }],
  hidden_count: 2,
  more_than_listed: false,
  approval_limit: 64,
};
const detail = {
  workflow: { id: "wfMoM", name: "MoM", description: "Month-over-month sales", active_version_id: "v1" },
  trigger: { node: "Webhook", kind: "webhook" },
  problem: null,
  draft_input_schema: { type: "object", properties: { month: {} }, additionalProperties: true },
  approval: null,
  version_changed: false,
};

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("n8n Workflows", () => {
  it("lists approvable workflows, counts the others and approves one with its schema", async () => {
    mocks.list.mockResolvedValue(list);
    mocks.get.mockResolvedValue(detail);
    mocks.approve.mockResolvedValue({ approval: {} });
    const onChanged = vi.fn();
    render(<SourceN8nWorkflowsDialog source={source} onClose={() => undefined} onChanged={onChanged} />);
    expect(await screen.findByText(/n8n_hidden/)).toBeTruthy();
    fireEvent.click(screen.getByLabelText('n8n_open_label {"name":"MoM"}'));
    const schema = (await screen.findByLabelText("n8n_schema_label")) as HTMLTextAreaElement;
    expect(JSON.parse(schema.value).properties).toEqual({ month: {} });
    const approve = screen.getByText("n8n_approve") as HTMLButtonElement;
    expect(approve.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("n8n_category_label"), { target: { value: "read" } });
    fireEvent.change(schema, { target: { value: '{"type":"object","properties":{"month":{"type":"string"}},"required":["month"]}' } });
    fireEvent.click(screen.getByText("n8n_approve"));
    await waitFor(() => expect(mocks.approve).toHaveBeenCalled());
    expect(mocks.approve.mock.calls[0]).toEqual([
      "ots_n8n",
      "wfMoM",
      {
        category: "read",
        description: "Month-over-month sales",
        input_schema: { type: "object", properties: { month: { type: "string" } }, required: ["month"] },
      },
    ]);
    expect(onChanged).toHaveBeenCalled();
    expect(await screen.findByText(/n8n_approved_notice/)).toBeTruthy();
  });

  it("refuses broken JSON locally and shows why n8n refused an approval", async () => {
    mocks.list.mockResolvedValue(list);
    mocks.get.mockResolvedValue(detail);
    render(<SourceN8nWorkflowsDialog source={source} onClose={() => undefined} onChanged={() => undefined} />);
    fireEvent.click(await screen.findByLabelText('n8n_open_label {"name":"MoM"}'));
    fireEvent.change(await screen.findByLabelText("n8n_category_label"), { target: { value: "external_write" } });
    fireEvent.change(screen.getByLabelText("n8n_schema_label"), { target: { value: "{" } });
    fireEvent.click(screen.getByText("n8n_approve"));
    expect(await screen.findByText("n8n_error_schema_json")).toBeTruthy();
    expect(mocks.approve).not.toHaveBeenCalled();

    mocks.approve.mockRejectedValue(
      new TeamToolApiError(422, "x", "n8n_workflow_not_approvable", undefined, { reason_code: "cannot_execute", reason: "…" }),
    );
    fireEvent.change(screen.getByLabelText("n8n_schema_label"), { target: { value: '{"type":"object"}' } });
    fireEvent.click(screen.getByText("n8n_approve"));
    expect(await screen.findByText("n8n_problem_cannot_execute")).toBeTruthy();
  });

  it("shows a workflow that cannot be approved without a form", async () => {
    mocks.list.mockResolvedValue(list);
    mocks.get.mockResolvedValue({ ...detail, trigger: null, problem: { code: "no_supported_trigger", message: "…" } });
    render(<SourceN8nWorkflowsDialog source={source} onClose={() => undefined} onChanged={() => undefined} />);
    fireEvent.click(await screen.findByLabelText('n8n_open_label {"name":"MoM"}'));
    expect(await screen.findByText(/n8n_problem_no_supported_trigger/)).toBeTruthy();
    expect(screen.queryByLabelText("n8n_category_label")).toBeNull();
    expect((screen.getByText("n8n_approve") as HTMLButtonElement).disabled).toBe(true);
  });
});
