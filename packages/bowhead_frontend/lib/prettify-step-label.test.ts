/**
 * prettify-step-label tests — table-driven per tech-design §2.3.
 *
 * Critical invariant: raw JSON dict input NEVER appears in the output.
 */

import { describe, expect, it } from "vitest";

import { prettifyStepLabel } from "./prettify-step-label";

describe("prettifyStepLabel — primitives", () => {
  it("null → ''", () => {
    expect(prettifyStepLabel(null)).toBe("");
  });
  it("undefined → ''", () => {
    expect(prettifyStepLabel(undefined)).toBe("");
  });
  it("null with tool_name → '' (no fallback to tool default)", () => {
    expect(prettifyStepLabel(null, "bi_query")).toBe("");
  });
  it("string under 60 chars → trimmed string", () => {
    expect(prettifyStepLabel("  hello world  ")).toBe("hello world");
  });
  it("string over 60 chars → first 60 chars", () => {
    const long = "a".repeat(120);
    expect(prettifyStepLabel(long)).toBe("a".repeat(60));
  });
  it("number → String(value)", () => {
    expect(prettifyStepLabel(42)).toBe("42");
  });
  it("boolean → String(value)", () => {
    expect(prettifyStepLabel(true)).toBe("true");
  });
});

describe("prettifyStepLabel — string raw-JSON guard (chat-solidify #附)", () => {
  it("stringified JSON object → 空标签, NEVER the raw blob", () => {
    const out = prettifyStepLabel('{"code":"print(1)","inputs":{"d":"art_1"}}');
    expect(out).toBe("");
    expect(out).not.toContain("code");
    expect(out).not.toContain("art_1");
  });
  it("stringified JSON array → 空标签", () => {
    expect(prettifyStepLabel('["a","b","c"]')).toBe("");
  });
  // chat-ux-2026-08 batch A — parse-gate gap: backend-truncated JSON cannot parse,
  // so it leaks from catch onto the screen as an ordinary string. Both cases below are verbatim 116 prod observations.
  // Mutation check: delete `if (t.startsWith("{")) return NO_LABEL;`; both cases must fail.
  it("TRUNCATED JSON object (unparseable) → 空标签, NEVER the raw fragment", () => {
    const out = prettifyStepLabel(
      '{"payload": {"delivery_surface": "chat", "exec_summary": "近3',
    );
    expect(out).toBe("");
    expect(out).not.toContain("payload");
    expect(out).not.toContain("delivery_surface");
  });
  it("TRUNCATED JSON with nested brace → 空标签", () => {
    expect(prettifyStepLabel('{"insights": [{"title": "退款率异')).toBe("");
  });

  it("human label starting with '[' is PRESERVED (subagent_spawn '[compute] task')", () => {
    // Must NOT be swallowed by the guard — it is not valid JSON.
    expect(prettifyStepLabel("[compute] 分析销售数据 Q4")).toBe("[compute] 分析销售数据 Q4");
    expect(prettifyStepLabel("[urgent] find revenue")).toBe("[urgent] find revenue");
  });
  it("normal string passes through unchanged", () => {
    expect(prettifyStepLabel("运行: df = pd.read_csv('x.csv')")).toBe(
      "运行: df = pd.read_csv('x.csv')",
    );
  });
});

describe("prettifyStepLabel — arrays", () => {
  it("array → '{n} 项'", () => {
    expect(prettifyStepLabel([1, 2, 3])).toBe("3 项");
  });
  it("empty array → '0 项'", () => {
    expect(prettifyStepLabel([])).toBe("0 项");
  });
});

describe("prettifyStepLabel — bi_query result shape", () => {
  it("{ results: [N items] } → '查询返回 N 行'", () => {
    expect(prettifyStepLabel({ results: [{}, {}, {}] })).toBe("查询返回 3 行");
    expect(prettifyStepLabel({ results: [] }, "bi_query")).toBe("查询返回 0 行");
  });
});

describe("prettifyStepLabel — table shape (columns/rows)", () => {
  it("{ columns + rows } → first 3 columns joined by ' · '", () => {
    expect(
      prettifyStepLabel({ columns: ["a", "b", "c", "d"], rows: [] }),
    ).toBe("a · b · c");
  });
  it("{ columns + rows } with fewer than 3 cols", () => {
    expect(prettifyStepLabel({ columns: ["only"], rows: [] })).toBe("only");
  });
});

describe("prettifyStepLabel — python_sandbox shape", () => {
  it("{ ok: true, exit_code: 0 } → '运行完成'", () => {
    expect(prettifyStepLabel({ ok: true, exit_code: 0 }, "python_sandbox")).toBe("运行完成");
  });
  it("{ ok: false } → '运行出错'", () => {
    expect(prettifyStepLabel({ ok: false }, "python_sandbox")).toBe("运行出错");
  });
  it("{ ok: true } only → '运行完成'", () => {
    expect(prettifyStepLabel({ ok: true })).toBe("运行完成");
  });
  it("{ exit_code: 1 } only (no ok flag) → '运行出错'", () => {
    expect(prettifyStepLabel({ exit_code: 1 })).toBe("运行出错");
  });
  it("{ exit_code: 0 } only → '运行完成'", () => {
    expect(prettifyStepLabel({ exit_code: 0 })).toBe("运行完成");
  });
});

describe("prettifyStepLabel — search shape (query)", () => {
  it("{ query: '...' } → '搜索: ...'", () => {
    expect(prettifyStepLabel({ query: "Q3 营业额" }, "kb_search")).toBe("搜索: Q3 营业额");
  });
  it("{ query: 'long...' } → truncate to 40 chars", () => {
    const longQ = "x".repeat(80);
    const out = prettifyStepLabel({ query: longQ });
    expect(out).toBe(`搜索: ${"x".repeat(40)}`);
  });
});

describe("prettifyStepLabel — never raw JSON for unknown dicts", () => {
  it("unknown dict shape → 空标签, NEVER raw JSON", () => {
    const out = prettifyStepLabel({ foo: "bar", baz: 1 });
    expect(out).toBe("");
    expect(out).not.toContain("foo");
    expect(out).not.toContain("bar");
    expect(out).not.toContain("{");
    expect(out).not.toContain('"');
  });
  it("nested dict → 空标签", () => {
    expect(prettifyStepLabel({ deep: { nested: { value: 5 } } })).toBe("");
  });
  it("unknown tool_name with unknown shape → 空标签", () => {
    expect(prettifyStepLabel({ something: 1 }, "mystery_tool")).toBe("");
  });
});

describe("prettifyStepLabel — invariant: raw JSON dict NEVER in output", () => {
  // Table-driven check that any pathological dict input doesn't leak JSON.
  const tricky: Array<{ input: unknown; tool?: string | null }> = [
    { input: { columns: ["a", "b"], rows: [["x", "y"]] } },
    { input: { results: [{ id: 1 }, { id: 2 }] } },
    { input: { ok: true, exit_code: 0 } },
    { input: { query: "find revenue" } },
    { input: { unknown_keys: "value" } },
    { input: {}, tool: "bi_query" },
    { input: [], tool: "kb_search" },
  ];
  for (const { input, tool } of tricky) {
    it(`input=${JSON.stringify(input).slice(0, 60)} → no raw JSON`, () => {
      const out = prettifyStepLabel(input, tool);
      expect(out).not.toMatch(/\{.*:/);
      expect(out).not.toMatch(/^\[.*,/); // not a JSON array literal
    });
  }
});
