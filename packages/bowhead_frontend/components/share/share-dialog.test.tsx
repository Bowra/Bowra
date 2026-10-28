import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { getShares, putAudience, type ShareState } from "@/lib/content-share-api";

import { ShareDialog } from "./share-dialog";

vi.mock("@/lib/use-media-query", () => ({ useMediaQuery: () => false }));
vi.mock("@/lib/content-share-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/content-share-api")>();
  return {
    ...actual,
    getShares: vi.fn(),
    putAudience: vi.fn(),
    addShareGrant: vi.fn(),
    revokeShareGrant: vi.fn(),
    fetchTeamDirectory: vi.fn(async () => ({ users: [], next_cursor: null })),
    fetchTeamGroups: vi.fn(async () => []),
  };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const SHARED: ShareState = {
  kind: "report",
  resource_id: "rpt_1",
  label: "月度复盘",
  audience: "custom",
  owner_id: "usr_owner",
  can_manage: true,
  grants: [
    {
      id: "grt_1",
      principal_type: "user",
      principal_id: "usr_2",
      principal_name: "Avery",
      permission: "view",
      created_by: "usr_owner",
      created_at: "2026-09-01T00:00:00Z",
    },
  ],
};

describe("ShareDialog", () => {
  it.each([
    [{ owner_name: "Jordan Lee" }, "Shared by Jordan Lee"],
    [{ owner_name: null }, "Shared by Team member"],
    [{ owner_name: "usr_owner" }, "Shared by Team member"],
  ])("read-only view names the owner, never the raw owner id (%j)", async (owner, expected) => {
    vi.mocked(getShares).mockResolvedValue({ ...SHARED, can_manage: false, ...owner } as ShareState);
    renderIntl(<ShareDialog kind="report" resourceId="rpt_1" onClose={vi.fn()} />, { locale: "en-US" });
    expect(await screen.findByText(expected)).toBeTruthy();
    expect(screen.queryByText(/usr_owner/)).toBeNull();
  });

  it("member avatars carry their name colour as a CSS variable on .legacy-share-avatar, not as inline colours", async () => {
    // The refreshed styles give avatars neutral backgrounds (styles/ui-refresh-surfaces.css); inline backgrounds override them, so colors must use classes + variables
    vi.mocked(getShares).mockResolvedValue(SHARED);
    renderIntl(<ShareDialog kind="report" resourceId="rpt_1" onClose={vi.fn()} />);
    const name = await screen.findByText("Avery");
    const avatar = name.closest("div")!.parentElement!.querySelector(".legacy-share-avatar") as HTMLElement;
    expect(avatar).toBeTruthy();
    expect(avatar.textContent).toBe("A");
    expect(avatar.style.getPropertyValue("--sh-avatar-bg")).toMatch(/^#[0-9a-f]{6}$/i);
    expect(avatar.style.background).toBe("");
    expect(avatar.style.color).toBe("");
    expect(avatar.classList.contains("is-self")).toBe(false);
  });

  it("closes on Escape", async () => {
    vi.mocked(getShares).mockResolvedValue(SHARED);
    const onClose = vi.fn();
    renderIntl(<ShareDialog kind="report" resourceId="rpt_1" onClose={onClose} />);
    const dialog = await screen.findByRole("dialog", { name: "分享 月度复盘" });

    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("Escape in the stop-access confirmation closes only the confirmation", async () => {
    vi.mocked(getShares).mockResolvedValue(SHARED);
    const onClose = vi.fn();
    renderIntl(<ShareDialog kind="report" resourceId="rpt_1" onClose={onClose} />);
    await screen.findByText("Avery");

    fireEvent.click(screen.getByRole("radio", { name: /仅自己/ }));
    const confirm = screen.getByRole("dialog", { name: "停止所有人的访问？" });
    fireEvent.keyDown(screen.getByRole("button", { name: "取消" }), { key: "Escape" });

    expect(confirm.isConnected).toBe(false);
    expect(onClose).not.toHaveBeenCalled();
    expect(putAudience).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "分享 月度复盘" })).toBeTruthy();
  });

  it("arrow keys on the audience only move focus; they never change who can see the report", async () => {
    vi.mocked(getShares).mockResolvedValue(SHARED);
    renderIntl(<ShareDialog kind="report" resourceId="rpt_1" onClose={vi.fn()} />);
    await screen.findByText("Avery");

    const custom = screen.getByRole("radio", { name: /指定人/ });
    custom.focus();
    fireEvent.keyDown(custom, { key: "ArrowRight" });

    expect(putAudience).not.toHaveBeenCalled();
    expect(custom.getAttribute("aria-checked")).toBe("true");
    expect(document.activeElement?.getAttribute("role")).toBe("radio");
    expect(document.activeElement).not.toBe(custom);
  });

  it("a non-admin author cannot narrow an approved team-wide Skill", async () => {
    vi.mocked(getShares).mockResolvedValue({ ...SHARED, kind: "team_skill", audience: "team_wide", grants: [] } as ShareState);
    renderIntl(<ShareDialog kind="team_skill" resourceId="oskill_1" downgradeLocked onClose={vi.fn()} />, { locale: "en-US" });

    const custom = await screen.findByRole("radio", { name: /People/ });
    const onlyMe = screen.getByRole("radio", { name: /Only me/ });
    for (const option of [custom, onlyMe]) {
      expect(option.hasAttribute("disabled") || option.getAttribute("aria-disabled") === "true").toBe(true);
      expect(option.getAttribute("title")).toMatch(/team administrator/);
      fireEvent.click(option);
    }
    expect(putAudience).not.toHaveBeenCalled();
  });

  it("an admin can still narrow an approved team-wide Skill", async () => {
    vi.mocked(getShares).mockResolvedValue({ ...SHARED, kind: "team_skill", audience: "team_wide", grants: [] } as ShareState);
    vi.mocked(putAudience).mockResolvedValue(undefined as never);
    renderIntl(<ShareDialog kind="team_skill" resourceId="oskill_1" onClose={vi.fn()} />, { locale: "en-US" });

    fireEvent.click(await screen.findByRole("radio", { name: /People/ }));
    expect(putAudience).toHaveBeenCalledWith("team_skill", "oskill_1", "custom");
  });
});
