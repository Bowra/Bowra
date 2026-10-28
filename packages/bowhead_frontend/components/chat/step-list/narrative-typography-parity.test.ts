/**
 * chat-ux-2026-08 batch E — lock chat body font size.
 *
 * Streaming and completed chat bodies now share the same `<Markdown>` in `LeadNarrative`;
 * font size comes solely from `.v2-md p` in `globals.css`. The former cross-file guard requiring streaming inline styles to match
 * `.v2-md p` was removed with the streaming shim (only one rendering path remains,
 * eliminating drift between two values).
 *
 * ⚠ Reading text is appropriate: jsdom does not load globals.css, so `getComputedStyle` cannot obtain CSS-side values.
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import path from "node:path";

const ROOT = path.resolve(__dirname, "../../..");

function read(rel: string): string {
  return readFileSync(path.join(ROOT, rel), "utf8");
}

describe("对话正文字号", () => {
  it("本批把它定在 15 / 1.7（改动意图钉，防被无意改回）", () => {
    const css = read("app/globals.css");
    const rule = css.match(/\.v2-md p\s*\{[^}]*\}/)![0];
    expect(rule).toContain("font-size: 0.9375rem");
    expect(rule).toContain("line-height: 1.7");
  });
});
