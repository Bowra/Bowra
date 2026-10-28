import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { ApiError } from "@/lib/api-client";
import type { AccessRequestStatus, RequesterRequestView } from "@/lib/access-requests-api";

const api = vi.hoisted(() => ({
  getAccessRequestStatus: vi.fn(),
  requestAccess: vi.fn(),
  withdrawAccessRequest: vi.fn(),
}));
vi.mock("@/lib/access-requests-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/access-requests-api")>()),
  ...api,
}));

import { AccessRequestPanel, accessRequestPanelView } from "./access-request-panel";

function view(overrides: Partial<RequesterRequestView>): RequesterRequestView {
  return {
    request_id: "acr_1",
    kind: "report",
    resource_id: "s1",
    state: "pending",
    message: null,
    note: null,
    created_at: "2026-09-30T08:00:00Z",
    expires_at: null,
    decided_at: null,
    granted_permission: null,
    grant_expires_at: null,
    retry_after: null,
    retry_at: null,
    ...overrides,
  };
}

function status(state: AccessRequestStatus["state"], request: Partial<RequesterRequestView> | null = {}, retryAfter: number | null = null): AccessRequestStatus {
  return {
    state,
    request: request === null || state === "none" ? null : view({ state: state as RequesterRequestView["state"], ...request }),
    retry_after: retryAfter,
  };
}

async function renderPanel(next: AccessRequestStatus, kind: "report" | "team_skill" | "team_subagent" = "report") {
  api.getAccessRequestStatus.mockResolvedValue(next);
  const onBack = vi.fn();
  const onOpen = vi.fn();
  const utils = renderIntl(<AccessRequestPanel kind={kind} resourceId="s1" onBack={onBack} onOpen={onOpen} />);
  await waitFor(() => expect(screen.getByTestId("access-request-panel").getAttribute("data-view")).not.toBe("loading"));
  return { ...utils, onBack, onOpen };
}

beforeEach(() => {
  api.getAccessRequestStatus.mockReset();
  api.requestAccess.mockReset();
  api.withdrawAccessRequest.mockReset();
});
afterEach(() => cleanup());

describe("accessRequestPanelView", () => {
  it.each([
    [status("none"), "idle"],
    [status("none", null, 3600), "cooldown"],
    [status("pending"), "pending"],
    [status("denied", { retry_at: "2026-10-07T00:00:00Z", retry_after: 600 }), "denied_cooldown"],
    [status("denied", {}), "reapply"],
    [status("expired"), "expired"],
    [status("approved"), "approved"],
    [status("approved_pending"), "approved_pending"],
    [status("withdrawn"), "idle"],
  ] as const)("maps %j to %s", (input, expected) => {
    expect(accessRequestPanelView(input)).toBe(expected);
  });
});

