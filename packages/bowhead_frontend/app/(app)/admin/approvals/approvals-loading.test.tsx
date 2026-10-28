/**
 * Approval-page loading/scope switching (both appearances):
 * - On first load or scope switch, show Loading until the current scope's first response, without flashing No Pending Approvals;
 * - Accept only the latest request's response: late responses from prior scopes or earlier same-scope requests cannot overwrite the current list;
 * - A failed switch to another scope must not display previous-scope items as current.
 */
import { act, cleanup, fireEvent, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

const api = vi.hoisted(() => ({ fetchApprovals: vi.fn() }));
vi.mock("@/lib/api", () => ({ ...api, approveApproval: vi.fn(), rejectApproval: vi.fn() }));
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams() }));
vi.mock("@/components/admin-shell", () => ({ AdminShell: ({ children }: { children: ReactNode }) => <>{children}</> }));
const access = vi.hoisted(() => ({ status: "authed" as string, options: null as unknown }));
vi.mock("@/components/access/access-request-panel", () => ({
  AccessWhoCanHelpWhenEnabled: ({ scope }: { scope: string }) => <aside data-testid="who-can-help">{scope}</aside>,
}));
vi.mock("@/lib/auth/use-auth-redirect", () => ({
  useAuthRedirect: (options: unknown) => {
    access.options = options;
    return { status: access.status, me: null, isTeamAdmin: access.status === "authed", error: null };
  },
}));

import ApprovalsPage from "./page";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  access.status = "authed";
});

