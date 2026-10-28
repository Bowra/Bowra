import {
  cleanup,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { COLOR_SCHEME_TOKEN_KEYS, type ColorSchemeMode } from "@/lib/theme-config";
import type {
  PersonalAppearanceResource,
  PersonalAppearanceSnapshot,
} from "@/lib/personal-appearance";

const state = vi.hoisted(() => ({
  load: vi.fn(async () => null),
  patch: vi.fn(async () => undefined),
  compile: vi.fn(),
  reset: vi.fn(async () => undefined),
  status: "saved",
  pendingFields: new Set<string>(),
  failedFields: new Set<string>(),
  useOverrides: false,
}));
const auth = vi.hoisted(() => ({
  fetchMe: vi.fn(async () => ({
    team_role: "member",
    is_team_admin: false,
  })),
}));

vi.mock("@/components/settings/personal-appearance-provider", () => ({
  usePersonalAppearance: () => ({
    snapshot: snapshot(state.useOverrides),
    status: state.status,
    errorCode: null,
    pendingFields: state.pendingFields,
    failedFields: state.failedFields,
    load: state.load,
    patch: state.patch,
    compile: state.compile,
    reset: state.reset,
    undo: vi.fn(),
  }),
}));
vi.mock("@/components/settings/chart-style-settings", () => ({
  PersonalChartStyleSettings: () => <div data-testid="chart-style-settings" />,
}));
vi.mock("@/lib/auth-api", () => ({ fetchMe: auth.fetchMe }));

import { PersonalAppearanceSettings } from "./personal-appearance-settings";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  state.status = "saved";
  state.pendingFields = new Set();
  state.failedFields = new Set();
  state.useOverrides = false;
  auth.fetchMe.mockResolvedValue({
    team_role: "member",
    is_team_admin: false,
  });
});

