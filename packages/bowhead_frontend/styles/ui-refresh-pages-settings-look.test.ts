/**
 * Interface appearance, page-by-page acceptance A · team-settings Appearance and Routine group:
 * source contracts for Brand and Appearance (team-theme.tsx, team-chart-style-library.tsx), Chart Style Editing (chart-style-editor.tsx),
 * and Suggestions Inbox (feedback-inbox-content.tsx).
 *
 *   1. Mappings: every selector starts with :root:root, no literal colors, --c-line, positioning or layer changes;
 *   2. Spec details: group headings 16/650 regular text (previously serif / uppercase mono), remove 01–06 numbering,
 * cards use content surface + hairline + --r-4;
 *      inbox badges use light-filled pills, and save scope selection through the save bar;
 *   3. Call sites: inbox inline styles removed.
 *
 * Both development CSS files are styles/_accept-a/ shards; update the two paths below when merging into
 * ui-refresh-pages-settings.css / ui-controls-legacy.css.
 */
import { describe, expect, it } from "vitest";

import {
  cascadeProblems,
  decls,
  LITERAL_COLOR,
  norm,
  parseRules,
  PREFIX,
  read,
  readSection,
  splitSelectors,
  stripComments,
} from "./css-contract";

const MAP_FILE = "styles/ui-refresh-pages-settings.css";
const LEGACY_FILE = "styles/ui-controls-legacy.css";

const RULES = parseRules(stripComments(readSection(MAP_FILE, "accept-a:look")));
const LEGACY = parseRules(stripComments(readSection(LEGACY_FILE, "accept-a:look")));

function exact(sel: string, media: RegExp | null = null): Record<string, string> {
  const found = RULES.filter(
    (r) => (media ? media.test(r.media ?? "") : r.media === null) && splitSelectors(r.selector).includes(`${PREFIX} ${norm(sel)}`),
  );
  expect(found.length, sel).toBeGreaterThan(0);
  return Object.assign({}, ...found.map((r) => decls(r.body)));
}

describe("mappings start with the :root:root prefix", () => {
  it("解析出规则", () => {
    expect(RULES.length).toBeGreaterThan(60);
  });
  it("每个选择器都以开关前缀开头（含 @media 内）", () => {
    expect(RULES.flatMap((r) => splitSelectors(r.selector)).filter((s) => !s.startsWith(PREFIX))).toEqual([]);
  });
  it("不写字面色值、不用 --c-line", () => {
    expect(RULES.filter((r) => LITERAL_COLOR.test(stripComments(r.body))).map((r) => r.selector)).toEqual([]);
    expect(RULES.filter((r) => /var\(--c-line\)/.test(r.body)).map((r) => r.selector)).toEqual([]);
  });
  it("不改定位与层级（草稿命令条、预览栏原有的吸顶不动）", () => {
    expect(RULES.filter((r) => "position" in decls(r.body) || "z-index" in decls(r.body)).map((r) => r.selector)).toEqual([]);
  });
  it("后写的覆盖不输给先写的规则", () => {
    expect(cascadeProblems(RULES)).toEqual([]);
  });
  it("the base-layer section carries no hook prefix", () => {
    expect(LEGACY.flatMap((r) => splitSelectors(r.selector)).filter((s) => s.startsWith(":root"))).toEqual([]);
  });
});

