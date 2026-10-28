import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import {
  confirmAgentLocalization,
  getAgentLocalizations,
  saveAgentLocalization,
  suggestAgentLocalization,
  type AgentLocalizationManager,
  type AgentLocalizedListing,
} from "@/lib/agents-api";
import { ApiError } from "@/lib/api-client";
import { AgentLocalizationPanel } from "./agent-localization-panel";

vi.mock("@/lib/agents-api", () => ({
  confirmAgentLocalization: vi.fn(),
  getAgentLocalizations: vi.fn(),
  saveAgentLocalization: vi.fn(),
  suggestAgentLocalization: vi.fn(),
}));

const MANAGER: AgentLocalizationManager = {
  kind: "catalog_localization_manager",
  schema_version: 1,
  resource_kind: "agent",
  resource_id: "osa_1",
  resource_revision: "awc_1",
  working_copy_row_version: 1,
  source_locale: "zh-CN",
  source: { display_name: "财务助手", summary: "分析团队财务数据。" },
  source_hash: `sha256:${"1".repeat(64)}`,
  items: [],
};

function item(overrides: Partial<AgentLocalizedListing> = {}): AgentLocalizedListing {
  return {
    id: "cll_1",
    team_id: "team_1",
    resource_kind: "agent",
    resource_id: "osa_1",
    resource_revision: "awc_1",
    locale: "en-US",
    source_locale: "zh-CN",
    fields: { display_name: "Finance Assistant", summary: "Analyze team finances." },
    source_hash: MANAGER.source_hash,
    generation_source: "llm",
    review_state: "draft",
    row_version: 1,
    reviewed_by: null,
    reviewed_at: null,
    created_by: "user_1",
    created_at: "2026-09-05T00:00:00Z",
    updated_at: "2026-09-05T00:00:00Z",
    ...overrides,
  };
}

