import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { ApiError } from "@/lib/api-client";
import type { AccessRequestItem, RequesterRequestView } from "@/lib/access-requests-api";

const api = vi.hoisted(() => ({
  decideAccessRequest: vi.fn(),
  listAccessRequests: vi.fn(),
  listMyAccessRequests: vi.fn(),
  withdrawAccessRequest: vi.fn(),
}));
const navigation = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }));
vi.mock("@/lib/access-requests-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/access-requests-api")>()),
  ...api,
}));
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => navigation }));
const session = vi.hoisted(() => ({ me: null as { id: string } | null }));
vi.mock("@/lib/auth/auth-session-context", () => ({ useOptionalAuthSession: () => (session.me ? { me: session.me } : null) }));
const narrow = vi.hoisted(() => ({ value: false }));
vi.mock("@/components/ui/sheet", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/components/ui/sheet")>()),
  useNarrowViewport: () => narrow.value,
}));

import { AccessRequestsContent } from "./access-requests-content";

function item(id: string, kind: string, overrides: Partial<AccessRequestItem> = {}): AccessRequestItem {
  return {
    id,
    kind,
    resource_id: `res_${id}`,
    label: `Label ${id}`,
    requester_id: "user_1",
    requester_name: "Wang Xiaoyu",
    requester_email: "wang@example.com",
    message: "需要门店数据",
    status: "pending",
    prior_denied: 0,
    created_at: "2026-09-30T09:00:00Z",
    decided_by: null,
    decided_at: null,
    decision_note: null,
    granted_permission: null,
    grant_expires_at: null,
    expires_at: "2026-10-14T09:00:00Z",
    withdrawn_at: null,
    ...overrides,
  };
}

function mine(id: string, overrides: Partial<RequesterRequestView> = {}): RequesterRequestView {
  return {
    request_id: id,
    kind: "report",
    resource_id: `res_${id}`,
    state: "pending",
    message: null,
    note: null,
    created_at: "2026-09-30T09:00:00Z",
    expires_at: "2026-10-14T09:00:00Z",
    decided_at: null,
    granted_permission: null,
    grant_expires_at: null,
    retry_after: null,
    retry_at: null,
    ...overrides,
  };
}

beforeEach(() => {
  api.listAccessRequests.mockResolvedValue({ items: [] });
  api.listMyAccessRequests.mockResolvedValue({ items: [] });
  api.decideAccessRequest.mockResolvedValue({ status: "approved" });
  api.withdrawAccessRequest.mockResolvedValue({ status: "withdrawn" });
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  session.me = null;
  narrow.value = false;
});

