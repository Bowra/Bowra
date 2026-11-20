/**
 * Signed-in device Revoke lives in the row-end “⋯” menu and still requires confirmation; during revocation,
 * “⋯” is replaced with progress.
 */
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { chooseRowAction, expectConfirmGate } from "@/__tests__/row-actions";

const api = vi.hoisted(() => ({ revokeSession: vi.fn() }));
vi.mock("@/lib/security-api", async (original) => ({
  ...(await original<typeof import("@/lib/security-api")>()),
  revokeSession: api.revokeSession,
}));

import { SessionsList } from "./sessions-list";

const ITEMS = [
  { session_id: "ses_cur", created_at: "2026-09-20T00:00:00Z", expires_at: "2026-10-20T00:00:00Z", last_used_at: null, user_agent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36", ip_masked: "10.0.*.*", is_current: true },
  { session_id: "ses_ipad", created_at: "2026-09-20T00:00:00Z", expires_at: "2026-10-20T00:00:00Z", last_used_at: null, user_agent: "Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1", ip_masked: "10.0.*.*", is_current: false },
];

afterEach(() => {
  cleanup();
  api.revokeSession.mockReset();
});

describe("SessionsList device revocation", () => {
  it("only other devices have a “⋯” menu; Revoke is a danger item confirmed first", async () => {
    const onRevoked = vi.fn();
    api.revokeSession.mockResolvedValue({ ok: true });
    renderIntl(<SessionsList items={ITEMS} currentIdentified onRevoked={onRevoked} />);
    expect(screen.queryByRole("button", { name: "Chrome · macOS 的更多操作" })).toBeNull();
    await chooseRowAction("Safari · iPadOS 的更多操作", "撤销…", { danger: true });
    await expectConfirmGate({
      action: api.revokeSession,
      dialogName: "撤销会话",
      confirmName: "撤销",
      reopen: () => chooseRowAction("Safari · iPadOS 的更多操作", "撤销…"),
    });
    await waitFor(() => expect(api.revokeSession).toHaveBeenCalledWith("ses_ipad"));
    await waitFor(() => expect(onRevoked).toHaveBeenCalled());
  });

  it("while revoking, the “⋯” trigger shows progress and cannot reopen the menu", async () => {
    let finish: (value: { ok: boolean }) => void = () => {};
    api.revokeSession.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    renderIntl(<SessionsList items={ITEMS} currentIdentified onRevoked={vi.fn()} />);
    await chooseRowAction("Safari · iPadOS 的更多操作", "撤销…");
    fireEvent.click(await screen.findByRole("button", { name: "撤销" }));
    // Keep confirmation open until the request ends (background hidden from screen readers), while row “⋯” becomes progress
    const pending = await screen.findByRole("button", { name: "正在撤销…", hidden: true });
    expect(pending.getAttribute("aria-busy")).toBe("true");
    fireEvent.click(pending);
    expect(screen.queryByRole("menu", { hidden: true })).toBeNull();
    finish({ ok: true });
    await waitFor(() => expect(screen.queryByRole("button", { name: "正在撤销…", hidden: true })).toBeNull());
  });

  it("shows the latest activity for a device and excludes the current session from revocation", async () => {
    const onRevoked = vi.fn();
    api.revokeSession.mockResolvedValue({ ok: true });
    const current = { ...ITEMS[0], device_key: "desktop" };
    const duplicate = { ...current, session_id: "ses_older_login", is_current: false, created_at: "2026-09-23T12:00:00Z" };
    renderIntl(<SessionsList items={[current, duplicate, ITEMS[1]]} currentIdentified onRevoked={onRevoked} />);

    const rows = screen.getAllByRole("article");
    expect(rows).toHaveLength(2);
    expect(rows[0].textContent).toContain("2026年9月23日");
    expect(rows[0].textContent).toContain("当前设备");

    await chooseRowAction("Chrome · macOS 的更多操作", "撤销此设备的其他会话…");
    fireEvent.click(await screen.findByRole("button", { name: "撤销" }));
    await waitFor(() => expect(onRevoked).toHaveBeenCalled());
    expect(api.revokeSession.mock.calls).toEqual([["ses_older_login"]]);
  });
});
