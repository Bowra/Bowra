import { cleanup, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { fetchSkills } from "@/lib/api";
import { fetchMe } from "@/lib/auth-api";
import { listTeamSkills } from "@/lib/workflow-solidify-api";
import type { TeamSkillPublic } from "@/lib/workflow-solidify-api";

const acr = vi.hoisted(() => ({ getAccessRequestStatus: vi.fn(), requestAccess: vi.fn(), withdrawAccessRequest: vi.fn() }));
vi.mock("@/lib/api", () => ({
  deleteSkill: vi.fn(),
  fetchSkills: vi.fn(),
  patchSkill: vi.fn(),
  patchSkillsBulk: vi.fn(),
  previewSkillImpact: vi.fn(),
}));
vi.mock("@/lib/workflow-solidify-api", () => ({
  deleteTeamSkill: vi.fn(),
  listTeamSkills: vi.fn(),
  patchTeamSkill: vi.fn(),
  previewTeamSkillImpact: vi.fn(),
}));
vi.mock("@/lib/auth-api", () => ({ fetchMe: vi.fn() }));
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));
vi.mock("@/components/skill-upload-modal", () => ({ SkillUploadModal: () => null }));
vi.mock("@/components/skill-drawer", () => ({
  SkillDrawer: ({ target }: { target: { skill: { name: string } } }) => <div data-testid="skill-drawer">{target.skill.name}</div>,
}));
vi.mock("@/components/share/share-dialog", () => ({ ShareDialog: () => null }));
vi.mock("@/lib/access-requests-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/access-requests-api")>()),
  ...acr,
}));

import { SkillsSettingsContent } from "./skills-settings-content";

const visible = { id: "tsk_visible", name: "visible-skill", display_name: "Visible", status: "enabled" } as unknown as TeamSkillPublic;

function setup(accessRequests: boolean) {
  vi.mocked(fetchMe).mockResolvedValue({
    id: "user_1",
    is_team_admin: false,
    capabilities: { access_requests: accessRequests, workflow_creator: true },
  } as never);
  vi.mocked(fetchSkills).mockResolvedValue({ builtin: [], user_upload: [], user_generated: [] });
  vi.mocked(listTeamSkills).mockResolvedValue({ items: [visible] });
  acr.getAccessRequestStatus.mockResolvedValue({ state: "none", request: null, retry_after: null });
}

describe("Skills page deep link to a skill the viewer cannot see", () => {
  beforeEach(() => window.history.replaceState({}, "", "/skills/?skill=tsk_hidden"));
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("access requests off: an unknown skill link keeps the normal page", async () => {
    setup(false);
    renderIntl(<SkillsSettingsContent />);
    await waitFor(() => expect(listTeamSkills).toHaveBeenCalled());
    await screen.findByRole("searchbox");
    expect(screen.queryByTestId("access-request-panel")).toBeNull();
    expect(acr.getAccessRequestStatus).not.toHaveBeenCalled();
  });

  it("access requests on: a skill the viewer cannot see shows the request panel", async () => {
    setup(true);
    renderIntl(<SkillsSettingsContent />);
    expect(await screen.findByText("这个技能不存在，或还没有分享给你。你可以申请使用，管理者同意后你会收到通知。")).toBeTruthy();
    expect(acr.getAccessRequestStatus).toHaveBeenCalledWith("team_skill", "tsk_hidden");
  });

  it("access requests on: a visible skill link opens its details instead", async () => {
    window.history.replaceState({}, "", "/skills/?skill=tsk_visible");
    setup(true);
    renderIntl(<SkillsSettingsContent />);
    expect((await screen.findByTestId("skill-drawer")).textContent).toBe("visible-skill");
    expect(screen.queryByTestId("access-request-panel")).toBeNull();
  });
});
