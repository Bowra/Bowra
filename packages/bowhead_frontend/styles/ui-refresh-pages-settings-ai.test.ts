/**
 * Interface appearance, page-by-page acceptance A · team-settings AI and Data / Routine Management groups:
 * data sources, tools, system-built-in Agents, team workflows and scheduled tasks.
 *
 * Source contracts (jsdom does not compute cascades; real-browser screenshots verify pixels):
 *   1. Every selector in the mapping (styles/ui-refresh-pages-settings.css) starts with :root:root,
 * without literal colors or --c-line;
 *   2. Call sites: row actions live in “⋯”, and moved inline styles are absent.
 */
import { describe, expect, it } from "vitest";

import {
  decls,
  isTokenRoot,
  LITERAL_COLOR,
  norm,
  parseRules,
  PREFIX,
  read,
  readSection,
  splitSelectors,
  stripComments,
} from "./css-contract";

const MAP_RULES = parseRules(stripComments(readSection("styles/ui-refresh-pages-settings.css", "accept-a:ai")));
const LEGACY_RULES = parseRules(stripComments(readSection("styles/ui-controls-legacy.css", "accept-a:ai")));

function on(sel: string, media: RegExp | null = null): Record<string, string> {
  const found = MAP_RULES.filter(
    (r) => (media ? media.test(r.media ?? "") : r.media === null) && splitSelectors(r.selector).includes(`${PREFIX} ${norm(sel)}`),
  );
  expect(found.length, sel).toBeGreaterThan(0);
  return Object.assign({}, ...found.map((r) => decls(r.body)));
}

describe("styles/ui-refresh-pages-settings.css: every rule starts with the :root:root prefix", () => {
  it("解析出规则", () => {
    expect(MAP_RULES.length).toBeGreaterThan(60);
  });

  it("every selector starts with :root:root (inside @media too)", () => {
    expect(MAP_RULES.flatMap((r) => splitSelectors(r.selector)).filter((s) => !s.startsWith(PREFIX))).toEqual([]);
  });

  it("不写字面色值、不用 --c-line", () => {
    const bodies = MAP_RULES.filter((r) => !splitSelectors(r.selector).every(isTokenRoot)).map((r) => r.body);
    expect(bodies.filter((b) => LITERAL_COLOR.test(b))).toEqual([]);
    expect(bodies.filter((b) => /var\(--c-line\)/.test(b))).toEqual([]);
  });

  it("the base-layer section carries no hook prefix", () => {
    expect(LEGACY_RULES.flatMap((r) => splitSelectors(r.selector)).filter((s) => s.startsWith(":root"))).toEqual([]);
  });
});

describe("共用：页头（唯一主操作在右）、分区标题、卡片列表", () => {
  it("页头 flex 换行、底对齐，主操作靠右；说明不再自带下边距", () => {
    expect(on(".ai-page-head")).toMatchObject({ display: "flex", "flex-wrap": "wrap", "align-items": "flex-end", margin: "0 0 24px" });
    expect(on(".ai-page-head-actions")).toMatchObject({ "margin-left": "auto" });
    expect(on(".ai-page-head .ui-page-desc")).toMatchObject({ "margin-bottom": "0" });
  });

  it("卡片列表间距 8；分区标题到内容 8", () => {
    expect(on(".ai-card-list")).toMatchObject({ gap: "8px" });
    expect(on(".ai-section-title")).toMatchObject({ margin: "0 0 8px" });
  });
});

