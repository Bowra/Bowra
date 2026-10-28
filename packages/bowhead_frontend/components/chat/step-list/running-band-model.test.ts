/**
 * running-band-model — data-extraction layer guards.
 *
 * These four guards cover **real defects across three implementations** (handoff document §5 items 6/7/8),
 * rather than imagined boundaries: lost concurrent steps, rejected filenames/table names, overly coarse handoffs.
 * Each records its mutation (restore old implementation → must fail); keep these records.
 */
import { describe, expect, it } from "vitest";

import {
  HISTORY_ROWS,
  buildBandModel,
  humanLabel,
  stripInternal,
} from "./running-band-model";
import { getToolMeta } from "./tool-catalog";
import type { StepView } from "./types";

/**
 * ⚠ Deliberately not `Partial<StepView>`: fixtures need shapes **broader at runtime than the types**;
 * `toolName`'s `ToolName` union cannot express `teammcp.<source>.<op>` (actually emitted by backend),
 * and store `headerZh` can be `null`. Type-valid fixtures cannot guard either case
 * (`docs/单元测试规范.md`: `运行时说谎时，fixture 必须造出类型上非法的形状`).
 */
function step(over: Record<string, unknown>): StepView {
  return {
    stepId: "s1",
    parentStepId: null,
    concurrencyGroupId: null,
    stepType: "tool_run",
    toolName: "bi_query",
    headerZh: "查询数据",
    state: "done",
    ...over,
  } as unknown as StepView;
}

describe("stripInternal — 只管内部串，不管形状", () => {
  it("116 实测直出的三种内部串一律整串丢弃", () => {
    expect(stripInternal('读取 pinned dataset "2025.csv" (artifact_id=art_5abc)')).toBe("");
    expect(stripInternal("运行: python_sandbox")).toBe("");
    expect(stripInternal("exit_code=1")).toBe("");
  });

  it("裸 JSON 丢弃", () => {
    expect(stripInternal('{"code":"print(1)"}')).toBe("");
    expect(stripInternal("[1,2,3]")).toBe("");
  });

  it("★ 文件名与表名**必须留着** —— 上一版的形状启发式把它们杀了", () => {
    // Mutation: restore /^[a-z][a-z0-9]*([_.][a-z0-9]+)+$/i → these three assertions must fail.
    expect(stripInternal("sales_2025.csv")).toBe("sales_2025.csv");
    expect(stripInternal("fact_orders")).toBe("fact_orders");
    expect(stripInternal("2025年客流量_94753e7.csv")).toBe("2025年客流量_94753e7.csv");
  });

  it("★ 后端把工具 slug 烤进了抬头 —— 末尾的 `(bi_query)` 必须砍掉", () => {
    // A real boundary: `_TOOL_HEADER_ZH` (bowhead_core/subagent.py:5154) contains literal values
    // `"查询数据 (bi_query)"` / `"查 schema (bi_schema)"` / `"搜知识库 (kb_search)"` /
    // `"跑代码 (Python)"`, the product's most common steps. None match the forbidden-token blacklist,
    // so current renders `正在查询数据 (bi_query)`, precisely the jargon PRD A5 removes.
    // ⚠ Originally **every** fixture here used clean `"查询数据"`, keeping all tests green.
    // Mutation: remove `stripToolSlugSuffix` → this test must fail.
    expect(stripInternal("查询数据 (bi_query)")).toBe("查询数据");
    expect(stripInternal("查 schema (bi_schema)")).toBe("查 schema");
    expect(stripInternal("搜知识库 (kb_search)")).toBe("搜知识库");
    expect(stripInternal("跑代码 (Python)")).toBe("跑代码");
  });

  it("中文括注不能被误砍（只砍纯 ASCII 标识符的尾巴）", () => {
    expect(stripInternal("查询数据（2025年）")).toBe("查询数据（2025年）");
    expect(stripInternal("查询数据 (近 30 天)")).toBe("查询数据 (近 30 天)");
  });

  it("`查 schema` 不算内部串（禁的是英文报错原文，不是这个词）", () => {
    expect(stripInternal("查 schema · 27 测度 + 8 维度")).toBe("查 schema · 27 测度 + 8 维度");
  });
});

describe("humanLabel — 认不出就留空，绝不出裸 slug", () => {
  it("后端中文抬头优先", () => {
    expect(humanLabel(step({ headerZh: "查 schema · 27 测度" }), false)).toBe("查 schema · 27 测度");
  });

  it("抬头被禁词滤掉 → 落回工具目录的人话", () => {
    const s = step({ toolName: "python_sandbox", headerZh: "运行: python_sandbox" });
    expect(humanLabel(s, false)).toBe("运行代码");
    // active uses verb form (catalog activeName when present).
    expect(humanLabel(s, true)).toBe("运行代码");
  });

  it("★ 认不出的工具 → 空串（渲染层写「处理中」），不是裸工具名", () => {
    expect(humanLabel(step({ toolName: "brand_new_tool", headerZh: null }), true)).toBe("");
    // Heading identical to tool name = no human-readable backend copy, also unrecognized → catalog fallback.
    // tool_aliases.yaml controls wording; guard only the absence of a raw slug,
    // rather than the previous fixed `搜索网页`, which falsely failed on copy changes instead of testing this invariant.
    const fallback = humanLabel(step({ toolName: "web_search", headerZh: "web_search" }), false);
    expect(fallback).not.toBe("web_search");
    expect(fallback).toBe(getToolMeta("web_search")?.name);
  });

  it("★ 团队工具（teammcp.*）不吐目录合成的裸 operation", () => {
    // getToolMeta synthesizes { name: <op> } for teammcp.*, a slug rather than human-readable copy.
    expect(humanLabel(step({ toolName: "teammcp.jira.list_issues", headerZh: null }), true))
      .toBe("");
    // Use human-readable backend headings normally when provided.
    expect(humanLabel(step({ toolName: "teammcp.jira.list_issues", headerZh: "查 Jira 工单" }), true))
      .toBe("查 Jira 工单");
  });
});

describe("三行结构 — 高度恒定的前提", () => {
  it("零步骤：preparing + 两条骨架 + 回落 Lead", () => {
    const m = buildBandModel({ steps: [] });
    expect(m.phase).toBe("preparing");
    expect(m.stepOrdinal).toBe(0);
    expect(m.history).toHaveLength(HISTORY_ROWS);
    expect(m.history.every((r) => r.kind === "skeleton")).toBe(true);
    expect(m.current?.agentName).toBe("Bowhead");
  });

  it("一步：仍是两条历史（一条骨架 + 一条真步）", () => {
    const m = buildBandModel({ steps: [step({ stepId: "a", state: "active" })] });
    expect(m.history).toHaveLength(HISTORY_ROWS);
    expect(m.history.filter((r) => r.kind === "skeleton")).toHaveLength(HISTORY_ROWS);
  });

  it("五步：仍是两条历史（只留最近的）", () => {
    const steps = ["a", "b", "c", "d", "e"].map((id, i) =>
      step({ stepId: id, state: i === 4 ? "active" : "done" }));
    const m = buildBandModel({ steps });
    expect(m.history).toHaveLength(HISTORY_ROWS);
    expect(m.history.map((r) => r.key)).toEqual(["c", "d"]);
  });

  it("★ 空窗（全部做完、下一步还没起）走 preparing，不把做完的事报成「正在…」", () => {
    const m = buildBandModel({ steps: [step({ stepId: "a", state: "done" })] });
    expect(m.phase).toBe("preparing");
    expect(m.current?.what).toBe("");
  });
});

describe("★ 并发不丢步（交接 §5 第 6 条）", () => {
  it("active 之后还有已完成步时，那一步仍在历史里", () => {
    // Mutation: restore others to `steps.slice(0, activeIdx)` → this test must fail.
    const steps = [
      step({ stepId: "a", state: "done" }),
      step({ stepId: "b", state: "active" }),
      step({ stepId: "c", state: "done" }),
    ];
    const m = buildBandModel({ steps });
    expect(m.current).not.toBeNull();
    expect(m.history.map((r) => r.key)).toEqual(["a", "c"]);
    expect(m.stepOrdinal).toBe(3);
  });

  it("两个并发 active：锁定**最后一个**，另一个进历史", () => {
    const steps = [
      step({ stepId: "a", state: "active" }),
      step({ stepId: "b", state: "active" }),
    ];
    const m = buildBandModel({ steps });
    expect(m.history.map((r) => r.key)).toEqual(["__skeleton_0", "a"]);
    expect(m.history[1].tone).toBe("active");
  });
});

