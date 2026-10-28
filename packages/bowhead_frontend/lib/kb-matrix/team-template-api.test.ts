import { describe, expect, it } from "vitest";

import type { KbDocTypeDTO } from "@/lib/kb-knowledge-tables-api";

import { type TeamTemplateDTO, indexEntriesBySource, publishStateOf } from "./team-template-api";

function dt(over: Partial<KbDocTypeDTO> & Pick<KbDocTypeDTO, "doc_type_id">): KbDocTypeDTO {
  return {
    workspace_id: "ws",
    name: "t",
    description: "",
    fields: [],
    record_count: 0,
    extractor_model: "qwen-plus",
    cross_verify_enabled: false,
    created_by: "u",
    created_at: "2026-07-01T00:00:00Z",
    updated_at: "2026-07-01T00:00:00Z",
    ...over,
  };
}

function entry(over: Partial<TeamTemplateDTO> & Pick<TeamTemplateDTO, "entry_id" | "source_doc_type_id">): TeamTemplateDTO {
  return {
    name: "t",
    description: "",
    field_count: 0,
    field_preview: [],
    source_workspace_id: "ws",
    source_workspace_name: null,
    published_by: "u",
    published_by_name: null,
    published_at: "2026-07-01T00:00:00Z",
    updated_at: "2026-07-01T00:00:00Z",
    ...over,
  };
}

describe("publishStateOf（我的模板卡发布态派生）", () => {
  it("无库条目 → none", () => {
    expect(publishStateOf(dt({ doc_type_id: "dt_1" }), undefined)).toBe("none");
  });
  it("有条目、发布后未改动（updated_at ≤ 条目）→ published", () => {
    const e = entry({ entry_id: "ot_1", source_doc_type_id: "dt_1", updated_at: "2026-07-10T00:00:00Z" });
    expect(publishStateOf(dt({ doc_type_id: "dt_1", updated_at: "2026-07-10T00:00:00Z" }), e)).toBe("published");
    expect(publishStateOf(dt({ doc_type_id: "dt_1", updated_at: "2026-07-05T00:00:00Z" }), e)).toBe("published");
  });
  it("模板 updated_at 晚于库条目 → drift（发布后又改了字段）", () => {
    const e = entry({ entry_id: "ot_1", source_doc_type_id: "dt_1", updated_at: "2026-07-10T00:00:00Z" });
    expect(publishStateOf(dt({ doc_type_id: "dt_1", updated_at: "2026-07-15T00:00:00Z" }), e)).toBe("drift");
  });
});

describe("indexEntriesBySource（rename-proof 对号锚，design review P1）", () => {
  it("按 source_doc_type_id 建索引（非 name）", () => {
    const m = indexEntriesBySource([
      entry({ entry_id: "ot_1", source_doc_type_id: "dt_a", name: "旧名" }),
      entry({ entry_id: "ot_2", source_doc_type_id: "dt_b" }),
    ]);
    // Renaming changes doc_type.name but entries retain old-name snapshots; match by source_doc_type_id to identify them correctly
    expect(m.get("dt_a")?.entry_id).toBe("ot_1");
    expect(m.get("dt_b")?.entry_id).toBe("ot_2");
    expect(m.get("dt_x")).toBeUndefined();
  });
});
