import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api-client";
import { effectiveChartStyleV2Fixture } from "@/__tests__/chart-style-export-fixture";
import messages from "@/messages/zh-CN.json";
import {
  getTeamChartStyle,
  getTeamChartStylePolicyV2,
  listTeamChartStyleRevisions,
  previewTeamChartStyle,
  publishTeamChartStyle,
  saveTeamChartStyleDraft,
  setTeamChartStylePolicyV2,
  type ChartStylePreviewResult,
  type TeamChartStyleDetail,
  type TeamChartStyleSource,
} from "@/lib/team-chart-styles-api";
import { refreshEffectiveTheme } from "@/lib/use-theme-config";

import { ChartStyleEditor } from "./chart-style-editor";

const source: TeamChartStyleSource = {
  schemaVersion: 2,
  name: "经营复盘",
  description: "季度经营复盘使用",
  overrides: { schemaVersion: 2 },
};

function detail(nextSource = source): TeamChartStyleDetail {
  return {
    style: {
      schemaVersion: 2,
      id: "tcs_0123456789abcdef01234567",
      lifecycle: "active",
      activeRevisionId: null,
      fallbackBuiltInPresetId: "bowhead-editorial",
      rowVersion: 1,
      createdBy: "usr_1",
      createdAt: "2026-09-07T08:00:00Z",
      updatedBy: "usr_1",
      updatedAt: "2026-09-07T08:00:00Z",
    },
    state: "draft_only",
    activeRevision: null,
    draft: {
      source: structuredClone(nextSource),
      basedOnRevisionId: null,
      basePreset: {
        id: "bowhead-editorial",
        revision: "2026.09.1",
        checksum: `sha256:${"a".repeat(64)}`,
      },
      rowVersion: 1,
      updatedBy: "usr_1",
      updatedAt: "2026-09-07T08:00:00Z",
    },
    headEtag: '"chart-style-head:1"',
    draftEtag: '"chart-style-draft:1"',
    resourceEtag: '"chart-style-resource:1"',
    capabilities: Object.fromEntries([
      "createDraft",
      "saveDraft",
      "discardDraft",
      "publish",
      "restore",
      "activate",
      "deactivate",
      "deleteStyle",
    ].map((key) => [key, { allowed: true, reason: null }])) as TeamChartStyleDetail["capabilities"],
  };
}

function preview(
  nextSource = source,
  warning = false,
): ChartStylePreviewResult {
  const effective = effectiveChartStyleV2Fixture();
  return {
    normalizedAuthoringSource: structuredClone(nextSource),
    candidateSnapshot: effective.style.resolvedPresetSnapshot,
    candidateSnapshotChecksum: effective.style.snapshotChecksum,
    validationFingerprint: `sha256:${"f".repeat(64)}`,
    validation: {
      blockers: [],
      warnings: warning ? [{
        path: "overrides.categoricalPalette.light.1",
        severity: "warning",
        code: "palette.pairwise_delta",
        details: {},
      }] : [],
      info: [],
    },
    surfaceEvidence: {
      contractVersion: "chart-surface/1",
      surfaceChecksums: {
        light: `sha256:${"1".repeat(64)}`,
        dark: `sha256:${"2".repeat(64)}`,
      },
      modes: effective.modeColors,
    },
  };
}

// The mock resolves the theme so a test can see WHICH snapshot the preview is
// drawing from — the production defect was that a rejected candidate silently
// swapped in a different palette, which a `view`-only mock cannot detect.
vi.mock("./chart-style-preview", () => ({
  ChartStylePreview: ({
    view,
    themeForMode,
  }: {
    view: string;
    themeForMode: (mode: "light" | "dark") => { palette: string[] };
  }) => (
    <div
      data-testid="chart-style-preview"
      data-view={view}
      data-palette={themeForMode("light").palette.join(",").toUpperCase()}
    />
  ),
}));
/**
 * A palette that is distinguishable from the product default.
 *
 * ⚠ `effectiveChartStyleV2Fixture()` is built from `bowhead-editorial`, whose
 * palette is byte-identical to `PALETTE_LIGHT` in `lib/chart-theme.ts` — the
 * hardcoded fallback this batch removes. Asserting against the plain fixture
 * cannot tell "showing the candidate" from "fell back to the product default";
 * that indistinguishability is exactly what made the production defect so hard
 * to see. Every test about which snapshot is on screen must use these colours.
 */
