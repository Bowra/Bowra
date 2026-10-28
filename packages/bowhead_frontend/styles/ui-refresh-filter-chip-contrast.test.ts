/**
 * Shared filter chip: selected text contrast.
 *
 * The selected chip (.sset .fchip.on, .kb-filterchip.active, agent marketplace capability radios) paints its label on
 * --c-accent-soft. Pure --c-accent there measured 4.47:1 in the default warm palette and about 4.0:1 on the served
 * product palette with a deeper soft fill, below body-text AA. The label now uses --c-accent-text (accent mixed toward
 * --c-ink); this test resolves the real token values of every globals.css palette in light and dark, plus a served
 * brand palette, and requires >= 4.5:1 for the selected chip and for the selected chip under hover (--c-fill-3).
 * Generated presets and legacy registry palettes are covered by bowhead_core/tests/test_theme_filter_chip_contrast.py.
 */
import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { E2E_PERSONAL_APPEARANCE } from "../e2e/_app-mocks";

const ROOT = resolve(__dirname, "..");
const BODY_TEXT_MIN = 4.5;

type Rgb = [number, number, number];
type Palette = Record<"accent" | "accentSoft" | "ink" | "surface" | "background", string>;

function stripComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, " ");
}

function blocks(css: string): Array<{ selector: string; body: string }> {
  return Array.from(stripComments(css).matchAll(/([^{}]+)\{([^{}]*)\}/g), (m) => ({ selector: m[1].split(";").pop()!.trim(), body: m[2] }));
}

function decl(body: string, name: string): string | undefined {
  return body.match(new RegExp(`(?:^|[;\\s])${name}\\s*:\\s*([^;]+);`))?.[1].trim();
}

function hex(value: string): Rgb {
  const v = value.replace("#", "");
  return [0, 2, 4].map((i) => parseInt(v.slice(i, i + 2), 16)) as Rgb;
}

/** color-mix(in srgb, a p, b) on gamma-encoded channels, as browsers do. */
function mix(a: Rgb, b: Rgb, p: number): Rgb {
  return a.map((channel, i) => channel * p + b[i] * (1 - p)) as Rgb;
}

function luminance(c: Rgb): number {
  const f = (x: number) => {
    const s = x / 255;
    return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]);
}

function contrast(a: Rgb, b: Rgb): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

const globalsCss = readFileSync(join(ROOT, "app/globals.css"), "utf-8");
const refreshCss = readFileSync(join(ROOT, "styles/ui-refresh.css"), "utf-8");

function pick(body: string): Partial<Palette> {
  const out: Partial<Palette> = {};
  for (const [key, name] of [["accent", "--c-accent"], ["accentSoft", "--c-accent-soft"], ["ink", "--c-ink"], ["surface", "--c-surface"], ["background", "--c-bg"]] as const) {
    const value = decl(body, name);
    if (value?.startsWith("#")) out[key] = value;
  }
  return out;
}

function globalsPalettes(): Array<{ name: string; mode: "light" | "dark"; palette: Palette }> {
  const all = blocks(globalsCss);
  const base = { light: {} as Partial<Palette>, dark: {} as Partial<Palette> };
  for (const block of all) {
    if (block.selector === ":root") Object.assign(base.light, pick(block.body));
    if (block.selector === '[data-theme="dark"]') Object.assign(base.dark, pick(block.body));
  }
  const out: Array<{ name: string; mode: "light" | "dark"; palette: Palette }> = [
    { name: "default", mode: "light", palette: base.light as Palette },
    { name: "default", mode: "dark", palette: base.dark as Palette },
  ];
  for (const block of all) {
    const profile = block.selector.match(/^:root(?:\[data-theme="dark"\])?\[data-color-profile="([a-z]+)"\]/)?.[1];
    if (!profile) continue;
    const mode = block.selector.startsWith(':root[data-theme="dark"]') ? "dark" : "light";
    out.push({ name: profile, mode, palette: { ...base[mode], ...pick(block.body) } as Palette });
  }
  return out;
}

function servedPalettes(): Array<{ name: string; mode: "light" | "dark"; palette: Palette }> {
  const scheme = E2E_PERSONAL_APPEARANCE.effective.colorScheme;
  return (["light", "dark"] as const).map((mode) => {
    const { accent, accentSoft, ink, surface, background } = scheme[mode];
    return { name: "served-product", mode, palette: { accent, accentSoft, ink, surface, background } };
  });
}

/** --c-accent-text weight from its definition, so the test follows the token rather than a copied constant. */
function accentTextWeight(): number {
  const match = refreshCss.match(/--c-accent-text:\s*color-mix\(in srgb, var\(--c-accent\) (\d+)%, var\(--c-ink\)\);/);
  expect(match).not.toBeNull();
  return Number(match![1]) / 100;
}

/** Share of --c-accent-soft in the selected-hover fill (`color-mix(in srgb, var(--c-accent-soft) N%, var(--c-surface))`). */
function selectedHoverWeight(): number {
  const match = refreshCss.match(
    /\.kb-filterchip\.active\):hover:not\(:disabled\) \{\s*background: color-mix\(in srgb, var\(--c-accent-soft\) (\d+)%, var\(--c-surface\)\);/,
  );
  expect(match, "selected chips keep an accent fill on hover").not.toBeNull();
  return Number(match![1]) / 100;
}

const SELECTED_RULE =/:is\(\.sset \.fchip\.on, \.kb-filterchip\.active\) \{([^}]*)\}/;

describe("shared filter chip selected contrast", () => {
  const palettes = [...globalsPalettes(), ...servedPalettes()];

  it("reads every globals palette (default + four profiles, light and dark) and the served palette", () => {
    expect(palettes.map((p) => `${p.name}:${p.mode}`).sort()).toEqual(
      ["default", "forest", "graphite", "ocean", "plum", "served-product"].flatMap((n) => [`${n}:dark`, `${n}:light`]),
    );
    for (const { palette } of palettes) expect(Object.keys(palette).sort()).toEqual(["accent", "accentSoft", "background", "ink", "surface"]);
  });

  it("paints the selected label with --c-accent-text on --c-accent-soft", () => {
    const body = refreshCss.match(SELECTED_RULE)?.[1] ?? "";
    expect(decl(body, "background")).toBe("var(--c-accent-soft)");
    expect(decl(body, "color")).toBe("var(--c-accent-text)");
  });

  it.each(palettes.map((p) => [`${p.name} ${p.mode}`, p] as const))("%s: selected and selected-hover text reach 4.5:1", (_label, { palette }) => {
    const text = mix(hex(palette.accent), hex(palette.ink), accentTextWeight());
    const selected = contrast(text, hex(palette.accentSoft));
    // Hovering a selected chip keeps the accent fill, eased toward the surface (selected-hover rule).
    const hover = contrast(text, mix(hex(palette.accentSoft), hex(palette.surface), selectedHoverWeight()));
    expect(selected).toBeGreaterThanOrEqual(BODY_TEXT_MIN);
    expect(hover).toBeGreaterThanOrEqual(BODY_TEXT_MIN);
  });

  it("would fail with pure --c-accent on the served palette (negative control)", () => {
    const light = servedPalettes().find((p) => p.mode === "light")!.palette;
    expect(contrast(hex(light.accent), hex(light.accentSoft))).toBeLessThan(BODY_TEXT_MIN);
  });
});
