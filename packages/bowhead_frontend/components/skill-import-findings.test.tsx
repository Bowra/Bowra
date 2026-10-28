import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import type { SkillImportFinding, SkillImportReport } from "@/lib/api";
import { SkillImportFindings } from "./skill-import-findings";

function f(id: string, severity: "red" | "yellow", zh: string, en = zh): SkillImportFinding {
  return {
    id,
    code: severity === "red" ? "tool_ref_unresolved" : "frontmatter_gap",
    severity,
    path: "SKILL.md",
    line: severity === "red" ? 12 : null,
    subject: id,
    resolved_against: "tool_registry",
    message_zh: zh,
    message_en: en,
  };
}

const REPORT: SkillImportReport = {
  grade: "red",
  red_count: 1,
  findings: [f("y1", "yellow", "黄一"), f("r1", "red", "红一", "red one"), f("y2", "yellow", "黄二"), f("y3", "yellow", "黄三")],
};

/** review P1-1: enough red findings to push buttons offscreen—red findings must collapse too. */
const MANY_REDS: SkillImportReport = {
  grade: "red",
  red_count: 12,
  findings: Array.from({ length: 12 }, (_, i) => f(`r${i}`, "red", `红${i}`)),
};

/** review P1-2: the report was truncated—the array is a subset; `red_count` is the actual pre-truncation count.
 *  Use a shape the **server actually produces**: red findings come first and truncation removes from the tail, so when reds are cut, no yellows remain
 *  (re-review caught my previous fixture inventing a shape the server cannot produce). */
const TRUNCATED: SkillImportReport = {
  grade: "red",
  red_count: 40,
  truncated: true,
  findings: [f("r1", "red", "红一"), f("r2", "red", "红二"), f("r3", "red", "红三")],
};

afterEach(cleanup);

describe("SkillImportFindings", () => {
  it("阻断项排在提示前面，与服务端给的顺序无关", () => {
    renderIntl(<SkillImportFindings report={REPORT} locale="zh-CN" />);
    const texts = screen.getAllByText(/红一|黄一|黄二/).map((n) => n.textContent);
    expect(texts[0]).toBe("红一");
  });

  it("提示折叠，展开后全部可见", () => {
    renderIntl(<SkillImportFindings report={REPORT} locale="zh-CN" />);
    expect(screen.queryByText("黄三")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /还有 1 条/ }));
    expect(screen.getByText("黄三")).toBeTruthy();
  });

  it("按界面语言取服务端两份文案中的一份，前端不做翻译", () => {
    renderIntl(<SkillImportFindings report={REPORT} locale="en-US" />, { locale: "en-US" });
    expect(screen.getByText("red one")).toBeTruthy();
    expect(screen.queryByText("红一")).toBeNull();
  });

  it("没有 findings 时什么都不渲染", () => {
    const { container } = renderIntl(<SkillImportFindings report={{ findings: [] }} locale="zh-CN" />);
    expect(container.querySelector(".imp-list")).toBeNull();
  });
});

describe("条数很多与报告被裁（review P1-1 / P1-2）", () => {
  it("红也折叠，不把按钮顶出可视区", () => {
    renderIntl(<SkillImportFindings report={MANY_REDS} locale="zh-CN" />);
    expect(screen.queryByText("红11")).toBeNull();
    expect(screen.getByRole("button", { name: /还有 9 条/ })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /还有 9 条/ }));
    expect(screen.getByText("红11")).toBeTruthy();
  });

  it("被裁过的报告按 red_count 说「至少」，不按数组少报", async () => {
    const { skillImportSummary } = await import("./skill-import-findings");
    let text = "";
    function Probe() {
      const t = (require("next-intl") as typeof import("next-intl")).useTranslations("skill_import");
      text = skillImportSummary(TRUNCATED, t);
      return null;
    }
    renderIntl(<Probe />);
    expect(text).toContain("至少 40");
    // Yellows are always cut first, so never mention them when truncated—saying “0 more hints” asserts a fact that was truncated away.
    expect(text).not.toContain("提示");
  });

  it("裁到一条红都不剩时，不能说「不影响使用」", async () => {
    const { skillImportSummary } = await import("./skill-import-findings");
    let text = "";
    function Probe() {
      const t = (require("next-intl") as typeof import("next-intl")).useTranslations("skill_import");
      text = skillImportSummary({ grade: "red", red_count: 7, truncated: true, findings: [] }, t);
      return null;
    }
    renderIntl(<Probe />);
    expect(text).toContain("至少 7");
    expect(text).not.toContain("不影响使用");
    expect(text).not.toContain("提示");
  });
});

describe("没有报告时不下结论（re-review P3-8）", () => {
  it("skillImportSummary(null) 返回空串，而不是「0 条提示，不影响使用」", async () => {
    const { skillImportSummary } = await import("./skill-import-findings");
    let text = "x";
    function Probe() {
      const t = (require("next-intl") as typeof import("next-intl")).useTranslations("skill_import");
      text = skillImportSummary(null, t);
      return null;
    }
    renderIntl(<Probe />);
    expect(text).toBe("");
  });

  it("报告被裁时，展开按钮不许诺「全部」", () => {
    renderIntl(<SkillImportFindings report={{ ...TRUNCATED, findings: [...TRUNCATED.findings!, f("y1", "yellow", "黄一"), f("y2", "yellow", "黄二"), f("y3", "yellow", "黄三")] }} locale="zh-CN" />);
    expect(screen.getByRole("button", { name: /未全部列出/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /展开看全部/ })).toBeNull();
  });
});
