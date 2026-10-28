/**
 * Desktop-sidebar session “⋯” (including Delete Session) appears with inline buttons on hover-capable devices (technical design, Behavior Batch A: Menus, Delete Session).
 * Previously `.unified-work-more-wrap` appeared only under `(hover: none)`, preventing desktop session deletion. jsdom cannot measure cascade-based visibility,
 * so lock source facts here; real-browser visibility, positioning and keyboard coverage live in e2e/sidebar-session-delete.spec.ts.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const CSS = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "globals.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

function mediaBlocks(query: RegExp): string[] {
  const out: string[] = [];
  const opener = new RegExp(`@media\\s*${query.source}\\s*\\{`, "g");
  for (let m = opener.exec(CSS); m !== null; m = opener.exec(CSS)) {
    let depth = 1;
    let i = m.index + m[0].length;
    const start = i;
    while (i < CSS.length && depth > 0) {
      if (CSS[i] === "{") depth += 1;
      else if (CSS[i] === "}") depth -= 1;
      i += 1;
    }
    out.push(CSS.slice(start, i - 1));
  }
  return out;
}

/** Split selector lists at top-level commas (not commas inside `:is(...)` or `:has(...)`). */
function splitSelectors(list: string): string[] {
  const out: string[] = [];
  let depth = 0;
  let current = "";
  for (const ch of list) {
    if (ch === "(") depth += 1;
    else if (ch === ")") depth -= 1;
    if (ch === "," && depth === 0) {
      out.push(current);
      current = "";
    } else current += ch;
  }
  out.push(current);
  return out.map((s) => s.replace(/\s+/g, " ").replace(/\( /g, "(").replace(/ \)/g, ")").trim());
}

const rules = (body: string) =>
  [...body.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map((m) => ({ selectors: splitSelectors(m[1]!), decls: m[2]! }));

const HOVER = mediaBlocks(/\(\s*hover:\s*hover\s*\)/).flatMap(rules);
const FINE = mediaBlocks(/\(\s*hover:\s*hover\s*\)\s*and\s*\(\s*pointer:\s*fine\s*\)/).flatMap(rules);
const padding = (selector: string) =>
  FINE.filter((r) => r.selectors.includes(selector)).map((r) => /padding-right:\s*(\d+)px/.exec(r.decls)?.[1]).find(Boolean);

describe("会话「⋯」：桌面（hover: hover）", () => {
  it("「⋯」的容器在可悬停设备上参与布局（不再是 display: none）", () => {
    expect(HOVER.some((r) => r.selectors.includes(".unified-work-item .unified-work-more-wrap") && /display:\s*block/.test(r.decls))).toBe(true);
  });

  it("悬停行、键盘聚焦到行内按钮或「⋯」、菜单开着时显示", () => {
    const show = HOVER.filter((r) => /opacity:\s*1\b/.test(r.decls) && /pointer-events:\s*auto/.test(r.decls)).flatMap((r) => r.selectors);
    expect(show).toEqual(
      expect.arrayContaining([
        ".unified-work-item:hover .unified-work-more-wrap",
        ".unified-work-item:has(.unified-work-actions:focus-within) .unified-work-more-wrap",
        ".unified-work-item .unified-work-more-wrap:focus-within",
        '.unified-work-item .unified-work-more-wrap:has(.unified-work-more[aria-expanded="true"])',
      ]),
    );
  });

  it("keeps the hidden ⋯ hit-testable above the title so the first pointer contact reaches the trigger", () => {
    const hidden = HOVER.find(
      (r) => r.selectors.includes(".unified-work-item .unified-work-more-wrap") && /opacity:\s*0\b/.test(r.decls),
    );
    expect(hidden?.decls).toBeDefined();
    expect(hidden?.decls).not.toMatch(/pointer-events:\s*none/);
    expect(hidden?.decls).toMatch(/z-index:\s*1\b/);
  });

  it("只作用于会话行：成员表复用同一个「更多」组件，不能被桌面规则藏掉", () => {
    for (const rule of HOVER) {
      for (const selector of rule.selectors) {
        if (selector.includes("unified-work-more-wrap")) expect(selector, selector).toMatch(/^\.unified-work-item\b/);
      }
    }
  });

  it("只作用于会话行：成员表复用同一个「更多」组件，不能被鼠标下的 32px 规则改尺寸", () => {
    for (const rule of FINE) {
      for (const selector of rule.selectors) {
        if (/unified-work-(more|action)/.test(selector)) expect(selector, selector).toMatch(/^\.unified-work-item\b/);
      }
    }
  });
});

describe("会话行按钮：鼠标（hover: hover and pointer: fine）", () => {
  it("行内按钮与「⋯」缩到 32，焦点环画在内侧（贴边不被滚动容器裁掉）", () => {
    const size = FINE.find((r) => r.selectors.includes(".unified-work-item :is(.unified-work-action, .unified-work-more)"));
    expect(size?.decls).toMatch(/width:\s*32px/);
    expect(size?.decls).toMatch(/height:\s*32px/);
    const focus = FINE.find((r) => r.selectors.includes(".unified-work-item :is(.unified-work-action, .unified-work-more):focus-visible"));
    expect(focus?.decls).toMatch(/outline-offset:\s*-2px/);
  });

  it("the row text gives the 32 px ⋯ button room (30 px) when it shows", () => {
    const shown = ":is(:hover, :has(.unified-work-actions:focus-within), :has(.unified-work-more-wrap:focus-within), :has(.unified-work-more[aria-expanded=\"true\"])) .unified-work-item-main";
    expect(padding(`.unified-work-item.has-more${shown}`)).toBe("30");
  });
});
