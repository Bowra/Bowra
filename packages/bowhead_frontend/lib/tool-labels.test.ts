/**
 * Unit tests for tool-labels locale-aware behavior + V5 PRD-E1.
 * Covers: ZH default + EN switch via cookie + tool action/result lines.
 * V5 E1: emoji on toolLabel, separator → │, redactArgs / redactSqlPii / audit.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  auditToolLabelCoverage,
  redactArgs,
  redactSqlPii,
  STATIC_TOOL_NAMES,
  toolActionLine,
  toolLabel,
  toolResultLine,
} from "./tool-labels";

import { TOOL_DISPLAY } from "@/lib/_generated/tool-display";

// Change locale through document.cookie mocked with vi.stubGlobal.
const setLocaleCookie = (loc: "zh-CN" | "en-US") => {
  // Override cookie + navigator.
  Object.defineProperty(globalThis, "document", {
    value: { cookie: `NEXT_LOCALE=${loc}` },
    writable: true,
    configurable: true,
  });
  Object.defineProperty(globalThis, "navigator", {
    value: { language: loc },
    writable: true,
    configurable: true,
  });
  Object.defineProperty(globalThis, "window", {
    value: { localStorage: { getItem: () => null } },
    writable: true,
    configurable: true,
  });
};

describe("toolLabel · ZH locale", () => {
  beforeEach(() => setLocaleCookie("zh-CN"));

  it("bi.ask_question → 问 BI", () => {
    expect(toolLabel("bi.ask_question").name).toBe("问 BI");
  });

  it("subagent.spawn → 派智能体", () => {
    expect(toolLabel("subagent.spawn").name).toBe("派智能体");
  });

  it("unknown tool → fallback to machine name", () => {
    expect(toolLabel("custom.unknown").name).toBe("custom.unknown");
  });

  it("retired section-edit tools still replay with a human label", () => {
    // These three tools retired (absent from generated table); legacy_names humanizes historical replay.
    expect(toolLabel("canvas_patch_section")).toMatchObject({ name: "改章节", family: "canvas" });
    expect(toolLabel("canvas_delete_section")).toMatchObject({ name: "删章节", family: "canvas" });
    expect(toolLabel("canvas_reorder_sections")).toMatchObject({ name: "调章节顺序", family: "canvas" });
  });

  it("family stays correct across locales", () => {
    expect(toolLabel("bi.ask_question").family).toBe("bi");
    expect(toolLabel("skill.list").family).toBe("skill");
    expect(toolLabel("subagent.spawn").family).toBe("subagent");
  });
});

describe("toolLabel · EN locale", () => {
  beforeEach(() => setLocaleCookie("en-US"));

  it("bi.ask_question → Ask BI", () => {
    expect(toolLabel("bi.ask_question").name).toBe("Ask BI");
  });

  it("subagent.spawn → Spawn subagent", () => {
    expect(toolLabel("subagent.spawn").name).toBe("Spawn subagent");
  });

  it("bi.list_measures → List measures", () => {
    expect(toolLabel("bi.list_measures").name).toBe("List measures");
  });

  it("retired section-edit tools replay in English", () => {
    expect(toolLabel("canvas_delete_section").name).toBe("Delete section");
    expect(toolLabel("canvas_reorder_sections").name).toBe("Reorder sections");
  });
});

describe("toolActionLine", () => {
  it("ZH bi.ask_question with text", () => {
    setLocaleCookie("zh-CN");
    const line = toolActionLine("bi.ask_question", '{"text":"今年营业额"}');
    expect(line).toContain("问 BI");
    expect(line).toContain("今年营业额");
    expect(line).toContain("│");  // V5 E1 separator
  });

  it("EN bi.ask_question with text", () => {
    setLocaleCookie("en-US");
    const line = toolActionLine("bi.ask_question", '{"text":"this year revenue"}');
    expect(line).toContain("Ask BI");
    expect(line).toContain("this year revenue");
    expect(line).toContain("│");  // V5 E1 separator (shared across zh/en).
  });

  it("ZH list_measures shows hint", () => {
    setLocaleCookie("zh-CN");
    expect(toolActionLine("bi.list_measures", "{}")).toContain("构造查询前");
  });

  it("EN list_measures shows hint", () => {
    setLocaleCookie("en-US");
    expect(toolActionLine("bi.list_measures", "{}")).toContain("look up what's available");
  });
});

describe("toolResultLine", () => {
  it("ZH success metric_count", () => {
    setLocaleCookie("zh-CN");
    const line = toolResultLine(
      "bi.ask_question", "{}", true, null,
      { type: "ask_question", metric_count: 3, q_type: "metric" } as Record<string, unknown>,
    );
    // metric_count > 0 + qt=metric falls to first metric branch; without metrics array → falls back metric_count
    expect(line).toMatch(/3 个指标|指标：/);
  });

  it("EN success metric_count", () => {
    setLocaleCookie("en-US");
    const line = toolResultLine(
      "bi.ask_question", "{}", true, null,
      { type: "ask_question", metric_count: 3, q_type: "metric" } as Record<string, unknown>,
    );
    expect(line).toMatch(/3 metrics returned|Metric: /);
  });

  it("ZH failure", () => {
    setLocaleCookie("zh-CN");
    expect(toolResultLine("bi.ask_question", "", false, "503 Service Unavailable"))
      .toBe("失败：未知错误");
  });

  it("EN failure", () => {
    setLocaleCookie("en-US");
    expect(toolResultLine("bi.ask_question", "", false, "503 Service Unavailable"))
      .toBe("Failed: unknown error");
  });

  it("EN got_dimensions plural", () => {
    setLocaleCookie("en-US");
    const line = toolResultLine(
      "bi.list_dimensions", "[1,2,3]", true, null,
      { type: "array", count: 3 } as Record<string, unknown>,
    );
    expect(line).toBe("Got 3 dimensions");
  });

  it("EN got_dimensions singular", () => {
    setLocaleCookie("en-US");
    const line = toolResultLine(
      "bi.list_dimensions", "[1]", true, null,
      { type: "array", count: 1 } as Record<string, unknown>,
    );
    expect(line).toBe("Got 1 dimension");
  });

  it("ZH done fallback", () => {
    setLocaleCookie("zh-CN");
    expect(toolResultLine("custom.unknown", "{}", true)).toBe("完成");
  });

  it("EN done fallback", () => {
    setLocaleCookie("en-US");
    expect(toolResultLine("custom.unknown", "{}", true)).toBe("Done");
  });
});


// ─── V5 PRD-E1: emoji + redact + audit ───────────────


describe("V5 E1: emoji on toolLabel", () => {
  it("BI tools 各自 emoji (各不重)", () => {
    expect(toolLabel("bi.ask_question").emoji).toBe("🔢");
    expect(toolLabel("bi.profile").emoji).toBe("🔍");
    expect(toolLabel("bi.list_measures").emoji).toBe("🔎");
  });

  it("Skill / Subagent / Canvas / Web emoji", () => {
    expect(toolLabel("skill.invoke").emoji).toBe("✨");
    expect(toolLabel("subagent.spawn").emoji).toBe("🤖");
    expect(toolLabel("canvas.add_chart").emoji).toBe("📊");
    expect(toolLabel("web.search").emoji).toBe("🌐");
  });

  it("BI write tools (approval) 用 ✏️ / 🗑", () => {
    expect(toolLabel("bi.update_measures").emoji).toBe("✏️");
    expect(toolLabel("bi.create_measures").emoji).toBe("✏️");
    expect(toolLabel("bi.delete_measures").emoji).toBe("🗑");
  });

  it("Unknown tool fallback to ⚙️", () => {
    expect(toolLabel("mcp.filesystem.read").emoji).toBe("⚙️");
    expect(toolLabel("custom.unknown").emoji).toBe("⚙️");
  });

  it("SQL emoji 跟 chart emoji 不重 (Round 1 P1 fix)", () => {
    expect(toolLabel("bi.ask_question").emoji).not.toBe(
      toolLabel("canvas.add_chart").emoji,
    );
  });
});


describe("V5 E1: separator changed to │", () => {
  it("toolActionLine uses │ instead of ：", () => {
    setLocaleCookie("zh-CN");
    const line = toolActionLine(
      "bi.ask_question",
      JSON.stringify({ text: "Q3 营收" }),
    );
    expect(line).toContain("│");
    expect(line).not.toMatch(/：(?!\))/);  // Exclude colon inside parentheses (ambiguity).
  });

  it("English uses │ same separator", () => {
    setLocaleCookie("en-US");
    const line = toolActionLine(
      "bi.ask_question",
      JSON.stringify({ text: "Q3 revenue" }),
    );
    expect(line).toContain("│");
  });
});


describe("V5 E1: toolActionLine 对 user-text 段跑 redactSqlPii (防 PII 回显)", () => {
  it("user query 含 email PII → chip source 段 redact", () => {
    setLocaleCookie("zh-CN");
    const line = toolActionLine(
      "bi.ask_question",
      JSON.stringify({ text: "找 email='alice@test.com' 的订单" }),
    );
    expect(line).not.toContain("alice@test.com");
    expect(line).toContain("***");
  });

  it("subagent.spawn task 含 PII → redact", () => {
    setLocaleCookie("zh-CN");
    const line = toolActionLine(
      "subagent.spawn",
      JSON.stringify({ profile: "compute", task: "查 phone='13800001234' 用户" }),
    );
    expect(line).not.toContain("13800001234");
    expect(line).toContain("***");
  });

  it("普通 query 不被改", () => {
    setLocaleCookie("zh-CN");
    const line = toolActionLine(
      "bi.ask_question",
      JSON.stringify({ text: "Q3 营收同比" }),
    );
    expect(line).toContain("Q3 营收同比");
    expect(line).not.toContain("***");
  });
});


describe("V5 E1: redactArgs (跟 PRD-E3 共用)", () => {
  it("password / token / api_key field → ***", () => {
    const out = redactArgs({
      password: "secret123",
      auth_token: "xyz",
      api_key: "abc",
      sql: "SELECT 1",
    });
    expect(out.password).toBe("***");
    expect(out.auth_token).toBe("***");
    expect(out.api_key).toBe("***");
    expect(out.sql).toBe("SELECT 1");
  });

  it("email / phone / ssn field → 前 2 字 + ***", () => {
    const out = redactArgs({
      email: "alice@test.com",
      phone: "13800001234",
      ssn: "123-45-6789",
    });
    expect(out.email).toBe("al***");
    expect(out.phone).toBe("13***");
    expect(out.ssn).toBe("12***");
  });

  it("SQL string 内嵌 PII regex redact", () => {
    const out = redactArgs({
      sql: "SELECT * FROM users WHERE email = 'alice@test.com'",
    });
    expect(out.sql as string).toContain("***");
    expect(out.sql as string).not.toContain("alice@test.com");
  });

  it("普通 SQL 不 redact", () => {
    const sql = "SELECT SUM(amount) FROM orders WHERE quarter = 'Q3'";
    const out = redactArgs({ sql });
    expect(out.sql).toBe(sql);
  });

  it("non-string non-PII fields 保原样", () => {
    const out = redactArgs({
      page: 1,
      limit: 50,
      nested: { foo: "bar" },
    });
    expect(out.page).toBe(1);
    expect(out.nested).toEqual({ foo: "bar" });
  });
});


describe("V5 E1: redactSqlPii regex", () => {
  it("email PII 替换", () => {
    expect(redactSqlPii("WHERE email='a@b.com'")).toBe("WHERE email='***'");
  });
  it("phone PII 替换 (双引号)", () => {
    expect(redactSqlPii(`WHERE phone="13800001234"`)).toBe(`WHERE phone="***"`);
  });
  it("ssn PII 替换", () => {
    expect(redactSqlPii("AND ssn='123-45-6789'")).toBe("AND ssn='***'");
  });
  it("无 PII pattern 保原样", () => {
    const sql = "SELECT * FROM orders WHERE quarter='Q3'";
    expect(redactSqlPii(sql)).toBe(sql);
  });
});


describe("V5 E1: 字典 audit", () => {
  it("STATIC_TOOL_NAMES 是 29 个 (Day 0 spike #1 confirmed)", () => {
    expect(STATIC_TOOL_NAMES.length).toBe(29);
  });

  it("字典覆盖率 ≥ 90% (E1 ship gate)", () => {
    const result = auditToolLabelCoverage();
    expect(result.coveragePct).toBeGreaterThanOrEqual(90);
  });

  it("100% covered (target — 29 都已 hardcoded)", () => {
    const result = auditToolLabelCoverage();
    expect(result.missing).toEqual([]);
  });
});

// ─── Full coverage of active tools (added after capability-surface-audit 2026-09-06) ────────
//
// Background: handwritten display tables had 47–69 entries, mostly retired tool names,
// covering only 9–19 of 35 active tools; missing entries exposed raw snake_case
// tool codes on chips, even worse in English (17/35). Names now all come from tool_aliases.yaml generated table.
//
// All 41 tests above were **blind** to this (reverting generated-table merge to handwritten-only
// still left them all passing). These guards are necessary, not duplicates.
describe("现役工具：展示名不许回落成工具代号", () => {
  const liveTools = Object.keys(TOOL_DISPLAY);

  it("生成表非空（import 断了的话本组会变成空转，先钉住）", () => {
    expect(liveTools.length).toBeGreaterThan(30);
  });

  it("zh-CN：每个现役工具都有中文名", () => {
    setLocaleCookie("zh-CN");
    expect(liveTools.filter((t) => toolLabel(t).name === t)).toEqual([]);
  });

  it("en-US：每个现役工具都有英文名，且不残留中文", () => {
    setLocaleCookie("en-US");
    const CJK = /[一-鿿]/;
    expect(
      liveTools.filter((t) => {
        const n = toolLabel(t).name;
        return n === t || CJK.test(n);
      }),
    ).toEqual([]);
  });

  // ⚙️ is `toolLabel` fallback for **unrecognized tools**, so must never also be a tool's
  // real icon, or chips cannot distinguish Python from an unknown tool.
  it("图标不回落成兜底 ⚙️", () => {
    setLocaleCookie("zh-CN");
    expect(liveTools.filter((t) => toolLabel(t).emoji === "⚙️")).toEqual([]);
  });
});
