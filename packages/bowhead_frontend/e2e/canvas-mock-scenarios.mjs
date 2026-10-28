/**
 * Canvas mock SSE engine and scenarios (canvas-comprehensive Batch 1 WP-0).
 *
 * Why this exists: gate plays canvas through frontend scenarios.ts setTimeout and never calls
 * server SSE; Batch 1 targets F1/F2/F5 bugs where SSE persists correctly but browser rendering is wrong.
 * To disprove "users still need F5 after passing gate", use real server SSE, injectable dropped frames,
 * and a real by-session endpoint (review P0-2). mock-api-server mounts this under SSE_MODE=sse for the canvas Playwright profile.
 *
 * Contract:
 *   - `primeTurn(message)` → choose a scenario by keywords, compute the **final** persisted canvas state
 *     (apply all canvas events and honor sections_hidden), save it in by-session; return {turnId, sessionId}.
 *     by-session always returns the **complete** final state, unaffected by ?drop, modeling a server-persisted
 *     frame lost by client SSE: exactly the case final-state reconciliation must address.
 *   - `serveStream(req,res,url,turnId)` → replay turn SSE; `?drop=type[:n]` drops the nth occurrence (comma-separated entries);
 *     `?cut=N` sends N events **on the first connection only**, then disconnects without done; reconnect replays all events.
 *   - `getBySession(sid)` → CanvasDetailDTO (canvas/sections/sourceRefs).
 *
 * Event shapes **copy real emit output**: canvas.* fields match the keys read by canvas-sse.ts handlers; evt_seq is
 * a monotonically increasing per-turn sequence (TD-1, mirroring bus _seq_counter); canvas.section_added also carries section-order `seq`.
 */

// ── Chart corpus fixture (read lazily once; see its header for the generator) ──
import { readFileSync } from "node:fs";
import { resolve as resolvePath, dirname } from "node:path";
import { fileURLToPath } from "node:url";

let chartCorpusCache = null;
function loadChartCorpus() {
  if (!chartCorpusCache) {
    const here = dirname(fileURLToPath(import.meta.url));
    chartCorpusCache = JSON.parse(readFileSync(
      resolvePath(here, "../../../eval/fixtures/canvas_export/chart_corpus_cv3c2a_shape_v1.json"),
      "utf8",
    ));
  }
  return chartCorpusCache;
}

// ── Persisted canvas state per session ────────────────────────────
const canvasBySession = new Map(); // sid → { canvas, sections, sourceRefs }
const artifactsBySession = new Map(); // sid → Results-visible artifact DTOs
const turnScript = new Map();      // turnId → { sessionId, events }
const turnConnects = new Map();    // turnId → connection count (?cut applies only to the first connection)

let turnCounter = 0;

// ── Section fixtures (faithful ReportSection shapes) ──────────
const sec = (id, kind, body) => ({ id, kind, ...body });

function reportSections() {
  return [
    sec("sec_rep_eyebrow", "eyebrow", { text: "2026 Q2 · 商超零售" }),
    sec("sec_rep_h1", "h1", { text: "华东区销售归因分析" }),
    sec("sec_rep_kpi", "kpi_row", {
      cells: [
        { label: "GMV", value: "1.24 亿", delta: "+8.3%" },
        { label: "客单价", value: "86.5 元", delta: "-1.2%" },
      ],
    }),
    sec("sec_rep_narr", "narrative", {
      markdown: "华东区 GMV 环比增长 8.3%，主要由生鲜品类拉动；客单价小幅回落。",
    }),
  ];
}

function defaultArtifacts() {
  return [
    {
      id: "art_stdout",
      filename: "sandbox-stdout.txt",
      mime: "text/plain",
      size: 12,
      source: "python_sandbox",
      created_at: "2026-08-07T00:02:00Z",
      preview_available: true,
      kind: "sandbox_output",
    },
    {
      id: "art_calc",
      filename: "calc.md",
      mime: "text/markdown",
      size: 17,
      source: "python_sandbox",
      created_at: "2026-08-07T00:01:00Z",
      preview_available: true,
      kind: "sandbox_output",
    },
  ];
}

