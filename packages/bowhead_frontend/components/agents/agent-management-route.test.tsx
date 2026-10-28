import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api-client";

const getAgentWorkingCopyMock = vi.fn();
const getSubagentManagerMock = vi.fn();
const managementReadyMock = vi.fn(() => true);
vi.mock("@/lib/agents-api", () => ({ getAgentWorkingCopy: (...args: unknown[]) => getAgentWorkingCopyMock(...args) }));
vi.mock("@/lib/team-subagents-api", () => ({ getSubagentManager: (...args: unknown[]) => getSubagentManagerMock(...args) }));
vi.mock("@/components/canvas/sections/create-subagent-card", () => ({
  CreateSubagentCard: ({ editing }: { editing: { id: string } }) => <div data-testid="legacy-editor">{editing.id}</div>,
}));
vi.mock("./agent-release-manager", () => ({
  AgentReleaseManager: ({ agentId }: { agentId: string }) => <div data-testid="release-editor">{agentId}</div>,
}));
vi.mock("./agent-management", () => ({ AgentManagement: () => <div>hub</div> }));
vi.mock("./agent-release-review", () => ({ AgentReleaseReview: () => <div>review</div> }));
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ push: vi.fn() }) }));
vi.mock("@/lib/auth/auth-session-context", () => ({
  useAuthSession: () => ({ me: { capabilities: { subagent_management_v2: managementReadyMock() } } }),
}));

import { renderIntl } from "@/__tests__/intl-render";
import { AgentManagementRoute } from "./agent-management-route";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  managementReadyMock.mockReturnValue(true);
  window.history.replaceState(null, "", "/");
});

describe("AgentManagementRoute", () => {
  it("opens the release editor for a revisioned Owner Agent", async () => {
    window.history.replaceState(null, "", "/agents/manage/?agent_id=osa_1");
    getSubagentManagerMock.mockResolvedValue({ id: "osa_1", definition_mode: "revisioned" });
    renderIntl(<AgentManagementRoute />);

    expect(await screen.findByTestId("release-editor")).toHaveProperty("textContent", "osa_1");
    expect(getAgentWorkingCopyMock).not.toHaveBeenCalled();
  });

  it("opens the editable legacy definition when release import is still pending", async () => {
    window.history.replaceState(null, "", "/agents/manage/?agent_id=osa_1");
    getSubagentManagerMock.mockResolvedValue({ id: "osa_1", definition_mode: "legacy_mutable", actions: { edit: true } });
    renderIntl(<AgentManagementRoute />);

    expect(await screen.findByTestId("legacy-editor")).toHaveProperty("textContent", "osa_1");
    expect(getSubagentManagerMock).toHaveBeenCalledWith("osa_1");
  });

  it("keeps an import instruction visible when the legacy definition cannot be edited", async () => {
    window.history.replaceState(null, "", "/agents/manage/?agent_id=osa_1");
    getSubagentManagerMock.mockResolvedValue({ id: "osa_1", definition_mode: "legacy_mutable", actions: { edit: false } });
    renderIntl(<AgentManagementRoute />);

    await waitFor(() => expect(screen.getByRole("alert")).toHaveProperty("textContent", expect.stringContaining("发布基线导入")));
  });

  it("falls back to the release read for an older Owner detail response", async () => {
    window.history.replaceState(null, "", "/agents/manage/?agent_id=osa_1");
    getSubagentManagerMock.mockResolvedValue({ id: "osa_1", actions: { edit: true } });
    getAgentWorkingCopyMock.mockRejectedValue(new ApiError(409, "cutover", { code: "agent_release_cutover_required" }));
    renderIntl(<AgentManagementRoute />);

    expect(await screen.findByTestId("legacy-editor")).toHaveProperty("textContent", "osa_1");
  });

  it("uses the legacy editor when an older deployment has no marketplace capability", async () => {
    window.history.replaceState(null, "", "/agents/manage/?agent_id=osa_1");
    managementReadyMock.mockReturnValue(false);
    getSubagentManagerMock.mockResolvedValue({ id: "osa_1", actions: { edit: true } });
    renderIntl(<AgentManagementRoute />);

    expect(await screen.findByTestId("legacy-editor")).toHaveProperty("textContent", "osa_1");
    expect(getAgentWorkingCopyMock).not.toHaveBeenCalled();
  });

  it("does not open the release editor before marketplace readiness", async () => {
    window.history.replaceState(null, "", "/agents/manage/?agent_id=osa_1");
    managementReadyMock.mockReturnValue(false);
    getSubagentManagerMock.mockResolvedValue({ id: "osa_1", definition_mode: "revisioned", actions: { edit: true } });
    renderIntl(<AgentManagementRoute />);

    await waitFor(() => expect(screen.getByRole("alert")).toHaveProperty("textContent", expect.stringContaining("尚未开放版本管理")));
    expect(screen.queryByTestId("release-editor")).toBeNull();
  });
});

it("resolves a different object and returns to the list without remounting the route", async () => {
  const { NextIntlClientProvider } = await import("next-intl");
  const messages = (await import("@/messages/en-US.json")).default;
  const surface = () => <NextIntlClientProvider locale="en-US" messages={messages}><AgentManagementRoute /></NextIntlClientProvider>;
  window.history.replaceState(null, "", "/agents/manage/created/");
  const rendered = render(surface());
  expect(screen.getByText("hub")).toBeTruthy();
  getSubagentManagerMock.mockImplementation((id: string) => Promise.resolve({ id, definition_mode: "revisioned" }));
  window.history.pushState(null, "", "/agents/manage/?agent_id=osa_first&return_view=created");
  rendered.rerender(surface());
  expect(await screen.findByTestId("release-editor")).toHaveProperty("textContent", "osa_first");
  window.history.pushState(null, "", "/agents/manage/?agent_id=osa_second&return_view=archived");
  rendered.rerender(surface());
  await waitFor(() => expect(screen.getByTestId("release-editor").textContent).toBe("osa_second"));
  window.history.pushState(null, "", "/agents/manage/archived/");
  rendered.rerender(surface());
  expect(screen.getByText("hub")).toBeTruthy();
});