describe("Chart style headings and cards", () => {
  it("uses readable chart headings and secondary labels", () => {
    for (const sel of [".cs-library__header h2"]) {
      expect(exact(`:is(.cs-library__header h2)`), sel).toMatchObject({
        "font-family": "var(--font-control)",
        "font-size": "var(--fs-lg)",
        "font-weight": "650",
      });
    }
    expect(exact(":is(.cs-kicker)")).toMatchObject({ color: "var(--c-ink-3)", "text-transform": "none", "letter-spacing": "normal" });
    expect(exact(".cs-editor-section h2")).toMatchObject({ "font-size": "var(--fs-lg)", "font-weight": "650" });
  });

  it("keeps editor section headings free of step numbers", () => {
    expect(exact(".cs-editor-section > header > span")).toEqual({ display: "none" });
  });

  it("uses consistent chart cards and separators", () => {
    const card = { "border-radius": "var(--r-4)", "box-shadow": "0 0 0 var(--hairline) var(--c-separator)" };
    expect(exact(".cs-editor-section__body")).toMatchObject({ ...card, background: "var(--c-surface)" });
    expect(exact(":is(.cs-style-list, .cs-policy-list)")).toMatchObject({ ...card, gap: "0" });
    expect(exact(".cs-built-in-card")).toMatchObject({ ...card, border: "0" });
    expect(exact(":is(.cs-style-row, .cs-policy-row) + :is(.cs-style-row, .cs-policy-row)")).toMatchObject({
      "box-shadow": "inset 0 var(--hairline) 0 var(--c-separator)",
    });
  });


  it("手机：选择范围行色条与名称一行、两个选项并排在下一行", () => {
    expect(exact(".cs-policy-row", /max-width:\s*767\.98px/)).toMatchObject({ "flex-direction": "row", "flex-wrap": "wrap" });
  });

  it("call site: the save bar saves the selection-scope policy and Discard returns to the saved one", () => {
    const src = read("components/settings/team-chart-style-library.tsx");
    expect(src).toMatch(/<SaveBar\s+dirty=\{policyDirty\}[\s\S]*?onSave=\{\(\) => void savePolicy\(\)\}\s+onDiscard=\{\(\) => \{ if \(confirmedPolicy\) setPolicy\(confirmedPolicy\); \}\}/);
  });
});

describe("建议收件箱", () => {
  it("list page drops the 760 cap; cards use a hairline and --r-4; expanded cards get a 1px accent ring", () => {
    expect(exact(".legacy-fbi-page")).toMatchObject({ "max-width": "none", margin: "0", padding: "0" });
    expect(exact(".legacy-fbi-row")).toMatchObject({
      border: "0",
      "border-radius": "var(--r-4)",
      "box-shadow": "0 0 0 var(--hairline) var(--c-separator)",
    });
    expect(exact(".legacy-fbi-row.is-open")).toMatchObject({ "box-shadow": "0 0 0 1px var(--c-accent)" });
  });

  it("徽标 20 高胶囊、无边框；类型强调色浅底，复发 / 等待警示色浅底", () => {
    expect(exact(":is(.legacy-fbi-kind, .legacy-fbi-chip)")).toMatchObject({ height: "20px", "border-radius": "999px", border: "0", opacity: "1" });
    expect(exact(".legacy-fbi-kind")).toMatchObject({ background: "var(--c-accent-soft)", color: "var(--c-accent-text)" });
    expect(exact(".legacy-fbi-chip.is-warn")).toMatchObject({ background: "var(--c-warn-soft)", color: "var(--c-warn)" });
  });

  it("详情小标题 12/600 --c-ink-3 普通字（原 11px 大写字距）；空态与加载框为发丝线卡片", () => {
    expect(exact(".legacy-fbi-label")).toMatchObject({ "text-transform": "none", "letter-spacing": "normal", color: "var(--c-ink-3)" });
    expect(exact(".legacy-fbi-empty")).toMatchObject({ border: "0", "box-shadow": "0 0 0 var(--hairline) var(--c-separator)" });
  });

  it("call site: no inline styles; case chips take their class from the tone; loading uses the skeleton", () => {
    const src = read("components/settings/feedback-inbox-content.tsx");
    expect(src).not.toContain("style={{");
    expect(src).toContain('className={`legacy-fbi-chip is-${cl.tone}`}');
    expect(src).toContain('<LoadingSkeleton label={t("loading")} />');
    expect(src).toMatch(/\{err \? \(/);
  });
});
