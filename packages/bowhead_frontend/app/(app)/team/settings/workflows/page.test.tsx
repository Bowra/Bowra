import { cleanup, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import type { TeamSkillPublic } from "@/lib/workflow-solidify-api";

const listTeamSkillsMock = vi.fn();
vi.mock("@/components/settings/settings-shell", () => ({
  SettingsShell: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));
vi.mock("@/lib/auth-api", () => ({
  fetchMe: vi.fn().mockResolvedValue({
    id: "usr_viewer",
    is_team_admin: true,
    capabilities: { workflow_creator: true },
  }),
}));
vi.mock("@/lib/workflow-solidify-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/workflow-solidify-api")>()),
  listTeamSkills: (...args: unknown[]) => listTeamSkillsMock(...args),
}));

import WorkflowsSettingsPage from "./page";

function skill(name: string, overrides: Partial<TeamSkillPublic>): TeamSkillPublic {
  return {
    id: `oskill_${name}`,
    name,
    display_name: name,
    description: "",
    visibility: "team_wide",
    status: "enabled",
    verdict: null,
    size_bytes: 579_584,
    approval: "approved",
    updated_at: "2026-09-04T08:00:00Z",
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("WorkflowsSettingsPage owner line", () => {
  it("shows the creator's name, never the raw user id", async () => {
    listTeamSkillsMock.mockResolvedValue({
      items: [
        skill("named-recipe", { created_by: "usr_ann", created_by_name: "Ann Lee" }),
        // Unresolvable creator: the current API sends null, older ones echoed the id.
        skill("ghost-recipe", { created_by: "usr_24f0f61b37bb", created_by_name: null }),
        skill("legacy-recipe", { created_by: "usr_35847490c01d", created_by_name: "usr_35847490c01d" }),
        skill("pending-recipe", {
          approval: "pending",
          created_by: "usr_24f0f61b37bb",
          created_by_name: "usr_24f0f61b37bb",
        }),
      ],
    });

    renderIntl(<WorkflowsSettingsPage />, { locale: "en-US" });

    expect(await screen.findByText(/^Ann Lee · /)).toBeTruthy();
    const metaLines = Array.from(document.querySelectorAll(".ai-sa-meta"), (node) => node.textContent);
    expect(metaLines.map((line) => line?.split(" · ")[0])).toEqual(["Ann Lee", "Team member", "Team member"]);
    expect(screen.getByText(/^by Team member · submitted/)).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/usr_/);
  });
});

describe("WorkflowsSettingsPage approval risk chips", () => {
  it("shows the update candidate's risks, including script languages, on the collapsed card", async () => {
    listTeamSkillsMock.mockResolvedValue({
      items: [
        skill("update-recipe", {
          candidate_state: "pending",
          risk_report: { langs: { ".py": 1 }, keywords: ["curl "], shebangs: [] },
          candidate_risk_report: { langs: { ".sh": 2 }, keywords: ["sudo "], shebangs: [] },
        }),
      ],
    });

    renderIntl(<WorkflowsSettingsPage />, { locale: "en-US" });

    const card = await screen.findByTestId("wf-approval-update-recipe");
    expect(card.textContent).toContain(".sh×2");
    expect(card.textContent).toContain("sudo");
    expect(card.textContent).not.toContain("curl");
    expect(card.textContent).not.toContain(".py×1");
  });
});
