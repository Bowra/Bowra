import { describe, expect, it } from "vitest";

import type { SubagentDraftDetail } from "./subagent-drafts-api";
import type { CreateSubagentOpenPayload } from "./types";
import { manualSubagentPayload, reviewSubagentPayload } from "./subagent-create-flow";

describe("Subagent creation handoff", () => {
  it("keeps the name explicitly supplied in Chat through draft review and manual fallback", () => {
    const fromChat: CreateSubagentOpenPayload = {
      purpose_hint: "分析销售",
      draft_id: null,
      draft_status: null,
      draft_title: null,
      name_hint: "sales-helper",
      display_name_hint: "销售助手",
    };
    const generated = {
      purpose: "分析销售异常",
      draft_id: "sad_1",
      draft_status: "ready",
      definition: { display_name: "AI 建议名称" },
    } as SubagentDraftDetail;

    const reviewing = reviewSubagentPayload(fromChat, generated);
    const manual = manualSubagentPayload(reviewing, reviewing.purpose_hint);

    expect(manual).toMatchObject({
      purpose_hint: "分析销售异常",
      draft_id: null,
      name_hint: "sales-helper",
      display_name_hint: "销售助手",
    });
  });
});