function resultsWorkspaceArtifacts() {
  return [
    {
      id: "art_delivery",
      filename: "executive-brief.html",
      mime: "text/html",
      size: 320,
      source: "canvas_export_skill",
      producer_kind: "canvas_export_skill",
      created_at: "2026-08-07T00:04:00Z",
      preview_available: true,
      kind: "deliverable",
    },
    {
      id: "art_video",
      filename: "walkthrough.mp4",
      mime: "video/mp4",
      size: 16,
      source: "canvas_export_skill",
      producer_kind: "canvas_export_skill",
      created_at: "2026-08-07T00:03:30Z",
      preview_available: true,
      kind: "deliverable",
    },
    ...defaultArtifacts(),
    {
      id: "art_input",
      filename: "source-notes.txt",
      mime: "text/plain",
      size: 24,
      source: "upload",
      producer_kind: "upload",
      created_at: "2026-08-07T00:00:00Z",
      preview_available: true,
      kind: "user_upload",
    },
    {
      id: "art_unsupported",
      filename: "source-bundle.zip",
      mime: "application/zip",
      size: 48,
      source: "upload",
      producer_kind: "upload",
      created_at: "2026-08-06T23:59:00Z",
      preview_available: false,
      kind: "user_upload",
    },
  ];
}

function sandboxStepEvents(turnId) {
  const outputArtifacts = [
    {
      artifact_id: "art_stdout",
      filename: "sandbox-stdout.txt",
      mime: "text/plain",
      size: 12,
      data_role: "stdout",
    },
    {
      artifact_id: "art_calc",
      filename: "calc.md",
      mime: "text/markdown",
      size: 17,
    },
  ];
  return [
    {
      type: "step_list_card_created",
      payload: { card_id: turnId, plan: [] },
    },
    {
      type: "step_started",
      payload: {
        card_id: turnId,
        step_id: "step_python_output",
        step_type: "tool_run",
        header_zh: "运行代码",
        started_at: "2026-08-06T00:00:00Z",
        tool_name: "python_sandbox",
        call_id: "call_python_output",
      },
    },
    {
      type: "tool_result",
      payload: {
        subagent_id: "lead",
        tool_name: "python_sandbox",
        call_id: "call_python_output",
        ok: true,
        duration_ms: 551,
        result_preview: "2 个输出",
        result_meta: { output_artifacts: outputArtifacts },
      },
    },
    {
      type: "step_done",
      payload: {
        card_id: turnId,
        step_id: "step_python_output",
        step_type: "tool_run",
        state: "success",
        duration_ms: 551,
        call_id: "call_python_output",
        output_preview: "2 个输出",
        result_inline: { stdout: "19×29=551\n", exit_code: 0 },
        result_meta: { output_artifacts: outputArtifacts },
      },
    },
  ];
}

