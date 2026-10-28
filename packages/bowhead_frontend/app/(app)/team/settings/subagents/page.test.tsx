import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const t = (key: string, values?: { count?: number }) =>
  values?.count === undefined ? key : `${key}:${values.count}`;
vi.mock("next-intl", () => ({ useTranslations: () => t }));
vi.mock("@/components/settings/settings-shell", () => ({
  SettingsShell: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

const listBuiltinSubagentsMock = vi.fn();
const listSubagentsMock = vi.fn();
const replaceMock = vi.fn();
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ replace: replaceMock }) }));
vi.mock("@/lib/team-subagents-api", () => ({
  listBuiltinSubagents: (...args: unknown[]) => listBuiltinSubagentsMock(...args),
  listSubagents: (...args: unknown[]) => listSubagentsMock(...args),
  getSkillsCatalog: vi.fn().mockResolvedValue({ builtin: [], team_skills: [] }),
  putBuiltinSubagentOverride: vi.fn(),
  clearBuiltinSubagentOverride: vi.fn(),
}));

const getAgentRunLimitsMock = vi.fn().mockResolvedValue(null);
vi.mock("@/components/settings/execution-limits", () => ({ ExecutionLimitsSettings: () => <div>Sandbox and HTML limits</div> }));
vi.mock("@/lib/agent-run-limits-api", () => ({
  getAgentRunLimits: (...args: unknown[]) => getAgentRunLimitsMock(...args),
  saveAgentRunLimits: vi.fn(),
  resetAgentRunLimits: vi.fn(),
}));

import SubagentsSettingsPage from "./page";

const builtin = (name = "consultant") => ({
  name,
  enabled: true,
  model: "analyst",
  description: "系统内置分析 Agent",
  display_name: "",
  max_iterations: 100,
  primary_capability: "cap_analysis",
  primary_capability_options: ["cap_analysis"],
  skills: [],
  prompt_append: "",
  defaults: {
    model: "analyst",
    description: "系统内置分析 Agent",
    max_iterations: 100,
    primary_capability: "cap_analysis",
  },
  overridden_fields: [],
  override: null,
  row_version: 0,
  etag: `"builtin:${name}:0"`,
  disable_warning: null,
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  window.history.replaceState(null, "", "/");
});

describe("SubagentsSettingsPage", () => {
  it("sends an old Owner deep link to the Agent manager", async () => {
    window.history.replaceState(null, "", "/team/settings/subagents/?agent_id=osa_1");
    listBuiltinSubagentsMock.mockResolvedValue({ items: [] });
    render(<SubagentsSettingsPage />);

    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/agents/manage/?agent_id=osa_1"));
  });

  it("loads and renders only the system built-in catalog", async () => {
    listBuiltinSubagentsMock.mockResolvedValue({
      items: [builtin(), builtin("compute")],
      manageable: false,
    });

    render(<SubagentsSettingsPage />);

    expect(await screen.findByTestId("builtin-row-consultant")).toBeTruthy();
    expect(screen.getByTestId("builtin-row-compute")).toBeTruthy();
    expect(screen.getByText("count:2")).toBeTruthy();
    expect(listBuiltinSubagentsMock).toHaveBeenCalledTimes(1);
    expect(listSubagentsMock).not.toHaveBeenCalled();
  });

  it("keeps loading visible until the built-in catalog resolves", async () => {
    let resolveList: ((value: { items: ReturnType<typeof builtin>[]; manageable: boolean }) => void) | undefined;
    listBuiltinSubagentsMock.mockReturnValue(new Promise((resolve) => {
      resolveList = resolve;
    }));

    render(<SubagentsSettingsPage />);

    expect(screen.getByRole("status", { name: "loading" })).toBeTruthy();
    expect(screen.queryByText("builtin_empty_title")).toBeNull();

    await act(async () => {
      resolveList?.({ items: [builtin()], manageable: false });
    });
    expect(await screen.findByTestId("builtin-row-consultant")).toBeTruthy();
    expect(screen.queryByRole("status", { name: "loading" })).toBeNull();
  });

  it("shows the built-in empty state only after a successful empty response", async () => {
    listBuiltinSubagentsMock.mockResolvedValue({ items: [], manageable: false });
    render(<SubagentsSettingsPage />);
    expect(await screen.findByText("builtin_empty_title")).toBeTruthy();
  });

  it("shows an error without presenting an empty catalog", async () => {
    listBuiltinSubagentsMock.mockRejectedValue(new Error("boom"));
    render(<SubagentsSettingsPage />);
    await waitFor(() => expect(screen.getByText("boom")).toBeTruthy());
    expect(screen.queryByText("builtin_empty_title")).toBeNull();
    listBuiltinSubagentsMock.mockResolvedValueOnce({ items: [builtin()], manageable: true });
    fireEvent.click(screen.getByRole("button", { name: "retry" }));
    expect(await screen.findByTestId("builtin-row-consultant")).toBeTruthy();
    expect(screen.queryByText("boom")).toBeNull();
  });
});

describe("SubagentsSettingsPage run limits tab", () => {
  it("adds the Run limits tab when the deployment enables agent run limits", async () => {
    listBuiltinSubagentsMock.mockResolvedValue({ items: [builtin()], manageable: true });
    getAgentRunLimitsMock.mockResolvedValueOnce({
      turnTimeLimitS: 1800,
      stepLimit: null,
      toolCallLimit: 300,
      wrapUpReserveS: 300,
      isDefault: true,
      version: "default",
      defaults: { turnTimeLimitS: 1800, stepLimit: null, toolCallLimit: 300 },
      ranges: { turnTimeLimitS: { min: 600, max: 36000 }, stepLimit: { min: 1, max: 10000 }, toolCallLimit: { min: 1, max: 10000 } },
    });
    render(<SubagentsSettingsPage />);
    const runLimitsTab = await screen.findByRole("tab", { name: "tab_run_limits" });
    expect(screen.getByRole("tab", { name: "tab_builtin" })).toBeTruthy();
    fireEvent.click(runLimitsTab);
    await screen.findByText("turn_time_title");
    fireEvent.change(screen.getByLabelText("turn_time_title"), { target: { value: "60" } });
    fireEvent.click(screen.getByRole("tab", { name: "tab_builtin" }));
    fireEvent.click(screen.getByRole("tab", { name: "tab_run_limits" }));
    expect((screen.getByLabelText("turn_time_title") as HTMLInputElement).value).toBe("60");
    expect(listBuiltinSubagentsMock).toHaveBeenCalledTimes(1);
  });

  it("keeps sandbox and HTML settings available when loop limits are off", async () => {
    listBuiltinSubagentsMock.mockResolvedValue({ items: [builtin()], manageable: true });
    getAgentRunLimitsMock.mockResolvedValueOnce(null);
    render(<SubagentsSettingsPage />);
    await waitFor(() => expect(listBuiltinSubagentsMock).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("tab", { name: "tab_run_limits" }));
    expect(screen.getByText("Sandbox and HTML limits")).toBeTruthy();
  });
});
