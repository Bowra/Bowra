import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import {
  getAgentTaxonomy,
  saveAgentTaxonomy,
  suggestAgentTaxonomy,
  type AgentTaxonomyManager,
  type AgentTaxonomySuggestion,
} from "@/lib/agents-api";
import { ApiError } from "@/lib/api-client";
import { AgentTaxonomyPanel } from "./agent-taxonomy-panel";

vi.mock("@/lib/agents-api", () => ({
  getAgentTaxonomy: vi.fn(),
  saveAgentTaxonomy: vi.fn(),
  suggestAgentTaxonomy: vi.fn(),
}));

const SOURCE_HASH = `sha256:${"1".repeat(64)}`;
const SUGGESTION: AgentTaxonomySuggestion = {
  id: `ats_${"2".repeat(32)}`,
  source_hash: SOURCE_HASH,
  taxonomy_version: "agent-taxonomy-v1",
  suggestion: {
    domains: ["finance"],
    jobs: ["analysis"],
    team_tag_ids: [`att_${"3".repeat(32)}`],
    new_team_tags: [],
    confidence: 0.91,
    reasons: ["适合财务分析场景"],
  },
  confidence: 0.91,
  state: "pending",
  row_version: 1,
  reviewed_by: null,
  reviewed_at: null,
  created_at: "2026-09-05T00:00:00Z",
  updated_at: "2026-09-05T00:00:00Z",
};

const MANAGER: AgentTaxonomyManager = {
  kind: "agent_taxonomy_manager",
  schema_version: 1,
  agent_id: "osa_1",
  working_copy_id: "awc_1",
  working_copy_row_version: 4,
  source_hash: SOURCE_HASH,
  source: { display_name: "财务伙伴", summary: "分析经营数据" },
  platform: {
    version: "agent-taxonomy-v1",
    domains: [{ id: "finance", label: "财务" }, { id: "sales", label: "销售" }],
    jobs: [{ id: "analysis", label: "分析" }, { id: "writing", label: "撰写" }],
  },
  selection: {
    version: "agent-taxonomy-v1",
    domains: ["finance"],
    jobs: ["analysis"],
    team_tag_ids: [`att_${"3".repeat(32)}`],
  },
  team_tags: [{
    id: `att_${"3".repeat(32)}`,
    display_name: "月度经营",
    status: "active",
    merged_into_id: null,
    row_version: 1,
    created_at: "2026-09-05T00:00:00Z",
    updated_at: "2026-09-05T00:00:00Z",
  }],
  suggestion: null,
};

describe("AgentTaxonomyPanel", () => {
  beforeEach(() => {
    vi.mocked(getAgentTaxonomy).mockResolvedValue(MANAGER);
    vi.mocked(suggestAgentTaxonomy).mockResolvedValue(SUGGESTION);
    vi.mocked(saveAgentTaxonomy).mockResolvedValue({
      ...MANAGER,
      working_copy_row_version: 5,
      suggestion: { ...SUGGESTION, state: "accepted" },
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("keeps an identical AI suggestion reviewable and records explicit Owner acceptance", async () => {
    const changed = vi.fn();
    renderIntl(
      <AgentTaxonomyPanel
        agentId="osa_1"
        archived={false}
        parentDirty={false}
        refreshToken="awc_1:4"
        onTaxonomyChanged={changed}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "让 AI 推荐" }));
    await waitFor(() => expect(suggestAgentTaxonomy).toHaveBeenCalledWith("osa_1", SOURCE_HASH));
    const save = screen.getByRole("button", { name: "保存分类" });
    expect(save.hasAttribute("disabled")).toBe(false);
    fireEvent.click(save);
    await waitFor(() => expect(saveAgentTaxonomy).toHaveBeenCalledWith("osa_1", {
      domains: ["finance"],
      jobs: ["analysis"],
      team_tag_ids: [`att_${"3".repeat(32)}`],
      new_team_tags: [],
      expected_working_copy_row_version: 4,
      suggestion_id: SUGGESTION.id,
    }));
    expect(changed).toHaveBeenCalledTimes(1);
  });

  it("does not render when the M4 capability is disabled", async () => {
    vi.mocked(getAgentTaxonomy).mockRejectedValue(new ApiError(404, "Not found"));
    renderIntl(
      <AgentTaxonomyPanel
        agentId="osa_1"
        archived={false}
        parentDirty={false}
        refreshToken="awc_1:4"
        onTaxonomyChanged={() => undefined}
      />,
    );

    await waitFor(() => expect(getAgentTaxonomy).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByText("让大家更容易找到这个智能体")).toBeNull());
  });

  it("waits for unsaved Agent edits before classifying", async () => {
    renderIntl(
      <AgentTaxonomyPanel
        agentId="osa_1"
        archived={false}
        parentDirty
        refreshToken="awc_1:4"
        onTaxonomyChanged={() => undefined}
      />,
    );

    expect((await screen.findByRole("button", { name: "让 AI 推荐" })).hasAttribute("disabled")).toBe(true);
    expect(screen.getByText(/请先保存智能体草稿/)).toBeTruthy();
  });

  it("does not overwrite unsaved manual classification with an AI suggestion", async () => {
    renderIntl(
      <AgentTaxonomyPanel
        agentId="osa_1"
        archived={false}
        parentDirty={false}
        refreshToken="awc_1:4"
        onTaxonomyChanged={() => undefined}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "销售" }));
    expect(screen.getByRole("button", { name: "让 AI 推荐" }).hasAttribute("disabled")).toBe(true);
    expect(suggestAgentTaxonomy).not.toHaveBeenCalled();
  });
});
