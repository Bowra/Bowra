/**
 * Plan-card / plan-graph *-tint values must not be light literals in dark mode (workbench batch 4a fix, active in both appearances).
 *
 * `--c-pos-tint` / `--c-warn-tint` / `--c-err-tint` scoped to `.pcard` were light literals without dark values:
 * completed plan-graph nodes (`.gnode.is-done`), failed / blocked nodes and card headers had glaring light backgrounds under dark themes.
 * Dark values now blend semantic colors with the content surface (tokens); light values remain unchanged.
 */
import { describe, expect, it } from "vitest";

import { decls, LITERAL_COLOR, parseRules, read, stripComments } from "./css-contract";

const RULES = parseRules(stripComments(read("styles/plan-card.css")));
const TINTS = ["--c-pos-tint", "--c-warn-tint", "--c-err-tint"] as const;

describe("plan-card.css：计划作用域 tint 的深色值", () => {
  it("深色主题下三个 tint 都有定义，且由 token 混出、不写字面色值", () => {
    const dark = RULES.find((r) => r.selector.includes('[data-theme="dark"]') && r.selector.includes(".pcard"));
    expect(dark).toBeDefined();
    const body = decls(dark!.body);
    for (const tint of TINTS) {
      expect(body[tint], tint).toMatch(/^color-mix\(in srgb, var\(--c-(pos|warn|err)\) \d+%, var\(--c-surface\)\)$/);
      expect(LITERAL_COLOR.test(body[tint]!), tint).toBe(false);
    }
  });

  it("深色规则覆盖与浅色声明相同的作用域（计划卡、计划模式条、样稿报告）", () => {
    const light = RULES.find((r) => r.selector.startsWith(".pcard, .pmode"));
    const dark = RULES.find((r) => r.selector.includes('[data-theme="dark"]') && r.selector.includes(".pcard"));
    const scopes = (sel: string) => (sel.match(/\.[a-z-]+/g) ?? []).sort();
    expect(scopes(dark!.selector)).toEqual(scopes(light!.selector));
  });

  it("深色下节点里的次要色小字（ink-3）在三种 tint 底上都 ≥4.5:1（默认深色主题）", () => {
    const globals = stripComments(read("app/globals.css"));
    const block = /\[data-theme="dark"\]\s*\{([\s\S]*?)\n\}/.exec(globals)![1];
    const token = (name: string) => new RegExp(`${name}:\\s*(#[0-9a-f]{6})`, "i").exec(block)![1];
    const hex = (c: string) => [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16));
    const lum = (rgb: number[]) => {
      const [r, g, b] = rgb.map((v) => v / 255).map((x) => (x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4));
      return 0.2126 * r + 0.7152 * g + 0.0722 * b;
    };
    const ratio = (a: number[], b: number[]) => {
      const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
      return (hi + 0.05) / (lo + 0.05);
    };
    const dark = decls(RULES.find((r) => r.selector.includes('[data-theme="dark"]') && r.selector.includes(".pcard"))!.body);
    const surface = hex(token("--c-surface"));
    const ink3 = hex(token("--c-ink-3"));
    for (const [tint, semantic] of [["--c-pos-tint", "--c-pos"], ["--c-warn-tint", "--c-warn"], ["--c-err-tint", "--c-err"]] as const) {
      const pct = Number(/(\d+)%/.exec(dark[tint]!)![1]) / 100;
      const mixed = hex(token(semantic)).map((v, i) => v * pct + surface[i] * (1 - pct));
      expect(ratio(ink3, mixed), tint).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("浅色值不变", () => {
    const light = decls(RULES.find((r) => r.selector.startsWith(".pcard, .pmode"))!.body);
    expect(light).toMatchObject({ "--c-pos-tint": "#f1f6ec", "--c-warn-tint": "#fbf5e6", "--c-err-tint": "#fbeef0" });
  });

  it("计划图「已完成」节点底色走 --c-pos-tint", () => {
    const graph = parseRules(stripComments(read("styles/plan-graph.css")));
    const done = graph.find((r) => r.selector === ".gwrap .gnode.is-done");
    expect(decls(done!.body).background).toBe("var(--c-pos-tint)");
  });
});
