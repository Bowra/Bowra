import { afterEach, describe, expect, it } from "vitest";

import { authoringSkillDescription, authoringSkillMatches, authoringSkillTitle, documentSkillGroup, skillDescription, skillMatches, skillTitle } from "./skills-meta";
import { localizedToolText, toolDisplay } from "./tool-display";
import { toolLabel } from "./tool-labels";
import { getToolMeta } from "@/components/chat/step-list/tool-catalog";

afterEach(() => { document.documentElement.lang = ""; });

const builtin = { name: "canvas-pptx", title: "Export report to PowerPoint", title_zh: "报告导出 PPT", source_type: "builtin" as const, description: "Export a report." };

describe("document catalog presentation groups", () => {
  it.each(["docx", "pdf", "pptx", "xlsx", "doc-coauthoring"])("keeps %s with the document guides", (name) => {
    expect(documentSkillGroup(name)).toBe("guides");
  });

  it.each(["docx", "pdf", "pptx", "xlsx"])("separates creation and report export for %s", (format) => {
    expect(documentSkillGroup(`bowra-${format}`)).toBe("create");
    expect(documentSkillGroup(`canvas-${format}`)).toBe("export");
  });
});

describe("built-in display names", () => {
  it.each([
    ["en-US", "Create New Presentation", "Edit Existing Presentation"],
    ["zh-CN", "创建新演示稿", "编辑已有 PPT"],
  ])("distinguishes new decks from private edits across %s display surfaces", (locale, createLabel, editLabel) => {
    document.documentElement.lang = locale;
    for (const [name, label] of [["render_deck", createLabel], ["template_document_submit", editLabel]]) {
      expect(toolDisplay(name).label).toBe(label);
      expect(toolLabel(name).name).toBe(label);
      expect(getToolMeta(name)?.name).toBe(label);
    }
  });

  it("uses English by default and keeps explicit Chinese titles searchable", () => {
    document.documentElement.lang = "en-US";
    expect(skillTitle(builtin)).toBe("Export report to PowerPoint");
    document.documentElement.lang = "zh-CN";
    expect(skillTitle(builtin)).toBe("报告导出 PPT");
    expect(skillMatches(builtin, "导出")).toBe(true);
    expect(skillMatches(builtin, "PowerPoint")).toBe(true);
  });

  it("preserves user-authored titles and unknown slugs", () => {
    for (const locale of ["en-US", "zh-CN"]) {
      document.documentElement.lang = locale;
      expect(skillTitle({ ...builtin, source_type: "user_upload", title: "我的演示模板" })).toBe("我的演示模板");
      expect(skillTitle({ name: "custom-id", title: "" })).toBe("Custom id");
    }
  });

  it("localizes built-in authoring labels without changing team names", () => {
    const entry = { name: builtin.name, display_name: builtin.title, display_name_zh: builtin.title_zh, source_kind: "builtin" as const };
    document.documentElement.lang = "en-US";
    expect(authoringSkillTitle(entry)).toBe(builtin.title);
    document.documentElement.lang = "zh-CN";
    expect(authoringSkillTitle(entry)).toBe(builtin.title_zh);
    expect(authoringSkillTitle({ ...entry, source_kind: "team", display_name: "My team Skill" })).toBe("My team Skill");
  });

  it("selects explicit Chinese Tool fields and supports older catalog responses", () => {
    const tool = { display_name: "Query data", description: "Read rows.", display_name_zh: "查询数据", description_zh: "读取数据。" };
    document.documentElement.lang = "en-US";
    expect(localizedToolText(tool)).toEqual({ name: "Query data", description: "Read rows." });
    document.documentElement.lang = "zh-CN";
    expect(localizedToolText(tool)).toEqual({ name: "查询数据", description: "读取数据。" });
    expect(localizedToolText({ display_name: "用户工具", description: "原始描述" })).toEqual({ name: "用户工具", description: "原始描述" });
  });

  it("shows reviewed built-in descriptions in the interface language instead of model text", () => {
    const listed = {
      ...builtin,
      description: "Export Canvas (ReportSection[] JSON). Use for '导出 PPT'.",
      description_en: "Exports the report on the Canvas as an editable PowerPoint deck.",
      description_zh: "把画布上的报告导出为可编辑的 PowerPoint 演示文稿。",
      description_de: "Exportiert den Bericht im Canvas als PowerPoint-Präsentation.",
      description_fr: "Exporte le rapport du Canvas en présentation PowerPoint.",
    };
    expect(skillDescription(listed, "zh-CN")).toBe(listed.description_zh);
    expect(skillDescription(listed, "en-US")).toBe(listed.description_en);
    expect(skillDescription(listed, "de-DE")).toBe(listed.description_de);
    expect(skillDescription(listed, "fr-FR")).toBe(listed.description_fr);
    // A missing translation falls back to English listing text, never to the model-facing description.
    expect(skillDescription({ ...listed, description_fr: "" }, "fr-FR")).toBe(listed.description_en);
    // Older servers and personal overrides carry no listing fields.
    expect(skillDescription(builtin, "de-DE")).toBe(builtin.description);
    expect(skillDescription({ ...listed, source_type: "user_upload" }, "zh-CN")).toBe(listed.description);

    const entry = { ...listed, display_name: listed.title, display_name_zh: listed.title_zh, source_kind: "builtin" as const };
    expect(authoringSkillDescription(entry, "de-DE")).toBe(listed.description_de);
    expect(authoringSkillDescription({ ...entry, source_kind: "team", description: "Team text" }, "de-DE")).toBe("Team text");
  });

  it("searches the localized title and description of the current interface language", () => {
    const listed = {
      name: "analyst-sop-period-compare",
      display_name: "Period comparison",
      display_name_zh: "周期对比分析",
      source_kind: "builtin" as const,
      description: "Compare metric across periods (YoY/QoQ/MoM/WoW) with time series + dim breakdown.",
      description_en: "Compares a metric across periods, such as year over year.",
      description_zh: "对比指标在不同周期的表现（同比、环比等）。",
      description_de: "Vergleicht eine Kennzahl über Zeiträume, etwa zum Vorjahr.",
      description_fr: "Compare un indicateur entre périodes, par exemple sur un an.",
    };
    expect(authoringSkillMatches(listed, "周期对比", "zh-CN")).toBe(true);
    expect(authoringSkillMatches(listed, "环比", "zh-CN")).toBe(true);
    expect(authoringSkillMatches(listed, "vorjahr", "de-DE")).toBe(true);
    expect(authoringSkillMatches(listed, "PÉRIODES", "fr-FR")).toBe(true);
    expect(authoringSkillMatches(listed, "period-compare", "fr-FR")).toBe(true);
    // Model-facing internals are not a search surface.
    expect(authoringSkillMatches(listed, "dim breakdown", "en-US")).toBe(false);
    expect(authoringSkillMatches(listed, "vorjahr", "fr-FR")).toBe(false);

    const summary = { ...listed, title: listed.display_name, title_zh: listed.display_name_zh, source_type: "builtin" as const };
    expect(skillMatches(summary, "Kennzahl", "de-DE")).toBe(true);
    expect(skillMatches(summary, "周期对比分析", "zh-CN")).toBe(true);
    expect(skillMatches(summary, "time series", "en-US")).toBe(false);
  });
});