describe("PersonalAppearanceSettings", () => {
  it("applies an overall SaaS palette as one atomic appearance change", async () => {
    renderIntl(<PersonalAppearanceSettings />);

    const palette = screen.getByTestId("personal-appearance-palettes");
    expect(within(palette).getByRole("heading", { name: "整体色调" })).toBeTruthy();
    expect(
      within(palette).getByRole("link", { name: "下载模板" }).getAttribute(
        "download",
      ),
    ).toBe("bowhead-palette.json");

    fireEvent.click(
      within(palette).getByRole("button", { name: /专业蓝/ }),
    );

    await waitFor(() =>
      expect(state.patch).toHaveBeenCalledWith({
        accent: { kind: "custom", seed: "#2563EB" },
        background: {
          kind: "custom",
          light: "#F8FAFC",
          dark: "#0F172A",
        },
      }),
    );
  });

  it("imports a palette as an editable validated draft before applying it", async () => {
    const current = snapshot();
    state.compile.mockImplementation(
      async (
        _candidate: unknown,
        candidateId: string,
      ) => ({
        candidateId,
        baseEtag: current.etag,
        colorScheme: current.resource.effective.colorScheme,
        validation: { blocking: [], warnings: [] },
      }),
    );
    renderIntl(<PersonalAppearanceSettings />);
    const fileContents = JSON.stringify({
      version: 1,
      name: "Imported indigo",
      accent: "#635BFF",
      background: { light: "#F6F9FC", dark: "#101323" },
    });
    const file = new File(
      [fileContents],
      "palette.json",
      { type: "application/json" },
    );
    Object.defineProperty(file, "text", {
      value: async () => fileContents,
    });

    fireEvent.change(document.querySelector('input[type="file"]')!, {
      target: { files: [file] },
    });

    expect(await screen.findByText("Imported indigo")).toBeTruthy();
    await waitFor(() => expect(screen.getByText("配色可用")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "应用配色" }));
    await waitFor(() =>
      expect(state.patch).toHaveBeenCalledWith({
        accent: { kind: "custom", seed: "#635BFF" },
        background: {
          kind: "custom",
          light: "#F6F9FC",
          dark: "#101323",
        },
      }),
    );
  });

  it("presents independent accent and background choices without language", async () => {
    renderIntl(<PersonalAppearanceSettings />);

    expect(screen.getByRole("heading", { name: "外观与主题" })).toBeTruthy();
    expect(screen.getByText("当前效果")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "强调色" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "页面底色" })).toBeTruthy();
    expect(screen.queryByText("界面语言")).toBeNull();
    expect(screen.getByTestId("chart-style-settings")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "跟随团队" })).toBeNull();
    expect(screen.getByText("当前：团队默认")).toBeTruthy();
    expect(screen.queryByText(/Revision/)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /纯净白/ }));

    await waitFor(() =>
      expect(state.patch).toHaveBeenCalledWith({
        background: {
          kind: "preset",
          registryVersion: 1,
          id: "pure-white",
        },
      }),
    );
  });

  it("links team administrators to the team defaults", async () => {
    auth.fetchMe.mockResolvedValueOnce({
      team_role: "team_admin",
      is_team_admin: true,
    });
    renderIntl(<PersonalAppearanceSettings />);

    const link = await screen.findByRole("link", {
      name: "查看团队默认",
    });
    expect(link.getAttribute("href")).toBe(
      "/team/settings/brand-appearance/",
    );
  });

  it("does not expose the team settings link to members", async () => {
    renderIntl(<PersonalAppearanceSettings />);

    await waitFor(() => expect(auth.fetchMe).toHaveBeenCalled());
    expect(
      screen.queryByRole("link", { name: "查看团队默认" }),
    ).toBeNull();
  });

  it("does not save when the selected appearance choices are clicked again", () => {
    state.useOverrides = true;
    renderIntl(<PersonalAppearanceSettings />);

    fireEvent.click(
      within(screen.getByTestId("personal-appearance-mode")).getByRole(
        "radio",
        { checked: true },
      ),
    );
    fireEvent.click(
      within(screen.getByTestId("personal-appearance-accent")).getByRole(
        "button",
        { pressed: true },
      ),
    );
    fireEvent.click(
      within(screen.getByTestId("personal-appearance-background")).getByRole(
        "button",
        { pressed: true },
      ),
    );

    expect(state.patch).not.toHaveBeenCalled();
  });

  it("arrow keys only move focus between appearance choices; each group is named for screen readers", () => {
    // Selection saves immediately (PATCH + full-page reflow): if arrows selected directly, moving from Follow System to Dark would first save Light on the server.
    renderIntl(<PersonalAppearanceSettings />);
    for (const testId of ["mode", "typography", "type-scale", "density", "radius", "motion"]) {
      const row = screen.queryByTestId(`personal-appearance-${testId}`);
      if (!row) continue;
      const group = within(row).getByRole("radiogroup");
      expect(group.getAttribute("aria-label"), testId).toBeTruthy();
    }
    const mode = screen.getByTestId("personal-appearance-mode");
    const checked = within(mode).getByRole("radio", { checked: true });
    checked.focus();
    fireEvent.keyDown(checked, { key: "ArrowRight" });
    expect(document.activeElement).not.toBe(checked);
    expect(state.patch).not.toHaveBeenCalled();
    fireEvent.click(document.activeElement as HTMLElement);
    expect(state.patch).toHaveBeenCalledTimes(1);
  });

  it("keeps custom background controls collapsed until requested", () => {
    renderIntl(<PersonalAppearanceSettings />);

    const toggle = screen.getByRole("button", {
      name: /自定义页面底色/,
    });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(
      screen.queryByRole("button", { name: /浅色页面底色/ }),
    ).toBeNull();

    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(
      screen.getByRole("button", { name: /浅色页面底色/ }),
    ).toBeTruthy();
  });

  it("shows field-level saving without disabling unrelated controls", () => {
    state.status = "saving";
    state.pendingFields = new Set(["mode"]);
    renderIntl(<PersonalAppearanceSettings />);

    const mode = screen.getByTestId("personal-appearance-mode");
    const background = screen.getByTestId("personal-appearance-background");
    expect(within(mode).getByText("保存中")).toBeTruthy();
    expect(
      within(mode).getByRole("radio", { name: /跟随系统/ }).hasAttribute(
        "disabled",
      ),
    ).toBe(false);
    expect(
      within(background)
        .getByRole("button", { name: /纯净白/ })
        .hasAttribute("disabled"),
    ).toBe(false);
  });

  it("places the theme preview after the quick appearance controls", () => {
    renderIntl(<PersonalAppearanceSettings />);

    const background = screen.getByTestId("personal-appearance-background");
    const preview = screen.getByText("当前效果").closest("section");
    expect(preview).not.toBeNull();
    expect(
      background.compareDocumentPosition(preview as HTMLElement) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("confirms a full appearance reset and excludes chart preferences", async () => {
    renderIntl(<PersonalAppearanceSettings />);

    fireEvent.click(
      screen.getByRole("button", { name: "清除个人外观" }),
    );

    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(screen.getByText(/图表设置不会被清除/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "确认清除" }));
    await waitFor(() => expect(state.reset).toHaveBeenCalledOnce());
  });
});

