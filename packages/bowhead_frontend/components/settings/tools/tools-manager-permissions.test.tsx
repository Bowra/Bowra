/**
 * Team tools page role boundary for the MCP tool-list refresh and Choose Tools: only the team
 * owner and team admins reach the page (backend refresh-preview / refresh-apply, GET tools and
 * PATCH use the same roles), so other roles never see either entry nor trigger a request.
 */
import "@testing-library/jest-dom/vitest";
import type { ReactNode } from "react";
import { cleanup, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { openRowMenu } from "@/__tests__/row-actions";
import { fetchMe, type MeUser } from "@/lib/auth-api";
import { previewToolSourceRefresh } from "@/lib/team-tool-source-refresh-api";
import { listSources, listSourceTools, type TeamToolSourcePublic } from "@/lib/team-tool-sources-api";

vi.mock("@/lib/auth-api", () => ({ fetchMe: vi.fn() }));
vi.mock("@/lib/invite-api", () => ({ getAuthCapabilities: vi.fn(async () => null) }));
vi.mock("@/lib/api", () => ({ fetchScheduledJobs: vi.fn(async () => ({ items: [] })) }));
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));
vi.mock("@/lib/use-connector-presets", () => ({ useConnectorPresets: () => [] }));
vi.mock("@/components/admin-shell", () => ({ AdminShell: ({ children }: { children: ReactNode }) => <div>{children}</div> }));
vi.mock("@/lib/team-tool-sources-api", () => ({
  authorizeSource: vi.fn(),
  deleteSource: vi.fn(),
  getSourceUsage: vi.fn(),
  listSources: vi.fn(),
  listSourceTools: vi.fn(),
  patchSource: vi.fn(),
  previewSourceImpact: vi.fn(),
  validateSource: vi.fn(),
  TeamToolApiError: class TeamToolApiError extends Error {},
}));
vi.mock("@/lib/team-builtin-tools-api", () => ({
  configureTavily: vi.fn(),
  createDatarcTool: vi.fn(),
  deleteDatarcTool: vi.fn(),
  listBuiltinTools: vi.fn(async () => []),
  refreshDatarcScope: vi.fn(),
  setDatarcEnabled: vi.fn(),
  setTavilyEnabled: vi.fn(),
  updateDatarcTool: vi.fn(),
}));
vi.mock("@/lib/team-tool-source-refresh-api", () => ({
  applyToolSourceRefresh: vi.fn(),
  previewToolSourceRefresh: vi.fn(),
}));

import { SettingsShell } from "@/components/settings/settings-shell";

import { ToolsManager } from "./tools-manager";

const MCP: TeamToolSourcePublic = {
  id: "ots_mcp",
  team_id: "team_1",
  name: "Remote MCP",
  base_url: "https://mcp.example.com/mcp",
  kind: "mcp",
  selected_op_ids: ["keep"],
  auth_type: "none",
  has_auth: false,
  visibility: "team_wide",
  status: "enabled",
  tools_count: 1,
  op_count: 1,
  impact_guard_enabled: true,
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-05T00:00:00Z",
};

function renderAs(role: MeUser["team_role"]) {
  vi.mocked(fetchMe).mockResolvedValue({
    id: "u1",
    team_role: role,
    is_team_admin: role === "owner" || role === "team_admin",
  } as MeUser);
  renderIntl(
    <SettingsShell active="tools">
      <ToolsManager />
    </SettingsShell>,
  );
}

beforeEach(() => {
  window.history.replaceState({}, "", "/team/settings/tools/");
  vi.mocked(listSources).mockResolvedValue([{ ...MCP }]);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Refresh Tool List and Choose Tools role boundary", () => {
  it.each(["member", "audit_admin"] as const)("%s sees no refresh entry and triggers no preview", async (role) => {
    renderAs(role);
    expect(await screen.findByRole("alert")).toHaveTextContent("只有团队负责人或团队管理员可以访问团队设置");
    expect(screen.queryByRole("button", { name: "Remote MCP 的更多操作" })).toBeNull();
    expect(listSources).not.toHaveBeenCalled();
    expect(previewToolSourceRefresh).not.toHaveBeenCalled();
    expect(listSourceTools).not.toHaveBeenCalled();
  });

  it.each(["owner", "team_admin"] as const)("%s sees the refresh and Choose Tools entries on an MCP source", async (role) => {
    renderAs(role);
    const menu = await openRowMenu("Remote MCP 的更多操作");
    expect(within(menu).getByRole("menuitem", { name: "刷新工具列表" })).toBeInTheDocument();
    expect(within(menu).getByRole("menuitem", { name: "选择工具" })).toBeInTheDocument();
  });
});