describe("approvals route guard (same as the other administration pages)", () => {
  it("allows team owners and team admins only", () => {
    api.fetchApprovals.mockResolvedValue([]);
    renderIntl(<ApprovalsPage />);
    expect(access.options).toEqual({ allowedTeamRoles: ["owner", "team_admin"] });
  });

  it("tells other roles they cannot view approvals and does not load the list", () => {
    access.status = "not_admin";
    renderIntl(<ApprovalsPage />);
    expect(screen.getByRole("alert").textContent).toContain("只有团队负责人或团队管理员可以查看审批。");
    // Same "who can help" aside as the other administration no-access pages.
    expect(screen.getByTestId("who-can-help").textContent).toBe("team");
    expect(api.fetchApprovals).not.toHaveBeenCalled();
  });

  it("shows a loading state while access is being checked", () => {
    access.status = "loading";
    renderIntl(<ApprovalsPage />);
    expect(screen.getByRole("status").textContent).toContain("加载中");
    expect(api.fetchApprovals).not.toHaveBeenCalled();
  });
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const item = (id: string, status = "pending") => ({
  id,
  turnId: "t",
  sessionId: null,
  workspaceId: "w",
  memberUid: "m",
  userId: "u",
  toolName: `tool_${id}`,
  arguments: {},
  reason: null,
  riskLevel: "high",
  status,
  decidedBy: null,
  decidedAt: null,
  executionOk: null,
  executionResult: null,
  executionError: null,
  executedAt: null,
  createdAt: new Date().toISOString(),
  updatedAt: new Date().toISOString(),
});

// Show localized failure explanations, not raw API wording or status codes.
const LOAD_FAILED = /没能加载审批列表/;

describe("审批页首次加载", () => {
  it("shows the loading skeleton without an empty state until the first response, then the empty state", async () => {
    const pending = deferred<unknown[]>();
    api.fetchApprovals.mockReturnValue(pending.promise);
    renderIntl(<ApprovalsPage />);

    expect(await screen.findByRole("status", { name: "加载中..." })).toBeTruthy();
    expect(screen.queryByText("暂无待审批操作")).toBeNull();

    await act(async () => pending.resolve([]));
    expect(await screen.findByText("暂无待审批操作")).toBeTruthy();
    expect(screen.queryByRole("status", { name: "加载中..." })).toBeNull();
  });

  it("加载失败时显示错误，不显示空状态", async () => {
    api.fetchApprovals.mockRejectedValue(new Error("boom"));
    renderIntl(<ApprovalsPage />);
    expect(await screen.findByText(LOAD_FAILED)).toBeTruthy();
    expect(screen.queryByText("暂无待审批操作")).toBeNull();
  });
});

// Scope is a segmented control (role=radio).
const clickScope = async (name: string) => {
  const el = screen.getByRole("radio", { name });
  await act(async () => {
    fireEvent.click(el);
  });
};

describe("approval scope switching", () => {
  it("上一范围晚到的返回不覆盖、不闪空状态；新范围返回前显示加载中", async () => {
    const pending = deferred<unknown[]>();
    const all = deferred<unknown[]>();
    api.fetchApprovals.mockImplementation((scope: string) => (scope === "pending" ? pending.promise : all.promise));
    renderIntl(<ApprovalsPage />);

    await clickScope("全部");
    await act(async () => pending.resolve([item("P_STALE")]));
    expect(screen.queryByText("tool_P_STALE")).toBeNull();
    expect(screen.queryByText("暂无审批记录")).toBeNull();
    // Loading shows a skeleton (role=status, name “加载中...”).
    expect(screen.getByRole("status", { name: "加载中..." })).toBeTruthy();

    await act(async () => all.resolve([item("A_OK", "approved")]));
    expect(await screen.findByText("tool_A_OK")).toBeTruthy();
    expect(screen.queryByText("tool_P_STALE")).toBeNull();
  });

  it("待处理→全部→待处理：同一范围更早发出的请求晚到时，不覆盖后一次的结果", async () => {
    const calls: Record<string, ReturnType<typeof deferred<unknown[]>>[]> = { pending: [], all: [] };
    api.fetchApprovals.mockImplementation((scope: string) => {
      const d = deferred<unknown[]>();
      calls[scope].push(d);
      return d.promise;
    });
    renderIntl(<ApprovalsPage />);
    await clickScope("全部");
    await clickScope("待处理");
    expect(calls.pending.length).toBe(2);

    await act(async () => calls.pending[1].resolve([item("NEW")]));
    expect(await screen.findByText("tool_NEW")).toBeTruthy();
    await act(async () => calls.pending[0].resolve([item("OLD")]));
    await act(async () => calls.all[0].resolve([item("ALL", "approved")]));
    expect(screen.getByText("tool_NEW")).toBeTruthy();
    expect(screen.queryByText("tool_OLD")).toBeNull();
    expect(screen.queryByText("tool_ALL")).toBeNull();
  });

  it("更早发出的请求晚到失败时，不给已成功的当前列表加上错误", async () => {
    const calls: Record<string, ReturnType<typeof deferred<unknown[]>>[]> = { pending: [], all: [] };
    api.fetchApprovals.mockImplementation((scope: string) => {
      const d = deferred<unknown[]>();
      calls[scope].push(d);
      return d.promise;
    });
    renderIntl(<ApprovalsPage />);
    await clickScope("全部");
    await clickScope("待处理");
    await act(async () => calls.pending[1].resolve([item("NEW")]));
    expect(await screen.findByText("tool_NEW")).toBeTruthy();
    await act(async () => calls.pending[0].reject(new Error("late boom")));
    expect(screen.queryByText(LOAD_FAILED)).toBeNull();
    expect(screen.getByText("tool_NEW")).toBeTruthy();
  });

  it("上一范围失败后切换范围：新范围返回前显示加载中，不留上一范围的错误", async () => {
    const all = deferred<unknown[]>();
    api.fetchApprovals.mockImplementation((scope: string) => (scope === "pending" ? Promise.reject(new Error("boom")) : all.promise));
    renderIntl(<ApprovalsPage />);
    expect(await screen.findByText(LOAD_FAILED)).toBeTruthy();
    await clickScope("全部");
    expect(screen.queryByText(LOAD_FAILED)).toBeNull();
    expect(screen.getByRole("status", { name: "加载中..." })).toBeTruthy();
  });

  it("切到「全部」失败时显示错误，不再显示「待处理」的条目", async () => {
    api.fetchApprovals.mockImplementation((scope: string) =>
      scope === "pending" ? Promise.resolve([item("P1")]) : Promise.reject(new Error("boom")),
    );
    renderIntl(<ApprovalsPage />);
    expect(await screen.findByText("tool_P1")).toBeTruthy();

    await clickScope("全部");
    expect(await screen.findByText(LOAD_FAILED)).toBeTruthy();
    expect(screen.queryByText("tool_P1")).toBeNull();
    expect(screen.queryByText("暂无审批记录")).toBeNull();

    // Switch back to Pending: clear the previous scope's error and restore the list.
    await clickScope("待处理");
    expect(await screen.findByText("tool_P1")).toBeTruthy();
    expect(screen.queryByText(LOAD_FAILED)).toBeNull();
  });
});
