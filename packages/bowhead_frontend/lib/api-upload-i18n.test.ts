import { afterEach, describe, expect, it, vi } from "vitest";

import { FILE_CATEGORIES, fileCategoryLabel, isFileCategory, listFiles, setFileCategory, validateUpload } from "./api";

function file(name: string, type: string, size?: number): File {
  const f = new File(["x"], name, { type });
  if (size !== undefined) Object.defineProperty(f, "size", { value: size });
  return f;
}

afterEach(() => {
  document.documentElement.lang = "";
});

describe("validateUpload", () => {
  it("keeps the Chinese copy and reports the kind", () => {
    document.documentElement.lang = "zh-CN";
    expect(validateUpload(file("a.csv", "text/csv", 3 * 1024 * 1024 * 1024))).toEqual({
      kind: "size",
      message: "文件太大（3.0GB），上限 2.0GB",
    });
    expect(validateUpload(file("a.exe", "application/x-msdownload"))).toEqual({
      kind: "unsupported",
      message: "类型 application/x-msdownload 不支持",
    });
    expect(validateUpload(file("README", ""))).toEqual({ kind: "unsupported", message: "类型 未知 不支持" });
    expect(validateUpload(file("a.csv", "text/csv"))).toBeNull();
  });

  it.each(["en-US", "de-DE", "fr-FR"] as const)("does not fall back to Chinese in %s", (locale) => {
    document.documentElement.lang = locale;
    const issues = [
      validateUpload(file("a.csv", "text/csv", 3 * 1024 * 1024 * 1024)),
      validateUpload(file("a.exe", "application/x-msdownload")),
      validateUpload(file("a.exe", "")),
      validateUpload(file("README", "")),
    ];
    expect(issues.map((issue) => issue?.kind)).toEqual(["size", "unsupported", "unsupported", "unsupported"]);
    for (const issue of issues) expect(issue?.message).not.toMatch(/\p{Script=Han}/u);
    expect(issues[0]?.message).toContain("2.0GB");
    expect(issues[1]?.message).toContain("application/x-msdownload");
  });
});

describe("fileCategoryLabel", () => {
  it("keeps the Chinese labels verbatim and translates every code elsewhere", () => {
    document.documentElement.lang = "zh-CN";
    expect(FILE_CATEGORIES.map(fileCategoryLabel)).toEqual(["汇报材料", "数据明细", "参考资料", "会议纪要", "口径定义", "截图", "其他"]);
    document.documentElement.lang = "en-US";
    expect(FILE_CATEGORIES.map(fileCategoryLabel)).toEqual([
      "Briefing Material",
      "Data Table",
      "Reference",
      "Meeting Minutes",
      "Metric Definition",
      "Screenshot",
      "Other",
    ]);
    document.documentElement.lang = "de-DE";
    for (const category of FILE_CATEGORIES) {
      const label = fileCategoryLabel(category);
      expect(label, `de-DE ${category}`).not.toMatch(/\p{Script=Han}/u);
      expect(label, `de-DE ${category}`).not.toBe(category);
    }
  });

  it("shows an unknown code verbatim", () => {
    document.documentElement.lang = "en-US";
    expect(isFileCategory("quarterly_review")).toBe(false);
    expect(fileCategoryLabel("quarterly_review")).toBe("quarterly_review");
  });
});

describe("setFileCategory", () => {
  it("sends the language-neutral code, never a Chinese label", async () => {
    document.documentElement.lang = "zh-CN";
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ file_id: "art_1", category: "dataset" }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    try {
      await setFileCategory("art_1", "dataset");
      const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
      expect(url).toBe("/api/v1/files/art_1/category");
      expect(JSON.parse(String(init.body))).toEqual({ category: "dataset" });
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("puts the code on the list filter", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ items: [], total: 0 }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    try {
      await listFiles({ category: "meeting_notes" });
      const [url] = fetchMock.mock.calls[0] as unknown as [string];
      expect(new URL(url, "http://x").searchParams.get("category")).toBe("meeting_notes");
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
