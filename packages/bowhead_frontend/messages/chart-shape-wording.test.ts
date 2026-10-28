/**
 * Shape-mismatch copy's **fully assembled wording** (chart-config-ux ⑦ follow-up to 116 dogfood).
 *
 * Component tests assert which key and parameters were used, without checking **whether the sentence reads correctly**.
 * Real 116 device output:
 *
 *     这份数据画不出双轴：还差1 个数值列。
 *
 * Two defects: no Chinese spacing before the number (space placed in the latter half of `{n} 个数值列`),
 * and English `column(s)` avoiding proper singular/plural. These cases render real messages through ICU, then inspect the wording.
 */
import IntlMessageFormat from "intl-messageformat";
import { describe, expect, it } from "vitest";

import en from "./en-US.json";
import zh from "./zh-CN.json";

type Fallback = Record<string, string>;
const ZH = (zh as unknown as { chart_block: { fallback: Fallback; kind: Record<string, string> } }).chart_block;
const EN = (en as unknown as { chart_block: { fallback: Fallback; kind: Record<string, string> } }).chart_block;

function render(locale: string, msg: string, values: Record<string, unknown> = {}): string {
  return String(new IntlMessageFormat(msg, locale).format(values));
}

/** Assemble exactly as the component does: `missing` joins several gaps with `and`. */
function sentence(
  locale: string,
  ns: { fallback: Fallback; kind: Record<string, string> },
  kind: string,
  gaps: Array<{ key: "need_dims" | "need_numeric"; n: number }>,
): string {
  const missing = gaps.map((g) => render(locale, ns.fallback[g.key]!, { n: g.n })).join(ns.fallback.and!);
  return render(locale, ns.fallback.shape_mismatch!, { kind: ns.kind[kind] ?? kind, missing });
}

describe("zh-CN 成句", () => {
  it("★ 数字两边有空格，不再是「还差1 个数值列」", () => {
    const s = sentence("zh-CN", ZH, "dual_axis", [{ key: "need_numeric", n: 1 }]);
    expect(s).toBe("这份数据画不出「双轴」：还差 1 个数值列。");
    expect(s).not.toContain("还差1");
  });

  it("★ 图种名加了引号——标签一半带「图」一半不带，直接拼读着不顺", () => {
    // 饼图 → “「饼图」”; 双轴 → “「双轴」”. Quotation marks make both form complete sentences.
    expect(sentence("zh-CN", ZH, "pie", [{ key: "need_dims", n: 1 }])).toContain("「饼图」");
    expect(sentence("zh-CN", ZH, "scatter", [{ key: "need_numeric", n: 1 }])).toContain("「散点」");
  });

  it("两个缺口用「、」连", () => {
    const s = sentence("zh-CN", ZH, "combo", [
      { key: "need_dims", n: 1 },
      { key: "need_numeric", n: 2 },
    ]);
    expect(s).toBe("这份数据画不出「柱+折」：还差 1 个分类/时间列、2 个数值列。");
  });
});

describe("en-US 成句", () => {
  it("★ 单复数走 ICU plural，不再是 `column(s)`", () => {
    expect(sentence("en-US", EN, "dual_axis", [{ key: "need_numeric", n: 1 }])).toBe(
      "This data can't be drawn as Dual Axis — it needs 1 more numeric column.",
    );
    expect(sentence("en-US", EN, "dual_axis", [{ key: "need_numeric", n: 2 }])).toContain(
      "2 more numeric columns",
    );
    for (const s of [
      sentence("en-US", EN, "pie", [{ key: "need_dims", n: 1 }]),
      sentence("en-US", EN, "pie", [{ key: "need_dims", n: 3 }]),
    ]) {
      expect(s).not.toContain("(s)");
    }
  });

  it("两个缺口用 and 连", () => {
    expect(
      sentence("en-US", EN, "combo", [
        { key: "need_dims", n: 1 },
        { key: "need_numeric", n: 2 },
      ]),
    ).toContain("1 more category/time column and 2 more numeric columns");
  });
});

describe("两个 locale 的键必须齐平", () => {
  it("fallback 下的键一一对应", () => {
    expect(Object.keys(ZH.fallback).sort()).toEqual(Object.keys(EN.fallback).sort());
  });
});
