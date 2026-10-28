// Knowledge base UX cleanup PRD §4: the single mapping for user-facing file statuses.
import { describe, expect, it } from "vitest";

import { kbFailReason, kbFileSettling, kbFileState } from "@/lib/kb-file-status";

describe("kbFileState", () => {
  it.each([
    ["awaiting_upload", "uploading"],
    ["pending", "queued"],
    ["queued", "queued"],
    ["parsing", "processing"],
    ["chunking", "processing"],
    ["embedding", "processing"],
    ["done", "ready"],
    ["failed", "failed"],
  ] as const)("%s → %s", (parse_status, expected) => {
    expect(kbFileState({ parse_status })).toBe(expected);
  });

  it("卡住的进行中文件按失败显示，原因为处理被中断，且不再轮询", () => {
    const stuck = { parse_status: "pending" as const, parse_stuck: true, parse_error_code: null };
    expect(kbFileState(stuck)).toBe("failed");
    expect(kbFailReason(stuck)).toBe("interrupted");
    expect(kbFileSettling(stuck)).toBe(false);
  });
});

describe("kbFailReason", () => {
  it("用后端给的原因代码；未知或缺失按通用失败；非失败为 null", () => {
    expect(kbFailReason({ parse_status: "failed", parse_error_code: "timeout" })).toBe("timeout");
    expect(kbFailReason({ parse_status: "failed", parse_error_code: "RAW_UNKNOWN" })).toBe("parse_failed");
    expect(kbFailReason({ parse_status: "failed" })).toBe("parse_failed");
    expect(kbFailReason({ parse_status: "done", parse_error_code: "timeout" })).toBeNull();
  });
});
