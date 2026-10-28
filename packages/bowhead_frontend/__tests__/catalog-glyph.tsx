// Shared assertions for catalog strings whose icon glyph moved from messages/*.json to a
// `<CharIcon>` at the call site: the glyph is a Lucide svg named after the character and the
// text keeps the catalog's spacing (glyph, one space, message — or message, one space, glyph).
import { expect } from "vitest";
import { createTranslator } from "use-intl/core";

import deMessages from "@/messages/de-DE.json";
import enMessages from "@/messages/en-US.json";
import frMessages from "@/messages/fr-FR.json";
import zhMessages from "@/messages/zh-CN.json";

export const GLYPH_LOCALES = ["zh-CN", "en-US", "de-DE", "fr-FR"] as const;
export type GlyphLocale = (typeof GLYPH_LOCALES)[number];

const CATALOGS = { "zh-CN": zhMessages, "en-US": enMessages, "de-DE": deMessages, "fr-FR": frMessages } as const;

/** Formatted catalog message (plain text) for `key` in `locale`. */
export function catalogText(locale: GlyphLocale, key: string, values?: Record<string, string | number>): string {
  const t = createTranslator({ locale, messages: CATALOGS[locale] as never }) as unknown as (
    k: string,
    v?: Record<string, string | number>,
  ) => string;
  return t(key, values);
}

/** Plain text of a rich catalog message: tags are rendered as their content only. */
export function catalogRichText(locale: GlyphLocale, key: string, values: Record<string, string | number>, tags: string[]): string {
  const t = createTranslator({ locale, messages: CATALOGS[locale] as never }) as unknown as {
    markup: (k: string, v: Record<string, unknown>) => string;
  };
  return t.markup(key, { ...values, ...Object.fromEntries(tags.map((tag) => [tag, (chunks: string) => chunks])) });
}

/** The string the call site rendered before the glyph left the catalog. */
export function legacyText(glyph: string, at: "start" | "end", message: string): string {
  return at === "start" ? `${glyph} ${message}` : `${message} ${glyph}`;
}

type Mode = { exact?: boolean };

function expectText(el: Element, want: string, { exact = true }: Mode): void {
  if (exact) expect(el.textContent).toBe(want);
  else expect(el.textContent).toContain(want);
}

/** Flag off: the text is the legacy string, character for character, and there is no glyph svg. */
export function expectLegacyGlyph(el: Element, glyph: string, at: "start" | "end", message: string, mode: Mode = {}): void {
  expect(el.querySelector("svg.bh-glyph")).toBeNull();
  expectText(el, legacyText(glyph, at, message), mode);
  // The glyph shares one text node with its space and the adjacent text, as the catalog string did.
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  const nodes: string[] = [];
  for (let n = walker.nextNode(); n; n = walker.nextNode()) nodes.push(n.textContent ?? "");
  const host = nodes.find((data) => data.includes(glyph));
  expect(host, `text node holding ${glyph}`).toBeDefined();
  if (at === "start") expect(host!.startsWith(`${glyph} `) && host!.length > glyph.length + 1).toBe(true);
  else expect(host!.endsWith(` ${glyph}`) && host!.length > glyph.length + 1).toBe(true);
}

/** Flag on: a Lucide glyph named after the character takes the character's place; spacing is kept. */
export function expectRefreshedGlyph(el: Element, glyph: string, at: "start" | "end", message: string, mode: Mode = {}): void {
  const svg = el.querySelector("svg.bh-glyph");
  expect(svg, `glyph svg for ${glyph}`).not.toBeNull();
  expect(svg?.getAttribute("role")).toBe("img");
  expect(svg?.getAttribute("aria-label")).toBe(glyph.replace(/️/g, ""));
  expect(el.textContent).not.toContain(glyph);
  expectText(el, at === "start" ? ` ${message}` : `${message} `, mode);
  const sibling = at === "start" ? svg?.nextSibling : svg?.previousSibling;
  expect(sibling?.nodeType).toBe(Node.TEXT_NODE);
  expect(sibling?.textContent).toBe(" ");
}