describe("AccessRequestPanel states", () => {
  it("idle: neutral copy, optional reason with a 280 counter, one primary Request button", async () => {
    await renderPanel(status("none"));
    expect(screen.getByRole("heading", { name: "你还没有访问权限" })).toBeTruthy();
    expect(screen.getByText("这份内容不存在，或还没有分享给你。你可以申请访问，管理者同意后你会收到通知。")).toBeTruthy();
    expect(screen.getByLabelText("申请理由（选填）").getAttribute("maxlength")).toBe("280");
    expect(screen.getByText("0 / 280")).toBeTruthy();
    expect(screen.getByText("只有管理者能看到这段话")).toBeTruthy();
    expect(screen.getByRole("button", { name: "申请访问" }).className).toContain("settings-button--primary");
    expect(screen.getByRole("button", { name: "返回" }).className).toContain("settings-button--secondary");
  });

  it.each([
    ["team_subagent", "这个智能体不存在，或你还不在它的使用范围内。你可以申请使用，管理者同意后你会收到通知。"],
    ["team_skill", "这个技能不存在，或还没有分享给你。你可以申请使用，管理者同意后你会收到通知。"],
  ] as const)("uses the %s wording on the same panel", async (kind, body) => {
    await renderPanel(status("none"), kind);
    expect(api.getAccessRequestStatus).toHaveBeenCalledWith(kind, "s1");
    expect(screen.getByText(body)).toBeTruthy();
  });

  it("pending: submitted date, Back is primary and Withdraw is secondary", async () => {
    await renderPanel(status("pending"));
    expect(screen.getByRole("heading", { name: "申请处理中" })).toBeTruthy();
    expect(screen.getByText(/提交。管理者处理后你会收到通知；14 天内未处理会自动失效。/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "返回" }).className).toContain("settings-button--primary");
    expect(screen.getByRole("button", { name: "撤回申请" }).className).toContain("settings-button--ghost");
    expect(screen.queryByLabelText("申请理由（选填）")).toBeNull();
  });

  it("denied in cooldown: the manager's note, the date to retry, Request again disabled", async () => {
    await renderPanel(status("denied", { note: "季度汇报请用对外版。", retry_at: "2026-10-07T00:00:00Z", retry_after: 600 }));
    expect(screen.getByRole("heading", { name: "申请未通过" })).toBeTruthy();
    expect(screen.getByText("管理者附言")).toBeTruthy();
    expect(screen.getByText("季度汇报请用对外版。")).toBeTruthy();
    expect(screen.getByText(/后可以再次申请。/)).toBeTruthy();
    expect((screen.getByRole("button", { name: "再次申请" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("declined and cooled down: back to the form with a Request again primary", async () => {
    await renderPanel(status("denied", { decided_at: "2026-09-20T00:00:00Z" }));
    expect(screen.getByRole("heading", { name: "你还没有访问权限" })).toBeTruthy();
    expect(screen.getByText(/上次申请未通过/)).toBeTruthy();
    const again = screen.getByRole("button", { name: "再次申请" }) as HTMLButtonElement;
    expect(again.disabled).toBe(false);
    expect(screen.getByLabelText("申请理由（选填）")).toBeTruthy();
  });

  it("expired: one neutral sentence for every way a request stops being active", async () => {
    await renderPanel(status("expired"));
    expect(screen.getByRole("heading", { name: "申请已失效" })).toBeTruthy();
    expect(screen.getByText("这次申请已不再有效。需要的话可以重新申请。")).toBeTruthy();
    expect(screen.getByRole("button", { name: "再次申请" })).toBeTruthy();
  });

  it("approved: permanent by default, a date when time-limited, and Open", async () => {
    const { onOpen } = await renderPanel(status("approved", { note: "请注意保密" }));
    expect(screen.getByRole("heading", { name: "你已获得访问权限" })).toBeTruthy();
    expect(screen.getByText(/可以阅读这份报告。权限不会过期，除非管理者收回。/)).toBeTruthy();
    expect(screen.getByText("请注意保密")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "打开" }));
    expect(onOpen).toHaveBeenCalled();
    cleanup();
    await renderPanel(status("approved", { grant_expires_at: "2026-10-30T00:00:00Z" }), "team_skill");
    expect(screen.getByText(/可以使用这个技能。权限到 .+ 为止。/)).toBeTruthy();
  });

  it("approved, taking effect: no Open yet", async () => {
    await renderPanel(status("approved_pending"), "team_subagent");
    expect(screen.getByRole("heading", { name: "已批准，正在生效" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "打开" })).toBeNull();
    expect(screen.getByRole("button", { name: "返回" })).toBeTruthy();
  });

  it("cooldown without an open request: the retry date and a disabled button", async () => {
    await renderPanel(status("none", null, 7 * 24 * 3600));
    expect(screen.getByText(/后可以再次申请。/)).toBeTruthy();
    expect((screen.getByRole("button", { name: "再次申请" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("falls back to the request form when the status read fails", async () => {
    api.getAccessRequestStatus.mockRejectedValue(new ApiError(500, "boom"));
    renderIntl(<AccessRequestPanel kind="report" resourceId="s1" />);
    expect(await screen.findByRole("button", { name: "申请访问" })).toBeTruthy();
  });
});

describe("AccessRequestPanel actions", () => {
  it("submits the trimmed reason and shows the pending state", async () => {
    await renderPanel(status("none"));
    api.requestAccess.mockResolvedValue({ status: "submitted", request_id: "acr_9" });
    api.getAccessRequestStatus.mockResolvedValue(status("pending", { request_id: "acr_9" }));
    fireEvent.change(screen.getByLabelText("申请理由（选填）"), { target: { value: "  季度汇报  " } });
    expect(screen.getByText("8 / 280")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "申请访问" }));
    await screen.findByRole("heading", { name: "申请处理中" });
    expect(api.requestAccess).toHaveBeenCalledWith("report", "s1", "季度汇报");
  });

  it("never sends more than 280 characters", async () => {
    await renderPanel(status("none"));
    fireEvent.change(screen.getByLabelText("申请理由（选填）"), { target: { value: "字".repeat(400) } });
    expect(screen.getByText("280 / 280")).toBeTruthy();
  });

  it("429: says the requester is going too fast and keeps the form", async () => {
    await renderPanel(status("none"));
    api.requestAccess.mockRejectedValue(
      new ApiError(429, "x", { code: "access_request_rate_limited", body: { detail: { code: "access_request_rate_limited", retry_after: 60 } } }),
    );
    fireEvent.click(screen.getByRole("button", { name: "申请访问" }));
    expect(await screen.findByText("申请太频繁，请稍后再试。")).toBeTruthy();
    expect(screen.getByRole("button", { name: "申请访问" })).toBeTruthy();
  });

  it("409 cooldown: re-reads the status and shows when to try again", async () => {
    await renderPanel(status("none"));
    api.requestAccess.mockRejectedValue(
      new ApiError(409, "x", { code: "access_request_cooldown", body: { detail: { code: "access_request_cooldown", retry_after: 600, retry_at: "2026-10-07T00:00:00Z" } } }),
    );
    api.getAccessRequestStatus.mockResolvedValue(status("denied", { retry_at: "2026-10-07T00:00:00Z", retry_after: 600 }));
    fireEvent.click(screen.getByRole("button", { name: "申请访问" }));
    expect(await screen.findByRole("heading", { name: "申请未通过" })).toBeTruthy();
    expect(api.getAccessRequestStatus).toHaveBeenCalledTimes(2);
  });

  it("withdraws a pending request and returns to the form", async () => {
    await renderPanel(status("pending", { request_id: "acr_7" }));
    api.withdrawAccessRequest.mockResolvedValue({ status: "withdrawn" });
    api.getAccessRequestStatus.mockResolvedValue(status("none"));
    fireEvent.click(screen.getByRole("button", { name: "撤回申请" }));
    expect(await screen.findByRole("button", { name: "申请访问" })).toBeTruthy();
    expect(api.withdrawAccessRequest).toHaveBeenCalledWith("acr_7");
  });
});

describe("AccessRequestPanel after a grant ends", () => {
  it("reads a time-limited grant that has run out as no longer active", async () => {
    await renderPanel(status("approved", { grant_expires_at: "2020-01-01T00:00:00Z" }));
    expect(screen.getByTestId("access-request-panel").getAttribute("data-view")).toBe("expired");
    expect(screen.getByRole("heading", { name: "申请已失效" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "再次申请" })).toBeTruthy();
  });

  it("approved but shown (access removed since): offers the reason form and Request again", async () => {
    await renderPanel(status("approved", { note: "请勿外传" }));
    api.requestAccess.mockResolvedValue({ status: "submitted", request_id: "acr_new" });
    api.getAccessRequestStatus.mockResolvedValue(status("pending", { request_id: "acr_new" }));
    expect(screen.getByRole("button", { name: "打开" })).toBeTruthy();
    fireEvent.change(screen.getByLabelText("申请理由（选填）"), { target: { value: "权限被收回了" } });
    fireEvent.click(screen.getByRole("button", { name: "再次申请" }));
    await screen.findByRole("heading", { name: "申请处理中" });
    expect(api.requestAccess).toHaveBeenCalledWith("report", "s1", "权限被收回了");
  });

  it("keeps the last manager note when the requester may ask again", async () => {
    await renderPanel(status("denied", { note: "请先找部门负责人确认" }));
    expect(screen.getByTestId("access-request-panel").getAttribute("data-view")).toBe("reapply");
    expect(screen.getByText("请先找部门负责人确认")).toBeTruthy();
  });
});

describe("AccessRequestPanel focus and announcements", () => {
  it("does not take focus on the first load", async () => {
    await renderPanel(status("none"));
    expect(document.activeElement).toBe(document.body);
    expect(screen.getByTestId("access-request-live").textContent).toBe("");
  });

  it("moves focus to the heading and announces the new state after submit and withdraw", async () => {
    await renderPanel(status("none"));
    api.requestAccess.mockResolvedValue({ status: "submitted", request_id: "acr_9" });
    api.getAccessRequestStatus.mockResolvedValue(status("pending", { request_id: "acr_9" }));
    fireEvent.click(screen.getByRole("button", { name: "申请访问" }));
    const pending = await screen.findByRole("heading", { name: "申请处理中" });
    await waitFor(() => expect(document.activeElement).toBe(pending));
    expect(pending.getAttribute("tabindex")).toBe("-1");
    expect(screen.getByTestId("access-request-live").textContent).toBe("申请处理中");

    api.withdrawAccessRequest.mockResolvedValue({ status: "withdrawn" });
    api.getAccessRequestStatus.mockResolvedValue(status("none"));
    fireEvent.click(screen.getByRole("button", { name: "撤回申请" }));
    const idle = await screen.findByRole("heading", { name: "你还没有访问权限" });
    await waitFor(() => expect(document.activeElement).toBe(idle));
    expect(screen.getByTestId("access-request-live").textContent).toBe("你还没有访问权限");
  });

  it("announces the remaining characters near the limit", async () => {
    await renderPanel(status("none"));
    const live = screen.getByTestId("access-request-count-live");
    fireEvent.change(screen.getByLabelText("申请理由（选填）"), { target: { value: "字".repeat(100) } });
    expect(live.textContent).toBe("");
    fireEvent.change(screen.getByLabelText("申请理由（选填）"), { target: { value: "字".repeat(260) } });
    expect(live.textContent).toBe("还可以输入 20 字");
    expect(live.getAttribute("aria-live")).toBe("polite");
  });
});

describe("AccessRequestPanel disclosure", () => {
  it("never shows who decided, even if a response carried it", async () => {
    const leaky = status("denied", { note: "请用对外版", retry_at: "2026-10-07T00:00:00Z", retry_after: 60 });
    Object.assign(leaky.request as object, { decided_by: "Alice Approver", decided_by_name: "Alice Approver" });
    const { container } = await renderPanel(leaky);
    expect(container.textContent).not.toContain("Alice");
  });

  it("renders the same markup for a missing, a cross-team and a forbidden item", async () => {
    // The server answers all three identically; the panel has no other input about existence.
    const html: string[] = [];
    for (const resourceId of ["missing", "other-team", "forbidden"]) {
      api.getAccessRequestStatus.mockResolvedValue(status("none"));
      const { container, unmount } = renderIntl(<AccessRequestPanel kind="team_skill" resourceId={resourceId} />);
      await screen.findByRole("button", { name: "申请访问" });
      // React-generated ids differ per mount; everything else must match.
      html.push(container.innerHTML.replace(/(id|for|aria-describedby)="[^"]*"/g, '$1="id"'));
      unmount();
    }
    expect(new Set(html).size).toBe(1);
  });
});