describe("AccessRequestsContent", () => {
  it("shows three segments and says what approving grants per kind", async () => {
    api.listAccessRequests.mockResolvedValue({
      items: [item("r", "report"), item("s", "team_skill"), item("g", "team_subagent")],
    });
    renderIntl(<AccessRequestsContent />);
    await screen.findByTestId("acr-row-r");
    for (const name of ["待我处理", "已处理", "我发起的"]) expect(screen.getByRole("button", { name })).toBeTruthy();
    expect(within(screen.getByTestId("acr-row-r")).getByText("· 批准后可阅读")).toBeTruthy();
    expect(within(screen.getByTestId("acr-row-s")).getByText("技能")).toBeTruthy();
    expect(within(screen.getByTestId("acr-row-g")).getByText("智能体")).toBeTruthy();
    expect(within(screen.getByTestId("acr-row-g")).getByText("· 批准后可使用")).toBeTruthy();
    // 30-day grant expiry is offered for reports and skills, never for agents.
    expect(within(screen.getByTestId("acr-row-r")).queryByLabelText("30 天后自动收回")).toBeTruthy();
    expect(within(screen.getByTestId("acr-row-s")).queryByLabelText("30 天后自动收回")).toBeTruthy();
    expect(within(screen.getByTestId("acr-row-g")).queryByLabelText("30 天后自动收回")).toBeNull();
  });

  it("approves with a note and the 30-day option", async () => {
    api.listAccessRequests.mockResolvedValue({ items: [item("r", "report")] });
    renderIntl(<AccessRequestsContent />);
    const row = await screen.findByTestId("acr-row-r");
    fireEvent.change(within(row).getByPlaceholderText("附言（选填，申请人可见）"), { target: { value: "只读，请勿外传" } });
    fireEvent.click(within(row).getByLabelText("30 天后自动收回"));
    fireEvent.click(within(row).getByRole("button", { name: "同意" }));
    await waitFor(() =>
      expect(api.decideAccessRequest).toHaveBeenCalledWith("r", "approve", { note: "只读，请勿外传", grantExpiresInDays: 30 }),
    );
  });

  it("declines and stops this person's requests for 30 days from More options", async () => {
    api.listAccessRequests.mockResolvedValue({ items: [item("g", "team_subagent")] });
    renderIntl(<AccessRequestsContent />);
    const row = await screen.findByTestId("acr-row-g");
    expect(within(row).queryByLabelText(/30 天内不再接收/)).toBeNull();
    fireEvent.click(within(row).getByRole("button", { name: "更多选项" }));
    fireEvent.click(within(row).getByLabelText("拒绝时，30 天内不再接收此人对该内容的申请"));
    fireEvent.click(within(row).getByRole("button", { name: "拒绝" }));
    await waitFor(() => expect(api.decideAccessRequest).toHaveBeenCalledWith("g", "deny", { note: "", stopRequests: true }));
  });

  it("my requests: state, the manager's note and actions, never the item name or the approver", async () => {
    api.listMyAccessRequests.mockResolvedValue({
      items: [
        mine("p"),
        mine("a", { kind: "team_subagent", state: "approved", resource_id: "osa_1", decided_at: "2026-09-29T00:00:00Z" }),
        mine("d", { state: "denied", note: "季度汇报请用对外版。", retry_at: "2026-10-07T00:00:00Z", retry_after: 600 }),
        mine("e", { state: "expired" }),
      ],
    });
    renderIntl(<AccessRequestsContent />);
    fireEvent.click(await screen.findByRole("button", { name: "我发起的" }));
    const pending = await screen.findByTestId("acr-mine-p");
    expect(within(pending).getByText("处理中")).toBeTruthy();
    const approved = screen.getByTestId("acr-mine-a");
    expect(within(approved).getByText("已通过")).toBeTruthy();
    fireEvent.click(within(approved).getByRole("button", { name: "打开" }));
    expect(navigation.push).toHaveBeenCalledWith("/agents/detail/?id=osa_1");
    const denied = screen.getByTestId("acr-mine-d");
    expect(within(denied).getByText("季度汇报请用对外版。")).toBeTruthy();
    expect(within(denied).getByText(/后可以再次申请/)).toBeTruthy();
    expect(within(screen.getByTestId("acr-mine-e")).getByText("这条申请已不再有效，如仍需访问可重新申请。")).toBeTruthy();
    const page = screen.getByTestId("access-requests-page").textContent ?? "";
    expect(page).not.toContain("Label");
    expect(page).not.toContain("Wang");

    fireEvent.click(within(pending).getByRole("button", { name: "撤回" }));
    await waitFor(() => expect(api.withdrawAccessRequest).toHaveBeenCalledWith("p"));
  });

  it("my requests ignores an approver field even if a response carried it", async () => {
    api.listMyAccessRequests.mockResolvedValue({
      items: [{ ...mine("x", { state: "denied" }), decided_by: "Alice Approver", decided_by_name: "Alice Approver" }],
    });
    renderIntl(<AccessRequestsContent />);
    fireEvent.click(await screen.findByRole("button", { name: "我发起的" }));
    await screen.findByTestId("acr-mine-x");
    expect(screen.getByTestId("access-requests-page").textContent).not.toContain("Alice");
  });

  it("my requests: a time-limited grant that has run out reads as no longer active, without Open", async () => {
    api.listMyAccessRequests.mockResolvedValue({
      items: [mine("old", { state: "approved", grant_expires_at: "2020-01-01T00:00:00Z" })],
    });
    renderIntl(<AccessRequestsContent />);
    fireEvent.click(await screen.findByRole("button", { name: "我发起的" }));
    const row = await screen.findByTestId("acr-mine-old");
    expect(within(row).getByText("已失效")).toBeTruthy();
    expect(within(row).queryByRole("button", { name: "打开" })).toBeNull();
  });

  it("puts More options content after its toggle and links them with aria-controls", async () => {
    api.listAccessRequests.mockResolvedValue({ items: [item("r", "report")] });
    renderIntl(<AccessRequestsContent />);
    const row = await screen.findByTestId("acr-row-r");
    const toggle = within(row).getByRole("button", { name: "更多选项" });
    fireEvent.click(toggle);
    const panel = document.getElementById(toggle.getAttribute("aria-controls") ?? "");
    expect(panel?.textContent).toContain("30 天内不再接收");
    expect(toggle.compareDocumentPosition(panel as Node) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("does not offer decisions on the viewer's own request", async () => {
    session.me = { id: "user_1" };
    api.listAccessRequests.mockResolvedValue({ items: [item("own", "report"), item("other", "report", { requester_id: "user_2" })] });
    renderIntl(<AccessRequestsContent />);
    const own = await screen.findByTestId("acr-row-own");
    expect(within(own).queryByRole("button", { name: "同意" })).toBeNull();
    expect(within(screen.getByTestId("acr-row-other")).getByRole("button", { name: "同意" })).toBeTruthy();
  });

  it("shows decision failures in the viewer's language, never the raw message", async () => {
    api.listAccessRequests.mockResolvedValue({ items: [item("g", "team_subagent")] });
    api.decideAccessRequest.mockRejectedValue(
      new ApiError(409, "raw backend text", { code: "access_request_agent_has_unpublished_changes" }),
    );
    renderIntl(<AccessRequestsContent />);
    const row = await screen.findByTestId("acr-row-g");
    fireEvent.click(within(row).getByRole("button", { name: "同意" }));
    expect(await screen.findByText(/还有未发布的修改或正在审核的版本/)).toBeTruthy();
    expect(screen.queryByText("raw backend text")).toBeNull();
  });

  it("my requests: a failed withdraw uses the withdraw copy", async () => {
    api.listMyAccessRequests.mockResolvedValue({ items: [mine("p")] });
    api.withdrawAccessRequest.mockRejectedValue(new ApiError(500, "raw backend text"));
    renderIntl(<AccessRequestsContent />);
    fireEvent.click(await screen.findByRole("button", { name: "我发起的" }));
    fireEvent.click(within(await screen.findByTestId("acr-mine-p")).getByRole("button", { name: "撤回" }));
    expect(await screen.findByText("撤回没有成功，请刷新后重试。")).toBeTruthy();
  });

  it("narrow screens: deciding in the sheet returns focus to the Review button", async () => {
    narrow.value = true;
    api.listAccessRequests.mockResolvedValue({ items: [item("r", "report")] });
    renderIntl(<AccessRequestsContent />);
    const review = within(await screen.findByTestId("acr-row-r")).getByRole("button", { name: "处理" });
    review.focus();
    fireEvent.click(review);
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "同意" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(document.activeElement?.textContent).toBe("处理"));
  });

  it("handled: shows the decision note and a time-limited grant", async () => {
    api.listAccessRequests.mockImplementation(async (scope: string) => ({
      items: scope === "history"
        ? [item("h", "report", { status: "approved", decision_note: "请勿外传", grant_expires_at: "2026-10-30T00:00:00Z" })]
        : [],
    }));
    renderIntl(<AccessRequestsContent />);
    fireEvent.click(await screen.findByRole("button", { name: "已处理" }));
    const row = await screen.findByTestId("acr-row-h");
    expect(within(row).getByText("附言：请勿外传")).toBeTruthy();
    expect(within(row).getByText(/权限到 .+ 为止/)).toBeTruthy();
  });
});
