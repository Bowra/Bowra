import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { ApiError } from "@/lib/api-client";
import type { PersonalAppearanceCompileResult } from "@/lib/personal-appearance";
import { COLOR_SCHEME_TOKEN_KEYS, type ColorSchemeMode } from "@/lib/theme-config";

import { PersonalColorPicker } from "./personal-color-picker";

const ETAG = `"personal-v3:${"a".repeat(64)}"`;

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("PersonalColorPicker", () => {
  it("accepts visual or Hex input but applies only the latest server-compiled color", async () => {
    const compile = vi.fn(
      async (_candidate, candidateId: string): Promise<PersonalAppearanceCompileResult> =>
        compileResult(candidateId),
    );
    const onApply = vi.fn(async () => undefined);

    renderIntl(
      <PersonalColorPicker
        label="自定义强调色"
        value="#2F6F84"
        mode="light"
        target="accent"
        etag={ETAG}
        recommendations={["#4B6F44", "#7A527C"]}
        buildCandidate={(hex) => ({ accent: { kind: "custom", seed: hex } })}
        compile={compile}
        onApply={onApply}
      />,
    );

    const trigger = screen.getByRole("button", { name: /自定义强调色/ });
    fireEvent.click(trigger);
    expect(screen.getByText("原值")).toBeTruthy();
    expect(screen.getByText("新值")).toBeTruthy();
    expect(compile).not.toHaveBeenCalled();
    expect(screen.getByText("当前颜色")).toBeTruthy();
    expect(screen.getByRole("button", { name: "应用颜色" })).toBeDisabled();

    fireEvent.change(screen.getByRole("textbox", { name: "Hex" }), {
      target: { value: "ffffff" },
    });

    await waitFor(() =>
      expect(compile).toHaveBeenCalledWith(
        { accent: { kind: "custom", seed: "#FFFFFF" } },
        expect.stringMatching(/^picker-/),
        expect.any(AbortSignal),
      ),
    );
    const apply = await screen.findByRole("button", { name: "应用颜色" });
    await waitFor(() => expect(apply).not.toBeDisabled());
    fireEvent.click(apply);
    await waitFor(() => expect(onApply).toHaveBeenCalledWith("#FFFFFF"));
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it("keeps incomplete Hex local and does not compile it", async () => {
    const compile = vi.fn(async (_candidate, candidateId: string) =>
      compileResult(candidateId),
    );

    renderIntl(
      <PersonalColorPicker
        label="Light 页面底色"
        value="#FAF9F6"
        mode="light"
        target="background"
        etag={ETAG}
        recommendations={[]}
        buildCandidate={(hex) => ({
          background: { kind: "custom", light: hex, dark: "#131210" },
        })}
        compile={compile}
        onApply={vi.fn(async () => undefined)}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /Light 页面底色/ }));
    await new Promise((resolve) => setTimeout(resolve, 240));
    expect(compile).not.toHaveBeenCalled();
    fireEvent.change(screen.getByRole("textbox", { name: "Hex" }), {
      target: { value: "#F" },
    });

    await new Promise((resolve) => setTimeout(resolve, 240));
    expect(compile).not.toHaveBeenCalled();
    expect(screen.getByText("等待完整 Hex")).toBeTruthy();
    expect(screen.getByRole("button", { name: "应用颜色" })).toBeDisabled();
  });

  it("distinguishes an incompatible team base from an unsafe color", async () => {
    const compile = vi.fn(async () => {
      throw new ApiError(409, "Conflict", {
        code: "personal_theme_base_incompatible",
      });
    });

    renderIntl(
      <PersonalColorPicker
        label="自定义强调色"
        value="#2F6F84"
        mode="light"
        target="accent"
        etag={ETAG}
        recommendations={[]}
        buildCandidate={(hex) => ({ accent: { kind: "custom", seed: hex } })}
        compile={compile}
        onApply={vi.fn(async () => undefined)}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /自定义强调色/ }));
    fireEvent.change(screen.getByRole("textbox", { name: "Hex" }), {
      target: { value: "#FFFFFF" },
    });

    expect(
      await screen.findByText("团队主题暂时无法用于个人配色，请联系团队管理员。"),
    ).toBeTruthy();
    expect(screen.queryByText("这个颜色无法生成安全主题")).toBeNull();
    expect(screen.getByRole("button", { name: "应用颜色" })).toBeDisabled();
  });
});

function compileResult(candidateId: string): PersonalAppearanceCompileResult {
  const light = tokens(0);
  const dark = tokens(40);
  light.background = "#FFFFFF";
  light.ink = "#1C1917";
  light.accent = "#2F6F84";
  light.accentInk = "#FFFFFF";
  dark.background = "#131210";
  dark.ink = "#F5F2EB";
  dark.accent = "#73B6C9";
  dark.accentInk = "#102126";
  return {
    candidateId,
    baseEtag: ETAG,
    team: { revisionId: null, revisionNumber: 0 },
    personalOverlayVersion: 1,
    colorScheme: {
      tokenSchemaVersion: 2,
      light,
      dark,
      checksum: `sha256:${"b".repeat(64)}`,
    },
    inputSeeds: {},
    validation: { blocking: [], warnings: [] },
    contextRecommendations: {
      palette: { version: 1, items: [] },
      accent: { version: 1, items: [] },
      background: { version: 1, items: [] },
    },
  };
}

function tokens(offset: number): ColorSchemeMode {
  return Object.fromEntries(
    COLOR_SCHEME_TOKEN_KEYS.map((key, index) => [
      key,
      `#${(index + offset + 1).toString(16).padStart(6, "0").toUpperCase()}`,
    ]),
  ) as ColorSchemeMode;
}
