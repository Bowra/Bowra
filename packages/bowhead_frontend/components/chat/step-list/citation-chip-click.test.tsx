/**
 * Chat citation-badge clicks (feedback-triage-2026-08-24, user report「数据引用按钮点击后无响应」).
 *
 * An unresolved viewer alias cannot scroll to a canvas section or open a data panel.
 * Original code searched IDs only in **currently rendered DOM**, but backend sent viewer-safe
 * aliases `source-ref-v1:…`, while canvas had only `data-section-id="sec_*"`, never matching.
 *
 * Split behavior into three cases, each pinned here:
 *   1. Target in DOM → scroll to it (preserve original behavior)
 *   2. Absent from DOM but a **real artifact ID** → ask workbench to open Results
 *   3. Alias → neither scroll nor navigate to nonexistent results; give an honest explanation
 */
import { fireEvent } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { renderIntl } from "@/__tests__/intl-render";
import type { Citation } from "@/lib/types";

import { ChatSourcesProvider, CitationsProvider, LeadNarrative } from "./LeadNarrative";
import { CANVAS_FOCUS_DATA_EVENT, CANVAS_OPEN_RESULT_EVENT } from "@/lib/canvas-workspace-focus";

function chipFor(citations: Citation[], text: string) {
  const { container } = renderIntl(
    <CitationsProvider citations={citations}>
      <LeadNarrative text={text} />
    </CitationsProvider>,
  );
  return container.querySelector(".bh-artifact-chip") as HTMLElement;
}

const ALIAS = "source-ref-v1:Tyh033DgJPXDLeNtcuy6df0aZ5fdM_SkmRsPJ1KqRcQ";

describe("引用角标点击", () => {
  let focusEvents: string[];
  let onFocus: (e: Event) => void;

  beforeEach(() => {
    focusEvents = [];
    onFocus = (e: Event) => {
      focusEvents.push((e as CustomEvent<{ artifactId: string }>).detail.artifactId);
    };
    window.addEventListener(CANVAS_FOCUS_DATA_EVENT, onFocus);
    // Module-level toast dedup holds IDs for 30s; use different IDs between cases or the second stays silent.
  });
  afterEach(() => {
    window.removeEventListener(CANVAS_FOCUS_DATA_EVENT, onFocus);
    vi.restoreAllMocks();
  });

  it("① DOM 里有目标 → 滚过去，不发导航事件", () => {
    const target = document.createElement("div");
    target.setAttribute("data-section-id", "sec_alpha");
    const scroll = vi.fn();
    (target as unknown as { scrollIntoView: unknown }).scrollIntoView = scroll;
    document.body.appendChild(target);
    try {
      const chip = chipFor(
        [{ seq: 1, artifactId: "sec_alpha", label: "图 A", kind: "chart_section" }],
        "见图 [cite:1]",
      );
      chip.click();
      expect(scroll).toHaveBeenCalledTimes(1);
      expect(focusEvents).toEqual([]);
    } finally {
      target.remove();
    }
  });

  it("② DOM 里没有但是真 artifact id → 请 workbench 打开结果面板", () => {
    // Closed canvas means target is absent from DOM; original implementation could only toast here.
    const chip = chipFor(
      [{ seq: 1, artifactId: "art_compute_beta", label: "计算 B", kind: "tool_output" }],
      "见计算 [cite:1]",
    );
    chip.click();
    expect(focusEvents).toEqual(["art_compute_beta"]);
  });

  it("★③ viewer 别名不得发导航 —— 它不是 artifact id，发过去只会导航到不存在的结果", () => {
    const chip = chipFor(
      [{ seq: 1, artifactId: ALIAS, label: "bi_query: 内销销售数量 按品规", kind: "data_ref" }],
      "见数据 [cite:1]",
    );
    chip.click();
    expect(focusEvents).toEqual([]);
  });

  it("★③b 画布区块 id 也不得发导航 —— 它导航的是 artifact，不是 section", () => {
    // sec_gamma is absent from DOM (canvas closed); neither pretend to scroll nor send artifact navigation
    // that must miss its target.
    const chip = chipFor(
      [{ seq: 1, artifactId: "sec_gamma", label: "图 C", kind: "chart_section" }],
      "见图 [cite:1]",
    );
    chip.click();
    expect(focusEvents).toEqual([]);
  });

  it("★③c 没解析出来的 cite:N 同样不得发导航", () => {
    // Empty registry leaves resolvedId at fake "cite:9", which is no real resource.
    const chip = chipFor([], "见 [cite:9]");
    chip?.click();
    expect(focusEvents).toEqual([]);
  });

  it("★ 悬停详情走即时小签而不是原生 title，且只给人话不给别名串", () => {
    const label = "bi_query: 2026年1-7月 内销销售数量 按品规分组";
    const chip = chipFor([{ seq: 1, artifactId: ALIAS, label, kind: "data_ref" }], "见数据 [cite:1]");
    expect(chip.hasAttribute("title")).toBe(false);
    expect(chip.getAttribute("data-tip")).toBe(label);
    // A 43-character alias means nothing to users and must not appear in hints.
    expect(chip.getAttribute("data-tip")).not.toContain("source-ref-v1:");
    expect(chip.className).toContain("bh-tip");
  });

  it("★ 角标仍是可点可聚焦的 —— 别为了修「点了没反应」把它变成纯装饰", () => {
    const chip = chipFor(
      [{ seq: 1, artifactId: ALIAS, label: "来源", kind: "data_ref" }],
      "见数据 [cite:1]",
    );
    expect(chip.getAttribute("role")).toBe("button");
    expect(chip.getAttribute("tabindex")).toBe("0");
  });
});


