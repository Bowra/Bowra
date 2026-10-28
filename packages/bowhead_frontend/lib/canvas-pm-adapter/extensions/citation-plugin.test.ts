/**
 * findCitedNumberRanges — Phase F "numeric phrase + [N]" adjacency detection (pure function).
 */

import { describe, expect, it } from "vitest";
import {
  buildCiteResolver,
  findCiteMarkerRanges,
  findCitedNumberRanges,
  sourceLinkedAria,
} from "./citation-plugin";

describe("findCiteMarkerRanges —— [cite:N] 只藏不删", () => {
  // Background: `markdownToPMNodes` previously stripped this marker directly from Markdown,
  // truncating the round-trip without marking degraded ⇒ a user edit silently erased citation anchors
  // (observed in sec_f828f0c2af72 on 116 dogfood, 2026-08-15). Original text now enters the doc unchanged,
  // located by this function and hidden with decorations. See write-fidelity.test.ts for round-trip fidelity.
  const spans = (t: string) => findCiteMarkerRanges(t).map((r) => t.slice(r.from, r.to));

  it("定位单个 marker", () => {
    const t = "全年规模稳定。 [cite:1]";
    expect(spans(t)).toEqual(["[cite:1]"]);
    // Positions must be exact: decorations cut these ranges; one character off consumes body text
    expect(t.slice(findCiteMarkerRanges(t)[0].from)).toBe("[cite:1]");
  });

  it("一段里多个 marker 全部定位", () => {
    expect(spans("甲 [cite:1] 乙 [cite:23] 丙")).toEqual(["[cite:1]", "[cite:23]"]);
  });

  it("不误伤 [N] 与普通方括号（它们各有各的 pass / 本来就该显示）", () => {
    expect(spans("营收 962.7 亿元 [1]")).toEqual([]);
    expect(spans("数组 arr[0] 与 [cite] 都不算")).toEqual([]);
    expect(spans("[citex:1] 也不算")).toEqual([]);
  });

  it("lastIndex 不残留 —— 同一个全局正则连续调用必须稳定", () => {
    // The global regex is a module constant; failing to reset lastIndex misses matches on the second call.
    const t = "甲 [cite:1] 乙";
    expect(spans(t)).toEqual(["[cite:1]"]);
    expect(spans(t)).toEqual(["[cite:1]"]);
  });
});

describe("sourceLinkedAria", () => {
  it("describes linkage without claiming verification in both locales", () => {
    expect(sourceLinkedAria("42%", 3, "zh-CN")).toBe("数据 42%，关联来源 3");
    expect(sourceLinkedAria("42%", 3, "en-US")).toBe("Data 42%, linked to source 3");
  });
});

const tok = (text: string, r: { from: number; to: number }) => text.slice(r.from, r.to);

describe("findCitedNumberRanges", () => {
  it("数字+中文单位+[N]", () => {
    const t = "营收 962.7 亿元 [1]，环比改善。";
    const rs = findCitedNumberRanges(t, 3);
    expect(rs).toHaveLength(1);
    expect(tok(t, rs[0])).toBe("962.7 亿元");
    expect(rs[0].n).toBe(1);
  });

  it("百分比 + 符号", () => {
    const t = "同比 +11.8% [2]，毛利 38% [3]";
    const rs = findCitedNumberRanges(t, 3);
    expect(rs.map((r) => tok(t, r))).toEqual(["+11.8%", "38%"]);
  });

  it("千分位数字", () => {
    const t = "共 1,200 件 [1]";
    const rs = findCitedNumberRanges(t, 1);
    expect(tok(t, rs[0])).toBe("1,200 件");
  });

  it("N 超出 sources 长度不标", () => {
    expect(findCitedNumberRanges("营收 962.7 亿元 [5]", 2)).toHaveLength(0);
  });

  it("numberedLen 0 → 全不标", () => {
    expect(findCitedNumberRanges("营收 962.7 亿元 [1]", 0)).toHaveLength(0);
  });

  it("裸年份不标 (年份是 claim cite 不是 verified 数字)", () => {
    expect(findCitedNumberRanges("于 2025 年 [1] 启动", 3)).toHaveLength(0);
    expect(findCitedNumberRanges("2024 [2] 的复盘", 3)).toHaveLength(0);
  });

  it("无数字前缀的 [N] 不产生 vnum (但 [N] 本体仍由 numbered pass 装饰)", () => {
    expect(findCitedNumberRanges("结论见附录 [1]", 3)).toHaveLength(0);
  });

  it("连续多个 cited number 不重叠", () => {
    const t = "A 为 5 件 [1] B 为 7 件 [2]";
    const rs = findCitedNumberRanges(t, 2);
    expect(rs.map((r) => tok(t, r))).toEqual(["5 件", "7 件"]);
  });
});

// ── Frontend/backend parity (tech-design §5.1; fixture shared with backend test_citation_marks.py) ──

import parityFixture from "./__fixtures__/citation-parity.json";
import { stripLegacy } from "@/lib/citation-marks";

type ParityRow = { mode: string; input?: string; text: string; token?: string; n?: number };
const parityRows = (parityFixture as { rows: ParityRow[] }).rows;

describe("findCitedNumberRanges ↔ BE binder parity", () => {
  it("BE 产出的每一形状都被 FE 识别（verified-edit-guard 同函数守卫）", () => {
    // Bake rows only fix strip output (no token/n dimensions) → covered by the next describe.
    for (const row of parityRows.filter((r) => r.mode !== "bake")) {
      const n = row.n as number;
      const hits = findCitedNumberRanges(row.text, Math.max(n, 3));
      const hit = hits.find((h) => h.n === n && row.text.slice(h.from, h.to) === row.token);
      expect(hit, `parity row 未命中: ${JSON.stringify(row)} → ${JSON.stringify(hits.map((h) => row.text.slice(h.from, h.to)))}`).toBeTruthy();
    }
  });
});