describe("★ 交棒判定用 peer id，不用 AgentKey（交接 §5 第 8 条）", () => {
  const peerNames = { p1: "compute", p2: "compute" };
  const peerDisplayNames = { p1: "销售分析", p2: "库存分析" };

  it("同一 profile 的两个 peer 交棒**要**播报", () => {
    // Mutation: compare agentKeyOf instead of ownerIdOf → this test must fail (both are "compute").
    const m = buildBandModel({
      steps: [
        step({ stepId: "a", state: "done", subagentId: "p1" }),
        step({ stepId: "b", state: "active", subagentId: "p2" }),
      ],
      peerNames,
      peerDisplayNames,
    });
    expect(m.current?.handoffFrom).toBe("销售分析");
    expect(m.current?.agentName).toBe("库存分析");
  });

  it("★ 上一步**仍在跑**时不算交棒（并发两个 peer 时那是假话）", () => {
    // Handoff means the previous owner finished and ownership changed. With concurrent peers, the previous step remains active,
    // so `接手自 X` is false: X has not handed over and is still working.
    // Mutation: remove `prev.state !== "active"` → this test must fail.
    const m = buildBandModel({
      steps: [
        step({ stepId: "a", state: "active", subagentId: "p1" }),
        step({ stepId: "b", state: "active", subagentId: "p2" }),
      ],
      peerNames,
      peerDisplayNames,
    });
    expect(m.current?.handoffFrom).toBeNull();
  });

  it("★ 两个 peer 的**显示名相同**时不播报（116 dogfood 实测）", () => {
    // A plan turn spawned three `compute` peers, all named `主分析`, producing footnote
    // `主分析 · … · 接手自 主分析`; current agent and handoff source have identical names,
    // offering users no information and appearing buggy.
    // Different peer IDs are internal facts; identical display names = **no user-visible owner change**.
    // Mutation: remove `prevName !== curName` → this test must fail.
    const m = buildBandModel({
      steps: [
        step({ stepId: "a", state: "done", subagentId: "p1" }),
        step({ stepId: "b", state: "active", subagentId: "p2" }),
      ],
      peerNames: { p1: "compute", p2: "compute" },
      peerDisplayNames: { p1: "主分析", p2: "主分析" },
    });
    expect(m.current?.agentName).toBe("主分析");
    expect(m.current?.handoffFrom).toBeNull();
  });

  it("同一个 peer 连续跑不播报", () => {
    const m = buildBandModel({
      steps: [
        step({ stepId: "a", state: "done", subagentId: "p1" }),
        step({ stepId: "b", state: "active", subagentId: "p1" }),
      ],
      peerNames,
      peerDisplayNames,
    });
    expect(m.current?.handoffFrom).toBeNull();
  });

  it("Lead → peer 也算交棒（非 peer 步统一算 lead）", () => {
    const m = buildBandModel({
      steps: [
        step({ stepId: "a", state: "done" }),
        step({ stepId: "b", state: "active", subagentId: "p1" }),
      ],
      peerNames,
      peerDisplayNames,
    });
    expect(m.current?.handoffFrom).toBe("Bowhead");
  });

  it("徽章拿的是后端 agent_name 原值，不是 AgentKey", () => {
    const m = buildBandModel({
      steps: [step({ stepId: "b", state: "active", subagentId: "p1" })],
      peerNames: { p1: "some-custom-agent" },
      peerDisplayNames: {},
    });
    expect(m.current?.agentBadgeName).toBe("some-custom-agent");
  });
});

describe("tone — 失败要留在账上", () => {
  it("preserves warning severity and counts the original failed step", () => {
    const m = buildBandModel({
      steps: [
        step({ stepId: "a", state: "failed", summary: "超时", error: { kind: "transient" } as never }),
        step({ stepId: "b", state: "active" }),
      ],
    });
    const row = m.history.find((r) => r.key === "a")!;
    expect(row.tone).toBe("warn");
    expect(row.meta).toBe("");
    expect(m.issueCount).toBe(1);
  });

  it("★ 硬失败进历史仍是红，不因为「Lead 绕开了」就报成绿 ✓", () => {
    const m = buildBandModel({
      steps: [
        step({ stepId: "a", state: "failed", error: { kind: "permanent" } as never }),
        step({ stepId: "b", state: "active" }),
      ],
    });
    expect(m.history.find((r) => r.key === "a")!.tone).toBe("fail");
    expect(m.issueCount).toBe(1);
  });
});