describe("批 4 · 六态与 chart_section 锚点（source-accessibility）", () => {
  it("★ 否定态（restricted）：tooltip 出人话、点击不发导航不开卡、外观淡化", () => {
    const events: string[] = [];
    const onFocus = (e: Event) => events.push((e as CustomEvent<{ artifactId: string }>).detail.artifactId);
    window.addEventListener(CANVAS_FOCUS_DATA_EVENT, onFocus);
    try {
      const chip = chipFor(
        [{ seq: 1, artifactId: "art_theirs", label: null, kind: "data_ref", accessState: "linked_restricted" }],
        "见 [cite:1]",
      );
      expect(chip.getAttribute("data-tip")).toBe("该来源不在这份报告的范围内");
      // Dim styling in styles/ui-controls-legacy.css .legacy-cite-chip.is-denied (original inline opacity .55).
      expect(chip.classList.contains("legacy-cite-chip")).toBe(true);
      expect(chip.classList.contains("is-denied")).toBe(true);
      chip.click();
      expect(events).toEqual([]);
      expect(document.querySelector(".chat-source-card")).toBeNull();
    } finally {
      window.removeEventListener(CANVAS_FOCUS_DATA_EVENT, onFocus);
    }
  });

  it("★ linked_labeled + safe_anchor_id：画布已渲 → 直接滚到区块", () => {
    const target = document.createElement("div");
    target.setAttribute("data-section-id", "sec_anchor1");
    const scroll = vi.fn();
    (target as unknown as { scrollIntoView: unknown }).scrollIntoView = scroll;
    document.body.appendChild(target);
    try {
      const chip = chipFor(
        [{ seq: 1, artifactId: "sec_anchor1", label: "月度走势", kind: "chart_section", accessState: "linked_labeled", safeAnchorId: "sec_anchor1" }],
        "见图 [cite:1]",
      );
      chip.click();
      expect(scroll).toHaveBeenCalledTimes(1);
    } finally {
      target.remove();
    }
  });

  it("★ linked_labeled + safe_anchor_id：画布关着 → 请 workbench 打开报告面", () => {
    const opened: string[] = [];
    const onOpen = (e: Event) => opened.push((e as CustomEvent<{ target: { type: string } }>).detail.target.type);
    window.addEventListener(CANVAS_OPEN_RESULT_EVENT, onOpen);
    vi.useFakeTimers();
    try {
      const chip = chipFor(
        [{ seq: 1, artifactId: "sec_anchor2", label: "对比图", kind: "chart_section", accessState: "linked_labeled", safeAnchorId: "sec_anchor2" }],
        "见图 [cite:1]",
      );
      chip.click();
      expect(opened).toEqual(["report"]);
      vi.runAllTimers(); // Timed retry naturally exhausts without throwing.
    } finally {
      vi.useRealTimers();
      window.removeEventListener(CANVAS_OPEN_RESULT_EVENT, onOpen);
    }
  });

  it("无 label 无状态（legacy 归档）：tooltip 兜底人话，绝不露别名/裸 id", () => {
    const chip = chipFor(
      [{ seq: 4, artifactId: "sec_old", label: null, kind: "chart_section" }],
      "见 [cite:4]",
    );
    const tip = chip.getAttribute("data-tip") ?? "";
    expect(tip).not.toContain("sec_old");
    expect(tip).not.toContain("source-ref-v1:");
    expect(tip.length).toBeGreaterThan(0);
  });
});