const DISTINCT_PALETTE = ["#503291", "#00A3B2", "#E61E50", "#0F69AF", "#149B5F", "#EB3C96"];
const PRODUCT_DEFAULT_FIRST_COLOR = "#B04A2A";

/** A preview result whose palette is `DISTINCT_PALETTE`; `blocked` withholds the
 *  snapshot and surface evidence exactly the way the server does. */
function previewWithPalette(
  nextSource: TeamChartStyleSource,
  options: { blocked?: boolean } = {},
): ChartStylePreviewResult {
  const base = preview(nextSource);
  const snapshot = structuredClone(base.candidateSnapshot!);
  snapshot.colors.light.categorical = [...DISTINCT_PALETTE];
  snapshot.colors.dark.categorical = [...DISTINCT_PALETTE];
  const evidence = structuredClone(base.surfaceEvidence!);
  for (const mode of ["light", "dark"] as const) {
    evidence.modes[mode].colors.categorical = [...DISTINCT_PALETTE];
    evidence.modes[mode].categoricalSeriesPalette = [...DISTINCT_PALETTE];
  }
  if (options.blocked) {
    return {
      ...base,
      candidateSnapshot: null,
      candidateSnapshotChecksum: null,
      surfaceEvidence: null,
      validation: {
        ...base.validation,
        blockers: [{
          path: "colors.light.categorical.1",
          severity: "blocker",
          code: "palette.surface_contrast",
          details: { minimum: 3, actual: 2.24 },
        }],
      },
    };
  }
  return { ...base, candidateSnapshot: snapshot, surfaceEvidence: evidence };
}

const routerPush = vi.hoisted(() => vi.fn());
vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({ push: routerPush }),
}));
vi.mock("@/lib/use-theme-config", () => ({
  refreshEffectiveTheme: vi.fn(async () => true),
}));
vi.mock("@/lib/team-chart-styles-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/team-chart-styles-api")>();
  return {
    ...actual,
    getTeamChartStyle: vi.fn(),
    listTeamChartStyleRevisions: vi.fn(),
    previewTeamChartStyle: vi.fn(),
    publishTeamChartStyle: vi.fn(),
    saveTeamChartStyleDraft: vi.fn(),
    getTeamChartStylePolicyV2: vi.fn(),
    setTeamChartStylePolicyV2: vi.fn(),
  };
});

function renderEditor(styleId = "tcs_0123456789abcdef01234567") {
  return render(
    <NextIntlClientProvider locale="zh-CN" messages={messages}>
      <ChartStyleEditor styleId={styleId} />
    </NextIntlClientProvider>,
  );
}

