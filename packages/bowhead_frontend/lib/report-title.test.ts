/**
 * chat-page-polish B1 — reportDisplayTitle fallback chain.
 *
 * Ensure `/reports` list cards never show raw `Untitled (cv_xxx)`.
 */

import { describe, expect, it } from "vitest";

import { reportDisplayTitle } from "./report-title";

describe("reportDisplayTitle", () => {
  const base = {
    id: "cv_0d8a189f309f",
    created_at: "2026-05-23T10:35:00Z",
  };

  it("uses canvas.title when set", () => {
    expect(reportDisplayTitle({
      ...base,
      title: "Q1 GMV 报告",
      first_section_title: "first-section",
    })).toBe("Q1 GMV 报告");
  });

  it("trims whitespace from canvas.title", () => {
    expect(reportDisplayTitle({
      ...base,
      title: "  Wrapped  ",
      first_section_title: null,
    })).toBe("Wrapped");
  });

  it("falls through to first_section_title when title is null", () => {
    expect(reportDisplayTitle({
      ...base,
      title: null,
      first_section_title: "2026年 Q1 GMV 同比分析",
    })).toBe("2026年 Q1 GMV 同比分析");
  });

  it("falls through to first_section_title when title is empty string", () => {
    expect(reportDisplayTitle({
      ...base,
      title: "",
      first_section_title: "Section title",
    })).toBe("Section title");
  });

  it("falls through to first_section_title when title is whitespace only", () => {
    expect(reportDisplayTitle({
      ...base,
      title: "   ",
      first_section_title: "OK",
    })).toBe("OK");
  });

  it("falls through to created_at timestamp when title + first_section_title both empty", () => {
    const t = reportDisplayTitle({
      ...base,
      title: null,
      first_section_title: null,
    });
    expect(t).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2} 报告$/);
  });

  it("NEVER returns the raw cv_X untitled string", () => {
    const result = reportDisplayTitle({
      ...base,
      title: null,
      first_section_title: null,
    });
    expect(result).not.toContain("Untitled");
    expect(result).not.toContain("cv_");
  });

  it("falls through to short id when even created_at is missing", () => {
    expect(reportDisplayTitle({
      id: "cv_0d8a189f309f",
      created_at: "",
      title: null,
      first_section_title: null,
    })).toBe("报告 cv_0d8a1");
  });

  it("returns 未命名报告 when nothing available", () => {
    expect(reportDisplayTitle({
      id: "",
      created_at: "",
      title: null,
      first_section_title: null,
    })).toBe("未命名报告");
  });

  it("ignores first_section_title when title is set (priority order)", () => {
    expect(reportDisplayTitle({
      ...base,
      title: "Primary",
      first_section_title: "Secondary",
    })).toBe("Primary");
  });
});
