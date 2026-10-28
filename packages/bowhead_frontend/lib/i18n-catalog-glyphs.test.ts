// Icon glyphs (arrows, check marks, warning signs, emoji, disclosure triangles) belong at the call
// site as `<CharIcon>`, not inside translated copy: there they render as Lucide glyphs and
// every locale gets the same icon. This guard keeps leading/trailing icon glyphs out of the
// catalogs, except for the keys listed below, which cannot host a component or stay on purpose.
import { describe, expect, it } from "vitest";

import de from "@/messages/de-DE.json";
import en from "@/messages/en-US.json";
import fr from "@/messages/fr-FR.json";
import zh from "@/messages/zh-CN.json";

const CATALOGS = { "zh-CN": zh, "en-US": en, "de-DE": de, "fr-FR": fr } as const;

const ICON_GLYPHS = [..."→←↑↓↗↺↻✓✔✗✕✖❌✅⚠⏸📌💡🔒🤖🛠📜◌▾▸▴▲▼▶◀＋⚡ⓘ⊘⋯"];

/** Keys that keep a leading/trailing icon glyph, with the reason. */
const KEPT: Record<string, string> = {
  "admin.credits.sort_balance_pct": "<option> text in the users table sort select",
  "admin.credits.sort_spend": "<option> text in the users table sort select",
  "composer.cycle_hint": "keyboard notation (arrow keys), not an icon",
  "composer_references.menu_keyboard_hint": "keyboard notation (arrow keys) inside a sentence",
  "composer.steer_placeholder": "textarea placeholder",
  "insight_card.open_table": "also passed as a plain-string label to the data focus request",
  "plan_card.join_after": "the whole value is a connector inside a composed phrase and a template string",
};

function flatten(node: unknown, prefix = "", out: Record<string, string> = {}): Record<string, string> {
  if (typeof node === "string") out[prefix] = node;
  else if (node && typeof node === "object") {
    for (const [key, value] of Object.entries(node)) flatten(value, prefix ? `${prefix}.${key}` : key, out);
  }
  return out;
}

function edgeGlyph(value: string): string | null {
  const chars = [...value.replace(/️/g, "").trim()];
  if (chars.length === 0) return null;
  if (ICON_GLYPHS.includes(chars[0])) return chars[0];
  if (ICON_GLYPHS.includes(chars[chars.length - 1])) return chars[chars.length - 1];
  return null;
}

describe("catalog icon glyphs", () => {
  it.each(Object.keys(CATALOGS) as (keyof typeof CATALOGS)[])("%s has no leading or trailing icon glyph outside the kept list", (locale) => {
    const flat = flatten(CATALOGS[locale]);
    const offenders = Object.entries(flat)
      .filter(([key, value]) => edgeGlyph(value) !== null && !(key in KEPT))
      .map(([key, value]) => `${key}: ${value}`);
    expect(offenders).toEqual([]);
  });

  it("lists only keys that exist everywhere and still carry an edge glyph", () => {
    for (const key of Object.keys(KEPT)) {
      const values = Object.values(CATALOGS).map((catalog) => flatten(catalog)[key]);
      for (const value of values) expect(value, key).toBeTypeOf("string");
      expect(values.some((value) => edgeGlyph(value) !== null), key).toBe(true);
    }
  });
});
