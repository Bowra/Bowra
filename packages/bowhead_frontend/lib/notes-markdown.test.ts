import { describe, expect, it } from "vitest";

import {
  countBullets,
  countChars,
  countFreeTextLines,
  countPinned,
  parseNotes,
} from "./notes-markdown";

describe("parseNotes", () => {
  it("returns empty array for empty content", () => {
    expect(parseNotes("")).toEqual([]);
  });

  it("recognizes pinned with 📌 prefix", () => {
    const lines = parseNotes("📌 Q3 cross-check");
    expect(lines).toHaveLength(1);
    expect(lines[0].kind).toBe("pinned");
    expect(lines[0].text).toBe("Q3 cross-check");
    expect(lines[0].bulletIndex).toBe(0);
  });

  it("recognizes bullet with • prefix", () => {
    const lines = parseNotes("• 偏好正式书面语");
    expect(lines[0].kind).toBe("bullet");
    expect(lines[0].text).toBe("偏好正式书面语");
  });

  it("recognizes bullet with - prefix", () => {
    const lines = parseNotes("- alt prefix");
    expect(lines[0].kind).toBe("bullet");
    expect(lines[0].text).toBe("alt prefix");
  });

  it("treats other lines as freetext", () => {
    const lines = parseNotes("我是审计师");
    expect(lines[0].kind).toBe("freetext");
    expect(lines[0].bulletIndex).toBeNull();
  });

  it("treats blank lines as blank kind", () => {
    const lines = parseNotes("\n");
    expect(lines).toHaveLength(2);
    expect(lines[0].kind).toBe("blank");
    expect(lines[1].kind).toBe("blank");
  });

  it("assigns bulletIndex skipping freetext + blank", () => {
    const content = "我是审计师\n📌 重点\n• 一\n\n• 二";
    const lines = parseNotes(content);
    const bullets = lines.filter((l) => l.kind === "pinned" || l.kind === "bullet");
    expect(bullets.map((b) => b.bulletIndex)).toEqual([0, 1, 2]);
    // freetext + blank get bulletIndex=null
    expect(lines[0].bulletIndex).toBeNull();
    expect(lines[3].bulletIndex).toBeNull();
  });
});

describe("count helpers", () => {
  it("countChars returns code point count (matches Python len())", () => {
    expect(countChars("")).toBe(0);
    expect(countChars("hello")).toBe(5);
    expect(countChars("中文")).toBe(2);
  });

  it("countChars handles emoji as 2 code points (📌 = 1 with VS16, but raw 📌 is 2)", () => {
    // 📌 is U+1F4CC = surrogate pair in UTF-16, single code point.
    // [..."📌"].length === 1 in modern engines.
    expect(countChars("📌")).toBe(1);
    expect(countChars("📌 hi")).toBe(4);
  });

  it("countPinned matches parseNotes pinned count", () => {
    const content = "📌 a\n• b\n📌 c";
    expect(countPinned(content)).toBe(2);
  });

  it("countBullets includes pinned + bullet", () => {
    const content = "📌 a\n• b\n• c\nhello\n";
    expect(countBullets(content)).toBe(3);
  });

  it("countFreeTextLines excludes blank + bullet", () => {
    const content = "freetext1\n📌 pinned\n• bullet\n\nfreetext2";
    expect(countFreeTextLines(content)).toBe(2);
  });

  it("empty content all zeros", () => {
    expect(countChars("")).toBe(0);
    expect(countPinned("")).toBe(0);
    expect(countBullets("")).toBe(0);
    expect(countFreeTextLines("")).toBe(0);
  });
});
