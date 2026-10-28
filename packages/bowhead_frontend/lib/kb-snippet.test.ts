import { describe, expect, it } from "vitest";

import { quoteLines, snippetWindow } from "./kb-snippet";

const TABLE = "| 2025-09-15 | 浦东张杨路店 | 酒饮 | 13828 | 830 |\n".repeat(20) + "| 2025-09-17 | 滨江龙湖天街店 | 进口水果 | 236,480 | 4121 |\n";

describe("snippetWindow", () => {
  it("语义查询：以最长的连续匹配为中心（116：命中行在片段深处）", () => {
    const w = snippetWindow(TABLE, "滨江龙湖天街店进口水果销售额");
    expect(w.match).toBe("滨江龙湖天街店");
    expect(w.after).toContain("进口水果");
  });

  it("整句出现时高亮整句；大小写不敏感", () => {
    expect(snippetWindow("Q3 Oncology market grew", "oncology MARKET").match).toBe("Oncology market");
  });

  it("没有任何两字以上的匹配：显示开头", () => {
    const w = snippetWindow(TABLE, "火星");
    expect(w.match).toBe("");
    expect(w.before).toBe(TABLE.slice(0, 40));
  });
});

describe("quoteLines", () => {
  const HEADER = "| 日期 | 门店 | 品类 | 销售额 | 客流 |\n| --- | --- | --- | --- | --- |\n";
  const BODY = HEADER + TABLE;

  it("表格片段：窗口落在命中行附近并补表头，命中处高亮，超出以省略号收尾", () => {
    const lines = quoteLines(BODY, "9 月 17 日滨江龙湖天街店进口水果销售额为 236,480 元", 8);
    expect(lines[0].before).toContain("日期");
    expect(lines[1].before).toContain("---");
    const hit = lines.find((l) => l.match);
    expect(hit?.match.trim()).toBe("236,480");
    expect(hit?.before).toContain("滨江龙湖天街店");
    expect(lines.length).toBeLessThanOrEqual(9);
  });

  it("没有匹配：从开头取 8 行", () => {
    const lines = quoteLines(BODY, "火星", 8);
    expect(lines.slice(0, 8).every((l) => !l.match)).toBe(true);
    expect(lines[0].before).toContain("日期");
    expect(lines[lines.length - 1].before).toBe("…");
  });

  it("只有两三个字的常见词对得上（如「销售」）：不据此定位、不打高亮，显示开头几行", () => {
    const body = ["第一行 概述", "第二行 门店名单", "第三行", "第四行", "第五行", "第六行", "第七行", "第八行", "第九行", "第十行 销售额汇总"].join("\n");
    const lines = quoteLines(body, "本月销售表现平稳", 8);
    expect(lines.every((l) => !l.match)).toBe(true);
    expect(lines[0].before).toBe("第一行 概述");
    expect(lines[lines.length - 1].before).toBe("…");
    // Control: matches of at least four characters still locate and highlight normally
    const hit = quoteLines(body, "请看销售额汇总", 8).find((l) => l.match);
    expect(hit?.match).toBe("销售额汇总");
  });

  it("短文本不补省略号", () => {
    expect(quoteLines("付款周期为 45 天", "付款周期")).toEqual([{ before: "", match: "付款周期", after: "为 45 天" }]);
  });
});