// ── Scenario definitions: return {events, finalSections, sourceRefs, turnResponseKind} ──
// Each events entry = {type, payload, mutate?}; mutate determines whether it changes persisted by-session state.
function buildScenario(message, turnId, sessionId) {
  const canvasId = `cv_${sessionId}`;
  const openEvt = {
    type: "canvas.opened",
    payload: { canvas_id: canvasId, session_id: sessionId, title: "E2E 报告" },
  };

  // chart-table-ux Batch 1 prerequisite: 27-chart regression sample matching cv_55d680eda3f7
  // (kind/column types/row counts match the real 116 database chart by chart; synthetic data—see the fixture note).
  if (/chartcorpus/i.test(message)) {
    const corpus = loadChartCorpus();
    const sections = [
      sec("sec_corpus_h1", "h1", { text: "27 图同形状回归样本" }),
      ...corpus.specs.map((spec, index) =>
        sec(`sec_corpus_chart_${index + 1}`, "chart", { spec })),
    ];
    return {
      responseKind: "canvas",
      finalSections: sections,
      sourceRefs: corpus.specs.map((spec, index) => ({
        n: index + 1,
        label: spec.title,
        tool: spec.source_meta.tool,
        artifact_id: spec.data_artifact_id,
        query: spec.source_meta.query,
      })),
      events: [
        openEvt,
        ...sections.map((section, index) => ({
          type: "canvas.section_added",
          payload: { canvas_id: canvasId, section, seq: index },
        })),
        { type: "canvas.done", payload: { canvas_id: canvasId, turn_id: turnId, section_count: sections.length, errors: [] } },
      ],
    };
  }

  if (/resultsworkspace/i.test(message)) {
    const dataSection = sec("sec_results_data", "chart", {
      spec: {
        kind: "table",
        title: "各市场净营收",
        rows: [
          { market: "华东", revenue: 120 },
          { market: "华南", revenue: 90 },
        ],
        columns: [
          { key: "market", header: "市场", type: "categorical" },
          { key: "revenue", header: "净营收", type: "numeric" },
        ],
        data_artifact_id: "art_stdout",
        row_count: 2,
        limit_rows: 30,
      },
    });
    const codeSection = sec("sec_results_code", "code_block", {
      lang: "python",
      code: "print(19 * 29)",
      stdout_preview: "551\n",
      output_artifacts: [{
        artifact_id: "art_stdout",
        filename: "sandbox-stdout.txt",
        mime: "text/plain",
      }],
      summary_payload: {
        n_lines: 1,
        statement_summary: "Python calculation",
        dur_ms: 551,
      },
    });
    const sections = [...reportSections(), dataSection, codeSection];
    return {
      responseKind: "canvas",
      finalSections: sections,
      sourceRefs: [
        { n: 1, label: "sandbox output", tool: "python_sandbox", artifact_id: "art_stdout" },
      ],
      events: [
        openEvt,
        ...sections.map((section, index) => ({
          type: "canvas.section_added",
          payload: { canvas_id: canvasId, section, seq: index },
        })),
        {
          type: "canvas.done",
          payload: { canvas_id: canvasId, turn_id: turnId, section_count: sections.length, errors: [] },
        },
      ],
    };
  }

  // chat-mode: Lead streams sec_lead_* sections → sections_hidden → empty by-session
  if (/chat-?mode|hidden|闲聊/i.test(message)) {
    const leadId = `sec_lead_${turnId}_1`;
    return {
      responseKind: "chat",
      finalSections: [], // Filtered out of by-session after sections_hidden
      sourceRefs: [],
      events: [
        { type: "canvas.turn_streaming", payload: { turn_id: turnId } },
        {
          type: "canvas.section_streaming",
          payload: { section_id: leadId, kind: "narrative", partial_payload: {} },
        },
        { type: "canvas.section_chunk", payload: { section_id: leadId, delta: "这是一段" } },
        { type: "canvas.section_chunk", payload: { section_id: leadId, delta: "闲聊回答。" } },
        {
          type: "canvas.section_done",
          payload: { section_id: leadId, payload: { id: leadId, kind: "narrative", markdown: "这是一段闲聊回答。" }, is_partial: false },
        },
        {
          type: "canvas.sections_hidden",
          payload: { turn_id: turnId, hidden_section_ids: [leadId], rationale: "chat-mode" },
        },
      ],
    };
  }

  // canvas-mode Lead narrative: buffered sec_lead_* sections + canvas.done (report-worthy, **no**
  // sections_hidden) → reveal sections at turn done and **retain** them. This automatically captures
  // the three-perspective review P0: the old bug empties the buffer at canvas.done→onCanvasDone → done misreads it as empty →
  // removes report sections (reconcile restores them, drift>0). The fix makes live state correct and reconcile a no-op (lastDiff==0).
  if (/leadcanvas|叙事/i.test(message)) {
    const leadId = `sec_lead_${turnId}_1`;
    const md = "华东区 GMV 环比增长 8.3%，生鲜品类拉动。";
    const leadSection = sec(leadId, "narrative", { markdown: md });
    return {
      responseKind: "canvas",
      finalSections: [leadSection], // Report-worthy, not hidden → present in by-session
      sourceRefs: [],
      events: [
        openEvt,
        { type: "canvas.turn_streaming", payload: { turn_id: turnId } },
        { type: "canvas.section_streaming", payload: { section_id: leadId, kind: "narrative", partial_payload: {} } },
        { type: "canvas.section_chunk", payload: { section_id: leadId, delta: "华东区 GMV 环比增长 8.3%，" } },
        { type: "canvas.section_chunk", payload: { section_id: leadId, delta: "生鲜品类拉动。" } },
        { type: "canvas.section_done", payload: { section_id: leadId, payload: leadSection, is_partial: false } },
        // canvas.done precedes turn done, with **no** sections_hidden (report-worthy).
        { type: "canvas.done", payload: { canvas_id: canvasId, turn_id: turnId, section_count: 1, errors: [] } },
      ],
    };
  }

  // DF-04 fault injection: python automatically projects code_block, but the final hide frame is lost and
  // by-session still returns this auxiliary section. The frontend must guard the Report surface independently,
  // preventing a raw workspace section from opening an empty report; explicit Data focus can still open the workspace.
  if (/pythondata|sandboxdata/i.test(message)) {
    const codeSection = sec("sec_python_stdout", "code_block", {
      lang: "python",
      code: "print(19 * 29)",
      stdout: "551\n",
      artifact_id: "art_stdout",
    });
    return {
      responseKind: "chat",
      finalSections: [codeSection],
      sourceRefs: [],
      events: [
        ...sandboxStepEvents(turnId),
        openEvt,
        {
          type: "canvas.section_added",
          payload: { canvas_id: canvasId, section: codeSection, seq: 0 },
        },
        {
          type: "canvas.done",
          payload: { canvas_id: canvasId, turn_id: turnId, section_count: 1, errors: [] },
        },
        {
          type: "step_list_card_finalized",
          payload: { card_id: turnId, state: "completed", step_count: 1 },
        },
      ],
    };
  }

  // fallback-empty: emit only fallback canvas.done and empty sections
  if (/fallback|empty|空/i.test(message)) {
    return {
      responseKind: "chat",
      finalSections: [],
      sourceRefs: [],
      events: [
        {
          type: "canvas.done",
          payload: { canvas_id: canvasId, turn_id: turnId, section_count: 0, errors: [], source: "turn_done_chokepoint_fallback" },
        },
      ],
    };
  }

  // streaming-chunk: accumulate a single narrative chunk by chunk (for double accumulation on reconnect tests)
  if (/stream|chunk|重连/i.test(message)) {
    const nid = "sec_stream_narr";
    const full = "第一段。第二段。第三段。";
    return {
      responseKind: "canvas",
      finalSections: [sec(nid, "narrative", { markdown: full })],
      sourceRefs: [],
      events: [
        openEvt,
        { type: "canvas.section_streaming", payload: { section_id: nid, kind: "narrative", partial_payload: {} } },
        { type: "canvas.section_chunk", payload: { section_id: nid, delta: "第一段。" } },
        { type: "canvas.section_chunk", payload: { section_id: nid, delta: "第二段。" } },
        { type: "canvas.section_chunk", payload: { section_id: nid, delta: "第三段。" } },
        { type: "canvas.section_done", payload: { section_id: nid, payload: sec(nid, "narrative", { markdown: full }), is_partial: false } },
        { type: "canvas.done", payload: { canvas_id: canvasId, turn_id: turnId, section_count: 1, errors: [] } },
      ],
    };
  }

  // perfburst: **rendering cost probe** for report-streaming Batch B (not a functional test).
  //
  // Question: can the frontend handle high-frequency chunks added to a report of **realistic size**?
  // Background: the report body is one TipTap editor and the whole report one ProseMirror doc; any section
  // content change → new initialDoc identity → `editor.commands.setContent` **rebuilds the entire document**.
  // Streaming through this path rebuilds the full document for every chunk.
  //
  // Size comes from measured 116 production data (discovery §8 unverified item #4):
  //   Canvas section counts p50=11 / p90=28 / max=96; narrative body avg 1702 characters (largest tier 2k-4k).
  // Start with **p90's 28 sections** and a 2k-character body, then inject 60 chunks
  // (≈ 3 seconds at 50ms throttling).
  //
  // A number in the keyword adjusts section count: `perfburst28` / `perfburst11` / `perfburst96`.
  const perfMatch = /perfburst(\d*)/i.exec(message);
  if (perfMatch) {
    const nSections = Number(perfMatch[1]) || 28;
    const nid = "sec_perf_narr";
    const filler = "华东区 GMV 环比增长 8.3%，主要由生鲜品类拉动；客单价小幅回落。".repeat(30); // ≈2k characters
    const base = [];
    for (let i = 0; i < nSections - 1; i++) {
      base.push(
        i % 3 === 0
          ? sec(`sec_perf_c${i}`, "chart", {
              spec: { title: `图 ${i}`, kind: "bar", data: [{ x: "沪", y: 12 }, { x: "苏", y: 9 }] },
            })
          : sec(`sec_perf_n${i}`, "narrative", { markdown: `## 小节 ${i}\n\n${filler}` }),
      );
    }
    const grown = sec(nid, "narrative", { markdown: "" });
    const chunks = [];
    for (let i = 0; i < 60; i++) {
      chunks.push({ type: "canvas.section_chunk", payload: { section_id: nid, delta: "增长了八点三个百分点。" } });
    }
    return {
      responseKind: "canvas",
      finalSections: [...base, sec(nid, "narrative", { markdown: "增长了八点三个百分点。".repeat(60) })],
      sourceRefs: [],
      events: [
        openEvt,
        ...base.map((s, i) => ({ type: "canvas.section_added", payload: { canvas_id: canvasId, section: s, seq: i } })),
        { type: "canvas.section_added", payload: { canvas_id: canvasId, section: grown, seq: nSections } },
        ...chunks,
        { type: "canvas.done", payload: { canvas_id: canvasId, turn_id: turnId, section_count: nSections, errors: [] } },
      ],
    };
  }

  // Default: report (payload-driven section_added, immediately visible)
  const sections = reportSections();
  return {
    responseKind: "canvas",
    finalSections: sections,
    sourceRefs: [
      { n: 1, label: "bi_query: 华东区 GMV", tool: "bi_query", artifact_id: "art_e2e_1", query: "华东区 GMV" },
    ],
    events: [
      openEvt,
      ...sections.map((s, i) => ({
        type: "canvas.section_added",
        payload: { canvas_id: canvasId, section: s, seq: i },
      })),
      { type: "canvas.done", payload: { canvas_id: canvasId, turn_id: turnId, section_count: sections.length, errors: [] } },
    ],
  };
}

