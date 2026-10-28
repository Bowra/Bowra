import { cleanup, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { ApiError } from "@/lib/api-client";
import { getAgentCapabilityReport, getAgentDetail } from "@/lib/agents-api";
import { AgentDetail } from "./agent-detail";

const session = vi.hoisted(() => ({ capabilities: {} as Record<string, unknown> }));
const acr = vi.hoisted(() => ({ getAccessRequestStatus: vi.fn(), requestAccess: vi.fn(), withdrawAccessRequest: vi.fn() }));
vi.mock("@/lib/auth/auth-session-context", () => ({ useOptionalAuthSession: () => ({ me: { capabilities: session.capabilities } }) }));
vi.mock("@/lib/composer-handoff", () => ({ handoffAgentToNewChat: vi.fn() }));
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));
vi.mock("@/lib/agents-api", () => ({
  forkAgent: vi.fn(),
  getAgentCapabilityReport: vi.fn(),
  getAgentDetail: vi.fn(),
  hireAgent: vi.fn(),
  previewAgentFork: vi.fn(),
  setAgentPersonalStatus: vi.fn(),
}));
vi.mock("@/lib/access-requests-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/access-requests-api")>()),
  ...acr,
}));

async function renderFailure(status: number) {
  vi.mocked(getAgentDetail).mockRejectedValueOnce(new ApiError(status, "raw"));
  const utils = renderIntl(<AgentDetail />);
  return utils;
}

describe("AgentDetail no-access request panel", () => {
  beforeEach(() => {
    window.history.replaceState({}, "", "/agents/detail/?id=osa_404");
    vi.mocked(getAgentCapabilityReport).mockRejectedValue(new ApiError(404, "raw"));
    acr.getAccessRequestStatus.mockResolvedValue({ state: "none", request: null, retry_after: null });
  });
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    session.capabilities = {};
  });

  it("access requests off: keeps the existing not-found copy without a request panel", async () => {
    session.capabilities = { access_requests: false };
    await renderFailure(404);
    expect(await screen.findByRole("heading", { name: "找不到这个智能体，或你已没有查看权限" })).toBeTruthy();
    expect(screen.queryByTestId("access-request-panel")).toBeNull();
    expect(acr.getAccessRequestStatus).not.toHaveBeenCalled();
  });

  it("access requests off: keeps the existing forbidden copy without a request panel", async () => {
    session.capabilities = { access_requests: false };
    await renderFailure(403);
    expect(await screen.findByRole("heading", { name: "你当前没有查看这个智能体的权限。" })).toBeTruthy();
    expect(screen.queryByTestId("access-request-panel")).toBeNull();
  });

  it("access requests on: a missing and a forbidden Agent render the same request panel", async () => {
    session.capabilities = { access_requests: true };
    const html: string[] = [];
    for (const status of [404, 403]) {
      const { container, unmount } = await renderFailure(status);
      await screen.findByRole("button", { name: "申请访问" });
      html.push(container.innerHTML.replace(/(id|for|aria-describedby)="[^"]*"/g, '$1="id"'));
      unmount();
    }
    expect(html[0]).toBe(html[1]);
    expect(html[0]).toContain("这个智能体不存在，或你还不在它的使用范围内。");
    expect(acr.getAccessRequestStatus).toHaveBeenCalledWith("team_subagent", "osa_404");
  });
});
