/**
 * P2 batch 4: the desktop layout starts at 1024 instead of 1200
 * (docs/design/组件统一-技术设计.md "P2 第 4 批结果", breakpoint table). Every former `max-width: 1199.98px` block is
 * split at 1023.98; the desktop-only `min-width: 1200px` workbench pane rules are repeated for 1024–1199; public pages
 * keep their 1024–1199 page scrolling through styles/public-narrow-band.css, which only the (public) layout imports.
 */
import { describe, expect, it } from "vitest";

import { parseRules, read, splitSelectors, stripComments } from "./css-contract";

const FILES = ["app/globals.css", "styles/agent-marketplace.css"];
const BAND = /min-width:\s*1024px\)\s*and\s*\(max-width:\s*(1199(\.98)?|1200)px/;

function rules(file: string) {
  return parseRules(stripComments(read(file)));
}

describe("1200 breakpoint moved to 1024", () => {
  it("leaves no unconditional 1024–1199 single-column rule", () => {
    for (const file of FILES) {
      const loose = rules(file).filter((r) => /max-width:\s*1199(\.98)?px|max-width:\s*1200px/.test(r.media ?? "") && !/min-width:\s*1024px/.test(r.media ?? ""));
      expect(loose.map((r) => `${file} ${r.media} ${r.selector}`)).toEqual([]);
    }
  });

  it("repeats exactly the desktop-only workbench pane rules for 1024–1199", () => {
    const all = rules("app/globals.css");
    const desktop = all.filter((r) => r.media === "@media (min-width: 1200px)");
    const band = all.filter((r) => BAND.test(r.media ?? ""));
    expect(desktop.length).toBeGreaterThan(0);
    expect(band.map((r) => splitSelectors(r.selector).join(", "))).toEqual(desktop.map((r) => splitSelectors(r.selector).join(", ")));
    expect(rules("styles/agent-marketplace.css").filter((r) => BAND.test(r.media ?? ""))).toEqual([]);
  });

  it("keeps the 1024–1199 page scrolling for public pages only", () => {
    const band = rules("styles/public-narrow-band.css");
    expect(band.map((r) => [r.media, splitSelectors(r.selector).join(", ")])).toEqual([
      ["@media (min-width: 1024px) and (max-width: 1199.98px)", "body"],
      ["@media (min-width: 1024px) and (max-width: 1199.98px)", "html, body"],
    ]);
    const below = rules("app/globals.css").filter((r) => r.media === "@media (max-width: 1023.98px)");
    for (const rule of band) {
      // The same declarations apply to every page below 1024.
      const twin = below.find((b) => b.selector === rule.selector && stripComments(b.body).replace(/\s+/g, " ").trim() === stripComments(rule.body).replace(/\s+/g, " ").trim());
      expect(twin, rule.selector).toBeTruthy();
    }
    const publicCss = Array.from(read("app/(public)/layout.tsx").matchAll(/^import "([^"]+\.css)";$/gm), (m) => m[1]);
    expect(publicCss.slice(0, 2)).toEqual(["../globals.css", "../../styles/public-narrow-band.css"]);
    expect(read("app/(app)/layout.tsx")).not.toContain("public-narrow-band.css");
  });

  it("moves the single-column interface rules to 1023.98", () => {
    for (const file of ["styles/ui-refresh-chrome.css", "styles/ui-refresh-materials.css", "styles/ui-refresh-pages-settings.css", "styles/ui-refresh-workbench.css"]) {
      expect(read(file), file).not.toMatch(/@media[^{]*1199/);
    }
  });
});
