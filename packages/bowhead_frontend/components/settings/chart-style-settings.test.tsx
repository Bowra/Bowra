import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { effectiveChartStyleV2Fixture } from "@/__tests__/chart-style-export-fixture";
import { renderIntl } from "@/__tests__/intl-render";
import messages from "@/messages/zh-CN.json";
import { patchChartAccessibility } from "@/lib/api";
import { chartStylePreset } from "@/lib/chart/chart-style-registry";
import {
  activateTeamChartStyle,
  getChartStyleOptionsV2,
  getTeamChartStyle,
  getTeamChartStylePolicyV2,
  listTeamChartStyles,
  setChartStyleSelectionV2,
  setTeamChartStylePolicyV2,
} from "@/lib/team-chart-styles-api";
import type { EffectiveThemeResponse } from "@/lib/theme-config";
import { refreshEffectiveTheme } from "@/lib/use-theme-config";

import { PersonalChartStyleSettings, TeamChartStyleSettings } from "./chart-style-settings";

const effectiveChartStyle = effectiveChartStyleV2Fixture();
const BUNDLE: EffectiveThemeResponse = {
  effective: {
    version: 1,
    mode: "light",
    colorProfile: { type: "preset", id: "bowhead" },
    typographyProfile: { type: "preset", id: "bowhead" },
    typeScale: "standard",
    density: "comfortable",
    radius: "balanced",
    motion: "standard",
    chartPalette: "default",
  },
  locks: {},
  source: {},
  userConfig: { version: 1 },
  chartStyle: effectiveChartStyle,
};

const options = (["bowhead-editorial", "clean-blue", "analytical-neutral"] as const).map((id) => ({
  id,
  kind: "builtIn" as const,
  name: id,
  description: id,
  revision: "2026.09.1",
  snapshotChecksum: `sha256:${"b".repeat(64)}`,
  resolvedPresetSnapshot: {
    ...effectiveChartStyle.style.resolvedPresetSnapshot,
    colors: {
      light: {
        categorical: [...chartStylePreset(id).tokens.light.palette],
        sequential: { ...chartStylePreset(id).tokens.light.sequential },
        diverging: { ...chartStylePreset(id).tokens.light.diverging },
      },
      dark: {
        categorical: [...chartStylePreset(id).tokens.dark.palette],
        sequential: { ...chartStylePreset(id).tokens.dark.sequential },
        diverging: { ...chartStylePreset(id).tokens.dark.diverging },
      },
    },
  },
}));

vi.mock("@/components/canvas/sections/chart-block", () => ({
  ChartBlock: (props: { spec: { kind: string }; surface?: string; renderPlacement?: string; renderMode?: string }) => (
    <div
      data-testid="real-chart-preview"
      data-kind={props.spec.kind}
      data-surface={props.surface}
      data-placement={props.renderPlacement}
      data-mode={props.renderMode}
    />
  ),
}));
vi.mock("@/lib/brand-runtime", () => ({
  getBrandSnapshot: () => BUNDLE,
  subscribeBrandSnapshot: () => () => undefined,
}));
vi.mock("@/lib/use-theme-config", () => ({
  refreshEffectiveTheme: vi.fn(async () => true),
}));
vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({ push: vi.fn(), back: vi.fn() }),
}));
vi.mock("@/lib/api", () => ({
  patchChartAccessibility: vi.fn(async () => undefined),
}));
vi.mock("@/lib/team-chart-styles-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/team-chart-styles-api")>();
  return {
    ...actual,
    getChartStyleOptionsV2: vi.fn(),
    setChartStyleSelectionV2: vi.fn(async () => undefined),
    listTeamChartStyles: vi.fn(),
    getTeamChartStylePolicyV2: vi.fn(),
    setTeamChartStylePolicyV2: vi.fn(),
    getTeamChartStyle: vi.fn(),
    activateTeamChartStyle: vi.fn(),
    deactivateTeamChartStyle: vi.fn(),
  };
});

