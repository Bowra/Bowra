/**
 * Interface appearance, page-by-page acceptance A · Team and Members, Space Settings:
 * source contracts for members, spaces, license management and space settings (General / Space Access / Term Aliases).
 *
 * Development styles live in styles/ui-refresh-pages-settings.css (mappings) and people-legacy.css (legacy classes
 * copied verbatim from inline styles);
 * merge into ui-refresh-pages-settings.css and ui-controls-legacy.css's Page-by-page Acceptance A section, respectively,
 * updating MAP / LEGACY sources together then.
 *   1. Every mapping selector starts with :root:root, without literal colors, --c-line or overrides defeated by earlier rules;
 *   2. Call sites: group cards, save bar and Switch; these inline styles are removed from pages.
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

const MAP = parseRules(stripComments(readSection("styles/ui-refresh-pages-settings.css", "accept-a:people")));
const LEGACY = parseRules(stripComments(readSection("styles/ui-controls-legacy.css", "accept-a:people")));

function exact(sel: string, media: RegExp | null = null): Record<string, string> {
  const found = MAP.filter(
    (r) => (media ? media.test(r.media ?? "") : r.media === null) && splitSelectors(r.selector).includes(`${PREFIX} ${norm(sel)}`),
  );
  expect(found.length, sel).toBeGreaterThan(0);
  return Object.assign({}, ...found.map((r) => decls(r.body)));
}

describe("mappings start with the :root:root prefix", () => {
  it("每个选择器都以开关前缀开头（含 @media 内）", () => {
    expect(MAP.length).toBeGreaterThan(30);
    expect(MAP.flatMap((r) => splitSelectors(r.selector)).filter((s) => !s.startsWith(PREFIX))).toEqual([]);
  });

  it("不写字面色值、不用 --c-line、不定位不改层级", () => {
    expect(MAP.filter((r) => LITERAL_COLOR.test(r.body)).map((r) => r.selector)).toEqual([]);
    expect(MAP.filter((r) => /var\(--c-line\)/.test(r.body)).map((r) => r.selector)).toEqual([]);
    expect(MAP.filter((r) => /(^|;)\s*(position|z-index)\s*:/.test(r.body) && !/\.settings-field__label/.test(r.selector)).map((r) => r.selector)).toEqual([]);
  });

  it("没有被先写规则压住的覆盖", () => {
    expect(cascadeProblems(MAP)).toEqual([]);
  });

  it("base-layer classes carry no hook prefix and start with .legacy-", () => {
    expect(LEGACY.flatMap((r) => splitSelectors(r.selector)).filter((s) => !s.startsWith(".legacy-"))).toEqual([]);
  });
});

describe("mappings (Page Layout Spec)", () => {
  it("成员页：「你的角色」不是页头徽标，工具行字段名只给读屏、同排居中", () => {
    expect(exact(".settings-people-hero__actions > .people-role-pill.people-role-pill")).toMatchObject({
      background: "transparent",
      "box-shadow": "none",
      padding: "0",
      color: "var(--c-ink-3)",
    });
    expect(exact(".people-list-toolbar > .settings-field > .settings-field__label")).toMatchObject({ position: "absolute", width: "1px", clip: "rect(0 0 0 0)" });
    expect(exact(".people-list-toolbar")).toMatchObject({ "align-items": "center" });
  });

  it("空间页：创建是分组卡片一行；列表是发丝线卡片、行高随 --table-row-h、状态为中性徽标", () => {
    expect(exact(".ui-tsa-create-control > .settings-input")).toMatchObject({ "max-width": "var(--group-control-max)" });
    expect(exact(".ui-tsa-list-card")).toMatchObject({ "border-radius": "var(--r-4)", background: "var(--c-surface)", "box-shadow": "0 0 0 var(--hairline) var(--c-separator)" });
    expect(exact(".ui-tsa-row")).toMatchObject({ "min-height": "var(--table-row-h)", "border-bottom": "0" });
    expect(exact(".ui-tsa-row + .ui-tsa-row")).toMatchObject({ "box-shadow": "inset 0 var(--hairline) 0 var(--c-separator)" });
    expect(exact(".ui-tsa-row-status")).toMatchObject({ height: "20px", "border-radius": "999px", background: "var(--c-fill-2)" });
  });



  it("空间常规：多语言每项一行，控件跨两行、最宽 360；窄屏回到上下", () => {
    expect(exact(".ui-ws-i18n-row > :nth-child(2)")).toMatchObject({ "grid-column": "2", "grid-row": "1 / span 2" });
    expect(exact(".ui-ws-i18n-row > select")).toMatchObject({ "max-width": "var(--group-control-max)" });
    expect(exact(".ui-ws-i18n-row > :nth-child(2)", /max-width:\s*767\.98px/)).toMatchObject({ "grid-column": "1", "grid-row": "auto" });
  });

  it("Space access uses grouped headings and bordered forms", () => {
    expect(exact(".ui-members-group.ui-section-card")).toMatchObject({ padding: "0", background: "transparent", "box-shadow": "none" });
    expect(exact(".ui-members-add-row")).toMatchObject({ "border-radius": "var(--r-4)", "box-shadow": "0 0 0 var(--hairline) var(--c-separator)" });
  });
});

describe("调用点", () => {
  it("space general: name and description sit in a group card and the save bar saves", () => {
    const src = read("components/settings/space-general.tsx");
    expect(src).toContain('<section className="ui-group ui-space-general-group"');
    // Since 3e922b6aa the save bar renders only once metadata has loaded, tracks per-field dirtiness,
    // and stays disabled for read-only viewers.
    expect(src).toMatch(/\{loading \? \(\s*<LoadingSkeleton[^]*?\) : metadata \? \(/);
    expect(src).toMatch(
      /<SaveBar\s+dirty=\{dirtyFields\.length > 0\}\s+saving=\{saving\}\s+saveDisabled=\{!name\.trim\(\) \|\| readOnly\}\s+onSave=\{\(\) => void save\(\)\}/,
    );
    expect(src).not.toContain("style={{");
  });

  it("space i18n: the feature toggle is an immediately effective Switch; no inline styles", () => {
    const src = read("components/settings/workspace-i18n.tsx");
    expect(src).toMatch(/<span className="ui-ws-i18n-switch">\s*<Switch/);
    expect(src).not.toContain("style={{");
  });

  it("spaces: the create area is a group card; list and filters show the selected chips; no inline styles", () => {
    const src = read("components/settings/team-spaces-admin.tsx");
    expect(src).toContain('<section className="ui-group ui-tsa-create"');
    expect(src).toContain("<ActiveFilterChips filters={activeFilters}");
    expect(src).toContain('<LoadingSkeleton label={t("loading")} />');
    expect(src).not.toMatch(/sectionTitleStyle|alignedControlStyle|padding: "12px 14px"/);
  });

  it("成员：搜索与角色都生效时出已选 chip；首次加载用骨架", () => {
    const src = read("components/settings/people.tsx");
    expect(src).toMatch(/<ActiveFilterChips filters=\{\[\s*\.\.\.\(memberSearch\.trim\(\)/);
    expect(src).toContain('<LoadingSkeleton rows={5} label={t("loading")} />');
  });

});