function snapshot(useOverrides = false): PersonalAppearanceSnapshot {
  const light = tokens(0);
  const dark = tokens(40);
  light.background = "#FAF9F6";
  light.surface = "#FFFFFF";
  light.ink = "#1C1917";
  light.ink2 = "#57534E";
  light.accent = "#2F6F84";
  light.accentInk = "#FFFFFF";
  light.line = "#E7E2DA";
  light.focus = "#2F6F84";
  dark.background = "#131210";
  dark.surface = "#1D1B18";
  dark.ink = "#F5F2EB";
  dark.ink2 = "#C6C0B6";
  dark.accent = "#73B6C9";
  dark.accentInk = "#102126";
  dark.line = "#3A3631";
  dark.focus = "#73B6C9";
  const resource: PersonalAppearanceResource = {
    schemaVersion: 3,
    materialized: false,
    rowVersion: 0,
    overrides: useOverrides
      ? {
          accent: { kind: "preset", registryVersion: 1, id: "ocean" },
          background: {
            kind: "preset",
            registryVersion: 1,
            id: "pure-white",
          },
        }
      : {},
    effective: {
      mode: "system",
      typographyProfile: { type: "preset", id: "bowhead" },
      typeScale: "standard",
      density: "comfortable",
      radius: "balanced",
      motion: "standard",
      colorScheme: {
        tokenSchemaVersion: 2,
        light,
        dark,
        checksum: `sha256:${"b".repeat(64)}`,
      },
    },
    sources: {
      mode: "product",
      accent: useOverrides ? "user" : "team_default",
      background: useOverrides ? "user" : "product",
      typographyProfile: "product",
      typeScale: "product",
      density: "product",
      radius: "product",
      motion: "product",
    },
    team: { revisionId: null, revisionNumber: 0 },
    registries: {
      palette: {
        version: 1,
        items: [
          {
            id: "saas-blue",
            labelKey: "appearance.palette.saasBlue",
            value: {
              accent: { kind: "custom", seed: "#2563EB" },
              background: {
                kind: "custom",
                light: "#F8FAFC",
                dark: "#0F172A",
              },
            },
            swatch: {
              light: {
                primary: "#2563EB",
                secondary: "#F8FAFC",
                onPrimary: "#FFFFFF",
              },
              dark: {
                primary: "#427FFF",
                secondary: "#0F172A",
                onPrimary: "#07142D",
              },
            },
            availability: "available",
          },
        ],
      },
      accent: {
        version: 1,
        items: [
          {
            id: "ocean",
            labelKey: "appearance.accent.ocean",
            value: { kind: "preset", registryVersion: 1, id: "ocean" },
            swatch: {
              light: { primary: "#2F6F84", onPrimary: "#FFFFFF" },
              dark: { primary: "#73B6C9", onPrimary: "#102126" },
            },
            availability: "available",
          },
        ],
      },
      background: {
        version: 1,
        items: [
          {
            id: "pure-white",
            labelKey: "appearance.background.pureWhite",
            value: {
              kind: "preset",
              registryVersion: 1,
              id: "pure-white",
            },
            swatch: {
              light: {
                primary: "#FFFFFF",
                secondary: "#F8F8F8",
                onPrimary: "#222222",
              },
              dark: {
                primary: "#121212",
                secondary: "#1A1A1A",
                onPrimary: "#EEEEEE",
              },
            },
            availability: "available",
          },
        ],
      },
    },
    degradations: [],
    pendingUndo: null,
  };
  return { resource, etag: `"personal-v3:${"a".repeat(64)}"` };
}

function tokens(offset: number): ColorSchemeMode {
  return Object.fromEntries(
    COLOR_SCHEME_TOKEN_KEYS.map((key, index) => [
      key,
      `#${(index + offset + 1).toString(16).padStart(6, "0").toUpperCase()}`,
    ]),
  ) as ColorSchemeMode;
}
