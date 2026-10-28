/**
 * Sprint τ Phase 0 — tool-display.
 */
import { describe, expect, it } from "vitest";
import { builtinTools, GOOGLE_DELIVERY_TOOL_REFS, localizedToolText, toolDisplay, TOOL_DISPLAY_TABLE } from "@/lib/tool-display";

describe("toolDisplay", () => {
  it("maps bi_query to zh-CN label + sigil + bi category", () => {
    const d = toolDisplay("bi_query");
    expect(d.label).toBe("查询数据");
    expect(d.sigil).toBe("🔍");
    expect(d.category).toBe("bi");
  });

  it("maps subagent_return to ✅ orchestrate", () => {
    const d = toolDisplay("subagent_return");
    expect(d.label).toBe("智能体完成");
    expect(d.category).toBe("orchestrate");
  });

  it("falls back to wire name + λ for unknown tools", () => {
    const d = toolDisplay("some_future_tool");
    expect(d.label).toBe("some_future_tool");
    expect(d.sigil).toBe("λ");
    expect(d.category).toBe("unknown");
  });

  // Previously enforced handwritten 12-tool list with long-retired `bi_fetch_rows` / `canvas_add_section`
  // / `kb_read`: preserving dead entries kept it green, while all-active-tool coverage was
  // untested. Now generated from tool_aliases.yaml, enforce **every entry complete**.
  it("every generated entry carries a label and blurb for every locale, sigil and category", () => {
    const entries = Object.entries(TOOL_DISPLAY_TABLE);
    expect(entries.length).toBeGreaterThan(30);
    for (const [name, d] of entries) {
      for (const locale of ["zh-CN", "en-US", "de-DE", "fr-FR"] as const) {
        expect(d.label[locale], `${name}.label[${locale}]`).toBeTruthy();
        expect(d.blurb[locale], `${name}.blurb[${locale}]`).toBeTruthy();
        // Display names are for business users, never raw tool codes.
        expect(d.label[locale], `${name}.label[${locale}] 是裸代号`).not.toBe(name);
      }
      // English/German/French UI cannot contain Chinese (untranslated German/French use English placeholders).
      for (const locale of ["en-US", "de-DE", "fr-FR"] as const) {
        expect(`${d.label[locale]} ${d.blurb[locale]}`, `${name}[${locale}] 含中文`).not.toMatch(/\p{Script=Han}/u);
      }
      expect(d.sigil, `${name}.sigil`).toBeTruthy();
      expect(d.category, `${name}.category`).not.toBe("unknown");
    }
  });

  // ─── Built-in Tool display (capability-surface-audit step 2) ────────────────
  it("陈列集是显式的：只排掉不该陈列的两个，其余全在", () => {
    const caps = builtinTools();
    const refs = caps.map((c) => c.ref);
    // Criterion: customer can perceive this happening; **not** author-side NATIVE_ALLOWLIST reuse.
    // Report-write/destructive/nonanalyst tools excluded from author surface still belong in display.
    for (const shown of ["write_report", "remove_chart"]) {
      expect(refs, `${shown} 该陈列却没有`).toContain(shown);
    }
    // Since 93e9c47db only entries declared `kind: "tool"` are listed; protocol and
    // runtime parts (Coach, settings, Skill runtime, sub-agent protocol) stay hidden.
    for (const part of ["subagent_return", "subagent_spawn", "tool_switch_context", "python_exec"]) {
      expect(refs).not.toContain(part);
    }
    expect(refs).toContain("python_sandbox");
    // Google delivery tools stay hidden until the team's Google connection is enabled.
    for (const ref of GOOGLE_DELIVERY_TOOL_REFS) expect(refs).not.toContain(ref);
    // Includes the current-user reader; Google delivery Tools require the connection.
    expect(GOOGLE_DELIVERY_TOOL_REFS.size).toBe(3);
    expect(caps.map((tool) => tool.ref)).toContain("get_current_user");
    expect(caps.length).toBe(32);
    const withGoogle = builtinTools({ googleWorkspace: true }).map((c) => c.ref);
    for (const ref of GOOGLE_DELIVERY_TOOL_REFS) expect(withGoogle).toContain(ref);
    expect(withGoogle.length).toBe(35);
  });

  it("组内按代号排，同族能力相邻（不按译名排）", () => {
    // 116 dogfood 2026-09-08: sorting translations separated three Power BI tools into
    // “查看 / 查询 / 读取” positions, splitting family and producing different Chinese/English order.
    const bi = builtinTools().filter((c) => c.category === "bi").map((c) => c.ref);
    const pbi = bi.filter((r) => r.startsWith("pbi_"));
    const firstPbi = bi.indexOf(pbi[0]);
    expect(bi.slice(firstPbi, firstPbi + pbi.length), "pbi_* 没有连在一起").toEqual(pbi);
  });

  it("每条都有人话名和说明，且不是裸代号", () => {
    for (const c of builtinTools()) {
      expect(c.name, `${c.ref} 没名字`).toBeTruthy();
      expect(c.blurb, `${c.ref} 没说明`).toBeTruthy();
      expect(c.name, `${c.ref} 名字是代号`).not.toBe(c.ref);
      expect(c.sigil, `${c.ref} 没图标`).toBeTruthy();
    }
  });

  it("resolves the en-US label when the locale is english", () => {
    const origDoc = globalThis.document;
    const origNav = globalThis.navigator;
    const origWin = globalThis.window;
    const restore = () => {
      for (const [k, v] of [["document", origDoc], ["navigator", origNav], ["window", origWin]] as const) {
        Object.defineProperty(globalThis, k, { value: v, writable: true, configurable: true });
      }
    };
    Object.defineProperty(globalThis, "document", {
      value: { cookie: "NEXT_LOCALE=en-US", documentElement: { lang: "en-US" } },
      writable: true, configurable: true,
    });
    Object.defineProperty(globalThis, "navigator", { value: { language: "en-US" }, writable: true, configurable: true });
    Object.defineProperty(globalThis, "window", {
      value: { localStorage: { getItem: () => null } }, writable: true, configurable: true,
    });
    try {
      expect(toolDisplay("bi_query").label).toBe("Query Data");
      expect(toolDisplay("bi_schema").label).toBe("View Data Fields");
      // Unknown tools fall back to original string in both languages.
      expect(toolDisplay("some_future_tool").label).toBe("some_future_tool");
    } finally {
      restore();
    }
  });
});

describe("localizedToolText（后端工具目录条目）", () => {
  const builtin = { display_name: "查询数据", description: "取数。", display_name_en: "Query Data", description_en: "Pull rows." };
  const teamTool = { display_name: "list_orders", description: "Orders API", display_name_en: "", description_en: "" };

  it.each([
    ["zh-CN", { name: "查询数据", description: "取数。" }],
    ["en-US", { name: "Query Data", description: "Pull rows." }],
    // Backend catalog lacks German; German UI uses English, never Chinese fallback.
    ["de-DE", { name: "Query Data", description: "Pull rows." }],
    ["fr-FR", { name: "Query Data", description: "Pull rows." }],
  ] as const)("%s", (locale, expected) => {
    document.documentElement.lang = locale;
    try {
      expect(localizedToolText(builtin)).toEqual(expected);
      // Team tool lacks translation ⇒ original fields.
      expect(localizedToolText(teamTool)).toEqual({ name: "list_orders", description: "Orders API" });
    } finally {
      document.documentElement.lang = "";
    }
  });
});