describe("★ peer 终态进带（运行态账本关掉后，这是它唯一的落点）", () => {
  const peerNames = { p1: "compute", p2: "compute" };
  const peerDisplayNames = { p1: "主分析", p2: "库存分析" };

  it("零子步骤的 peer 挂掉 → 历史行上出一行红 ✕", () => {
    // Without this, direct-LLM-result / early-hard-failure / plan-path peers are entirely invisible on the band.
    // Mutation: omit `peerTerminalById` from buildBandModel (or ignore it) → this test must fail.
    const m = buildBandModel({
      steps: [step({ stepId: "a", state: "active" })],
      peerNames, peerDisplayNames,
      peerTerminalById: { p1: { status: "failed", evtSeq: 5 } },
    });
    const row = m.history.find((r) => r.kind === "peer");
    expect(row).toBeTruthy();
    expect(row!.tone).toBe("fail");
    expect(row!.peerOutcome).toBe("fail");
    expect(row!.peerName).toBe("主分析");
  });

  it("★ **有**子步骤的 peer **成功**时不出终态行（成功由它的绿步骤体现）", () => {
    // An extra `已完成` row adds noise and appears before its actual work.
    // Mutation: stop excluding peers with child steps from `ownedPeerIds` → this test must fail.
    const m = buildBandModel({
      steps: [
        step({ stepId: "a", state: "done", subagentId: "p1" }),
        step({ stepId: "b", state: "active" }),
      ],
      peerNames, peerDisplayNames,
      peerTerminalById: { p1: { status: "succeeded", evtSeq: 9 } },
    });
    expect(m.history.some((r) => r.kind === "peer")).toBe(false);
  });

  it("★★★ 但**有**子步骤的 peer **挂掉**时一定要出红行（fail/done 刻意不对称）", () => {
    // Seventh independent review caught a real sprint regression: before ③ disabled the ledger, it correctly
    // showed peer terminals; afterward these failures had only spawn's
    // `派子 agent · 已重试` (backend hardcoded is_transient), **without even a red row**.
    // Failure is new information, so include it; success already appears in green steps, so may omit it: asymmetric rules.
    // Mutation: remove `.filter(id => !failPeerIds.has(id))` from `ownedPeerIds` → this test must fail.
    const m = buildBandModel({
      steps: [
        step({ stepId: "a", state: "done", subagentId: "p1", evtSeq: 1 }),
        step({ stepId: "b", state: "active", evtSeq: 2 }),
      ],
      peerNames, peerDisplayNames,
      peerTerminalById: { p1: { status: "failed", evtSeq: 9 } },
    });
    const peerRow = m.history.find((r) => r.kind === "peer");
    expect(peerRow).toBeTruthy();
    expect(peerRow!.peerOutcome).toBe("fail");
    expect(m.peerFailCount).toBe(1);
  });

  it("★★★ peer 已挂但 spawn 步还 active 时，当前行不许还在「正在派子 agent」", () => {
    // Eighth independent review: `peer_dispatch_failed` emits **inside** subagent_spawn
    // (subagent.py:5930, before the tool returns), creating a real window with
    // failed terminal state and still-active spawn.
    // Filtering only historical candidates previously rendered history as `主分析 未完成`,
    // while current showed `正在派子 agent` + spinner: two faces for one fact, one untruthful.
    // Mutation: move `visibleSteps` filtering back into `others` (history only) → this test must fail.
    const m = buildBandModel({
      steps: [
        step({ stepId: "q", state: "done", evtSeq: 1, headerZh: "查询数据" }),
        step({ stepId: "spawn", state: "active", toolName: "subagent_spawn",
          headerZh: "派子 agent", subagentId: "lead_1", peerProseKey: "p1", evtSeq: 2 }),
      ],
      peerNames, peerDisplayNames,
      peerTerminalById: { p1: { status: "failed", evtSeq: 3 } },
    });
    // Current must not remain on that failed spawn step.
    expect(m.current?.what).not.toContain("派子");
    expect(m.phase).toBe("preparing");        // No other active step → gap copy
    // Failure still gets its red row and normal count.
    expect(m.history.some((r) => r.kind === "peer" && r.peerOutcome === "fail")).toBe(true);
    expect(m.peerFailCount).toBe(1);
    // `第 N 步` still counts it, since that step really occurred.
    expect(m.stepOrdinal).toBe(2);
  });

  it("★★★ peer 挂掉时它的 spawn 行整行去掉，不许留着渲「已重试」", () => {
    // Two surfaces of one fact: retain the red terminal row, remove the misleading spawn row.
    // Backend hardcodes spawn failures as is_transient=True; retaining it puts `✕ 未完成` beside
    // `派子 agent · 已重试`, contradicting itself.
    // Mutation: skip `timelineSteps` filtering (use others directly) → this test must fail.
    const m = buildBandModel({
      steps: [
        step({ stepId: "spawn", state: "failed", toolName: "subagent_spawn",
          headerZh: "派子 agent", subagentId: "lead_1", peerProseKey: "p1", evtSeq: 1,
          error: { kind: "transient", uiKind: "permanent", message: "挂了" } }),
        step({ stepId: "t1", state: "done", subagentId: "p1", evtSeq: 2 }),
      ],
      peerNames, peerDisplayNames,
      peerTerminalById: { p1: { status: "failed", evtSeq: 3 } },
    });
    const rows = m.history.filter((r) => r.kind !== "skeleton");
    expect(rows.some((r) => r.key === "spawn")).toBe(false);
    expect(rows.some((r) => r.kind === "peer" && r.peerOutcome === "fail")).toBe(true);
    // The failed spawn remains a recorded step issue; it is not a retry receipt.
    expect(m.issueCount).toBe(1);
  });

  it("★★ 排序按 evtSeq —— 早挂的 peer 会被后来的步骤挤出历史行", () => {
    // Why use evtSeq instead of application-time steps.length or always appending:
    // always appending **permanently pins** a peer failing at step 1 to the last slot,
    // crowding out all subsequent steps and freezing history for the entire turn.
    // Mutation: make mergeTimeline always push peer rows to the end → this test must fail.
    const m = buildBandModel({
      steps: [
        step({ stepId: "a", state: "done", evtSeq: 1 }),
        step({ stepId: "b", state: "done", evtSeq: 10 }),
        step({ stepId: "c", state: "done", evtSeq: 11 }),
        step({ stepId: "d", state: "active", evtSeq: 12 }),
      ],
      peerNames, peerDisplayNames,
      peerTerminalById: { p1: { status: "failed", evtSeq: 2 } },
    });
    // Peer terminal occurred after step 1, before step 2, so was already pushed out.
    expect(m.history.map((r) => r.key)).toEqual(["b", "c"]);
  });

  it("★★ 同样按 evtSeq —— 刚挂的 peer 就该在历史行上", () => {
    // Positive control for the preceding test: alone it would also pass if peer rows could never enter history.
    const m = buildBandModel({
      steps: [
        step({ stepId: "a", state: "done", evtSeq: 1 }),
        step({ stepId: "b", state: "done", evtSeq: 2 }),
        step({ stepId: "c", state: "active", evtSeq: 3 }),
      ],
      peerNames, peerDisplayNames,
      peerTerminalById: { p1: { status: "failed", evtSeq: 99 } },
    });
    expect(m.history.map((r) => r.key)).toEqual(["b", "peer:p1"]);
  });

  it("没有 evtSeq 的 peer 终态回落到末尾（bypass-bus 事件没有序）", () => {
    const m = buildBandModel({
      steps: [
        step({ stepId: "a", state: "done", evtSeq: 1 }),
        step({ stepId: "b", state: "done", evtSeq: 2 }),
      ],
      peerNames, peerDisplayNames,
      peerTerminalById: { p1: { status: "failed" } },
    });
    expect(m.history.map((r) => r.key)).toEqual(["b", "peer:p1"]);
  });

  it("★ 带上只有 fail / done —— bail 在带的生命周期里不可达，不做那一态", () => {
    // Independent review found the former amber bail state + dedicated glyph + two i18n entries were all dead code.
    // Production `peerFailureRecovered` is `cardFinalState === "completed"`,
    // while the band requires `runner.running`, overlapping only for one or two frames between card_finalized and done,
    // yielding only a red-to-amber flash one frame before band unmount.
    // Saying the peer did not finish while running is truthful; discounting it because Lead took over
    // is retrospective, rendered by the ledger after the turn ends.
    // Mutation: pass external peerFailureRecovered through `bandTerminalStatus` → this test must fail.
    const m = buildBandModel({
      steps: [], peerNames, peerDisplayNames,
      peerTerminalById: { p1: { status: "failed", evtSeq: 7 } },
    });
    const row = m.history.find((r) => r.kind === "peer")!;
    expect(row.peerOutcome).toBe("fail");
    expect(row.tone).toBe("fail");
    expect(m.peerFailCount).toBe(1);
  });

  it("成功的零子步骤 peer（直接 LLM 结果）也出行，且不计进失败数", () => {
    const m = buildBandModel({
      steps: [], peerNames, peerDisplayNames,
      peerTerminalById: { p1: { status: "succeeded", durationMs: 1200, evtSeq: 3 } },
    });
    const row = m.history.find((r) => r.kind === "peer")!;
    expect(row.peerOutcome).toBe("done");
    expect(row.tone).toBe("done");
    // Reuse the ledger's formatPeerDuration instead of implementing another formatter.
    expect(row.meta).toBe("1.2s");
    expect(m.peerFailCount).toBe(0);
  });

  it("★ 恒高不变：加了 peer 行之后历史仍恒为 HISTORY_ROWS", () => {
    const m = buildBandModel({
      steps: [], peerNames, peerDisplayNames,
      peerTerminalById: { p1: { status: "failed", evtSeq: 1 } },
    });
    expect(m.history).toHaveLength(HISTORY_ROWS);
  });

  it("peerIdsWithSteps 显式传入时压过从 steps 的推断（节流期不闪）", () => {
    // The band supplies **unthrottled** data: peer steps may remain queued when its terminal arrives;
    // deriving from released steps misclassifies it as zero-child-step, briefly showing a row that disappears after 450ms.
    // Mutation: ignore `peerIdsWithSteps`, always deriving from steps → this test must fail.
    const m = buildBandModel({
      steps: [],                       // No steps released by throttling yet
      peerNames, peerDisplayNames,
      // Use a **successful** terminal: failures always get rows (asymmetric fail/done), so a failure fixture would make this guard ineffective.
      peerTerminalById: { p1: { status: "succeeded", evtSeq: 4 } },
      peerIdsWithSteps: new Set(["p1"]),  // But it actually has child steps
    });
    expect(m.history.some((r) => r.kind === "peer")).toBe(false);
  });
});