// ── Prime: choose the scenario, compute persisted state, and build the evt_seq event stream ─────────
export function primeTurn(message, sessionId = "s_e2e") {
  turnCounter += 1;
  const turnId = `t_canvas_${turnCounter}`;
  const scn = buildScenario(message || "", turnId, sessionId);
  artifactsBySession.set(
    sessionId,
    /resultsworkspace/i.test(message || "")
      ? resultsWorkspaceArtifacts()
      : defaultArtifacts(),
  );

  // Persisted final state (complete, unaffected by drop)
  const canvasId = `cv_${sessionId}`;
  canvasBySession.set(sessionId, {
    canvas: scn.finalSections.length > 0
      ? { id: canvasId, sessionId, workspaceId: "ws_test", title: "E2E 报告", status: "active" }
      : null,
    sections: scn.finalSections,
    sourceRefs: scn.sourceRefs || [],
  });

  // Build the stream: turn_started → scenario events → final_answer → final done. evt_seq increases throughout.
  const seqRef = { v: 0 };
  const stamp = (type, payload) => ({ type, payload: { ...payload, evt_seq: (seqRef.v += 1), seq: payload.seq ?? seqRef.v } });

  const events = [
    // Include session_id (the real backend includes it in turn_started) → frontend captures it to locate by-session for final reconciliation.
    stamp("turn_started", { turn_id: turnId, session_id: sessionId }),
    ...scn.events.map((e) => stamp(e.type, e.payload)),
    stamp("final_answer", { turn_id: turnId, text: "完成。", reason_code: "ok" }),
    stamp("done", { turn_id: turnId, session_id: sessionId, response_kind: scn.responseKind, status: "completed", tokens_in: 10, tokens_out: 20, tool_invocation_count: 1 }),
  ];
  turnScript.set(turnId, { sessionId, events });
  turnConnects.set(turnId, 0);
  return { turnId, sessionId };
}