describe("AgentLocalizationPanel", () => {
  beforeEach(() => {
    vi.mocked(getAgentLocalizations).mockResolvedValue(MANAGER);
    vi.mocked(suggestAgentLocalization).mockResolvedValue(item());
    vi.mocked(saveAgentLocalization).mockResolvedValue(item({
      fields: { display_name: "Finance Partner", summary: "Analyze team finances." },
      generation_source: "manual",
      row_version: 2,
    }));
    vi.mocked(confirmAgentLocalization).mockResolvedValue(item({
      fields: { display_name: "Finance Partner", summary: "Analyze team finances." },
      generation_source: "manual",
      review_state: "confirmed",
      row_version: 3,
      reviewed_by: "user_1",
      reviewed_at: "2026-09-05T00:01:00Z",
    }));
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("keeps AI output editable and requires a saved draft before confirmation", async () => {
    const changed = vi.fn();
    renderIntl(
      <AgentLocalizationPanel
        agentId="osa_1"
        archived={false}
        parentDirty={false}
        refreshToken="awc_1:1"
        onLocalizationChanged={changed}
      />,
    );

    expect(await screen.findByText("让不同语言的同事都看懂这个智能体")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "让 AI 起草" }));
    await waitFor(() => expect(suggestAgentLocalization).toHaveBeenCalledWith("osa_1", {
      source_locale: "zh-CN",
      target_locale: "en-US",
      expected_source_hash: MANAGER.source_hash,
      expected_row_version: null,
    }));
    const name = await screen.findByDisplayValue("Finance Assistant");
    fireEvent.change(name, { target: { value: "Finance Partner" } });
    expect(screen.getByRole("button", { name: "确认译文" }).hasAttribute("disabled")).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await waitFor(() => expect(saveAgentLocalization).toHaveBeenCalledWith(
      "osa_1",
      "en-US",
      {
        source_locale: "zh-CN",
        fields: { display_name: "Finance Partner", summary: "Analyze team finances." },
        expected_source_hash: MANAGER.source_hash,
        expected_row_version: 1,
      },
    ));
    const confirm = screen.getByRole("button", { name: "确认译文" });
    await waitFor(() => expect(confirm.hasAttribute("disabled")).toBe(false));
    fireEvent.click(confirm);
    await waitFor(() => expect(confirmAgentLocalization).toHaveBeenCalledWith("osa_1", "en-US", 2));
    expect(changed).toHaveBeenCalledTimes(3);
    expect(await screen.findByText("译文已确认，将随下一次发布生效。")).toBeTruthy();
  });

  it.each([
    ["de-DE", "德语", "Finanzassistent"],
    ["fr-FR", "Français", "Assistant financier"],
  ] as const)("saves and confirms a %s listing translation", async (locale, label, translatedName) => {
    vi.mocked(saveAgentLocalization).mockResolvedValue(item({
      locale,
      fields: { display_name: translatedName, summary: "Translated summary" },
      generation_source: "manual",
    }));
    vi.mocked(confirmAgentLocalization).mockResolvedValue(item({ locale, review_state: "confirmed" }));
    renderIntl(<AgentLocalizationPanel
      agentId="osa_1" archived={false} parentDirty={false}
      refreshToken="awc_1:1" onLocalizationChanged={() => undefined}
    />);
    await screen.findByText("财务助手");
    const target = screen.getByRole("combobox", { name: "翻译语言" });
    expect(within(target).getByRole("option", { name: label })).toHaveProperty("value", locale);
    fireEvent.change(target, { target: { value: locale } });
    fireEvent.change(screen.getByRole("textbox", { name: "翻译后的名称" }), { target: { value: translatedName } });
    fireEvent.change(screen.getByRole("textbox", { name: "翻译后的介绍" }), { target: { value: "Translated summary" } });
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await waitFor(() => expect(saveAgentLocalization).toHaveBeenCalledWith("osa_1", locale, {
      source_locale: "zh-CN",
      fields: { display_name: translatedName, summary: "Translated summary" },
      expected_source_hash: MANAGER.source_hash,
      expected_row_version: null,
    }));
    const confirm = screen.getByRole("button", { name: "确认译文" });
    await waitFor(() => expect(confirm.hasAttribute("disabled")).toBe(false));
    fireEvent.click(confirm);
    await waitFor(() => expect(confirmAgentLocalization).toHaveBeenCalledWith("osa_1", locale, 1));
  });

  it("stays absent when M4 is unavailable", async () => {
    vi.mocked(getAgentLocalizations).mockRejectedValue(new ApiError(404, "Not found"));
    renderIntl(
      <AgentLocalizationPanel
        agentId="osa_1"
        archived={false}
        parentDirty={false}
        refreshToken="awc_1:1"
        onLocalizationChanged={() => undefined}
      />,
    );

    await waitFor(() => expect(getAgentLocalizations).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByText("让不同语言的同事都看懂这个智能体")).toBeNull());
  });

  it("lets the owner re-save unchanged text after its source becomes stale", async () => {
    const stale = item({ review_state: "stale", row_version: 2 });
    vi.mocked(getAgentLocalizations).mockResolvedValue({ ...MANAGER, items: [stale] });
    renderIntl(
      <AgentLocalizationPanel
        agentId="osa_1"
        archived={false}
        parentDirty={false}
        refreshToken="awc_1:2"
        onLocalizationChanged={() => undefined}
      />,
    );

    const save = await screen.findByRole("button", { name: "保存草稿" });
    await waitFor(() => expect(save.hasAttribute("disabled")).toBe(false));
    fireEvent.click(save);
    await waitFor(() => expect(saveAgentLocalization).toHaveBeenCalledWith(
      "osa_1",
      "en-US",
      {
        source_locale: "zh-CN",
        fields: stale.fields,
        expected_source_hash: MANAGER.source_hash,
        expected_row_version: 2,
      },
    ));
  });

  it("refreshes instead of binding an old translation to changed source text", async () => {
    vi.mocked(suggestAgentLocalization).mockRejectedValue(new ApiError(
      409,
      "Conflict",
      { code: "catalog_localization_source_changed" },
    ));
    renderIntl(
      <AgentLocalizationPanel
        agentId="osa_1"
        archived={false}
        parentDirty={false}
        refreshToken="awc_1:1"
        onLocalizationChanged={() => undefined}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "让 AI 起草" }));
    await waitFor(() => expect(getAgentLocalizations).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("原文或译文已在其他窗口更新。这里已刷新，请重新检查后再保存。")).toBeTruthy();
  });
});