describe("数据源", () => {
  it("连接卡片：内容面 + 发丝线外框 + --r-4；名称改控件字 14/600；状态为胶囊", () => {
    expect(on(".ai-source-card")).toMatchObject({
      border: "0",
      "border-radius": "var(--r-4)",
      background: "var(--c-surface)",
      "box-shadow": "0 0 0 var(--hairline) var(--c-separator)",
    });
    expect(on(".ai-source-name")).toMatchObject({ "font-family": "var(--font-control)", "font-weight": "600" });
    expect(on(".ai-source-status.is-active")).toMatchObject({ background: "var(--c-accent-soft)", color: "var(--c-accent-text)" });
  });

  it("call site: the add button sits in the page header; card actions live in the ⋯ menu", () => {
    const src = read("components/settings/datarc-connections.tsx");
    expect(src).toContain('<div className="ai-page-head-actions">{addButton}</div>');
    expect(src).toMatch(/<RowActionsMenu\s+label=\{t\("more_actions_for", \{ name: conn\.name \}\)\}/);
    expect(src).not.toMatch(/style=\{\{ (maxWidth: 720|fontSize: 15, margin|padding: 14, border)/);
  });
});

describe("工具", () => {
  it("工具源卡片与内置工具行：发丝线外框；失败为 1px 错误色描边", () => {
    expect(on(".ai-tools-card")).toMatchObject({ border: "0", "border-radius": "var(--r-4)" });
    expect(on(".ai-tools-card.is-failed")).toMatchObject({ "box-shadow": "0 0 0 1px var(--c-err)" });
    expect(on(".ai-tools-builtin")).toMatchObject({ border: "0", "box-shadow": "0 0 0 var(--hairline) var(--c-separator)" });
  });

  it("操作列表小标题不再等宽大写；搜索框不撑满，左侧留出图标位", () => {
    expect(on(".ai-tools-ops-label")).toMatchObject({ "font-family": "var(--font-control)", "text-transform": "none", "letter-spacing": "normal" });
    expect(on(".ai-tools-search")).toMatchObject({ "max-width": "360px" });
    expect(on(".ai-tools-search > input.ai-tools-search-input.ai-tools-search-input")).toMatchObject({ "padding-left": "32px" });
  });

  it("调用点：旧的 iconBtn 常量与卡片内联样式已搬走", () => {
    const src = read("components/settings/tools/tools-manager.tsx");
    expect(src).not.toContain("const iconBtn");
    expect(src).not.toMatch(/style=\{\{ (maxWidth: 900|marginBottom: 14, padding: "14px 16px"|position: "relative", marginBottom: 14)/);
    expect(src).toContain('className={`legacy-ai-tools-card ai-tools-card${failed ? " is-failed" : ""}${off ? " is-off" : ""}`}');
  });
});

describe("系统内置智能体与团队流程", () => {
  it("卡片发丝线外框；徽标为胶囊；底栏分隔为发丝线；计数改普通字", () => {
    expect(on(".ai-sa-card")).toMatchObject({ border: "0", "border-radius": "var(--r-4)" });
    expect(on(".ai-sa-chip")).toMatchObject({ height: "20px", "border-radius": "999px", "font-family": "var(--font-control)" });
    expect(on(".ai-sa-foot")).toMatchObject({ "border-top": "0", "box-shadow": "inset 0 var(--hairline) 0 var(--c-separator)" });
    expect(on(".ai-count")).toMatchObject({ "font-family": "var(--font-control)", color: "var(--c-ink-3)" });
    expect(on(".ai-wf-card.is-approval")).toMatchObject({ border: "0", "box-shadow": "0 0 0 1px color-mix(in srgb, var(--c-warn) 45%, transparent)" });
  });

  it("call site: agent cards keep only Configure; workflow share / toggle / delete live in the ⋯ menu (delete in red)", () => {
    const sa = read("components/settings/builtin-subagents-section.tsx");
    expect(sa).toMatch(/<RowActionsMenu\s/);
    // The row binds the localized agent name once (4886dd248) and reuses it for every accessible label.
    expect(sa).toContain("const name = builtinName(item, agentLabels);");
    expect(sa).toContain('label={t("more_actions_for", { name })}');
    const wf = read("app/(app)/team/settings/workflows/page.tsx");
    expect(wf).toContain('{ id: "delete", label: t("action_delete"), danger: true, onSelect: () => onDelete(r) }');
    expect(wf).toContain('...(r.approval === "approved" ? [{ id: "share", label: t("action_share"), onSelect: () => onShare(r) }] : [])');
    expect(wf).not.toMatch(/fg: "#7a5e1c"/);
  });
});

describe("定时任务", () => {
  it("面包屑改普通字；标题旁计数为中性徽标；行卡片发丝线外框；运行记录小标题不再大写", () => {
    expect(on(".ai-sched-eyebrow")).toMatchObject({ "font-family": "var(--font-control)", "text-transform": "none", "letter-spacing": "normal" });
    expect(on(".ai-sched-count")).toMatchObject({ height: "20px", background: "var(--c-fill-2)", "font-variant-numeric": "tabular-nums" });
    expect(on(".ai-sched-routine")).toMatchObject({ border: "0", "border-radius": "var(--r-4)" });
    expect(on(".ai-sched-history-head")).toMatchObject({ "text-transform": "none" });
    expect(on(".ai-sched")).toMatchObject({ gap: "12px" });
  });

  it("call site: New scheduled task uses the shared primary button; the moved S entries are gone", () => {
    const src = read("components/settings/scheduled.tsx");
    expect(src).toMatch(/<div className="ai-page-head-actions">\s*<Button\s+variant="primary"/);
    expect(src).not.toMatch(/\n {2}(head|eyebrow|routine|rRow|expandBtn|historyHead|emptyIco): \{/);
  });
});
