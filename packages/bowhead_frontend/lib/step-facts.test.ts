/**
 * Turn visibility FR3/FR4: step facts are worded in the current interface language, and anything this
 * version does not recognise falls back to the stored text (the functions return null).
 */
import { afterEach, describe, expect, it } from "vitest";

import { readStepFacts, stepFactsHeader, stepFactsLabel, stepFactsSummary } from "./step-facts";

function inLocale<T>(locale: string, run: () => T): T {
  document.documentElement.lang = locale;
  return run();
}

afterEach(() => {
  document.documentElement.lang = "en-US";
});

describe("step facts", () => {
  it("words an attachment read in every interface language", () => {
    const started = readStepFacts({ v: 1, kind: "attachment_read", filename: "store_sales_2026H1.csv", size_bytes: 14827 });
    const done = readStepFacts({ v: 1, kind: "attachment_read", chars_read: 5017, truncated: true });
    expect(inLocale("zh-CN", () => [stepFactsHeader(started), stepFactsLabel(started), stepFactsSummary(done)])).toEqual([
      "读取附件",
      "store_sales_2026H1.csv",
      "已读入前 5,017 字",
    ]);
    expect(inLocale("en-US", () => stepFactsSummary(done))).toBe("Read the first 5,017 characters");
    expect(inLocale("de-DE", () => [stepFactsHeader(started), stepFactsSummary(done)])).toEqual(["Anhang lesen", "Erste 5.017 Zeichen gelesen"]);
    expect(inLocale("fr-FR", () => stepFactsHeader(started))).toBe("Lire la pièce jointe");
    expect(inLocale("zh-CN", () => stepFactsSummary(readStepFacts({ v: 1, kind: "attachment_read", chars_read: 120, truncated: false })))).toBe("已读取");
  });

  it("names chart kinds with the interface's chart names instead of codes", () => {
    const added = readStepFacts({ v: 1, kind: "canvas_added", section_kind: "hbar" });
    expect(inLocale("zh-CN", () => stepFactsSummary(added))).toBe("已添加 · 横向柱状");
    expect(inLocale("en-US", () => stepFactsSummary(added))).toBe("Added · Horizontal Bar");
    expect(inLocale("zh-CN", () => stepFactsSummary(readStepFacts({ v: 1, kind: "canvas_added", section_kind: "image" })))).toBe("已添加 · 图片");
    // A kind without a name keeps its code, as the stored text did.
    expect(inLocale("zh-CN", () => stepFactsSummary(readStepFacts({ v: 1, kind: "canvas_added", section_kind: "mystery" })))).toBe("已添加 · mystery");
    expect(inLocale("en-US", () => stepFactsSummary(readStepFacts({ v: 1, kind: "canvas_not_added", reason: "not_an_image" })))).toBe(
      "Not added · not an image",
    );
  });

  it("lists the types of the files a run saved", () => {
    const outputs = readStepFacts({ v: 1, kind: "code_outputs", count: 2, types: ["html", "json"] });
    expect(inLocale("zh-CN", () => stepFactsSummary(outputs))).toBe("运行成功 · 2 输出（HTML、JSON）");
    expect(inLocale("en-US", () => stepFactsSummary(outputs))).toBe("Ran OK · 2 outputs (HTML, JSON)");
    expect(inLocale("en-US", () => stepFactsSummary(readStepFacts({ v: 1, kind: "code_outputs", count: 1, types: ["pptx"] })))).toBe(
      "Ran OK · 1 output (PPTX)",
    );
    expect(inLocale("zh-CN", () => stepFactsSummary(readStepFacts({ v: 1, kind: "code_outputs", count: 0, types: [] })))).toBe("运行成功");
  });

  it("names a result lookup without enum codes and says a parse failure is being retried", () => {
    expect(inLocale("zh-CN", () => stepFactsLabel(readStepFacts({ v: 1, kind: "result_lookup", result_kind: "data_file" })))).toBe("数据文件");
    expect(inLocale("zh-CN", () => stepFactsLabel(readStepFacts({ v: 1, kind: "result_lookup", result_kind: "data_file", name: "sales" })))).toBe("sales");
    expect(inLocale("zh-CN", () => stepFactsLabel(readStepFacts({ v: 1, kind: "arguments_retry" })))).toBe("参数有误，正在重试");
    expect(inLocale("fr-FR", () => stepFactsLabel(readStepFacts({ v: 1, kind: "arguments_retry" })))).toBe("Arguments non valides, nouvel essai");
  });

  it("falls back to stored text for anything it does not recognise", () => {
    for (const value of [null, "text", { v: 2, kind: "attachment_read" }, { v: 1, kind: "new_kind" }, { v: 1, kind: "code_outputs", count: "2", types: [] }]) {
      const facts = readStepFacts(value);
      expect(facts).toBeNull();
      expect([stepFactsHeader(facts), stepFactsLabel(facts), stepFactsSummary(facts)]).toEqual([null, null, null]);
    }
    // Facts of a phase that has nothing to say leave that phase alone.
    expect(stepFactsSummary(readStepFacts({ v: 1, kind: "attachment_read", filename: "x.csv" }))).toBeNull();
    expect(stepFactsLabel(readStepFacts({ v: 1, kind: "code_outputs", count: 1, types: ["html"] }))).toBeNull();
  });
});