// ── SSE replay ─────────────────────────────────────────────────
function parseDrops(url) {
  // ?drop=canvas.section_added:1,canvas.done → Map<type, Set<n>|"all">
  const raw = url.searchParams.get("drop");
  const drops = new Map();
  if (!raw) return drops;
  for (const spec of raw.split(",")) {
    const [type, n] = spec.split(":");
    if (!type) continue;
    if (n) {
      const set = drops.get(type) || new Set();
      set.add(Number(n));
      drops.set(type, set);
    } else {
      drops.set(type, "all");
    }
  }
  return drops;
}

export function serveStream(req, res, url, turnId) {
  const script = turnScript.get(turnId);
  res.writeHead(200, {
    "content-type": "text/event-stream; charset=utf-8",
    "cache-control": "no-store",
    connection: "keep-alive",
    // The frontend EventSource deliberately uses an **absolute** apiBase URL to bypass Next dev proxy SSE buffering
    // (use-turn-runner.ts:1791 new EventSource(absUrl)) → cross-origin request to this mock server →
    // CORS is required; otherwise EventSource cannot connect and all canvas events are lost (live sections=0).
    // No withCredentials → "*" is sufficient.
    "access-control-allow-origin": "*",
  });
  if (!script) {
    // Unknown turn (e.g. resume mock): immediately synthesize replayed done to finish.
    res.write(`event: done\ndata: ${JSON.stringify({ turn_id: turnId, status: "replayed", seq: 0, evt_seq: 0 })}\n\n`);
    res.end();
    return;
  }

  const connectN = (turnConnects.get(turnId) || 0) + 1;
  turnConnects.set(turnId, connectN);
  const drops = parseDrops(url);
  const cut = url.searchParams.get("cut") ? Number(url.searchParams.get("cut")) : 0;
  const seenType = new Map(); // type → count (determines drop's nth occurrence)

  let emitted = 0;
  for (const ev of script.events) {
    const c = (seenType.get(ev.type) || 0) + 1;
    seenType.set(ev.type, c);
    const rule = drops.get(ev.type);
    if (rule === "all" || (rule instanceof Set && rule.has(c))) {
      continue; // Drop this frame
    }
    // ?cut applies only to the first connection: after N events (counted after drop), disconnect without done to simulate a midstream failure.
    if (cut > 0 && connectN === 1 && emitted >= cut) {
      res.destroy();
      return;
    }
    res.write(`id: ${ev.payload.evt_seq}\nevent: ${ev.type}\ndata: ${JSON.stringify(ev.payload)}\n\n`);
    emitted += 1;
  }
  res.end();
}

export function getBySession(sid) {
  return canvasBySession.get(sid) || { canvas: null, sections: [], sourceRefs: [] };
}

export function getSessionArtifacts(sid) {
  return artifactsBySession.get(sid) || defaultArtifacts();
}

// Test isolation: clear state (each spec beforeEach can POST /api/v1/_test/reset)
export function resetCanvasMock() {
  canvasBySession.clear();
  artifactsBySession.clear();
  turnScript.clear();
  turnConnects.clear();
  turnCounter = 0;
}