describe("chart style settings V2", () => {
  beforeEach(() => {
    vi.mocked(refreshEffectiveTheme).mockResolvedValue(true);
    vi.mocked(getChartStyleOptionsV2).mockResolvedValue({
      schemaVersion: 2,
      items: options,
      teamPolicy: effectiveChartStyle.teamPolicy as never,
      registryVersion: 0,
      builtInRegistryChecksum: effectiveChartStyle.builtInRegistryChecksum,
    });
    vi.mocked(listTeamChartStyles).mockResolvedValue({ items: [], nextCursor: null });
    vi.mocked(getTeamChartStylePolicyV2).mockResolvedValue({
      policy: {
        version: 2,
        defaultStyleId: "bowhead-editorial",
        enabledStyleIds: ["bowhead-editorial", "clean-blue", "analytical-neutral"],
        fallbackBuiltInPresetId: "bowhead-editorial",
      },
      etag: effectiveChartStyle.teamPolicy.etag,
      registryVersion: 0,
      builtInRegistryChecksum: effectiveChartStyle.builtInRegistryChecksum,
    });
    vi.mocked(setTeamChartStylePolicyV2).mockImplementation(async (policy) => ({
      policy,
      etag: `"chart-style-policy-v2:${"c".repeat(64)}"`,
      registryVersion: 1,
      builtInRegistryChecksum: effectiveChartStyle.builtInRegistryChecksum,
    }));
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("keeps the saved preference and retries only the failed appearance read", async () => {
    vi.mocked(refreshEffectiveTheme).mockRejectedValueOnce(new Error("refresh failed"));
    renderIntl(<PersonalChartStyleSettings />);
    const option = await screen.findByRole("radio", { name: /专业中性/ });
    fireEvent.click(option);
    expect(await screen.findByText("偏好已保存，但页面外观未能刷新。")).toBeTruthy();
    expect(option.getAttribute("aria-checked")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "刷新状态" }));
    await waitFor(() => expect(refreshEffectiveTheme).toHaveBeenCalledTimes(2));
    expect(setChartStyleSelectionV2).toHaveBeenCalledOnce();
  });

  it("saves member style and accessibility independently", async () => {
    renderIntl(<PersonalChartStyleSettings />);

    fireEvent.click(await screen.findByRole("radio", { name: /专业中性/ }));
    fireEvent.click(screen.getByRole("switch", { name: "增强对比度" }));

    await waitFor(() => {
      expect(setChartStyleSelectionV2).toHaveBeenCalledWith("analytical-neutral");
      expect(patchChartAccessibility).toHaveBeenCalledWith({ contrast: "high" });
    });
    expect(screen.getByTestId("real-chart-preview")).toBeTruthy();
  });

  it("style cards: arrow keys and Home/End only move focus; choosing a card saves once", async () => {
    // Selection saves immediately ⇒ arrows must not save (design: radio selection that immediately submits must not submit on arrow navigation),
    // or traversing from first to last PUTs every card passed along the way.
    renderIntl(<PersonalChartStyleSettings />);
    const group = await screen.findByRole("radiogroup", { name: "图表风格" });
    const cards = within(group).getAllByRole("radio");
    const checked = cards.findIndex((card) => card.getAttribute("aria-checked") === "true");
    expect(checked).toBeGreaterThanOrEqual(0);
    expect(cards.filter((card) => card.tabIndex === 0)).toEqual([cards[checked]]);

    cards[checked].focus();
    fireEvent.keyDown(cards[checked], { key: "End" });
    const last = cards[cards.length - 1];
    expect(document.activeElement).toBe(last);
    fireEvent.keyDown(last, { key: "ArrowUp" });
    expect(document.activeElement).toBe(cards[cards.length - 2]);
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Home" });
    expect(document.activeElement).toBe(cards[0]);
    expect(cards[checked].getAttribute("aria-checked")).toBe("true");
    expect(setChartStyleSelectionV2).not.toHaveBeenCalled();

    // Space / Enter on a button are equivalent to clicking.
    fireEvent.click(last);
    await waitFor(() => expect(vi.mocked(setChartStyleSelectionV2).mock.calls).toEqual([["analytical-neutral"]]));
  });

  it("resets member chart choices without touching general appearance", async () => {
    renderIntl(<PersonalChartStyleSettings />);
    fireEvent.click(await screen.findByRole("button", { name: "恢复图表默认" }));

    await waitFor(() => {
      expect(setChartStyleSelectionV2).toHaveBeenCalledWith(null);
      expect(patchChartAccessibility).toHaveBeenCalledWith({
        contrast: null,
        colorVision: null,
      });
    });
  });

  it("uses the production chart renderer for all preview contexts", async () => {
    renderIntl(<PersonalChartStyleSettings />);
    const preview = await screen.findByTestId("real-chart-preview");
    expect(preview.dataset.kind).toBe("line");
    expect(preview.dataset.surface).toBe("canvas");
    expect(preview.dataset.placement).toBe("settings-preview");

    fireEvent.click(screen.getByRole("radio", { name: "构成" }));
    fireEvent.click(screen.getByRole("radio", { name: "深色" }));
    fireEvent.click(screen.getByRole("radio", { name: "聊天" }));
    expect(screen.getByTestId("real-chart-preview").dataset.kind).toBe("pie");
    expect(screen.getByTestId("real-chart-preview").dataset.surface).toBe("chat");
    expect(screen.getByTestId("real-chart-preview").dataset.mode).toBe("dark");
  });

  it("saves an explicit V2 team default and member choice policy", async () => {
    renderIntl(<TeamChartStyleSettings />);
    const checkboxes = await screen.findAllByRole("checkbox", { name: "允许成员选择" });
    fireEvent.click(checkboxes[2]);
    fireEvent.click(screen.getByRole("button", { name: "保存选择范围" }));

    await waitFor(() => expect(setTeamChartStylePolicyV2).toHaveBeenCalledWith(
      {
        version: 2,
        defaultStyleId: "bowhead-editorial",
        enabledStyleIds: ["bowhead-editorial", "clean-blue"],
        fallbackBuiltInPresetId: "bowhead-editorial",
      },
      effectiveChartStyle.teamPolicy.etag,
    ));
    expect(refreshEffectiveTheme).toHaveBeenCalled();
  });

  it.each([false, "reject"] as const)("keeps an acknowledged team policy when theme refresh returns %s", async (failure) => {
    renderIntl(<TeamChartStyleSettings />);
    const checkboxes = await screen.findAllByRole("checkbox", { name: "允许成员选择" });
    fireEvent.click(checkboxes[2]);
    if (failure === false) vi.mocked(refreshEffectiveTheme).mockResolvedValueOnce(false);
    else vi.mocked(refreshEffectiveTheme).mockRejectedValueOnce(new Error("theme refresh failed"));
    fireEvent.click(screen.getByRole("button", { name: "保存选择范围" }));
    await screen.findByText(messages.settings.appearance.chartStyleV2.errors.saved_refresh_failed);
    expect((checkboxes[2] as HTMLInputElement).checked).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: messages.settings.appearance.chartStyleV2.retryRefresh }));
    await waitFor(() => expect(screen.queryByText(messages.settings.appearance.chartStyleV2.errors.saved_refresh_failed)).toBeNull());
    expect(setTeamChartStylePolicyV2).toHaveBeenCalledTimes(1);
    expect(refreshEffectiveTheme).toHaveBeenCalledTimes(2);
  });

  describe("the bottom save bar saves the selection scope (page acceptance A, decision 17)", () => {

    it("没改动时没有保存按钮也没有保存栏；改了才出现，保存提交同一份策略", async () => {
      renderIntl(<TeamChartStyleSettings />);
      const checkboxes = await screen.findAllByRole("checkbox", { name: "允许成员选择" });
      expect(screen.queryByRole("button", { name: "保存选择范围" })).toBeNull();
      expect(screen.queryByRole("region", { name: "有未保存的更改" })).toBeNull();

      fireEvent.click(checkboxes[2]);
      const bar = screen.getByRole("region", { name: "有未保存的更改" });
      fireEvent.click(within(bar).getByRole("button", { name: "保存选择范围" }));

      await waitFor(() => expect(setTeamChartStylePolicyV2).toHaveBeenCalledWith(
        {
          version: 2,
          defaultStyleId: "bowhead-editorial",
          enabledStyleIds: ["bowhead-editorial", "clean-blue"],
          fallbackBuiltInPresetId: "bowhead-editorial",
        },
        effectiveChartStyle.teamPolicy.etag,
      ));
      await waitFor(() => expect(screen.queryByRole("region", { name: "有未保存的更改" })).toBeNull());
    });

    it("「放弃」恢复到上次保存的选择范围，不发请求", async () => {
      renderIntl(<TeamChartStyleSettings />);
      const checkboxes = await screen.findAllByRole("checkbox", { name: "允许成员选择" });
      fireEvent.click(checkboxes[2]);
      expect((checkboxes[2] as HTMLInputElement).checked).toBe(false);
      fireEvent.click(within(screen.getByRole("region", { name: "有未保存的更改" })).getByRole("button", { name: "放弃" }));
      expect((checkboxes[2] as HTMLInputElement).checked).toBe(true);
      expect(screen.queryByRole("region", { name: "有未保存的更改" })).toBeNull();
      expect(setTeamChartStylePolicyV2).not.toHaveBeenCalled();
    });

    it("启用 / 停用团队样式不丢未保存的选择范围，保存栏不误显示「保存中…」", async () => {
      const style = {
        id: "tcs_1", lifecycle: "inactive" as const, state: "inactive", name: "品牌蓝",
        activeRevisionId: "rev_1", fallbackBuiltInPresetId: "clean-blue" as const,
        headEtag: '"h1"', draftEtag: null, resourceEtag: '"r1"', updatedAt: "2026-09-20T10:00:00Z",
      };
      vi.mocked(listTeamChartStyles).mockResolvedValue({ items: [style as never], nextCursor: null });
      vi.mocked(getTeamChartStyle).mockResolvedValue({
        headEtag: '"h1"',
        activeRevision: null,
        capabilities: { activate: { allowed: true }, deactivate: { allowed: true } },
      } as never);
      let finishActivate: () => void = () => undefined;
      vi.mocked(activateTeamChartStyle).mockImplementation(() => new Promise((resolve) => {
        finishActivate = () => resolve({
          detail: {
            style: { lifecycle: "active" },
            state: "active",
            capabilities: { activate: { allowed: true }, deactivate: { allowed: true } },
          },
        } as never);
      }));
      renderIntl(<TeamChartStyleSettings />);
      const checkboxes = await screen.findAllByRole("checkbox", { name: "允许成员选择" });
      fireEvent.click(checkboxes[2]);
      expect(screen.getByRole("region", { name: "有未保存的更改" })).toBeTruthy();

      // Enabling underway: Save bar remains, button says “保存选择范围” rather than “保存中…”.
      vi.mocked(listTeamChartStyles).mockResolvedValue({ items: [{ ...style, lifecycle: "active" } as never], nextCursor: null });
      fireEvent.click(screen.getByRole("button", { name: "重新启用" }));
      const bar = screen.getByRole("region", { name: "有未保存的更改" });
      expect(within(bar).queryByRole("button", { name: "保存中…" })).toBeNull();
      expect(within(bar).getByRole("button", { name: "保存选择范围" })).toBeTruthy();
      finishActivate();

      // Refresh retains unsaved changes: third built-in style stays unchecked, Save bar remains, without returning to whole-page loading.
      await waitFor(() => expect(getTeamChartStylePolicyV2).toHaveBeenCalledTimes(2));
      await waitFor(() => expect(screen.getByRole("button", { name: "停用" })).toBeTruthy());
      const after = screen.getAllByRole("checkbox", { name: "允许成员选择" });
      expect((after[2] as HTMLInputElement).checked).toBe(false);
      expect(screen.getByRole("region", { name: "有未保存的更改" })).toBeTruthy();
      expect(setTeamChartStylePolicyV2).not.toHaveBeenCalled();
    });
  });
});
