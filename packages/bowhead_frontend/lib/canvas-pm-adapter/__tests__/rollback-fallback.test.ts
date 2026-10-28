/**
 * Sprint ι Phase A · P1 #4 (tech-r2 §5.3.1, §6.3 spec 7).
 *
 * Verify rollback fallback in `canvas-to-pm.ts`: when an emergency image-revert
 * downgrades the frontend, persisted post-ι sections of unknown kind (e.g.
 * `code_block` if old build doesn't register it) MUST NOT throw Tiptap
 * `RangeError: Unknown node type`. Instead, `sectionToPMNode` degrades to the
 * dedicated `unknown` render, preserving readable content as `text_content`.
 *
 * 2026-09-05: change the target from `sql_block` to `unknown`; the old approach disguised all unrecognized content
 * as SQL and made a business kind a structural safety net (retiring it would first require removing the safeguard).
 */

import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { sectionToPMNode } from "../canvas-to-pm";
import { KIND_TO_NODE_NAME } from "../schema";
import type { ReportSection } from "@/lib/types";

describe("canvas-to-pm rollback fallback (Sprint ι §5.3.1)", () => {
  let warnSpy: ReturnType<typeof vi.spyOn>;
  beforeEach(() => {
    warnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);
  });
  afterEach(() => {
    warnSpy.mockRestore();
  });

  it("unknown kind degrades to `unknown` atomic node (no RangeError)", () => {
    const futureKind = {
      id: "f_future",
      kind: "future_kind_not_registered",
      code: "print('hello from sandbox')",
    } as unknown as ReportSection;

    expect(() => sectionToPMNode(futureKind)).not.toThrow();
    const node = sectionToPMNode(futureKind);
    expect(node.type).toBe(KIND_TO_NODE_NAME.unknown);
    expect(warnSpy).toHaveBeenCalledWith(
      expect.stringContaining("future_kind_not_registered"),
    );
  });

  it("preserves `code` field as `text_content` when degrading", () => {
    const cb = {
      id: "f_code",
      kind: "code_block_v_future",
      code: "df.groupby('store').sum()",
    } as unknown as ReportSection;

    const node = sectionToPMNode(cb);
    expect(node.type).toBe(KIND_TO_NODE_NAME.unknown);
    const payload = (node.attrs as { payload?: { text_content?: string; original_kind?: string } } | undefined)?.payload;
    expect(payload?.text_content).toBe("df.groupby('store').sum()");
    expect(payload?.original_kind).toBe("code_block_v_future");
  });

  it("记下原 kind 名，即便一点可读内容都提不出来", () => {
    const empty = {
      id: "f_empty",
      kind: "totally_unknown_kind",
    } as unknown as ReportSection;

    const node = sectionToPMNode(empty);
    expect(node.type).toBe(KIND_TO_NODE_NAME.unknown);
    const payload = (node.attrs as { payload?: { text_content?: string; original_kind?: string } } | undefined)?.payload;
    expect(payload?.text_content).toBe("");
    expect(payload?.original_kind).toBe("totally_unknown_kind");
  });

  it("Sprint ι code_block kind is registered (not falling through to fallback)", () => {
    const cb: ReportSection = {
      id: "f_code_real",
      kind: "code_block",
      call_id: "tl_x",
      lang: "python",
      code: "x = 1",
      dur_ms: 0,
    } as ReportSection;

    const node = sectionToPMNode(cb);
    expect(node.type).toBe(KIND_TO_NODE_NAME.code_block);
    expect(warnSpy).not.toHaveBeenCalled();
  });
});

/**
 * Positive control (added when retiring trend_chart / attribution on 2026-09-05).
 *
 * Zero sections in the database cannot prove correct removal: real-data replay differences are **necessarily** zero regardless of correctness.
 * Verify **existing sections that may remain on other deployments** render a fallback when read, rather than a blank page.
 * Without this, the preceding no-crash assertions do not exercise these two kinds.
 */
describe("已下线 kind 的存量数据（正对照）", () => {
  let warnSpy2: ReturnType<typeof vi.spyOn>;
  beforeEach(() => {
    warnSpy2 = vi.spyOn(console, "warn").mockImplementation(() => undefined);
  });
  afterEach(() => {
    warnSpy2.mockRestore();
  });

  it.each([
    [
      "trend_chart",
      {
        id: "f_old_trend",
        kind: "trend_chart",
        points: [{ label: "1 月", value: 100 }],
        unit: "万元",
      },
    ],
    [
      "attribution",
      {
        id: "f_old_attr",
        kind: "attribution",
        summary: { label: "总变化", value: "+12%", pct: "+12.0%", pctTone: "pos" },
        rows: [{ rank: 1, name: "天猫", dim: "渠道", from: "100", to: "120" }],
      },
    ],
  ])("存量 %s 段降级成 unknown，不抛 RangeError", (kind, raw) => {
    const section = raw as unknown as ReportSection;
    expect(() => sectionToPMNode(section)).not.toThrow();
    const node = sectionToPMNode(section);
    expect(node.type).toBe(KIND_TO_NODE_NAME.unknown);
    const payload = (node.attrs as { payload?: { original_kind?: string } } | undefined)?.payload;
    // Keep the original kind name: users and troubleshooting must know what this used to be
    expect(payload?.original_kind).toBe(kind);
    expect(warnSpy2).toHaveBeenCalledWith(expect.stringContaining(kind));
  });
});

/**
 * Unlike the two above, `sql_block` **must not** fall into unknown.
 *
 * It has always been a code_block with `lang="sql"` (writes have produced only code_block since Sprint ι, with comments stating
 * legacy alias, 90-day grace). Grace ended on 2026-09-05: retire the section kind, adding a read-side
 * alias. Old data was already code, so code-block rendering is **more accurate**, without degrading to "此段无法显示".
 *
 * The backend has a symmetric implementation (`canvas_store._normalize_legacy_kind`); this covers paths without backend projection
 * (SSE event replay, old sections in local caches).
 */
describe("sql_block 读侧别名（不是降级）", () => {
  it("存量 sql_block 归一成 code_block，而不是 unknown", () => {
    const legacy = {
      id: "f_legacy_sql",
      kind: "sql_block",
      sql: "SELECT store, SUM(amount) FROM orders GROUP BY store",
    } as unknown as ReportSection;

    const node = sectionToPMNode(legacy);
    expect(node.type).toBe(KIND_TO_NODE_NAME.code_block);
    expect(node.type).not.toBe(KIND_TO_NODE_NAME.unknown);
    const payload = (node.attrs as {
      payload?: { code?: string; lang?: string; call_id?: string };
    } | undefined)?.payload;
    expect(payload?.code).toContain("SELECT store");
    expect(payload?.lang).toBe("sql");
    // code_block requires call_id; synthesize a traceable one when old data lacks it
    expect(payload?.call_id).toBe("legacy_sql_f_legacy_sql");
  });

  it("meta.call_id 存在时优先用它，不覆盖真实调用 id", () => {
    const legacy = {
      id: "f_legacy_sql2",
      kind: "sql_block",
      sql: "SELECT 1",
      meta: { call_id: "tl_real_42", source: "bi.ask_question" },
    } as unknown as ReportSection;

    const payload = (sectionToPMNode(legacy).attrs as {
      payload?: { call_id?: string; meta?: Record<string, string> };
    } | undefined)?.payload;
    expect(payload?.call_id).toBe("tl_real_42");
    expect(payload?.meta?.source).toBe("bi.ask_question");
  });
});
