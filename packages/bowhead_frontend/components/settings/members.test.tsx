import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

import messages from "@/messages/zh-CN.json";

vi.mock("@/lib/auth-api", () => ({
  fetchMe: vi.fn().mockResolvedValue({ id: "u_admin", team_role: "team_admin" }),
  fetchWithRefresh: (...args: Parameters<typeof fetch>) => fetch(...args),
}));
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ push: vi.fn() }) }));
vi.mock("@/lib/use-space", () => ({
  useSpace: () => ({ items: [{ id: "ws_1", name: "销售" }], activeId: "ws_1", loading: false, error: null }),
}));

import { SettingsMembers } from "./members";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("SettingsMembers", () => {
  it("adds an existing Team member without rendering an email invitation field", async () => {
    const fetchMock = vi.spyOn(global, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/member-candidates")) {
        return new Response(JSON.stringify({ items: [{ user_id: "u_1", email: "a@example.com", display_name: "Alice" }] }));
      }
      if (url.endsWith("/members") && init?.method === "POST") return new Response("{}", { status: 200 });
      return new Response(JSON.stringify({ members: [] }));
    });
    render(<NextIntlClientProvider locale="zh-CN" messages={messages}><SettingsMembers /></NextIntlClientProvider>);
    const select = await screen.findByLabelText("团队成员");
    expect(screen.queryByLabelText("邮箱")).toBeNull();
    fireEvent.change(select, { target: { value: "u_1" } });
    fireEvent.click(screen.getByRole("button", { name: "添加到空间" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/workspaces/ws_1/members",
      expect.objectContaining({ method: "POST" }),
    ));
  });

  describe("行尾「⋯」里的「移除」，仍走确认框（外观段第四批）", () => {
    const MEMBERS = [
      { user_id: "u_admin", role: "admin", joined_at: "2026-09-20T10:00:00Z", email: "me@example.com" },
      { user_id: "u_2", role: "member", joined_at: "2026-09-21T10:00:00Z", email: "bob@example.com", display_name: "Bob" },
    ];
    const mockFetch = () =>
      vi.spyOn(global, "fetch").mockImplementation(async (input, init) => {
        const url = String(input);
        if (url.endsWith("/member-candidates")) return new Response(JSON.stringify({ items: [] }));
        if (init?.method === "DELETE") return new Response(null, { status: 204 });
        return new Response(JSON.stringify({ members: MEMBERS }));
      });
    const renderPage = () =>
      render(<NextIntlClientProvider locale="zh-CN" messages={messages}><SettingsMembers /></NextIntlClientProvider>);

    it("has no inline Remove; the row menu item is a danger item that confirms before deleting", async () => {
      const fetchMock = mockFetch();
      renderPage();
      await screen.findByText("Bob");
      expect(screen.queryByRole("button", { name: "移除" })).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: "Bob 的更多操作" }));
      const item = screen.getByRole("menuitem", { name: "移除…" });
      expect(item.className).toContain("is-danger");
      fireEvent.click(item);
      const dialog = await screen.findByRole("dialog", { name: "移除成员" });
      expect(dialog).toBeTruthy();
      expect(fetchMock).not.toHaveBeenCalledWith(expect.stringContaining("/members/u_2"), expect.anything());
      fireEvent.click(screen.getAllByRole("button", { name: "移除" }).at(-1)!);
      await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
        "/api/v1/workspaces/ws_1/members/u_2",
        expect.objectContaining({ method: "DELETE" }),
      ));
    });

    it("disables the menu item when the current user is the last administrator", async () => {
      mockFetch();
      renderPage();
      await screen.findByText("Bob");
      fireEvent.click(screen.getByRole("button", { name: "me@example.com 的更多操作" }));
      expect(screen.getByRole("menuitem", { name: "移除…" }).getAttribute("aria-disabled")).toBe("true");
    });

    it("renders list rows below 640 with an editable role and Remove still in the row menu", async () => {
      const matchMedia = vi.fn().mockReturnValue({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() });
      vi.stubGlobal("matchMedia", matchMedia);
      try {
        const fetchMock = mockFetch();
        renderPage();
        const list = await screen.findByRole("list", { name: "活跃成员（2）" });
        expect(document.querySelector("table.legacy-members-table")).toBeNull();
        const rows = list.querySelectorAll(".ui-list-row");
        expect(rows).toHaveLength(2);
        // With a display name, put email on line two; otherwise email is the heading, without duplication
        expect(rows[1].querySelector(".ui-list-row-title")?.textContent).toBe("Bob");
        expect(rows[1].querySelector(".ui-list-row-sub")?.textContent).toBe("bob@example.com");
        expect(rows[0].querySelector(".ui-list-row-sub")).toBeNull();
        // When a long email is visually truncated in the row, both the hover hint and node text retain the full value
        expect(rows[0].querySelector(".ui-list-row-title [title]")?.getAttribute("title")).toBe("me@example.com");
        expect(rows[1].querySelector(".ui-list-row-title [title]")?.getAttribute("title")).toBe("Bob");
        expect(rows[1].querySelector(".ui-list-row-sub [title]")?.getAttribute("title")).toBe("bob@example.com");
        const roleSelect = screen.getByLabelText("bob@example.com 的角色") as HTMLSelectElement;
        expect(rows[1].querySelector(".ui-list-row-status")?.contains(roleSelect)).toBe(true);
        fireEvent.click(screen.getByRole("button", { name: "Bob 的更多操作" }));
        fireEvent.click(screen.getByRole("menuitem", { name: "移除…" }));
        await screen.findByRole("dialog", { name: "移除成员" });
        expect(fetchMock).not.toHaveBeenCalledWith(expect.stringContaining("/members/u_2"), expect.objectContaining({ method: "DELETE" }));
      } finally {
        vi.unstubAllGlobals();
      }
    });
  });
});


it.each(["role", "remove"] as const)("shows a rejected member %s operation in its active context", async (operation) => {
  vi.spyOn(global, "fetch").mockImplementation(async (input, init) => {
    if (init?.method === "PUT" || init?.method === "DELETE") {
      return new Response(JSON.stringify({ error: { code: "unexpected_failure", message: "private detail" } }), { status: 500 });
    }
    if (String(input).endsWith("/member-candidates")) return new Response(JSON.stringify({ items: [] }));
    return new Response(JSON.stringify({ members: [{ user_id: "u_bob", role: "member", joined_at: "2026-09-21T10:00:00Z", email: "bob@example.com", display_name: "Bob" }] }));
  });
  render(<NextIntlClientProvider locale="zh-CN" messages={messages}><SettingsMembers /></NextIntlClientProvider>);
  await screen.findByText("Bob");
  if (operation === "role") {
    fireEvent.change(screen.getByLabelText("bob@example.com 的角色"), { target: { value: "viewer" } });
  } else {
    fireEvent.click(screen.getByRole("button", { name: "Bob 的更多操作" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "移除…" }));
    fireEvent.click(await screen.findByRole("button", { name: "移除" }));
  }
  expect(await screen.findByText(operation === "role" ? /未能修改该成员的角色/ : /未能移除该成员/)).toBeTruthy();
  expect(screen.queryByText("private detail")).toBeNull();
});