describe("★ 散文尾巴进当前行（②，运行态账本关掉后它的唯一落点）", () => {
  const peerNames = { p1: "compute", p2: "compute" };
  const peerDisplayNames = { p1: "主分析", p2: "库存分析" };

  it("当前步归属 peer 的散文优先", () => {
    // Mutation: omit `peerProse` from the model / always return empty from `proseTailOf` → this test must fail.
    const m = buildBandModel({
      steps: [step({ stepId: "a", state: "active", subagentId: "p2" })],
      peerNames, peerDisplayNames,
      peerProse: { p1: "我在看退款率", p2: "我在按品牌汇总营业额" },
    });
    expect(m.current?.prose).toBe("我在按品牌汇总营业额");
  });

  it("★ 一步都没有的 peer 轮回落到最后一个非空散文", () => {
    // Existing `t-band-peer-prose` has this exact shape: peer + prose + zero steps.
    // Without this branch, ③ disabling the ledger silently loses that capability (currently green, unnoticed).
    // Mutation: remove branch ② from `proseTailOf` → this test must fail.
    const m = buildBandModel({
      steps: [], peerNames, peerDisplayNames,
      peerProse: { p1: "先看退款率的分布……" },
    });
    expect(m.current?.prose).toBe("先看退款率的分布……");
  });

  it("★ 空窗（步骤全做完、下一步还没起）也有散文 —— 这一态别的东西全是静止的", () => {
    const m = buildBandModel({
      steps: [step({ stepId: "a", state: "done", subagentId: "p1" })],
      peerNames, peerDisplayNames,
      peerProse: { p1: "正在核对口径" },
    });
    expect(m.phase).toBe("preparing");
    expect(m.current?.prose).toBe("正在核对口径");
  });

  it("★★ **先切尾再过滤**：早先出现过一次禁词，不能让散文此后整轮消失", () => {
    // `stripInternal` discards the whole string on a forbidden match, while prose grows monotonically.
    // Filtering before windowing lets one earlier `payload` occurrence discard all subsequent prose.
    // Mutation: use `stripInternal(raw).slice(-PROSE_TAIL_CHARS)` in `proseTailOf` → this test must fail.
    // ⚠ Tail must exceed the **wide window** (PROSE_TAIL_CHARS + PROSE_TAIL_GUARD), putting the token truly outside.
    const poisoned = "先看了 payload 的结构，" + "然后按品牌汇总营业额与退款率，再排名。".repeat(6);
    const m = buildBandModel({
      steps: [step({ stepId: "a", state: "active", subagentId: "p1" })],
      peerNames, peerDisplayNames,
      peerProse: { p1: poisoned },
    });
    expect(m.current?.prose).not.toBe("");
    expect(m.current?.prose).not.toContain("payload");
  });

  it("禁词落在**尾窗内**时照样整串丢弃（过滤没有被绕过）", () => {
    // Positive control: the previous test alone also passes if filtering is disabled entirely.
    // Mutation: skip `stripInternal` in `proseTailOf` → this test must fail.
    const m = buildBandModel({
      steps: [step({ stepId: "a", state: "active", subagentId: "p1" })],
      peerNames, peerDisplayNames,
      peerProse: { p1: "读取 pinned dataset (artifact_id=art_5abc)" },
    });
    expect(m.current?.prose).toBe("");
  });

  it("★★ 压在窗口边界上的禁词不能漏出去（判定用宽窗、显示用窄窗）", () => {
    // Independent review found a real leak: cutting PROSE_TAIL_CHARS before filtering truncates `pinned dataset`
    // to `inned dataset`, missed by regex, leaking strings PRD A5 explicitly prohibits onto the main surface.
    // Mutation: `stripInternal(raw.slice(-PROSE_TAIL_CHARS))` (remove GUARD margin) → this test must fail.
    const m = buildBandModel({
      steps: [step({ stepId: "a", state: "active", subagentId: "p1" })],
      peerNames, peerDisplayNames,
      peerProse: {
        p1: '我先读取 pinned dataset "2025年客流量_94753e7.csv" 里的字段，再按品牌汇总营业额',
      },
    });
    expect(m.current?.prose).not.toContain("dataset");
    expect(m.current?.prose).toBe("");
  });

  it("★★ Lead 在跑时**不**回落别的 peer 的散文（归属不能串味）", () => {
    // Independent review's HIGH issue: unconditional fallback encounters Lead subagentId never being
    // a peerProse key → every Lead step falls back, rendering current as
    // Bowhead with a finished peer's words from minutes ago, **throughout the turn**.
    // Mutation: restore `allowFallback` to true in the running branch → this test must fail.
    const m = buildBandModel({
      steps: [
        step({ stepId: "a", state: "done", subagentId: "p1" }),
        step({ stepId: "b", state: "active" }),   // Lead's own step
      ],
      peerNames, peerDisplayNames,
      peerProse: { p1: "我早就说完了" },
    });
    expect(m.current?.agentName).toBe("Bowhead");
    expect(m.current?.prose).toBe("");
  });

  it("★ 拿不到名字的 peer 终态**不编名字**（编出来是中文常量，en-US 会串味）", () => {
    // Real path: `peer_dispatch_failed` before `agent_spawned`,
    // with neither peerNames nor peerDisplayNames containing the peer yet.
    // Mutation: fall back to `AGENT_META.generic.zh` for `peerName` → this test must fail.
    const m = buildBandModel({
      steps: [], peerTerminalById: { pX: { status: "failed", evtSeq: 1 } },
    });
    const row = m.history.find((r) => r.kind === "peer")!;
    expect(row.peerName).toBe("");
  });

  it("★★★ spawn 行上 peer 的 id 在 `peerProseKey`，不在 `subagentId`", () => {
    // `turn-store.ts:77-82` calls this inversion the 3-strikes bug: on Lead's
    // `subagent_spawn`, `subagentId` is **Lead's own ID**; peer ID lives in `peerProseKey`.
    // On the non-plan path it remains active throughout the peer's **entire lifecycle**; while producing
    // LLM tokens (prose arriving), no child tool step is active, so spawn becomes current.
    // Reading via `subagentId` = `peerProse[leadId]`, always undefined ⇒
    // **prose stays empty precisely in the window where it is most needed**.
    // ⚠ Other fixtures supply `subagentId` directly, never spawn rows, keeping all tests green;
    //   independent review caught this, rather than the author's guards.
    // Mutation: restore `rowPeerIdOf` to `s.subagentId ?? null` → this test must fail.
    const m = buildBandModel({
      steps: [step({
        stepId: "spawn", state: "active",
        toolName: "subagent_spawn", headerZh: "启动 数据专家",
        subagentId: "lead_1",     // ← Lead's own ID
        peerProseKey: "p1",       // ← peer ID lives here
      })],
      peerNames: { p1: "compute" },
      peerDisplayNames: { p1: "主分析" },
      peerProse: { p1: "我正在拆解营收构成" },
    });
    expect(m.current?.prose).toBe("我正在拆解营收构成");
    // Ownership must match: this row shows that peer, not Bowhead.
    expect(m.current?.agentName).toBe("主分析");
    expect(m.current?.owner).toBe("p1");
  });

  it("★ 但 profile 还没到时不认 —— 免得把内部 `sub_*` id 当成 agent 名", () => {
    // Same guard as `agent-grouping.ts:165-172`: without this ID in `peerNames`,
    // retain it in Lead's stream. Mutation: remove `peerNames[spawnFor]` from `rowPeerIdOf` → this test must fail.
    const m = buildBandModel({
      steps: [step({
        stepId: "spawn", state: "active",
        toolName: "subagent_spawn", headerZh: "启动",
        subagentId: "lead_1", peerProseKey: "sub_未知",
      })],
      peerNames: {},
      peerProse: { "sub_未知": "还没登记就说话" },
    });
    expect(m.current?.agentName).toBe("Bowhead");
    expect(m.current?.prose).toBe("");
  });

  it("★★ 零步骤回落时，散文归谁这一行就判给谁（不能恒写 Bowhead）", () => {
    // Independent review: fallback selects the last nonempty entry, but agentName always used
    // AGENT_META.lead.zh, rendering Bowhead with another peer's words,
    // the same misattribution just blocked in the model, particularly apparent with concurrent prose-only peers.
    // Mutation: always return the lead literal from that branch → this test must fail.
    const m = buildBandModel({
      steps: [],
      peerNames: { p1: "compute", p2: "compute" },
      peerDisplayNames: { p1: "销售分析", p2: "库存分析" },
      peerProse: { p1: "", p2: "我在看库存周转" },
    });
    expect(m.current?.prose).toBe("我在看库存周转");
    expect(m.current?.agentName).toBe("库存分析");
    expect(m.current?.owner).toBe("p2");
  });

  it("回落但那个 peer 还没登记名字 → 仍回落 Lead（不编造）", () => {
    const m = buildBandModel({ steps: [], peerProse: { pX: "无名氏说话" } });
    expect(m.current?.prose).toBe("无名氏说话");
    expect(m.current?.agentName).toBe("Bowhead");
  });

  it("★★ 散文里的 `[` / `{` 不能让整条散文闪没", () => {
    // `stripInternal`'s discard-on-initial-{/[ rule handles **whole strings**. Prose windows are cut from the middle,
    // with a cut advancing one character each frame; any `[` in prose (e.g. `[1]` citation marker) makes
    // the whole prose disappear for frames where the window starts there, then return.
    // Mutation: directly call `stripInternal(win)` again in `proseTailOf` → this test must fail.
    const m = buildBandModel({
      steps: [step({ stepId: "a", state: "active", subagentId: "p1" })],
      peerNames: { p1: "compute" },
      peerProse: { p1: "[1] 按品牌汇总营业额与退款率，再排名" },
    });
    expect(m.current?.prose).toContain("按品牌汇总");
  });

  it("★★ 只落在**余量区**的禁词不清空显示窗口（否则会闪 32 个字）", () => {
    // The margin (characters 49–80) serves only to match forbidden tokens overlapping the boundary.
    // Counting margin-only tokens as matches lets one `python_sandbox` swallow the next 32 prose characters,
    // although this cell's entire responsibility is conveying liveness.
    // Mutation: restore stripInternal over the entire wide window → this test must fail.
    // ⚠ Offsets are **calculated**, not estimated: `python_sandbox` ends 49 characters from the end (>48, outside
    //   the visible window) and starts 63 from the end (<=80, still detected inside the margin).
    //   An arbitrary string might overlap the visible window, testing a different condition.
    const m = buildBandModel({
      steps: [step({ stepId: "a", state: "active", subagentId: "p1" })],
      peerNames: { p1: "compute" },
      peerProse: {
        p1: "我先跑了 python_sandbox 里的脚本再核对了一遍口径然后按品牌汇总营业额与退款率并排名给出结论建议共十项另附三张图表统计对比",
      },
    });
    expect(m.current?.prose).toContain("按品牌汇总");
    expect(m.current?.prose).not.toContain("python_sandbox");
  });

  /** Lead spawn + peer terminal: **one row per peer**, truthfully reporting the fact. */
  function spawnOnlyPeer(status: "succeeded" | "failed") {
    return buildBandModel({
      steps: [step({
        stepId: "spawn", state: status === "failed" ? "failed" : "done",
        toolName: "subagent_spawn", headerZh: "派子 agent",
        subagentId: "lead_1", peerProseKey: "p1",
        // Backend **hardcodes** is_transient=True for spawn failure (subagent.py:4020/4047),
        // without diagnostics.retryable, the source of misreporting; fixtures must reproduce that shape.
        // ⚠ Reproduce the actual **post-projection** shape: backend gives kind:"transient" (hardcoded),
        //   but the single `step-view.ts` projection receives the peer's failed terminal and corrects
        //   `uiKind` to "permanent". The band consumes projected StepView,
        //   so uiKind:"transient" creates a production-unreachable shape, making the guard ineffective.
        ...(status === "failed"
          ? { error: { kind: "transient", uiKind: "permanent", message: "子任务失败" } }
          : {}),
      })],
      peerNames: { p1: "compute" },
      peerDisplayNames: { p1: "主分析" },
      peerTerminalById: { p1: { status, durationMs: 1200, evtSeq: 9 } },
    });
  }

  it("★★ 零子步骤 peer 只占**一行**（不是 spawn 行 + 终态行两行）", () => {
    // Mutation: restore `ownedPeerIds` to `steps.map(s => s.subagentId)` → two rows, this test must fail.
    const rows = spawnOnlyPeer("succeeded").history.filter((r) => r.kind !== "skeleton");
    expect(rows).toHaveLength(1);
    expect(rows[0].peerName).toBe("主分析");
    expect(rows[0].peerOutcome).toBe("done");
    expect(rows[0].meta).toBe("1.2s");     // Ownership and duration both remain
  });

  it("★★★ 零子步骤 peer **挂掉**时那一行必须说「没完成」，不能说「已重试」", () => {
    // Fifth independent review caught a regression **introduced while fixing the previous review**:
    // backend hardcodes spawn failure as is_transient=True; FE excludes self-healing only when
    // diagnostics.retryable===false ⇒ toneOf gives warn ⇒
    // `派子 agent · 已重试`. The peer hard-failed, rather than retrying.
    // Its existing spawn row causes independent terminal omission, leaving only that false claim on screen
    // (previously false row + real red row; the fix removed the truthful one).
    // Mutation: remove spawn rewriting from `rowForStep`, using `rowOf(st)` directly → this test must fail.
    const rows = spawnOnlyPeer("failed").history.filter((r) => r.kind !== "skeleton");
    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe("peer");
    expect(rows[0].peerOutcome).toBe("fail");
    expect(rows[0].tone).toBe("fail");        // Red, not amber
    expect(rows[0].peerName).toBe("主分析");
  });

  it("★★★ 「已重试」的计数也不能跟着谎报", () => {
    // `1 个未完成 · 已重试 1 次` contradicts itself: this peer never retried; it failed.
    // ⚠ Correction **moved up to projection** (`step-view.ts`'s `correctSpawnFailureKind`,
    //   benefiting band/collapsed strip/expanded ledger together); this guards that correct projected input
    //   is not counted as a retry by the band. Projection's own guard is in `step-view.correction.test.ts`.
    const m = spawnOnlyPeer("failed");
    expect(m.peerFailCount).toBe(1);
    expect(m.issueCount).toBe(1);
  });

  it("retains recorded transient failures in the issue count", () => {
    // The previous test alone also passes with issueCount always 0.
    const m = buildBandModel({
      steps: [
        step({ stepId: "r", state: "failed", error: { kind: "transient" } as never }),
        step({ stepId: "b", state: "active" }),
      ],
    });
    expect(m.issueCount).toBe(1);
  });

  it("★★★ **有**子步骤的 peer：spawn 行不许抢先宣布「已完成」", () => {
    // Sixth independent review: the previous spawn rewrite was too broad,
    // showing `主分析 已完成 · 120s` on spawn while that peer's work still appeared below;
    // completion preceded actual work, violating this module's no-terminal-row-for-peers-with-child-steps rule.
    // Common in multi-agent turns, rather than an edge case.
    // Mutation: remove `!childStepPeerIds.has(spawnFor)` from `rowForStep` → this test must fail.
    const m = buildBandModel({
      steps: [
        step({ stepId: "spawn", state: "done", toolName: "subagent_spawn",
          headerZh: "派子 agent", subagentId: "lead_1", peerProseKey: "p1" }),
        step({ stepId: "t1", state: "active", subagentId: "p1", headerZh: "查询数据" }),
      ],
      peerNames: { p1: "compute" }, peerDisplayNames: { p1: "主分析" },
      peerTerminalById: { p1: { status: "succeeded", durationMs: 120000, evtSeq: 9 } },
    });
    expect(m.history.some((r) => r.kind === "peer")).toBe(false);
  });

  it("★★ 名字还没到（peer_dispatch_failed 早于 agent_spawned）也只出一行，且不说谎", () => {
    // Sixth independent review: gating on `peerNames` left spawn unrewritten on this real path,
    // falsely rendering `已重试` alongside an extra unnamed terminal row: two rows, one false claim.
    // Mutation: add `peerNames[spawnFor]` to either `rowForStep` / `ownedPeerIds` → this test must fail.
    const m = buildBandModel({
      steps: [step({
        stepId: "spawn", state: "failed", toolName: "subagent_spawn",
        headerZh: "派子 agent", subagentId: "lead_1", peerProseKey: "sub_x",
        error: { kind: "transient", uiKind: "permanent", message: "挂了" },
      })],
      peerNames: {},   // agent_spawned has not arrived yet
      peerTerminalById: { sub_x: { status: "failed", evtSeq: 2 } },
    });
    const rows = m.history.filter((r) => r.kind !== "skeleton");
    expect(rows).toHaveLength(1);
    expect(rows[0].peerOutcome).toBe("fail");
    expect(rows[0].peerName).toBe("");        // Unnamed → rendering uses anonymous copy, never exposing internal ID
    expect(rows[0].tone).toBe("fail");
  });

  it("终态没带时长时，spawn 行自己的 meta 不能被换成空白", () => {
    const m = buildBandModel({
      steps: [step({
        stepId: "spawn", state: "done", toolName: "subagent_spawn",
        headerZh: "派子 agent", subagentId: "lead_1", peerProseKey: "p1",
        summary: "1 个子任务",
      })],
      peerNames: { p1: "compute" }, peerDisplayNames: { p1: "主分析" },
      peerTerminalById: { p1: { status: "succeeded", evtSeq: 9 } },   // No durationMs
    });
    const row = m.history.find((r) => r.kind === "peer")!;
    expect(row.meta).toBe("1 个子任务");
  });

  it("★★ 两个归属集合必须同源：spawn 行还没放行时不出独立行（否则 key 会跳）", () => {
    // Ninth independent review: `doneViaSpawnRow` used throttled steps, `childStepPeerIds`
    // unthrottled props; a zero-child-step peer done before spawn release first showed
    // independent key=`peer:p1`, then rewritten key=`spawn` after 450ms when spawn was released,
    // replaying entry animation and shifting the row as the React key changed.
    // Mutation: ignore `peerIdsWithSpawnRow` in `spawnRowPeerIds`, deriving only from steps → this test must fail.
    const m = buildBandModel({
      steps: [],                                  // Throttling has not released spawn yet
      peerNames, peerDisplayNames,
      peerTerminalById: { p1: { status: "succeeded", evtSeq: 5 } },
      peerIdsWithSteps: new Set(),
      peerIdsWithSpawnRow: new Set(["p1"]),       // But spawn really exists
    });
    // Spawn carries it → no independent row this frame; rewrite in place on release, retaining stepId throughout.
    expect(m.history.some((r) => r.kind === "peer")).toBe(false);
  });

  it("peer 还没终态时 spawn 行照常是普通步骤行（不提前改写）", () => {
    // Positive control: always treating spawn as peer rows must not satisfy the two preceding tests.
    const m = buildBandModel({
      steps: [
        step({ stepId: "spawn", state: "active", toolName: "subagent_spawn",
          headerZh: "派子 agent", subagentId: "lead_1", peerProseKey: "p1" }),
        step({ stepId: "b", state: "active" }),
      ],
      peerNames: { p1: "compute" }, peerDisplayNames: { p1: "主分析" },
    });
    const rows = m.history.filter((r) => r.kind !== "skeleton");
    expect(rows.every((r) => r.kind === "step")).toBe(true);
  });

  it("没有任何散文时为空串（渲染层据此不渲那一格）", () => {
    const m = buildBandModel({
      steps: [step({ stepId: "a", state: "active", subagentId: "p1" })],
      peerNames, peerDisplayNames,
    });
    expect(m.current?.prose).toBe("");
  });
});

