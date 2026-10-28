/**
 * V4 PRD-D-4-4 · Admin replay snapshot panel rendering tests.
 *
 * Coverage:
 *  - empty state when snapshot is null
 *  - error state when fetch threw
 *  - full DTO renders fields, breakdown, and content (collapsed by default)
 *  - missing team_id / workspace_id show "—"
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import type { MemoryPackSnapshotDTO } from "@/lib/api";

// Re-import the panel component. It's defined inside page.tsx — since it's a
// module-private helper, we import via dynamic require pattern. Easier: import
// the page module to access the named export pattern. Since it's not exported,
// duplicate the rendering here is brittle. Better — extract panel into its own
// file? For Sprint 1 we accept the duplication but add a deeper integration
// test below.
//
// Strategy: render the entire admin page with mocked api functions, then
// verify the snapshot panel DOM.

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string, vars?: Record<string, unknown>) => {
    if (vars && "n" in vars) return `${key}:${vars.n}`;
    return key;
  },
}));

vi.mock("@/components/admin-shell", () => ({
  AdminShell: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="admin-shell">{children}</div>
  ),
}));

const fetchTurnIdsMock = vi.fn();
const fetchTurnEventsMock = vi.fn();
const fetchAdminTurnSnapshotMock = vi.fn();
const fetchMeMock = vi.fn();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    fetchTurnIds: (...args: unknown[]) => fetchTurnIdsMock(...args),
    fetchTurnEvents: (...args: unknown[]) => fetchTurnEventsMock(...args),
    fetchAdminTurnSnapshot: (...args: unknown[]) =>
      fetchAdminTurnSnapshotMock(...args),
  };
});

// useAuthRedirect calls fetchMe; stub a healthy authed user so the page renders.
vi.mock("@/lib/auth-api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/auth-api")>(
    "@/lib/auth-api",
  );
  return {
    ...actual,
    fetchMe: (...args: unknown[]) => fetchMeMock(...args),
  };
});

const authedUser = {
  id: "u_member",
  email: "u@example.com",
  display_name: null,
  avatar_url: null,
  team_id: "team_a",
  tier: "ultra" as const,
  role: "member",
  status: "active",
  email_verified: true,
  credit_balance: 0,
  credit_quota: 0,
  locale: "en",
  timezone: "UTC",
  is_team_admin: true,
  workspace_count: 1,
  needs_onboarding: false,
};

afterEach(() => {
  cleanup();
  fetchTurnIdsMock.mockReset();
  fetchTurnEventsMock.mockReset();
  fetchAdminTurnSnapshotMock.mockReset();
  fetchMeMock.mockReset();
  fetchMeMock.mockResolvedValue(authedUser);
});

// Default authed before each test
fetchMeMock.mockResolvedValue(authedUser);


function makeSnapshot(overrides: Partial<MemoryPackSnapshotDTO> = {}): MemoryPackSnapshotDTO {
  return {
    turn_id: "t_abc",
    user_id: "u_member",
    team_id: "team_a",
    workspace_id: "ws1",
    char_count: 42,
    sections_breakdown: { notes: 30, entries: 10 },
    content: "📌 some pinned fact\n• another bullet",
    pack_version: "v2",
    created_at: "2026-05-03T10:00:00+00:00",
    ...overrides,
  };
}


describe("Admin replay snapshot panel", () => {
  it("renders empty-state when snapshot is null after select", async () => {
    fetchTurnIdsMock.mockResolvedValue(["t_abc", "t_xyz"]);
    fetchTurnEventsMock.mockResolvedValue([]);
    fetchAdminTurnSnapshotMock.mockResolvedValue(null);

    const { default: TurnsPage } = await import("./page");
    render(<TurnsPage />);

    // Wait for ids to load + click first turn
    const tid = await screen.findByText("t_abc");
    tid.click();
    await screen.findByTestId("admin-replay-snapshot");
    expect(screen.getByTestId("admin-replay-empty")).toBeTruthy();
  });

  it("renders snapshot DTO fields + breakdown + content", async () => {
    fetchTurnIdsMock.mockResolvedValue(["t_abc"]);
    fetchTurnEventsMock.mockResolvedValue([]);
    fetchAdminTurnSnapshotMock.mockResolvedValue(makeSnapshot());

    const { default: TurnsPage } = await import("./page");
    render(<TurnsPage />);

    const tid = await screen.findByText("t_abc");
    tid.click();
    const panel = await screen.findByTestId("admin-replay-snapshot");

    // user_id, pack_version visible inside the panel
    expect(panel.textContent).toContain("u_member");
    expect(panel.textContent).toContain("v2");
    expect(panel.textContent).toContain("t_abc");

    // breakdown rendered
    const breakdown = screen.getByTestId("admin-replay-breakdown");
    expect(breakdown.textContent).toContain("notes: 30");
    expect(breakdown.textContent).toContain("entries: 10");

    // content panel exists (inside details), prefilled with content
    expect(screen.getByTestId("admin-replay-content").textContent).toContain(
      "some pinned fact",
    );
  });

  it("renders error state when fetch throws", async () => {
    fetchTurnIdsMock.mockResolvedValue(["t_err"]);
    fetchTurnEventsMock.mockResolvedValue([]);
    fetchAdminTurnSnapshotMock.mockRejectedValue(new Error("admin replay snapshot: 500"));

    const { default: TurnsPage } = await import("./page");
    render(<TurnsPage />);

    const tid = await screen.findByText("t_err");
    tid.click();
    await screen.findByTestId("admin-replay-snapshot");
    const err = await screen.findByTestId("admin-replay-error");
    // Keep status codes out of user-facing text; show a localized explanation (next-intl is mocked to return keys here).
    expect(err.textContent).toContain("snapshot_failed_notice");
    expect(err.textContent).not.toContain("500");
  });

  it("shows '—' for null team_id / workspace_id (orphan turn rendered with override)", async () => {
    fetchTurnIdsMock.mockResolvedValue(["t_orphan"]);
    fetchTurnEventsMock.mockResolvedValue([]);
    fetchAdminTurnSnapshotMock.mockResolvedValue(
      makeSnapshot({ team_id: null, workspace_id: null }),
    );

    const { default: TurnsPage } = await import("./page");
    render(<TurnsPage />);

    const tid = await screen.findByText("t_orphan");
    tid.click();
    await screen.findByTestId("admin-replay-snapshot");

    const dashes = screen.getAllByText("—");
    // Two dashes: team_id + workspace_id
    expect(dashes.length).toBeGreaterThanOrEqual(2);
  });
});
