import { describe, expect, it } from "vitest";

import { reportChangesOf } from "./report-changes";

describe("reportChangesOf (chat running state R2.2)", () => {
  it("counts chapters and charts apart, leaves the report's frame out, and opens at the first chapter", () => {
    const changes = reportChangesOf({
      report_changes: {
        added: [
          { section_id: "sec_h1", kind: "h1", title: "年度汇报" },
          { section_id: "sec_a", kind: "narrative", title: " 核心结论 " },
          { section_id: "sec_c", kind: "chart", title: "月坪效前十" },
          { section_id: "sec_src", kind: "sources", title: "" },
        ],
        updated: [{ section_id: "sec_b", kind: "narrative", title: "门店排名" }],
        added_kinds: { h1: 1, narrative: 26, chart: 8, sources: 1 },
        updated_kinds: { narrative: 1 },
        added_omitted: 32,
      },
    });
    expect(changes).toMatchObject({
      added: { sections: 26, figures: 8 },
      updated: { sections: 1, figures: 0 },
      firstSectionId: "sec_a",
    });
    expect(changes?.sections[1]).toEqual({ sectionId: "sec_a", kind: "narrative", title: "核心结论" });
  });

  it("falls back to the entries when the counts are missing, and to the first change when only the frame changed", () => {
    expect(reportChangesOf({
      report_changes: { added: [{ section_id: "sec_a", kind: "narrative" }, { section_id: "sec_x" }], updated: [], added_omitted: 3 },
    })).toMatchObject({ added: { sections: 5, figures: 0 }, firstSectionId: "sec_a" });
    expect(reportChangesOf({
      report_changes: { added: [], updated: [{ section_id: "sec_h1", kind: "h1", title: "新标题" }], updated_kinds: { h1: 1 } },
    })).toMatchObject({ added: { sections: 0, figures: 0 }, updated: { sections: 0, figures: 0 }, firstSectionId: "sec_h1" });
  });

  it("reads nothing from earlier Turns, empty changes or malformed entries", () => {
    expect(reportChangesOf({ outputs: [{ kind: "report", count: 1 }] })).toBeNull();
    expect(reportChangesOf({ report_changes: { added: [], updated: [] } })).toBeNull();
    expect(reportChangesOf({ report_changes: { added: [{ title: "无编号" }, "sec_x", null], updated: "x" } })).toBeNull();
    expect(reportChangesOf(null)).toBeNull();
  });
});