describe("★★ 116 dogfood 逮到的两条", () => {
  const peerNames = { p1: "compliance" };
  const peerDisplayNames = { p1: "合规风险" };
  const spawn = (over: Record<string, unknown> = {}) => step({
    stepId: "spawn", state: "active", toolName: "subagent_spawn",
    headerZh: "派子 agent", subagentId: "lead_1", peerProseKey: "p1", ...over,
  });

  it("★★★ P1：spawn 行不能当当前行（116 上它 20 秒一字未变都是「正在派子 agent」）", () => {
    // `subagent_spawn` stays active throughout the peer's lifecycle; selecting the last active step
    // repeatedly chooses it: jargon (PRD A5 requires human-readable copy) and static (violates interaction principle 2).
    // Mutation: remove `!isSpawnRow(...)` from the `activeIdx` loop → this test must fail.
    const m = buildBandModel({
      steps: [step({ stepId: "q", state: "done", evtSeq: 1 }), spawn({ evtSeq: 2 })],
      peerNames, peerDisplayNames,
    });
    expect(m.current?.what).toBe("");                 // No longer renders `正在派子 agent`
    expect(m.current?.waitingOnPeer).toBe("合规风险"); // Instead identifies the awaited peer
    expect(m.phase).toBe("preparing");
  });

  it("★ 但 peer 的子步骤在跑时，当前行必须是那个子步骤（不是「正在等」）", () => {
    // Positive control: the prior test alone also passes if any spawn always means waiting,
    // falsely describing active peer work as idle waiting.
    const m = buildBandModel({
      steps: [
        spawn({ evtSeq: 1 }),
        step({ stepId: "t1", state: "active", subagentId: "p1",
          headerZh: "查询数据", evtSeq: 2 }),
      ],
      peerNames, peerDisplayNames,
    });
    expect(m.phase).toBe("running");
    expect(m.current?.what).toBe("查询数据");
    expect(m.current?.waitingOnPeer).toBeNull();
  });

  it("peer 已出终态时不再说「正在等」（它已经回来了）", () => {
    const m = buildBandModel({
      steps: [spawn({ evtSeq: 1 })],
      peerNames, peerDisplayNames,
      peerTerminalById: { p1: { status: "succeeded", evtSeq: 2 } },
    });
    expect(m.current?.waitingOnPeer).toBeNull();
  });

  it("★★★ `subagent_return` 的黑话抬头要让位给目录里的人话（不是跳过这一步）", () => {
    // 📏 On 116 v1.1.10, `正在子 agent 交付` remained from ms 119401 until band unmount.
    // ⚠ **Do not copy spawn's skip policy**: active here means the peer is actually delivering;
    //   skipping turns a real wait into a gap; only wording is wrong.
    // Mutation: remove `/子\s*agent\s*交付/` from `INTERNAL_TOKENS` → this test fails.
    const m = buildBandModel({
      steps: [step({
        stepId: "r", state: "active",
        toolName: "subagent_return", headerZh: "子 agent 交付",
        subagentId: "p1",
      })],
      peerNames: { p1: "compute" }, peerDisplayNames: { p1: "主分析" },
    });
    expect(m.current?.what).not.toContain("子 agent");
    // Fall back to existing human-readable tool-catalog copy
    expect(m.current?.what).toBe("整理结果");
  });

  it("★★ 当前行取**不带「正在」**的名字（渲染层会包一层「正在{what}」）", () => {
    // Catalog `activeName` already contains `正在`; wrapping it with `band_doing` produces
    // `正在正在整理结果`. Previously backend headings almost always prevented catalog fallback.
    // Mutation: restore `humanLabel(cur, false)` to `true` → this test fails.
    const m = buildBandModel({
      steps: [step({
        stepId: "r", state: "active",
        toolName: "subagent_return", headerZh: "子 agent 交付",
        subagentId: "p1",
      })],
      peerNames: { p1: "compute" }, peerDisplayNames: { p1: "主分析" },
    });
    expect(m.current?.what?.startsWith("正在")).toBe(false);
  });

  it("★ 正对照：历史行仍用 active 名（它不包「正在」那一层）", () => {
    const m = buildBandModel({
      steps: [
        step({ stepId: "r", state: "active", toolName: "subagent_return", headerZh: "子 agent 交付", subagentId: "p1" }),
        step({ stepId: "b", state: "active", subagentId: "p1" }),
      ],
      peerNames: { p1: "compute" }, peerDisplayNames: { p1: "主分析" },
    });
    const row = m.history.find((r) => r.key === "r");
    expect(row?.label).toBe("正在整理结果");
  });

  it("★★★ spawn 步**还没出散文**时也不许当当前行（判据是工具名不是 peerProseKey）", () => {
    // 📏 116 v1.1.6 dogfood: `正在派子 agent` remained from ms 75603 to 181201 (**105 seconds**),
    //    precisely the shape P1 should remove. Root cause: `isSpawnRow` checked `peerProseKey`,
    //    which identifies prose ownership and gains a value only when the peer starts producing text ⇒
    //    spawn without prose is not recognized and still becomes current.
    // Mutation: remove `toolName === "subagent_spawn"` from `isSpawnRow` → this test fails.
    const m = buildBandModel({
      steps: [
        step({ stepId: "a", state: "done" }),
        step({
          stepId: "spawn", state: "active",
          toolName: "subagent_spawn", headerZh: "派子 agent",
          subagentId: "lead_1",          // ← No peerProseKey (peer has not produced text yet)
        }),
      ],
      peerNames: {}, peerDisplayNames: {},
    });
    expect(m.current?.what).not.toBe("派子 agent");
    expect(m.current?.what).toBe("");        // Gap-copy fallback, with human-readable wording supplied by rendering
  });

  it("★ 正对照：**只**有 peerProseKey（工具名缺失的老事件）也仍然不当当前行", () => {
    // ⚠ The previous fixture supplied both fields, passing through the new branch too;
    //   removing `|| Boolean(st.peerProseKey)` left all 295 step-list tests green,
    //   with **zero fallback coverage** (independent review). **Deliberately omit toolName** here.
    // Mutation: remove `|| Boolean(st.peerProseKey)` from `isSpawnRow` → this test fails.
    const m = buildBandModel({
      steps: [
        step({ stepId: "a", state: "done" }),
        step({
          stepId: "spawn", state: "active",
          toolName: "legacy_tool_without_name", headerZh: "派子 agent",
          subagentId: "lead_1", peerProseKey: "p1",
        }),
      ],
      peerNames: { p1: "compute" }, peerDisplayNames: { p1: "主分析" },
    });
    expect(m.current?.what).not.toBe("派子 agent");
    expect(m.current?.waitingOnPeer).toBe("主分析");
  });

  it("★★ peer 已出终态时，认不出是哪个 peer 的 spawn 行不许说「正在等」", () => {
    // Independent review: freshness guard `peerTerminalById[st.peerProseKey ?? ""]`
    // is **ineffective without a key** (`peerTerminalById[""]` always undefined).
    // Thus an already failed peer's spawn still qualifies as waiting,
    // with fail in history and `正在等一个 Agent 返回` in current simultaneously, contradicting itself.
    // Mutation: remove `Boolean(st.peerProseKey)` from `waitingSpawn` → this test fails.
    const m = buildBandModel({
      steps: [
        step({ stepId: "a", state: "done" }),
        step({
          stepId: "spawn", state: "active",
          toolName: "subagent_spawn", headerZh: "派子 agent",
          subagentId: "lead_1",          // ← Cannot identify the peer
        }),
      ],
      peerNames: { p1: "compute" }, peerDisplayNames: { p1: "主分析" },
      peerTerminalById: { p1: { status: "failed", evtSeq: 9 } },
    });
    expect(m.current?.waitingOnPeerUnknown).toBe(false);
    expect(m.current?.waitingOnPeer).toBeNull();
  });

  it("★★★ 名字还没到时不许说「正在等 Bowhead 返回」（Lead 在等它自己）", () => {
    // 📏 Captured on 116 v1.1.5 dogfood: `.bh-band__act` = `正在等 Bowhead 返回`.
    //    Root cause: missing names make `displayNameOf` fall back to `AGENT_META.lead.zh`; correct
    //    for `agentName` (Lead really owns spawn), **false** for the awaited peer.
    // Mutation: remove the `agentNameOf(...)` precondition from `waitingOnPeer` → this test fails.
    const m = buildBandModel({
      steps: [step({
        stepId: "spawn", state: "active",
        toolName: "subagent_spawn", headerZh: "派子 agent",
        subagentId: "lead_1", peerProseKey: "p1",
      })],
      peerNames: {},          // ← Name has not arrived yet
      peerDisplayNames: {},
    });
    expect(m.current?.waitingOnPeer).toBeNull();
    expect(m.current?.waitingOnPeerUnknown).toBe(true);
  });

  it("★ 正对照：名字到了就要点名（别一律不点名）", () => {
    // The previous test alone also passes with always-null `waitingOnPeer`, disabling `正在等 X 返回` entirely.
    // Mutation: always-null `waitingOnPeer` → this test fails.
    const m = buildBandModel({
      steps: [step({
        stepId: "spawn", state: "active",
        toolName: "subagent_spawn", headerZh: "派子 agent",
        subagentId: "lead_1", peerProseKey: "p1",
      })],
      peerNames: { p1: "compute" },
      peerDisplayNames: { p1: "主分析" },
    });
    expect(m.current?.waitingOnPeer).toBe("主分析");
    expect(m.current?.waitingOnPeerUnknown).toBe(false);
  });

  it("★★★ P2：散文尾窗开头的残词要掐掉（116 实测渲出过「合规风险告。 Now let me…」）", () => {
    // `告。` is the residual tail of the prior `…报告。` cut by the window, appearing as broken text.
    // Mutation: return the input directly from `dropLeadingFragment` → this test must fail.
    // ⚠ **Calculate** offsets: make the display window (last 48 characters) start **exactly at `告。`**;
    //   2-character fragment + 46 English characters = 48. An arbitrary string would not cut midword,
    //   making this guard ineffective (the first version did this, discovered when mutation did not fail).
    const pad = "梳理合规风险并写成完整的合规风险专项报";      // Ends with `报`
    const eng = "Now let me generate the charts for the report.";  // 46
    const m = buildBandModel({
      steps: [step({ stepId: "a", state: "active", subagentId: "p1" })],
      peerNames, peerDisplayNames,
      peerProse: { p1: `${pad.repeat(5)}告。${eng}` },
    });
    expect(m.current?.prose?.startsWith("告")).toBe(false);
    expect(m.current?.prose).toContain("Now let me generate the charts");
  });

  it("★ 但开头**恰好是一整句**时不许误删（残尾很短才掐）", () => {
    // The preceding test alone also passes if always trimming past the first punctuation, needlessly losing an entire sentence.
    // Mutation: remove `m.index > HEAD_FRAGMENT_MAX` from `dropLeadingFragment` → this test must fail.
    const head = "这一句本身就是完整的一句话不该被丢掉。";
    const m = buildBandModel({
      steps: [step({ stepId: "a", state: "active", subagentId: "p1" })],
      peerNames, peerDisplayNames,
      peerProse: { p1: "x".repeat(200) + head + "后面还有一点点" },
    });
    expect(m.current?.prose).toContain("这一句本身就是完整的一句话");
  });

  it("掐完为空时退回原样（宁可半截，也不能把散文整条吞掉）", () => {
    const m = buildBandModel({
      steps: [step({ stepId: "a", state: "active", subagentId: "p1" })],
      peerNames, peerDisplayNames,
      peerProse: { p1: "告。" },
    });
    expect(m.current?.prose).toBe("告。");
  });

  // ★★★ 2026-08-15 116 dogfood: the preceding guard was **ineffective on Chinese prose**.
  //   Of 55 observed frames, 6 showed fragments (`营业额`→`业额或客流量），`,
  //   `unknown_words`→`nknown_words\` (unrecognizable), `），`, matching pre-fix behavior.
  //   Root cause: most Chinese pauses use `，`, excluded from boundaries; nearest `。` is usually beyond 16 characters,
  //   so `m.index > HEAD_FRAGMENT_MAX` always holds and never trims.
  //   ⚠ All three fixtures have **exactly 48 characters** (= display window); Node calculated offsets rather than manual counting.
  //     Without a midword cut, these guards become ineffective again (fourth occurrence this sprint).

  it("★★★ 中文残词必掐：停顿是「，」时也要掐（旧边界集只认「。」→ 空转）", () => {
    // Reproduce the observed 116 frame: cut within `按照要求`, leaving fragment `求`.
    // Mutation①: remove full-width `，、：` from `PROSE_BREAK_CHARS` → trim past `。`, yielding `先对两个指标…` → fails.
    // Mutation②: return the input directly from `dropLeadingFragment` → still starts with `求` → fails.
    const win48 = "求，我会用多种口径反复重试查询，用实际结果来验证。先对两个指标各尝试一批可能的措辞如下，以此类推";
    const m = buildBandModel({
      steps: [step({ stepId: "a", state: "active", subagentId: "p1" })],
      peerNames, peerDisplayNames,
      peerProse: { p1: `但按照要${win48}` },   // Preceding character `要` = midword cut
    });
    expect(m.current?.prose?.startsWith("求")).toBe(false);
    expect(m.current?.prose?.startsWith("我会用多种口径")).toBe(true);
  });

  it("★ 正对照：窗口**恰好**从一句话开头起时不许掐（116 实测真会撞上）", () => {
    // The previous test alone also passes if always trimming past the first pause, needlessly losing an entire sentence.
    // The criterion is **known**: preceding character `。` ⇒ this sentence is complete.
    // Mutation: force `headIsClean` false (unconditional trim) → trim past `，`
    //       yielding `我会用多种口径…` → this test fails.
    const win48 = "但按照要求，我会用多种口径反复重试查询，用实际结果来验证。先对两个指标各尝试一批可能的措辞如下：";
    const m = buildBandModel({
      steps: [step({ stepId: "a", state: "active", subagentId: "p1" })],
      peerNames, peerDisplayNames,
      peerProse: { p1: `逐一检查后确认没有相关字段。${win48}` },   // Preceding character is `。`
    });
    expect(m.current?.prose?.startsWith("但按照要求")).toBe(true);
  });

  // ★★★ Independent review caught a real regression: adding ASCII `,` `:` to pauses
  //   made separators in `13,500` / `18:30` pauses, starting windows after them,
  //   rendering `500 万元`, `30`, **plausible-looking but incorrect numbers**.
  //   In BI this is far worse than a fragment: visibly broken words, undetectably wrong numbers.
  //   Mutation: always-false `isNumericSeparator` (treat `,:` as real pauses) → corresponding tests fail.

  it("★★★ 千分位逗号不是停顿：`13,500` 不许被切成 `500`（会渲出错的数）", () => {
    const win48 = "500 万元人民币较上季度增长明显主要由东湖街与西岸二期两家门店带动其余门店基本持平稳定向好一些";
    const m = buildBandModel({
      steps: [step({ stepId: "a", state: "active", subagentId: "p1" })],
      peerNames, peerDisplayNames,
      peerProse: { p1: `本季度合计营业额为 13,${win48}` },   // Preceding character is an ASCII comma
    });
    expect(m.current?.prose?.startsWith("500")).toBe(false);
    // Trim past the next real pause (space): prefer no number over half a number.
    expect(m.current?.prose?.startsWith("万元人民币")).toBe(true);
  });

  it("★★★ 千分位逗号在**掐头那一支**里也不是停顿（上一条够不着 `findBreak`）", () => {
    // Above, window starts with `500 ` and first pause is space; `findBreak` never sees the `,`,
    // so that test stays green without numeric-separator detection in `findBreak` (verified by mutation).
    // Here the window **starts with digits and the comma comes first**, forcing `dropLeadingFragment` to encounter `,`.
    const win48 = "3,500 万元人民币较上季度增长明显主要由东湖街与西岸二期两家门店带动其余各门店基本持平稳定些";
    const m = buildBandModel({
      steps: [step({ stepId: "a", state: "active", subagentId: "p1" })],
      peerNames, peerDisplayNames,
      peerProse: { p1: `本季度合计营业额为 1${win48}` },     // Preceding character is `1` ⇒ must trim
    });
    expect(m.current?.prose?.startsWith("500")).toBe(false);
    expect(m.current?.prose?.startsWith("万元人民币")).toBe(true);
  });

  // ★★★ Second-review probes: excluding only ASCII `,:` is a point solution;
  //   Chinese `9：30`, `3：1`, `13，500` use **full-width** forms and still fail.
  //   Make the between-two-digits criterion width-independent and **at least** discard the number fragment on midnumber cuts,
  //   even without a later pause; `500万元` reads as a complete amount, far more dangerous than half a word.
  //   Mutation: always-false `isNumericSeparator` → all three fail; always-false `midNumber` → all three fail.

  it("★★★ 全角时间冒号：`9：30` 不许被切成 `30开始盘点…`", () => {
    const win48 = "30开始盘点各门店店长需在系统内确认当日营业数据并提交差异说明表单并按既定节奏推进后续各项安排落";
    const m = buildBandModel({
      steps: [step({ stepId: "a", state: "active", subagentId: "p1" })],
      peerNames, peerDisplayNames,
      peerProse: { p1: `今日各门店统一于上午9：${win48}` },
    });
    expect(m.current?.prose?.startsWith("30")).toBe(false);
    expect(m.current?.prose?.startsWith("开始盘点")).toBe(true);
  });

  it("★★★ 全角比例冒号：`3：1` 不许被切成 `1其余…`", () => {
    const win48 = "1其余门店保持原有节奏不变具体安排见附件表格资料等相关事项另行通知并按既定节奏推进后续各项安排落";
    const m = buildBandModel({
      steps: [step({ stepId: "a", state: "active", subagentId: "p1" })],
      peerNames, peerDisplayNames,
      peerProse: { p1: `线上线下销售占比约为3：${win48}` },
    });
    expect(m.current?.prose?.startsWith("1")).toBe(false);
    expect(m.current?.prose?.startsWith("其余门店")).toBe(true);
  });

  it("★★★ 全角千分位：`13，500` 不许被切成 `500万元`（最危险的一种）", () => {
    const win48 = "500万元较上季度稳定向好其余门店保持原有节奏不变具体安排见附件表格并按既定节奏推进后续各项安排";
    const m = buildBandModel({
      steps: [step({ stepId: "a", state: "active", subagentId: "p1" })],
      peerNames, peerDisplayNames,
      peerProse: { p1: `本季度合计营业额为13，${win48}` },
    });
    expect(m.current?.prose?.startsWith("500")).toBe(false);
    expect(m.current?.prose?.startsWith("万元")).toBe(true);
  });

  it("★★★ 没有分隔符也一样：窗口切在 `13500` 中间不许渲出 `500万元`", () => {
    // Preceding character is a digit, with no punctuation, beyond `isNumericSeparator`'s reach;
    // relies on `isDigitLike(prev)` in `midNumber`.
    const win48 = "500万元较上季度稳定向好其余门店保持原有节奏不变具体安排见附件表格并按既定节奏推进后续各项安排";
    const m = buildBandModel({
      steps: [step({ stepId: "a", state: "active", subagentId: "p1" })],
      peerNames, peerDisplayNames,
      peerProse: { p1: `本季度合计营业额为13${win48}` },
    });
    expect(m.current?.prose?.startsWith("500")).toBe(false);
    expect(m.current?.prose?.startsWith("万元")).toBe(true);
  });

  // ★★★ Second review found two more: preceding-character checks miss decimal points/date hyphens,
  //   while testing the **entire pause set** for between-digits misclassifies real periods as numeric separators.

  it("★★★ 窗口切在**分隔符上**（`24,558,771`→`,771`）也不许漏出 `771`", () => {
    // 📏 Actual frame captured on 116 v1.1.5 dogfood: `.bh-band__act` prose rendered
    //    `771 | 16.0% | …`, actual value `24,558,771`.
    // ★ **Window-only checks cannot detect** this: `charAt(0)` is `,`, not a digit, and
    //   `isNumericSeparator` needs the preceding character, outside the window.
    //   Previous numeric fixtures all started **after** separators, leaving all-green tests unable to guard this
    //   (another `feedback_my_own_fixtures_are_the_happy_path`).
    // Mutation: always-false `startsOnSeparator` → this test fails.
    const win48 = ",771 | 16.0% |\n| **老庙** | 22,034,118 | 17.9% | 次";
    const m = buildBandModel({
      steps: [step({ stepId: "a", state: "active", subagentId: "p1" })],
      peerNames, peerDisplayNames,
      peerProse: { p1: `| **金辉福** | 24,558${win48}` },   // Preceding character is `8`
    });
    expect(m.current?.prose?.startsWith("771")).toBe(false);
    expect(m.current?.prose?.startsWith(",771")).toBe(false);
    expect(m.current?.prose?.startsWith("| 16.0%")).toBe(true);
  });

  it("★★★ 正对照：真·中文逗号夹在两个数字中间时仍是停顿，不许吃掉后面那个完整的数", () => {
    // Third review: the comma in `…门店数为12，2026年将继续…` does have digits on both sides;
    // testing only that misclassifies it as thousands separator ⇒ `leadingNumberEnd` consumes **complete 2026**,
    // rendering `年将继续…`. Require exactly three digits after thousands separators; `2026` has four.
    // Mutation: remove the `THOUSANDS_SEPARATORS` branch from `isNumericSeparator` → this test fails.
    const win48 = "，2026年将继续保持增长态势并推进各项安排落实到位确保执行不打折扣稳步向前推进各门店持续改善的";
    const m = buildBandModel({
      steps: [step({ stepId: "a", state: "active", subagentId: "p1" })],
      peerNames, peerDisplayNames,
      peerProse: { p1: `上季度末在营门店数合计为12${win48}` },   // Window starts exactly at `，`
    });
    expect(m.current?.prose?.startsWith("2026年")).toBe(true);
  });

  it("★★★ 小数点：`12.5%` 被切在小数点后不许渲出 `5%`（真值 12.5%）", () => {
    // `.` is neither a digit nor a pause; preceding-character `midNumber` checks miss this whole class.
    // Mutation: restore `midNumber` to `isDigitLike(prev) || PROSE_BREAK_CHARS.has(prev)` → this test fails.
    const win48 = "5%的同比增幅主要来自华北大区门店其余大区基本持平具体见附件明细并按既定节奏推进后续各项安排落实";
    const m = buildBandModel({
      steps: [step({ stepId: "a", state: "active", subagentId: "p1" })],
      peerNames, peerDisplayNames,
      peerProse: { p1: `本季度同比增长12.${win48}` },
    });
    expect(m.current?.prose?.startsWith("5")).toBe(false);
  });

  it("★★★ 日期连字符：`2025-08-15` 被切在连字符后不许渲出 `15日…`", () => {
    const win48 = "15日的门店营业数据已全部归档后续将按月度节奏继续推进相关工作并按既定节奏推进后续各项安排落实到";
    const m = buildBandModel({
      steps: [step({ stepId: "a", state: "active", subagentId: "p1" })],
      peerNames, peerDisplayNames,
      peerProse: { p1: `统计区间截止到2025-08-${win48}` },
    });
    expect(m.current?.prose?.startsWith("15")).toBe(false);
    expect(m.current?.prose?.startsWith("日的门店")).toBe(true);
  });

  it("★★★ 正对照：夹在数字中间的**句号**仍然是句号，不许当数字分隔符", () => {
    // `…同比增长为12。2026年…`: `。` is precisely between `2` and `2`.
    // Testing the whole pause set in `isNumericSeparator` calls it non-pause, misclassifying a window at a sentence start
    // as broken-headed; `leadingNumberEnd` then consumes `2026` ⇒ `年将继续…`.
    // Mutation: remove `NUMBER_INNER_CHARS.has(...)` from `isNumericSeparator` → this test fails.
    const win48 = "2026年将继续推进该项工作并在各大区同步落实具体安排见附件明细表并按既定节奏推进后续各项安排落";
    const m = buildBandModel({
      steps: [step({ stepId: "a", state: "active", subagentId: "p1" })],
      peerNames, peerDisplayNames,
      peerProse: { p1: `上季度同比增长为12。${win48}` },
    });
    expect(m.current?.prose?.startsWith("2026年")).toBe(true);
  });

  it("★★★ 时间冒号不是停顿：`18:30` 不许被切成 `30`", () => {
    const win48 = "30 分启动闭店盘点流程各门店店长需在系统内确认当日营业数据并提交差异说明表单资料等全部相关事项";
    const m = buildBandModel({
      steps: [step({ stepId: "a", state: "active", subagentId: "p1" })],
      peerNames, peerDisplayNames,
      peerProse: { p1: `今日各门店统一于 18:${win48}` },     // Preceding character is an ASCII colon
    });
    expect(m.current?.prose?.startsWith("30")).toBe(false);
    expect(m.current?.prose?.startsWith("分启动闭店盘点")).toBe(true);
  });

  it("★ 停顿太远时宁可留半截，也不掐掉大半个窗口（`HEAD_FRAGMENT_MAX` 仍在拦）", () => {
    // The head really is a fragment, but the nearest pause is at character 31; trimming leaves only 16 characters,
    // worse for a cell whose responsibility is conveying liveness.
    // Mutation: remove `m.index > HEAD_FRAGMENT_MAX` → starts with `后续需要复核` → this test fails.
    const win48 = "款项与结算周期以及对账口径的差异会直接影响到最终的应付金额判断，后续需要复核并出具书面结论说明。";
    const m = buildBandModel({
      steps: [step({ stepId: "a", state: "active", subagentId: "p1" })],
      peerNames, peerDisplayNames,
      peerProse: { p1: `应付${win48}` },       // Preceding character `付` = midword cut
    });
    expect(m.current?.prose?.startsWith("款项与结算周期")).toBe(true);
  });
});

