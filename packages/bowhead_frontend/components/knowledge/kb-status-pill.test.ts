import { expect, it } from "vitest";

import type { KbFileDTO } from "@/lib/kb-api";

import { getExtractionState } from "./kb-status-pill";

it("shows a newly parsed searchable file as available", () => {
  const file = {
    parse_status: "done",
    uploaded_at: "2026-09-29T04:59:50Z",
    doc_type_id: null,
    auto_classified_doc_type_id: null,
  } as unknown as KbFileDTO;

  expect(getExtractionState({ file, hasRecord: false, recordVerifyStatus: null })).toBe("chunks_only");
});