describe("stripLegacy ↔ BE strip_legacy parity（M3-4 永久兜底文法双端互锁）", () => {
  it("同一 input 两端 bake 出同一文本", () => {
    const bakeRows = parityRows.filter((r) => r.mode === "bake");
    expect(bakeRows.length).toBeGreaterThan(0);
    for (const row of bakeRows) {
      expect(stripLegacy(row.input as string), `bake parity: ${JSON.stringify(row)}`).toBe(row.text);
    }
  });

  it("幂等：bake 过的文本再 bake 不变", () => {
    for (const row of parityRows.filter((r) => r.mode === "bake")) {
      expect(stripLegacy(row.text)).toBe(row.text);
    }
  });
});

// ── Dense parity comparison (machine gate for impl review P1-1) ─────────────────────────────
// The 26 manually chosen golden rows are **samples, not proof**: implementing formatting twice (Python/TS) inevitably
// diverges somewhere; confirmed backend f-string `.Nf`=round-half-even versus JS toFixed=half-away,
// `[num:12500:CNY]` → 1.2万元 versus 1.3万元; the ten-thousand tier hits a tie every 10,000 (everyday range), but the six
// currency golden cases hit none. This scans backend-generated dense samples (tie/tier boundaries,
// four currency families/nan/inf/prototype-chain keys), failing on any divergence.
import denseFixture from "./__fixtures__/citation-bake-dense.json";

describe("stripLegacy 密集对拍（BE 生成 golden）", () => {
  const denseRows = (denseFixture as { rows: Array<{ input: string; text: string }> }).rows;

  it("每一行 FE 输出 == BE 输出", () => {
    expect(denseRows.length).toBeGreaterThan(150);
    const diffs: string[] = [];
    for (const row of denseRows) {
      const fe = stripLegacy(row.input);
      if (fe !== row.text) diffs.push(`${row.input}: BE=${JSON.stringify(row.text)} FE=${JSON.stringify(fe)}`);
    }
    expect(diffs, `双端 bake 分家 ${diffs.length}/${denseRows.length} 处:\n${diffs.slice(0, 12).join("\n")}`).toEqual([]);
  });
});


describe("buildCiteResolver —— (段落, N) → sidecar 行（source-accessibility c2）", () => {
  const sections = [
    { id: "sec_a", turn_id: "t_1" },
    { id: "sec_b", turn_id: "t_2" },
    { id: "sec_no_turn" }, // legacy: turn_id NULL
    { id: "sec_chart", turn_id: "t_1" }, // Anchor target (visible in this view ⇒ retain anchor)
  ];
  const map = {
    t_1: [
      { seq: 1, kind: "data_ref", access_state: "linked_accessible", safe_label: "bi_query: 营业额" },
      { seq: 2, kind: "chart_section", access_state: "linked_labeled", safe_label: "客单价Top3", safe_anchor_id: "sec_chart" },
      // Non-OWNED: backend stripped safe_*, leaving only the triple; parser must consider it unrenderable
      { seq: 3, kind: "data_ref", access_state: "linked_restricted" },
    ],
    t_2: [{ seq: 1, kind: "data_ref", access_state: "linked_accessible", safe_label: "另一轮的 1 号" }],
  };

  it("按所在段落的 turn 解析 —— 同一个 N 在不同轮指向不同引用", () => {
    const resolve = buildCiteResolver(sections, map);
    expect(resolve("sec_a", 1)).toEqual({ label: "bi_query: 营业额" });
    expect(resolve("sec_b", 1)).toEqual({ label: "另一轮的 1 号" });
  });

  it("chart_section 带锚点（目标在本视图可见）", () => {
    const resolve = buildCiteResolver(sections, map);
    expect(resolve("sec_a", 2)).toEqual({ label: "客单价Top3", anchorId: "sec_chart" });
  });

  it("review F1：锚点目标不在本视图（hidden 被服务端剥 / superseded 不在本层）→ 丢锚保 label", () => {
    // Rendering an inert citation badge repeats the chat-side P1 just fixed; degrading to a hover-only chip is truthful.
    const without = sections.filter((s) => s.id !== "sec_chart");
    const resolve = buildCiteResolver(without, map);
    expect(resolve("sec_a", 2)).toEqual({ label: "客单价Top3" });
  });

  it("review F10：空数据恒返回模块级常量解析器（decoration 缓存 key 依赖 identity 稳定）", () => {
    expect(buildCiteResolver(sections, undefined)).toBe(buildCiteResolver([], {}));
  });

  it("非 OWNED（无 safe_label）/ 未知 seq / legacy 无 turn 段落 / 未知段落 → null（照旧藏）", () => {
    const resolve = buildCiteResolver(sections, map);
    expect(resolve("sec_a", 3)).toBeNull(); // Backend stripped label from restricted rows
    expect(resolve("sec_a", 99)).toBeNull();
    expect(resolve("sec_no_turn", 1)).toBeNull();
    expect(resolve("sec_ghost", 1)).toBeNull();
    expect(resolve(null, 1)).toBeNull();
  });

  it("sidecar 缺失 / 全空 → 恒 null（与现状完全一致）", () => {
    expect(buildCiteResolver(sections, undefined)("sec_a", 1)).toBeNull();
    expect(buildCiteResolver(sections, {})("sec_a", 1)).toBeNull();
    expect(buildCiteResolver(sections, { t_1: [] })("sec_a", 1)).toBeNull();
  });
});