describe("★ density — 纯聊天轮收成单行（清点 §4 ② 的拍板）", () => {
  it("答案流式中 + 零步骤 → single，历史为空", () => {
    const m = buildBandModel({ steps: [], answerStreaming: true });
    expect(m.density).toBe("single");
    expect(m.history).toHaveLength(0);
  });

  it("★ 答案流式中 + 零步骤，但**有 peer 终态** → 不收单行（否则那一行直接丢）", () => {
    // Criterion: no execution facts this turn, rather than merely zero steps.
    // Mutation: restore density to checking only `steps.length === 0` → this test must fail.
    const m = buildBandModel({
      steps: [], answerStreaming: true,
      peerTerminalById: { p1: { status: "failed", evtSeq: 2 } },
    });
    expect(m.density).toBe("rows");
    expect(m.history.some((r) => r.kind === "peer")).toBe(true);
  });

  it("答案流式中但**有**步骤 → 仍是三行（分析轮不受影响）", () => {
    const m = buildBandModel({
      steps: [step({ stepId: "a", state: "active" })],
      answerStreaming: true,
    });
    expect(m.density).toBe("rows");
    expect(m.history).toHaveLength(HISTORY_ROWS);
  });

  it("答案还没开始 + 零步骤 → 三行骨架（开场空窗仍要占位）", () => {
    const m = buildBandModel({ steps: [], answerStreaming: false });
    expect(m.density).toBe("rows");
    expect(m.history).toHaveLength(HISTORY_ROWS);
  });
});
