import { describe, expect, it } from "vitest";

import zhMessages from "@/messages/zh-CN.json";
import { releaseNoteText } from "./agent-release-notes";

const t = (key: string) => (zhMessages.agents_marketplace as Record<string, string>)[key];

describe("releaseNoteText", () => {
  it("shows server-written notes in the reader's language and Owner notes as written", () => {
    expect(releaseNoteText("Initial private release", t)).toBe("首个私有版本");
    expect(releaseNoteText("Initial sharing request", t)).toBe("首次共享申请");
    expect(releaseNoteText("报告第一段先给结论", t)).toBe("报告第一段先给结论");
    expect(releaseNoteText(null, t)).toBe("本次发布没有填写更新说明。");
  });
});
