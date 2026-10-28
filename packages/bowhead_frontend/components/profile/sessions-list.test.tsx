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
  { session_id: "ses_cur", created_at: "2026-09-20T00:00:00Z", expires_at: "2026-10-20T00:00:00Z", last_used_at: null, user_agent: "Chrome · macOS", ip_masked: "10.0.*.*", is_current: true },
  { session_id: "ses_ipad", created_at: "2026-09-20T00:00:00Z", expires_at: "2026-10-20T00:00:00Z", last_used_at: null, user_agent: "Safari · iPadOS", ip_masked: "10.0.*.*", is_current: false },
];

afterEach(() => {
  cleanup();
  api.revokeSession.mockReset();
});

describe("SessionsList 撤销其他设备", () => {
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
});
