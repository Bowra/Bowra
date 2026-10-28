/**
 * Sprint µ — fail-safe stripInlineNumMarkers tests.
 *
 * Verifies raw `[num:V:U]` markers never leak to user via Markdown component.
 * Per discovery.md design error #2.
 *
 * citation-audit M3-1: delegate to single `citation-marks.stripLegacy` module (same semantics as backend
 * bake); **intent unchanged, shape changed** — former `13.9 亿元` (space before CJK unit) /
 * `1.67倍` (truncated) were private md.ts conventions differing from actual backend-persisted `13.9亿元` / no truncation
 * (debt 5 symptom). Update assertions to shared semantics; retain each original intent of
 * never exposing raw markers to users.
 */

import { describe, expect, it } from "vitest";

import { stripInlineNumMarkers } from "./md";

describe("stripInlineNumMarkers (Sprint µ fail-safe)", () => {
  it("strips unbound [num:V:U] to formatted plain text", () => {
    // M3-1: attach CJK units without space (same as backend format_marker_value)
    expect(stripInlineNumMarkers("营收 [num:13.9:亿元] 元")).toMatch(/13\.9亿元/);
    expect(stripInlineNumMarkers("增长 [num:0.132:%]")).toMatch(/0\.132%/);
  });

  it("strips bound [num:V:U:src:S] (no popover in plain Markdown)", () => {
    const out = stripInlineNumMarkers("营收 [num:13.9:亿元:src:tl_abc] 元");
    expect(out).not.toContain("[num:");
    expect(out).not.toContain(":src:");
  });

  it("ratio unit maps to 倍 (banned-unit leak guard)", () => {
    // Local dogfood 2026-05-10: peer LLM emitted [num:1.032:ratio] despite
    // prompt rule (µ-N2b). Frontend strips to user-friendly "1.032倍" not
    // "1.032 ratio" — ratio is meaningless to CN users. backend payload
    // validator rejects ratio in kpis[i].value (µ-N2c) but free-form prose
    // markers leak. This is the last-line defense.
    const out = stripInlineNumMarkers("库存周转率 [num:1.5:ratio]");
    expect(out).toContain("1.5倍");
    expect(out).not.toContain("ratio");
    expect(out).not.toContain("[num:");
  });

  it("rate unit also maps to 倍", () => {
    const out = stripInlineNumMarkers("增长 [num:2.5:rate]");
    expect(out).toContain("2.5倍");
    expect(out).not.toContain("rate");
  });

  it("ratio 长尾原样保留（bake 是永久文本，不做显示端截断）", () => {
    // M3-1 semantics change: private md.ts truncated >2 decimals to 1.67; shared module does not —
    // bake writes permanently persisted text; irreversible precision loss is worse than occasional long decimals; production census found
    // only one >3-decimal marker in migrated content (harmless). Truncation is a display decision, outside bake.
    const out = stripInlineNumMarkers("周转率 [num:1.6701526066063577:ratio]");
    expect(out).toBe("周转率 1.6701526066063577倍");
    expect(out).not.toContain("[num:");
  });

  it("% 符号形是显示形，绝不放大（0.66% 不是 66%）", () => {
    // Real M3-1 bug: old md.ts applied `|v|<=1 → ×100` to `%`, showing actual production corpus text
    // "同比增长仅 [num:0.66:%] 基本持平" as 66% in chat (canvas at the same time showed
    // 0.66% — the same number differing 100-fold across surfaces, actual debt 5 harm). All 15 production `%` markers with |v|≤1
    // were checked against context and were genuinely small percentages. Only `percent` **enum** (i18n-v1
    // contract = raw fraction) gets ×100.
    expect(stripInlineNumMarkers("同比增长仅 [num:0.66:%] 基本持平")).toBe("同比增长仅 0.66% 基本持平");
    expect(stripInlineNumMarkers("误差降至 [num:0.1:%] 以下")).toBe("误差降至 0.1% 以下");
    // percent enum fraction shape → ×100
    expect(stripInlineNumMarkers("同比 [num:0.0039:percent]")).toBe("同比 0.39%");
  });

  it("handles [num:V] no unit", () => {
    expect(stripInlineNumMarkers("count [num:42]")).toContain("42");
  });

  it("handles [num:V:] LLM trailing colon", () => {
    expect(stripInlineNumMarkers("[num:42:]")).toContain("42");
  });

  it("preserves text outside markers", () => {
    expect(stripInlineNumMarkers("**前 10** 月份: [num:1.5:%] 增长").startsWith("**前 10**")).toBe(true);
  });

  it("multiple markers in one string", () => {
    const out = stripInlineNumMarkers("A [num:1.0:%] B [num:2.5:元] C");
    expect(out).not.toContain("[num:");
    expect(out).toContain("A ");
    expect(out).toContain("B ");
    expect(out).toContain("C");
  });

  it("CNY scale formatting（raw 货币值必须缩放成人话）", () => {
    // M3-1 production regression fixed: M1 bake attached currency-enum suffix directly (`7016893124元`),
    // baking deposit KPI cards into 11 raw digits (before migration FE rendered ¥70.17亿). Original r3 intent
    // bake = FE formatNumValue semantics included scaling; M1 inherited only mandatory unit preservation.
    expect(stripInlineNumMarkers("[num:139000000:CNY]")).toBe("1.39亿元");
    expect(stripInlineNumMarkers("[num:50000:CNY]")).toBe("5.0万元");
    expect(stripInlineNumMarkers("[num:7016893124:CNY]")).toBe("70.17亿元");
    expect(stripInlineNumMarkers("[num:9999:CNY]")).toBe("9,999元");
    expect(stripInlineNumMarkers("[num:5200000:USD]")).toBe("520.0万美元");
  });

  it("highlight suffix removed", () => {
    const out = stripInlineNumMarkers("[num:42:%:src:tl_abc#highlight=foo bar]");
    expect(out).not.toContain("highlight");
    expect(out).not.toContain("[num:");
  });

  it("empty/null input safe", () => {
    expect(stripInlineNumMarkers("")).toBe("");
    expect(stripInlineNumMarkers("no markers here")).toBe("no markers here");
  });
});
