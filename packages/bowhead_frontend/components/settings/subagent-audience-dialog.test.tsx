import {
  afterEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

const mocks = vi.hoisted(() => {
  return {
    setSubagentAudience: vi.fn(),
    fetchTeamDirectory: vi.fn(),
    fetchTeamGroups: vi.fn(),
    translate: (
      key: string,
      values?: Record<string, string | number>,
    ) => values?.count === undefined ? key : `${key}:${values.count}`,
  };
});

vi.mock("next-intl", () => ({
  useTranslations: () => mocks.translate,
}));
vi.mock("@/lib/team-subagents-api", () => ({
  TeamSubagentApiError: class TeamSubagentApiError extends Error {
    code?: string;

    constructor(_status: number, message: string, code?: string) {
      super(message);
      this.code = code;
    }
  },
  setSubagentAudience: (...args: unknown[]) =>
    mocks.setSubagentAudience(...args),
}));

vi.mock("@/lib/content-share-api", () => ({
  fetchTeamDirectory: (...args: unknown[]) =>
    mocks.fetchTeamDirectory(...args),
  fetchTeamGroups: (...args: unknown[]) =>
    mocks.fetchTeamGroups(...args),
}));

import { SubagentAudienceDialog } from "./subagent-audience-dialog";

const actions = {
  view_consumer_detail: false,
  view_manager_detail: true,
  edit: true,
  change_status: true,
  manage_audience: true,
  transfer_owner: false,
  delete: true,
  validate: false,
  bring_to_web_chat: false,
  govern: false,
};

const detail = (over: Record<string, unknown> = {}) => ({
  schema_version: 1 as const,
  kind: "team_owner_manager_detail" as const,
  id: "osa_1",
  team_id: "team_1",
  name: "sales-agent",
  display_name: "Sales agent",
  description: "",
  system_prompt: "",
  tool_refs: ["bi_query"],
  tool_count: 1,
  max_iterations: 10,
  skills: [],
  model: null,
  output_schema: null,
  visibility: "team_wide",
  status: "enabled",
  created_by: "u_owner",
  created_at: "2026-07-29T00:00:00Z",
  updated_at: "2026-07-29T00:00:00Z",
  source: "team" as const,
  owner: {
    id: "u_owner",
    display_name: "Owner",
    status: "active" as const,
  },
  audience: {
    mode: "team_wide" as const,
    roster: [],
  },
  lifecycle_state: "active",
  version: 1,
  etag: 'W/"osa_1:1"',
  actions,
  action_reasons: {},
  ...over,
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("SubagentAudienceDialog", () => {
  it("builds a local roster and saves one exact replacement", async () => {
    mocks.fetchTeamDirectory.mockResolvedValue({
      items: [
        {
          id: "u_1",
          display_name: "Bob",
          email_masked: "b***@example.com",
        },
      ],
      next_cursor: null,
    });
    mocks.fetchTeamGroups.mockResolvedValue({
      items: [
        {
          id: "grp_1",
          name: "Reviewers",
          member_count: 3,
        },
      ],
    });
    const updated = detail({
      audience: {
        mode: "custom",
        roster: [
          {
            principal_type: "user",
            principal_id: "u_1",
            permission: "use",
          },
        ],
      },
      visibility: "creator_only",
      version: 2,
      etag: 'W/"osa_1:2"',
    });
    mocks.setSubagentAudience.mockResolvedValue(updated);
    const onSaved = vi.fn();

    render(
      <SubagentAudienceDialog
        detail={detail()}
        onClose={vi.fn()}
        onSaved={onSaved}
        onOpenReleaseManager={vi.fn()}
      />,
    );
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(
      within(dialog).getByRole("radio", { name: "audience_custom" }),
    );
    const bob = await within(dialog).findByText("Bob");
    const bobRow = bob.parentElement?.parentElement;
    expect(bobRow).toBeTruthy();
    fireEvent.click(
      within(bobRow as HTMLElement).getByRole("button", {
        name: "audience_add",
      }),
    );
    fireEvent.click(
      within(dialog).getByRole("button", {
        name: "audience_save",
      }),
    );

    await waitFor(() =>
      expect(mocks.setSubagentAudience).toHaveBeenCalledWith(
        "osa_1",
        {
          mode: "custom",
          user_ids: ["u_1"],
          group_ids: [],
        },
      ),
    );
    expect(onSaved).toHaveBeenCalledWith(updated);
  });

  it("shows a safe message when the team directory cannot be loaded", async () => {
    mocks.fetchTeamDirectory.mockRejectedValue(
      new Error("upstream socket contained private diagnostics"),
    );
    mocks.fetchTeamGroups.mockResolvedValue({ items: [] });

    render(
      <SubagentAudienceDialog
        detail={detail()}
        onClose={vi.fn()}
        onSaved={vi.fn()}
        onOpenReleaseManager={vi.fn()}
      />,
    );

    const dialog = await screen.findByRole("dialog");
    expect(
      await within(dialog).findByText("audience_directory_failed"),
    ).toBeTruthy();
    expect(
      within(dialog).queryByText(/private diagnostics/),
    ).toBeNull();
  });

  it("allows an oversized legacy roster to be repaired by switching to private", async () => {
    mocks.fetchTeamDirectory.mockResolvedValue({
      items: [],
      next_cursor: null,
    });
    mocks.fetchTeamGroups.mockResolvedValue({ items: [] });
    const legacy = detail({
      audience: {
        mode: "custom",
        roster: Array.from({ length: 201 }, (_, index) => ({
          principal_type: "user",
          principal_id: `u_${index}`,
          permission: "use",
        })),
      },
      visibility: "creator_only",
    });
    mocks.setSubagentAudience.mockResolvedValue(detail({
      audience: { mode: "private", roster: [] },
      visibility: "creator_only",
      version: 2,
      etag: 'W/"osa_1:2"',
    }));

    render(
      <SubagentAudienceDialog
        detail={legacy}
        onClose={vi.fn()}
        onSaved={vi.fn()}
        onOpenReleaseManager={vi.fn()}
      />,
    );

    const dialog = await screen.findByRole("dialog");
    fireEvent.click(
      within(dialog).getByRole("radio", { name: "audience_private" }),
    );
    fireEvent.click(
      within(dialog).getByRole("button", {
        name: "audience_save",
      }),
    );

    await waitFor(() =>
      expect(mocks.setSubagentAudience).toHaveBeenCalledWith(
        "osa_1",
        {
          mode: "private",
          user_ids: [],
          group_ids: [],
        },
      ),
    );
  });

  it("offers the release workspace when a governed Agent rejects the legacy audience write", async () => {
    mocks.fetchTeamDirectory.mockResolvedValue({ items: [], next_cursor: null });
    mocks.fetchTeamGroups.mockResolvedValue({ items: [] });
    const onOpenReleaseManager = vi.fn();
    const { TeamSubagentApiError } = await import("@/lib/team-subagents-api");
    mocks.setSubagentAudience.mockRejectedValue(
      new TeamSubagentApiError(409, "Agent uses releases", "agent_revision_writer_required"),
    );

    render(
      <SubagentAudienceDialog
        detail={detail({ audience: { mode: "private", roster: [] }, visibility: "creator_only" })}
        onClose={vi.fn()}
        onSaved={vi.fn()}
        onOpenReleaseManager={onOpenReleaseManager}
      />,
    );
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("radio", { name: "audience_team_wide" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "audience_save" }));

    expect(await within(dialog).findByText("audience_release_managed")).toBeTruthy();
    fireEvent.click(within(dialog).getByRole("button", { name: "audience_open_release_manager" }));
    expect(onOpenReleaseManager).toHaveBeenCalledOnce();
    expect(within(dialog).queryByText(/Agent uses releases/)).toBeNull();
  });
});
