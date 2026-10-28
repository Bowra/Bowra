/**
 * chat-solidify follow-up (#5) — extractWritingContent projects the accumulated
 * raw tool-args JSON of a live `lead_thinking_writing` step down to the code /
 * query being written, dropping the JSON scaffold + sibling fields (the python
 * `inputs` tail that dogfood 0.72.8 showed after the code).
 */
import { describe, expect, it } from "vitest";

import { extractWritingContent } from "./extract-writing-content";

describe("extractWritingContent", () => {
  it("strips the trailing python_sandbox `inputs` field (the reported bug)", () => {
    const raw =
      '{"code": "import pandas as pd\\nprint(df.head())", "inputs": {"df": "art_abc123"}}';
    expect(extractWritingContent(raw, "python_sandbox")).toBe(
      "import pandas as pd\nprint(df.head())",
    );
  });

  it("extracts python `script` when `code` is absent", () => {
    const raw = '{"script": "print(1)", "inputs": {}}';
    expect(extractWritingContent(raw, "python_sandbox")).toBe("print(1)");
  });

  it("streaming: code value not yet closed → shows partial code, no scaffold", () => {
    // Mid-stream: opening quote present, closing quote not yet arrived.
    const raw = '{"code": "SELECT * FROM sales\\nWHERE region';
    expect(extractWritingContent(raw, "python_sandbox")).toBe(
      "SELECT * FROM sales\nWHERE region",
    );
  });

  it("streaming: code closed but `inputs` mid-stream → clean code (drops tail)", () => {
    const raw = '{"code": "print(42)", "inputs": {"d": "art_';
    expect(extractWritingContent(raw, "python_sandbox")).toBe("print(42)");
  });

  it("streaming: value not started yet → empty (no scaffold flash)", () => {
    expect(extractWritingContent('{"code": ', "python_sandbox")).toBe("");
    expect(extractWritingContent('{"code":"', "python_sandbox")).toBe("");
  });

  it("bi_query single query → extracts query", () => {
    const raw = '{"query": "各区域销售额", "limit": 50}';
    expect(extractWritingContent(raw, "bi_query")).toBe("各区域销售额");
  });

  it("bi_query queries[] complete → joins with newline", () => {
    const raw = '{"queries": ["2024 GMV", "各门店客流"]}';
    expect(extractWritingContent(raw, "bi_query")).toBe("2024 GMV\n各门店客流");
  });

  it("kb_search query streaming → partial query, no scaffold", () => {
    const raw = '{"query": "商超零售 客单';
    expect(extractWritingContent(raw, "kb_search")).toBe("商超零售 客单");
  });

  it("escaped quotes inside the code are decoded, not truncated", () => {
    const raw = '{"code": "print(\\"hi\\")", "inputs": {}}';
    expect(extractWritingContent(raw, "python_sandbox")).toBe('print("hi")');
  });

  it("empty / unrecognized input falls back to raw (no regression)", () => {
    expect(extractWritingContent("", "python_sandbox")).toBe("");
    // no target key at all → raw returned unchanged
    expect(extractWritingContent('{"foo": "bar"}', "python_sandbox")).toBe(
      '{"foo": "bar"}',
    );
  });
});