describe("citation chips expand projected source cards", () => {
  // Synthetic payloads exercise the shared alias contract between citation and source carriers:
  //   citation_registered → {citation_seq, artifact_id: <alias>, label, access_state}
  //   lead.final.payload.sources[] → {id: <same alias>, access_state, safe_label, safe_query, safe_preview_url}
  // ★ Both carriers use the same alias because alias_for signs effective identities with
  //   ["source-ref-v1", ws, scope, identity], excluding slot and caching by identity.
  const CITES: Citation[] = [
    { seq: 1, artifactId: ALIAS, label: "bi_query: 各城市 营业额 对比", kind: "data_ref" },
  ];

  function renderWithSources(sources: unknown[]) {
    const { container } = renderIntl(
      <CitationsProvider citations={CITES}>
        <ChatSourcesProvider sources={sources as never}>
          <LeadNarrative text="各城市营业额差异明显[cite:1]。" />
        </ChatSourcesProvider>
      </CitationsProvider>,
    );
    return container;
  }

  const ACCESSIBLE = [
    {
      id: ALIAS,
      source_alias: ALIAS,
      access_state: "linked_accessible",
      safe_label: "各城市营业额",
      safe_query: "各城市 营业额 对比",
      safe_preview_url: "/api/v1/canvas/by-session/ses_1/artifacts/art_1/preview",
      ordinal: 1,
    },
  ];

  it("★ 可访问来源：点击展开来源卡，显示名字与查询", () => {
    const container = renderWithSources(ACCESSIBLE);
    const chip = container.querySelector(".bh-artifact-chip") as HTMLElement;
    expect(container.querySelector(".bh-chat-source-card")).toBeNull();
    fireEvent.click(chip);
    const card = container.querySelector(".bh-chat-source-card") as HTMLElement;
    expect(card).not.toBeNull();
    expect(card.textContent).toContain("各城市营业额");
    expect(card.textContent).toContain("各城市 营业额 对比");
  });

  it("再点一次收起（同一个角标是开关）", () => {
    const container = renderWithSources(ACCESSIBLE);
    const chip = container.querySelector(".bh-artifact-chip") as HTMLElement;
    fireEvent.click(chip);
    expect(container.querySelector(".bh-chat-source-card")).not.toBeNull();
    fireEvent.click(chip);
    expect(container.querySelector(".bh-chat-source-card")).toBeNull();
  });

  it("★ 不可访问来源：不展开卡片，退回原有提示（后端按设计只给别名，前端不许自己补全）", () => {
    const container = renderWithSources([
      { id: ALIAS, source_alias: ALIAS, access_state: "linked_unavailable", ordinal: 1 },
    ]);
    const chip = container.querySelector(".bh-artifact-chip") as HTMLElement;
    fireEvent.click(chip);
    expect(container.querySelector(".bh-chat-source-card")).toBeNull();
  });

  it("★ 别名对不上（另一条来源）→ 不展开，绝不把别人的来源安到这个角标上", () => {
    const container = renderWithSources([
      {
        id: "source-ref-v1:OTHEROTHEROTHEROTHEROTHEROTHEROTHEROTHEROTH",
        source_alias: "source-ref-v1:OTHEROTHEROTHEROTHEROTHEROTHEROTHEROTHEROTH",
        access_state: "linked_accessible",
        safe_label: "别人的来源",
      },
    ]);
    const chip = container.querySelector(".bh-artifact-chip") as HTMLElement;
    fireEvent.click(chip);
    expect(container.querySelector(".bh-chat-source-card")).toBeNull();
  });

  it("★ 被丢弃的角标与正常角标共存时不炸（hook 顺序守卫）", () => {
    // Real bug caught in self-review: initially useChatSources/useState came
    // **after** return null when both seq and label were absent, creating conditional hook calls.
    // Tests all passed because no case ever took that return.
    // Render a dropped badge (unregistered art_ → absent seq/label) alongside a normal one,
    // then click to rerender: wrong hook order makes React throw "Rendered fewer hooks than expected".
    const container = renderIntl(
      <CitationsProvider citations={CITES}>
        <ChatSourcesProvider sources={ACCESSIBLE as never}>
          <LeadNarrative text="未注册的[data:art_never_registered]与正常的[cite:1]并存。" />
        </ChatSourcesProvider>
      </CitationsProvider>,
    ).container;
    const chips = container.querySelectorAll(".bh-artifact-chip");
    expect(chips.length).toBe(1); // Unregistered badge is dropped by design.
    fireEvent.click(chips[0] as HTMLElement);
    expect(container.querySelector(".bh-chat-source-card")).not.toBeNull();
  });

  it("没有 sources（未接 provider 的老调用点）→ 行为与改动前一致", () => {
    const { container } = renderIntl(
      <CitationsProvider citations={CITES}>
        <LeadNarrative text="各城市营业额差异明显[cite:1]。" />
      </CitationsProvider>,
    );
    const chip = container.querySelector(".bh-artifact-chip") as HTMLElement;
    fireEvent.click(chip);
    expect(container.querySelector(".bh-chat-source-card")).toBeNull();
  });
});
