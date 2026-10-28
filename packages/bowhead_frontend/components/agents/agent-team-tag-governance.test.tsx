import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import {
  changeAgentTeamTagStatus,
  createAgentTeamTag,
  getAgentTeamTags,
  type AgentTeamTagManager,
} from "@/lib/agents-api";
import { ApiError } from "@/lib/api-client";
import { AgentTeamTagGovernance } from "./agent-team-tag-governance";

vi.mock("@/lib/agents-api", () => ({
  changeAgentTeamTagStatus: vi.fn(),
  createAgentTeamTag: vi.fn(),
  getAgentTeamTags: vi.fn(),
}));

const OLD_ID = `att_${"1".repeat(32)}`;
const TARGET_ID = `att_${"2".repeat(32)}`;
const ALIAS_ID = `att_${"3".repeat(32)}`;
const MANAGER: AgentTeamTagManager = {
  kind: "agent_team_tag_manager",
  schema_version: 1,
  taxonomy_version: "agent-taxonomy-v1",
  limits: { team: 200, per_agent: 12 },
  items: [
    {
      id: OLD_ID,
      display_name: "Budget",
      status: "active",
      merged_into_id: null,
      row_version: 1,
      created_at: "2026-09-05T00:00:00Z",
      updated_at: "2026-09-05T00:00:00Z",
    },
    {
      id: TARGET_ID,
      display_name: "Financial planning",
      status: "active",
      merged_into_id: null,
      row_version: 2,
      created_at: "2026-09-05T00:00:00Z",
      updated_at: "2026-09-05T00:00:00Z",
    },
  ],
};

describe("AgentTeamTagGovernance", () => {
  beforeEach(() => {
    vi.mocked(getAgentTeamTags).mockResolvedValue(MANAGER);
    vi.mocked(changeAgentTeamTagStatus).mockResolvedValue({
      ...MANAGER.items[0],
      status: "merged",
      merged_into_id: TARGET_ID,
      row_version: 2,
    });
    vi.mocked(createAgentTeamTag).mockResolvedValue(MANAGER.items[0]);
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    vi.restoreAllMocks();
  });

  it("requires an explicit confirmation before merging a shared tag", async () => {
    renderIntl(<AgentTeamTagGovernance />);

    const budgetLabels = await screen.findAllByText("Budget");
    const row = budgetLabels.find((item) => item.tagName === "STRONG")?.closest("article");
    if (!row) throw new Error("tag row missing");
    fireEvent.change(within(row).getByRole("combobox"), { target: { value: TARGET_ID } });
    fireEvent.click(within(row).getByRole("button", { name: "合并" }));

    const dialog = await screen.findByRole("dialog", { name: "把 Budget 合并到 Financial planning？" });
    expect(changeAgentTeamTagStatus).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "合并" }));
    await waitFor(() => expect(changeAgentTeamTagStatus).toHaveBeenCalledWith(OLD_ID, {
      action: "merge",
      row_version: 1,
      target_tag_id: TARGET_ID,
    }));
  });

  it("stays absent when taxonomy governance is unavailable", async () => {
    vi.mocked(getAgentTeamTags).mockRejectedValue(new ApiError(404, "Not found"));
    renderIntl(<AgentTeamTagGovernance />);

    await waitFor(() => expect(getAgentTeamTags).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByText("团队智能体标签")).toBeNull());
  });

  it("explains and disables changes to a tag that already owns aliases", async () => {
    vi.mocked(getAgentTeamTags).mockResolvedValue({
      ...MANAGER,
      items: [
        ...MANAGER.items,
        {
          ...MANAGER.items[0],
          id: ALIAS_ID,
          display_name: "Finance legacy",
          status: "merged",
          merged_into_id: TARGET_ID,
        },
      ],
    });
    renderIntl(<AgentTeamTagGovernance />);

    const target = (await screen.findAllByText("Financial planning"))
      .find((item) => item.tagName === "STRONG")
      ?.closest("article");
    if (!target) throw new Error("target tag row missing");
    expect(within(target).getByText(/已有标签指向这里/)).toBeTruthy();
    expect(within(target).getByRole("button", { name: "停用" }).hasAttribute("disabled")).toBe(true);
    expect(within(target).getByRole("button", { name: "合并" }).hasAttribute("disabled")).toBe(true);
  });
});
