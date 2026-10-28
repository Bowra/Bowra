import { expect, test } from "@playwright/test";

import { parseEffectiveThemeResponse } from "../lib/theme-config";
import { E2E_PERSONAL_APPEARANCE, mockAuthedApp } from "./_app-mocks";
import customTheme from "./fixtures/composer-custom-theme.json";
import themes from "./fixtures/home-accent-themes.json";

type Mode = "light" | "dark";
const palettes = {
  warm: {
    accent: { kind: "preset", registryVersion: 1, id: "bowhead" },
    colorScheme: E2E_PERSONAL_APPEARANCE.effective.colorScheme,
  },
  blue: themes.blue,
  // Compiled from a user-defined indigo seed, distinct from the built-in presets.
  custom: {
    accent: { kind: "custom", seed: "#635BFF" },
    colorScheme: customTheme,
  },
} as const;

function rgb(hex: string) {
  return `rgb(${[1, 3, 5].map((offset) => parseInt(hex.slice(offset, offset + 2), 16)).join(", ")})`;
}

for (const mode of ["light", "dark"] as const) {
  for (const [paletteName, palette] of Object.entries(palettes)) {
    for (const width of [840, 390]) {
      test(`Composer refresh: ${paletteName} ${mode} ${width}px`, async ({ page }, testInfo) => {
        await page.setViewportSize({ width, height: 900 });
        const personal = {
          ...E2E_PERSONAL_APPEARANCE,
          materialized: true,
          overrides: { mode, accent: palette.accent },
          effective: { ...E2E_PERSONAL_APPEARANCE.effective, mode, colorScheme: palette.colorScheme },
        };
        await mockAuthedApp(page, {
          planning: true,
          personalAppearance: personal,
          effectiveTheme: parseEffectiveThemeResponse({
            schemaVersion: 2,
            brand: {
              applicationName: "Bowhead", logoAlt: "Bowhead",
              logoOnLight: { kind: "text", text: "Bowhead" },
              logoOnDark: { kind: "text", text: "Bowhead" },
              mark: { kind: "generatedInitial", text: "B" },
              favicon: { kind: "generatedInitial", text: "B" },
            },
            effective: {
              ...personal.effective, version: 1,
              colorProfile: { type: "preset", id: "bowhead" }, chartPalette: "default",
            },
            locks: {},
            source: {
              mode: "user", colorProfile: "user", colorScheme: "user",
              typographyProfile: "product", typeScale: "product", density: "product",
              radius: "product", motion: "product", chartPalette: "product",
            },
            userConfig: { version: 1, mode },
            team: { id: "team_smoke", configurationType: "legacy", themeRevision: null, revisionId: null },
          }),
        });
        await page.goto("/");
        const root = page.locator("html");
        await expect(root).toHaveAttribute("data-theme", mode);
        const colors = palette.colorScheme[mode];
        await expect.poll(() => root.evaluate((node) =>
          getComputedStyle(node).getPropertyValue("--c-accent").trim(),
        )).toBe(colors.accent);

        const composer = page.locator('[data-composer-surface="home"]');
        await expect(composer).toBeVisible();
        await expect(composer).toHaveCSS("background-color", rgb(colors.surface));
        const textarea = composer.locator("textarea");
        await textarea.fill("经营分析验收");
        await expect(composer.locator(".v2-send-btn")).toBeEnabled();
        await expect(composer.locator(".v2-send-btn")).toHaveCSS("background-color", rgb(colors.accent));
        await expect(composer).toHaveCSS("border-top-color", rgb(colors.focus));
        const layout = await composer.evaluate((node) => ({
          client: (node as HTMLElement).clientWidth,
          scroll: (node as HTMLElement).scrollWidth,
        }));
        expect(layout.scroll).toBeLessThanOrEqual(layout.client + 1);

        await composer.locator('.composer-v2__model-trigger[aria-haspopup="menu"]').click();
        await expect(page.locator('.composer-v2__model-menu[role="menu"]')).toBeVisible();
        await page.keyboard.press("Escape");
        await expect(page.locator('.composer-v2__model-menu[role="menu"]')).toBeHidden();
        await page.mouse.move(0, 0);
        await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
        await expect(composer.locator(".composer-v2__model-wrap > .composer-v2__tooltip"))
          .toHaveCSS("opacity", "0");
        await page.evaluate(async () => document.fonts.ready);
        await composer.screenshot({
          path: testInfo.outputPath(`composer-${paletteName}-${mode}-${width}.png`),
          animations: "disabled",
          caret: "hide",
        });
      });
    }
  }
}
