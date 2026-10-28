/**
 * report-trust: counting criteria for three citation generations (Phase F adaptation batch).
 */

import { describe, expect, it } from "vitest";
import type { ReportSection } from "@/lib/types";
import { computeReportTrust, unverifiedCount } from "./report-trust";

const narrative = (markdown: string, extra?: Partial<ReportSection>): ReportSection =>
  ({ id: "s1", kind: "narrative", markdown, ...extra }) as ReportSection;

const sourcesSec = (n: number): ReportSection =>
  ({
    id: "src",
    kind: "sources",
    entries: Array.from({ length: n }, (_, i) => ({ label: `源 ${i + 1}` })),
  }) as unknown as ReportSection;

describe("computeReportTrust — gen3 单一口径 (citation-audit M1-4)", () => {
  it("退役 gen2 标记不再计数（发射端已停写；存量未迁移 → pct=null 不显 0%）", () => {
    const stats = computeReportTrust([
      narrative("营收 [num:962.7:亿元:src:tl_a] 与基准 [num:5.2:percent]"),
    ]);
    expect(stats.verified).toBe(0);
    expect(stats.unverified).toBe(0);
    expect(stats.verifiedPct).toBeNull();
  });

  it("unverified = 结构化 unverified_numbers 字段（非标记）", () => {
    const stats = computeReportTrust([
      narrative("预计将达 150亿元。", { unverified_numbers: ["150亿元"] } as Partial<ReportSection>),
    ]);
    expect(stats.verified).toBe(0);
    expect(stats.unverified).toBe(1);
    // C-P2-2: verified==0 → pct=null (omit misleading 0%)
    expect(stats.verifiedPct).toBeNull();
  });

  it("现行 Phase F: plain number + [N] → verified (N ≤ sources)", () => {
    const stats = computeReportTrust([
      narrative("营收 962.7 亿元 [1]，同比 +11.8% [2]。"),
      sourcesSec(2),
    ]);
    expect(stats.verified).toBe(2);
    expect(stats.sources).toBe(2);
    expect(stats.verifiedPct).toBe(100);
  });

  it("[N] 超出 sources 范围不计 (markdown 脚注/列表序号防御)", () => {
    const stats = computeReportTrust([
      narrative("见附录 [3]，营收 962.7 亿元 [1]"),
      sourcesSec(1),
    ]);
    expect(stats.verified).toBe(1);
  });

  it("无 sources section 时 [N] 全不计", () => {
    const stats = computeReportTrust([narrative("第 [1] 项与第 [2] 项")]);
    expect(stats.verified).toBe(0);
    expect(stats.verifiedPct).toBeNull();
  });

  it("非 string 字段的数组不被 [N] 误匹配 (string-walker, 非 JSON.stringify)", () => {
    const sec = {
      id: "c1",
      kind: "chart",
      spec: { title: "图", data: [5], rows: [{ v: [3] }] },
    } as unknown as ReportSection;
    const stats = computeReportTrust([sec, sourcesSec(9)]);
    expect(stats.verified).toBe(0);
  });

  it("narrative.unverified_numbers 计入 unverified, 嵌套 section 递归", () => {
    const nested = {
      id: "wrap",
      kind: "section",
      body: [narrative("X 增长 [1]", { unverified_numbers: ["5.2%"] } as Partial<ReportSection>)],
    } as unknown as ReportSection;
    const stats = computeReportTrust([nested, sourcesSec(1)]);
    expect(stats.verified).toBe(1);
    expect(stats.unverified).toBe(1);
    expect(stats.verifiedPct).toBe(50);
  });

  it("全空 → pct null (条隐藏)", () => {
    const stats = computeReportTrust([narrative("纯文字，没有任何 claim。")]);
    expect(stats.verifiedPct).toBeNull();
    expect(stats.sources).toBe(0);
  });
});

describe("可信度分母用真实条数", () => {
  const sources = { kind: "sources", entries: [{ label: "a" }] } as never;
  const md = Array.from({ length: 10 }, (_, i) => `数字${i} [1]`).join("、");
  const capped = Array.from({ length: 10 }, () => "毛估 X");

  it("★★ `unverified_total` 优先 —— 列表被截断不该把百分比抬高", () => {
    const stats = computeReportTrust([
      { kind: "narrative", markdown: md, unverified_numbers: capped, unverified_total: 30 } as never,
      sources,
    ]);
    expect(stats.unverified).toBe(30);
    expect(stats.verifiedPct).toBe(25);
  });

  it("负对照：存量段没有这个字段 ⇒ 退回列表长度，百分比虚高到 50%（夹具能区分两种算法）", () => {
    const stats = computeReportTrust([
      { kind: "narrative", markdown: md, unverified_numbers: capped } as never,
      sources,
    ]);
    expect(stats.unverified).toBe(10);
    expect(stats.verifiedPct).toBe(50);
  });

  it("★ 真实条数比列表还短 ⇒ 数据坏了，按列表长度算（只朝更保守兜底）", () => {
    const stats = computeReportTrust([
      { kind: "narrative", markdown: "数字 [1]", unverified_numbers: ["a", "b", "c"], unverified_total: 1 } as never,
      sources,
    ]);
    expect(stats.unverified).toBe(3);
  });
});

describe("unverifiedCount —— 与后端逐条同口径", () => {
  // ⚠ These fixtures match Python's `test_unverified_count_matches_the_frontend_predicate`
  // **Verbatim**. Changes on either side must update the other.
  const CASES: Array<[Record<string, unknown>, number]> = [
    [{ unverified_numbers: Array(10).fill("a"), unverified_total: 30 }, 30],
    [{ unverified_numbers: Array(10).fill("a") }, 10],
    [{ unverified_numbers: [], unverified_total: 30 }, 0],
    [{ unverified_numbers: ["a", "b", "c"], unverified_total: 1 }, 3],
    [{ unverified_numbers: Array(3).fill("a"), unverified_total: 30.0 }, 30],
    [{ unverified_numbers: Array(3).fill("a"), unverified_total: 30.5 }, 3],
    [{ unverified_numbers: ["a"], unverified_total: 1e21 }, 1],
    [{ unverified_numbers: ["a"], unverified_total: true }, 1],
    [{ unverified_numbers: ["a"], unverified_total: "30" }, 1],
    [{}, 0],
  ];
  it.each(CASES)("%j → %i", (section, expected) => {
    expect(unverifiedCount(section)).toBe(expected);
  });

  it("★★★ 列表清空后残留的 total 不许被采信 —— 否则 100% 反向崩成 3%", () => {
    const stats = computeReportTrust([
      { kind: "narrative", markdown: "新正文 [1]", unverified_numbers: [], unverified_total: 30 } as never,
      { kind: "sources", entries: [{ label: "a" }] } as never,
    ]);
    expect(stats.unverified).toBe(0);
    expect(stats.verifiedPct).toBe(100);
  });
});