describe("ChartStyleEditor", () => {
  beforeEach(() => {
    vi.mocked(refreshEffectiveTheme).mockResolvedValue(true);
    vi.mocked(getTeamChartStyle).mockResolvedValue(detail());
    vi.mocked(listTeamChartStyleRevisions).mockResolvedValue({ items: [], nextCursor: null });
    vi.mocked(previewTeamChartStyle).mockImplementation(async (_styleId, candidate) => preview(candidate));
    vi.mocked(saveTeamChartStyleDraft).mockImplementation(async (_styleId, _etag, candidate) => ({
      detail: detail(candidate),
      registryVersion: 0,
      builtInRegistryChecksum: effectiveChartStyleV2Fixture().builtInRegistryChecksum,
    }));
    vi.mocked(publishTeamChartStyle).mockResolvedValue({
      detail: detail(),
      registryVersion: 1,
      builtInRegistryChecksum: effectiveChartStyleV2Fixture().builtInRegistryChecksum,
    });
    vi.mocked(getTeamChartStylePolicyV2).mockResolvedValue({
      policy: {
        version: 2,
        defaultStyleId: "bowhead-editorial",
        enabledStyleIds: ["bowhead-editorial", "clean-blue"],
        fallbackBuiltInPresetId: "bowhead-editorial",
      },
      etag: '"chart-style-policy:1"',
      registryVersion: 0,
      builtInRegistryChecksum: effectiveChartStyleV2Fixture().builtInRegistryChecksum,
    });
    vi.mocked(setTeamChartStylePolicyV2).mockImplementation(async (policy) => ({
      policy,
      etag: '"chart-style-policy:2"',
      registryVersion: 2,
      builtInRegistryChecksum: effectiveChartStyleV2Fixture().builtInRegistryChecksum,
    }));
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("without ?styleId= asks the admin to pick a style instead of loading the list as a style", async () => {
    renderEditor("");

    await screen.findByText(messages.settings.appearance.chartStyleV2.unavailable.missingStyle);
    expect(getTeamChartStyle).not.toHaveBeenCalled();
    expect(listTeamChartStyleRevisions).not.toHaveBeenCalled();
    expect(screen.queryByText(messages.settings.appearance.chartStyleV2.errors.load)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "返回图表样式" }));
    expect(routerPush).toHaveBeenCalledWith("/team/settings/brand-appearance/");
  });

  it("a deleted style says it is gone and offers the way back, not a retry that cannot succeed", async () => {
    vi.mocked(getTeamChartStyle).mockRejectedValue(
      new ApiError(404, "not found", { code: "chart_style_not_found" }),
    );
    renderEditor();

    await screen.findByText(messages.settings.appearance.chartStyleV2.unavailable.notFound);
    expect(screen.queryByRole("button", { name: "重试" })).toBeNull();
    expect(screen.getByRole("button", { name: "返回图表样式" })).toBeTruthy();
  });

  it("a transient failure still offers retry", async () => {
    vi.mocked(getTeamChartStyle).mockRejectedValueOnce(new ApiError(503, "boom"));
    renderEditor();

    await screen.findByText(messages.settings.appearance.chartStyleV2.errors.load);
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await screen.findByRole("heading", { name: "经营复盘" });
  });

  it("debounces authoritative preview and saves the draft with its strong ETag", async () => {
    renderEditor();

    await screen.findByRole("heading", { name: "经营复盘" });
    await waitFor(() => expect(previewTeamChartStyle).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("radio", { name: "直线" }));

    await waitFor(() => expect(previewTeamChartStyle).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));

    await waitFor(() => expect(saveTeamChartStyleDraft).toHaveBeenCalledWith(
      "tcs_0123456789abcdef01234567",
      '"chart-style-draft:1"',
      expect.objectContaining({
        overrides: expect.objectContaining({ line: { curve: "straight" } }),
      }),
    ));
  });

  it("requires an explicit acknowledgement before publishing warnings", async () => {
    vi.mocked(previewTeamChartStyle).mockImplementation(async (_styleId, candidate) => preview(candidate, true));
    renderEditor();

    await waitFor(() => expect(previewTeamChartStyle).toHaveBeenCalledTimes(1));
    const publish = screen.getByRole("button", { name: "保存并应用" }) as HTMLButtonElement;
    expect(publish.disabled).toBe(true);

    fireEvent.click(screen.getByRole("checkbox", { name: /我已查看这些可辨识度提醒/ }));
    expect(publish.disabled).toBe(false);
    fireEvent.click(publish);

    await waitFor(() => expect(publishTeamChartStyle).toHaveBeenCalledWith(
      "tcs_0123456789abcdef01234567",
      '"chart-style-draft:1"',
      `sha256:${"f".repeat(64)}`,
    ));
    expect(refreshEffectiveTheme).toHaveBeenCalled();

    fireEvent.click(await screen.findByRole("button", { name: "设为团队默认" }));
    await waitFor(() => expect(setTeamChartStylePolicyV2).toHaveBeenCalledWith({
      version: 2,
      defaultStyleId: "tcs_0123456789abcdef01234567",
      enabledStyleIds: [
        "bowhead-editorial",
        "clean-blue",
        "tcs_0123456789abcdef01234567",
      ],
      fallbackBuiltInPresetId: "bowhead-editorial",
    }, '"chart-style-policy:1"'));
  });

  it("shows a color-distinctness note without requiring an acknowledgement", async () => {
    vi.mocked(previewTeamChartStyle).mockImplementation(async (_styleId, candidate) => {
      const result = preview(candidate);
      result.validation.info = [{
        path: "colors.dark.categorical",
        severity: "info",
        code: "palette.distinctness",
        details: { firstIndex: 0, secondIndex: 1, normal: 4.2, cvd: 2.3, normalMinimum: 10, cvdMinimum: 7 },
      }];
      return result;
    });
    renderEditor();

    await screen.findByText(messages.settings.appearance.chartStyleV2.validation.distinct);
    expect(screen.queryByRole("checkbox", { name: /我已查看这些可辨识度提醒/ })).toBeNull();
    expect((screen.getByRole("button", { name: "保存并应用" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it.each(["history", "theme-rejection", "theme-unavailable"])(
    "keeps a confirmed publication when the subsequent %s refresh fails and retries only reads",
    async (failure) => {
      renderEditor();
      await waitFor(() => expect(previewTeamChartStyle).toHaveBeenCalledTimes(1));
      if (failure === "history") {
        vi.mocked(listTeamChartStyleRevisions).mockRejectedValueOnce(new Error("history unavailable"));
      } else if (failure === "theme-rejection") {
        vi.mocked(refreshEffectiveTheme).mockRejectedValueOnce(new Error("theme unavailable"));
      } else {
        vi.mocked(refreshEffectiveTheme).mockResolvedValueOnce(false);
      }
      fireEvent.click(screen.getByRole("button", { name: "保存并应用" }));
      await screen.findByText(messages.settings.appearance.chartStyleV2.errors.saved_refresh_failed);
      expect(screen.queryByText(messages.settings.appearance.chartStyleV2.errors.publish)).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: messages.settings.appearance.chartStyleV2.retryRefresh }));
      await waitFor(() => expect(screen.queryByText(
        messages.settings.appearance.chartStyleV2.errors.saved_refresh_failed,
      )).toBeNull());
      expect(publishTeamChartStyle).toHaveBeenCalledTimes(1);
      expect(refreshEffectiveTheme).toHaveBeenCalledTimes(2);
    },
  );

  it("keeps the acknowledgement for the same warning candidate after saving the draft", async () => {
    vi.mocked(previewTeamChartStyle).mockImplementation(async (_styleId, candidate) => preview(candidate, true));
    renderEditor();

    await waitFor(() => expect(previewTeamChartStyle).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("radio", { name: "直线" }));
    await waitFor(() => expect(previewTeamChartStyle).toHaveBeenCalledTimes(2));

    fireEvent.click(screen.getByRole("checkbox", { name: /我已查看这些可辨识度提醒/ }));
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));

    await waitFor(() => expect(saveTeamChartStyleDraft).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(previewTeamChartStyle).toHaveBeenCalledTimes(3));
    expect((screen.getByRole("checkbox", {
      name: /我已查看这些可辨识度提醒/,
    }) as HTMLInputElement).checked).toBe(true);
    expect((screen.getByRole("button", { name: "保存并应用" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("blocks draft saves and preview requests until an invalid HEX color is corrected", async () => {
    renderEditor();

    const color = await screen.findByRole("textbox", { name: "第 1 个颜色的 HEX 值" });
    await waitFor(() => expect(previewTeamChartStyle).toHaveBeenCalledTimes(1));

    fireEvent.change(color, { target: { value: "#GGGGGG" } });

    const invalidColor = screen.getByRole("textbox", { name: "第 1 个颜色的 HEX 值" });
    expect(invalidColor.getAttribute("aria-invalid")).toBe("true");
    expect(screen.getByRole("alert").textContent).toContain("请输入 6 位 HEX 颜色");
    expect((screen.getByRole("button", { name: "保存草稿" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "返回图表样式" }));
    expect((screen.getByRole("button", { name: "保存草稿并离开" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "继续编辑" }));
    await new Promise((resolve) => window.setTimeout(resolve, 250));
    expect(previewTeamChartStyle).toHaveBeenCalledTimes(1);

    fireEvent.change(invalidColor, { target: { value: "#2563EB" } });

    await waitFor(() => expect(previewTeamChartStyle).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("textbox", { name: "第 1 个颜色的 HEX 值" }).getAttribute("aria-invalid")).toBe("false");
    expect(screen.queryByText(/请输入 6 位 HEX 颜色/)).toBeNull();
    expect((screen.getByRole("button", { name: "保存草稿" }) as HTMLButtonElement).disabled).toBe(false);
  });

  // docs/sprints/brand-appearance-repair-2026-09-09/spike.md §3 — a rejected
  // candidate used to leave the preview drawing a hardcoded product palette that
  // belongs to neither the draft nor the team's published style, with no notice.
  describe("候选被发布检查拒绝时", () => {
    async function renderWithValidThenBlocked() {
      vi.mocked(previewTeamChartStyle).mockImplementation(async (_id, candidate) =>
        previewWithPalette(candidate));
      renderEditor();
      const node = await screen.findByTestId("chart-style-preview");
      await waitFor(() => expect(node.getAttribute("data-view")).toBe("candidate"));

      vi.mocked(previewTeamChartStyle).mockImplementation(async (_id, candidate) =>
        previewWithPalette(candidate, { blocked: true }));
      fireEvent.change(screen.getByRole("textbox", { name: "名称" }), {
        target: { value: "改名触发重新校验" },
      });
      return node;
    }

    it("保留上一版通过校验的候选，并说明正在显示旧版本", async () => {
      const node = await renderWithValidThenBlocked();

      await waitFor(() => expect(node.getAttribute("data-view")).toBe("lastValid"));
      // Positive control: before the fix, this used the product default palette instead of the candidate's own colors.
      expect(node.getAttribute("data-palette")).toContain("#503291");
      expect(node.getAttribute("data-palette")).not.toContain(PRODUCT_DEFAULT_FIRST_COLOR);
      // Verify only state passed from editor to preview here; copy for each of the four states is checked by
      // chart-style-preview.test.tsx (which renders the real component).
    });

    it("重新校验在途期间不闪回产品默认色板", async () => {
      const node = await renderWithValidThenBlocked();

      // Check immediately after renaming: previewing=true, but display must retain the previous candidate revision's colors.
      expect(node.getAttribute("data-view")).toBe("loading");
      expect(node.getAttribute("data-palette")).toContain("#503291");
      expect(node.getAttribute("data-palette")).not.toContain(PRODUCT_DEFAULT_FIRST_COLOR);
    });

    it("从未有过通过校验的候选时进入阻断态，不画图", async () => {
      vi.mocked(previewTeamChartStyle).mockImplementation(async (_id, candidate) =>
        previewWithPalette(candidate, { blocked: true }));
      renderEditor();

      const node = await screen.findByTestId("chart-style-preview");
      await waitFor(() => expect(node.getAttribute("data-view")).toBe("blocked"));
    });

    it("置灰的「保存并应用」旁边说明原因，并把阻断项的定位和度量渲染出来", async () => {
      vi.mocked(previewTeamChartStyle).mockImplementation(async (_id, candidate) =>
        previewWithPalette(candidate, { blocked: true }));
      renderEditor();
      await screen.findByTestId("chart-style-preview");

      await waitFor(() => expect(screen.getByText("先修复 1 项阻断问题")).toBeTruthy());
      expect((screen.getByRole("button", { name: /保存并应用/ }) as HTMLButtonElement).disabled).toBe(true);
      // Before fixing, this row showed only code/path, without identifying the color or shortfall.
      expect(screen.getByText("浅色模式 · 第 2 个颜色")).toBeTruthy();
      expect(screen.getByText(/对比度 实际 2\.24:1 · 需要 ≥ 3\.00:1/)).toBeTruthy();
    });

    it("深色色卡说明取不到值，并禁用「添加颜色」而不是造出一个 1 色色板", async () => {
      vi.mocked(previewTeamChartStyle).mockImplementation(async (_id, candidate) =>
        previewWithPalette(candidate, { blocked: true }));
      renderEditor();
      await screen.findByTestId("chart-style-preview");

      fireEvent.click(screen.getByRole("radio", { name: "深色模式" }));

      await waitFor(() =>
        expect(screen.getByText(/这一档色卡暂时取不到值/)).toBeTruthy());
      expect(screen.queryByText(/编辑任一颜色即可建立深色覆盖/)).toBeNull();
      expect((screen.getByRole("button", { name: /添加颜色/ }) as HTMLButtonElement).disabled).toBe(true);
    });
  });
});
