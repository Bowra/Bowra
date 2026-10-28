/**
 * Phase 1.6 G3 — LeadNarrative component test.
 *
 * Pins the rendering contract that TurnContainer relies on for the narrative
 * surface (Lead's voice, streamed above StepListCard):
 *   · Text renders inline
 *   · Cursor appears only while isStreaming
 *   · Paragraph splits on blank lines
 *   · `**bold**` runs render as <strong>
 */

import { afterEach, describe, expect, it } from "vitest";
import { cleanup, screen } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { CitationsProvider, LeadNarrative } from "./LeadNarrative";
import type { Citation } from "@/lib/types";

afterEach(() => cleanup());

describe("LeadNarrative", () => {
  it("renders text content", () => {
    renderIntl(<LeadNarrative text="好的，我先查 Q3 数据" />);
    expect(screen.getByText("好的，我先查 Q3 数据")).toBeTruthy();
  });

  it("shows cursor when isStreaming=true", () => {
    const { container } = renderIntl(
      <LeadNarrative text="streaming…" isStreaming />,
    );
    // Cursor primitive emits a span (no visible text). Detect via presence
    // of more siblings than the paragraph alone.
    const paragraphs = container.querySelectorAll("p");
    expect(paragraphs.length).toBe(1);
    // Cursor wraps a span; the wrapper div has > 1 child element.
    const wrapper = container.firstElementChild as HTMLElement;
    expect(wrapper.children.length).toBe(2);  // <p> + cursor wrapper
  });

  it("does not render cursor when isStreaming is false / default", () => {
    const { container } = renderIntl(<LeadNarrative text="locked." />);
    const wrapper = container.firstElementChild as HTMLElement;
    expect(wrapper.children.length).toBe(1);  // only <p>, no cursor
  });

  it("splits paragraphs on blank lines", () => {
    const { container } = renderIntl(
      <LeadNarrative text={"段一\n\n段二\n\n段三"} />,
    );
    const paragraphs = container.querySelectorAll("p");
    expect(paragraphs.length).toBe(3);
    expect(paragraphs[0].textContent).toBe("段一");
    expect(paragraphs[1].textContent).toBe("段二");
    expect(paragraphs[2].textContent).toBe("段三");
  });

  it.each([false, true])("preserves answer lines without doubling existing hard breaks (streaming=%s)", (isStreaming) => {
    const { container } = renderIntl(
      <LeadNarrative text={"Revenue: 800\nProfit: 120  \nMargin: 15.0%"} isStreaming={isStreaming} />,
    );
    const paragraph = container.querySelector("p")!;
    expect(paragraph.querySelectorAll("br")).toHaveLength(2);
    expect(paragraph.textContent).toBe("Revenue: 800\nProfit: 120\nMargin: 15.0%");
  });

  it("renders **bold** runs as <strong>", () => {
    const { container } = renderIntl(
      <LeadNarrative text="增长 **+8.4%** 集中在 **江苏**" />,
    );
    const strongs = container.querySelectorAll("strong");
    expect(strongs.length).toBe(2);
    expect(strongs[0].textContent).toBe("+8.4%");
    expect(strongs[1].textContent).toBe("江苏");
  });

  it("renders empty text without crashing (defensive)", () => {
    const { container } = renderIntl(<LeadNarrative text="" />);
    expect(container.querySelectorAll("p").length).toBe(0);
  });

  // ── p0-followup F4 — `[data:art_*]` artifact reference chip render ──

  it("renders [data:art_*] tokens as `.bh-artifact-chip` spans (when registered)", () => {
    // 0.39.8 #2 — legacy markers require citation registry hit to render.
    const citations: Citation[] = [
      { seq: 1, artifactId: "art_0fa494fbf141", label: null, kind: "data_ref" },
    ];
    const { container } = renderIntl(
      <CitationsProvider citations={citations}>
        <LeadNarrative text="总盘子 [data:art_0fa494fbf141] ¥123 亿" />
      </CitationsProvider>,
    );
    const chips = container.querySelectorAll(".bh-artifact-chip");
    expect(chips.length).toBe(1);
    expect(chips[0].getAttribute("data-artifact-ref")).toBe(
      "art_0fa494fbf141",
    );
  });

  it("renders multiple artifact tokens in order (when registered)", () => {
    const citations: Citation[] = [
      { seq: 1, artifactId: "art_aaa1f1", label: null, kind: "data_ref" },
      { seq: 2, artifactId: "art_bbb2c2", label: null, kind: "data_ref" },
    ];
    const { container } = renderIntl(
      <CitationsProvider citations={citations}>
        <LeadNarrative text="GMV [data:art_aaa1f1] 同比 [data:art_bbb2c2] 增长" />
      </CitationsProvider>,
    );
    const chips = container.querySelectorAll(".bh-artifact-chip");
    expect(chips.length).toBe(2);
    expect(chips[0].getAttribute("data-artifact-ref")).toBe("art_aaa1f1");
    expect(chips[1].getAttribute("data-artifact-ref")).toBe("art_bbb2c2");
  });

  it("does NOT render chip for plain [N] citations (N15 suppression)", () => {
    const { container } = renderIntl(
      <LeadNarrative text="增长 [1] 集中在 [12] 江苏" />,
    );
    expect(container.querySelectorAll(".bh-artifact-chip").length).toBe(0);
  });

  it("artifact tokens coexist with **bold** runs in the same paragraph (when registered)", () => {
    const citations: Citation[] = [
      { seq: 1, artifactId: "art_abc123", label: null, kind: "data_ref" },
    ];
    const { container } = renderIntl(
      <CitationsProvider citations={citations}>
        <LeadNarrative text="**总盘子** [data:art_abc123] 同比 **+8%**" />
      </CitationsProvider>,
    );
    expect(container.querySelectorAll("strong").length).toBe(2);
    expect(container.querySelectorAll(".bh-artifact-chip").length).toBe(1);
  });

  // ── prod-bug-bash-039 Phase 5 (0.39.3) — `[cite:N]` chip resolution ──

  it("renders `[cite:N]` as numbered chip with seq", () => {
    const { container } = renderIntl(
      <LeadNarrative text="总盘子 [cite:1] ¥123 亿" />,
    );
    const chips = container.querySelectorAll(".bh-artifact-chip");
    expect(chips.length).toBe(1);
    // chat-ux-2026-08 batch D: remove brackets for inline superscripts (see LeadNarrative display comment).
    expect(chips[0].textContent).toBe("1");
    expect(chips[0].getAttribute("data-cite-seq")).toBe("1");
  });

  it("resolves `[cite:N]` to actual artifact_id via CitationsProvider", () => {
    const citations: Citation[] = [
      { seq: 1, artifactId: "sec_chart_q1", label: "Q1 客流", kind: "chart_section" },
      { seq: 2, artifactId: "art_compute_xy", label: null, kind: "tool_output" },
    ];
    const { container } = renderIntl(
      <CitationsProvider citations={citations}>
        <LeadNarrative text="见图 [cite:1] 与计算 [cite:2]" />
      </CitationsProvider>,
    );
    const chips = container.querySelectorAll(".bh-artifact-chip");
    expect(chips.length).toBe(2);
    expect(chips[0].getAttribute("data-artifact-ref")).toBe("sec_chart_q1");
    // feedback-triage-2026-08-24: hover details switch from native `title` to instant `data-tip` label
    // because `title` takes about 1s of hover to appear; customers hit the same issue on confidence badges.
    expect(chips[0].getAttribute("data-tip")).toContain("Q1 客流");
    expect(chips[0].hasAttribute("title")).toBe(false);
    expect(chips[1].getAttribute("data-artifact-ref")).toBe("art_compute_xy");
  });

  it("orphan `[cite:N]` (no matching citation) renders chip but no resolved ref", () => {
    // Empty provider → cite:5 falls through to pseudo-id (artifact_id stays "cite:5")
    const { container } = renderIntl(
      <CitationsProvider citations={[]}>
        <LeadNarrative text="见 [cite:5]" />
      </CitationsProvider>,
    );
    const chip = container.querySelector(".bh-artifact-chip") as HTMLElement;
    expect(chip).toBeTruthy();
    expect(chip.textContent).toBe("5");
    // unresolved → data-artifact-ref is pseudo-id "cite:5", scroll falls through
    expect(chip.getAttribute("data-artifact-ref")).toBe("cite:5");
  });

  // ── 0.39.4 prod walkthrough bug F2 investigation ──
  // User reported `[N]元` rendering with a strikethrough/horizontal line in
  // canvas insight narrative. Source text contains `~` ranges like
  // `171~173万人次` and `9.8~10.0 亿`. These tests pin actual DOM behavior so
  // we know whether it's actual strikethrough, an `<hr>`, or some other
  // visual artifact mis-interpreted as strikethrough.

  it("renders `9.8~10.0` (single tilde range) WITHOUT strikethrough", () => {
    // Source: prod insight_card sec_e02d24278072 narrative text.
    const text = "营业额低谷（9.8~10.0 亿 [4]元），与模型方向一致。";
    const { container } = renderIntl(<LeadNarrative text={text} isStreaming={false} />);
    // No `<del>` / `<s>` / `<strike>` should be present
    expect(container.querySelector("del")).toBeNull();
    expect(container.querySelector("s")).toBeNull();
    expect(container.querySelector("strike")).toBeNull();
    // Text content should preserve the tilde range as-is
    expect(container.textContent).toContain("9.8~10.0");
    expect(container.textContent).toContain("[4]");
  });

  it("renders `~~text~~` (double tilde) AS strikethrough (markdown spec)", () => {
    // Sanity: double-tilde IS strikethrough per GFM, so verify our renderer
    // does NOT misfire on single-tilde ranges by confirming the double-tilde
    // path still works.
    const text = "正常文本 ~~删除文本~~ 后续";
    const { container } = renderIntl(<LeadNarrative text={text} isStreaming={false} />);
    expect(container.querySelector("del, s")).not.toBeNull();
  });

  it("renders prod prose with `~` ranges + brackets without spurious `<hr>` / `<del>`", () => {
    // Full prod-shape paragraph from sec_e02d24278072.
    const text = "线性回归模型为「营业额(万元) = 895.06 × 客流量(万人次) − 36,816.48」。" +
      "从月份数据看：1月客流最高（235万人次）对应营业额峰值（19.8 亿 [4]元）；" +
      "7-8月客流低谷（171~173万人次）对应营业额低谷（9.8~10.0 亿 [4]元），与模型方向一致。";
    const { container } = renderIntl(<LeadNarrative text={text} isStreaming={false} />);
    expect(container.querySelector("hr")).toBeNull();  // no horizontal rule
    expect(container.querySelector("del, s, strike")).toBeNull();  // no strike
    expect(container.textContent).toContain("9.8~10.0");
  });
});

describe("LeadNarrative — additional bug F1 regression", () => {
  it("renders **bold** with full-width Chinese quotes correctly when not streaming", () => {
    // Synthetic emphasis containing quoted text must preserve the intended strong element.
    // showed raw asterisks instead of bold.
    const text = '从月度走势看，2025 年呈明显的 **"前高后低再反弹"** 格局。';
    const { container } = renderIntl(<LeadNarrative text={text} isStreaming={false} />);
    const strong = container.querySelector("strong");
    expect(strong).not.toBeNull();
    expect(strong?.textContent).toBe('"前高后低再反弹"');
    // Raw `**` should NOT appear in text content (Markdown should consume them)
    expect(container.textContent).not.toContain("**");
  });
});

describe("LeadNarrative — original bug F regressions (closing block)", () => {
  it("legacy `[data:art_*]` resolves via citations registry by artifactId", () => {
    // 0.39.8 #2 — legacy markers now look up in citations registry by
    // artifactId so they get a resolved seq + numbered chip instead of
    // the literal "[数据]" fallback that polluted prose readability.
    const citations: Citation[] = [
      { seq: 1, artifactId: "sec_one", label: null, kind: "chart_section" },
      { seq: 2, artifactId: "art_abc123", label: "源数据", kind: "data_ref" },
    ];
    const { container } = renderIntl(
      <CitationsProvider citations={citations}>
        <LeadNarrative text="legacy [data:art_abc123] then new [cite:1]" />
      </CitationsProvider>,
    );
    const chips = container.querySelectorAll(".bh-artifact-chip");
    expect(chips.length).toBe(2);
    // legacy chip: resolves to seq=2 via artifactId lookup
    expect(chips[0].textContent).toBe("2");
    expect(chips[0].getAttribute("data-artifact-ref")).toBe("art_abc123");
    // new chip: numbered, resolved
    expect(chips[1].textContent).toBe("1");
    expect(chips[1].getAttribute("data-artifact-ref")).toBe("sec_one");
  });

  it("legacy `[data:art_*]` with NO citation registry hit → chip silently dropped", () => {
    // 0.39.8 #2 — user-reported "这里还是有[数据][数据]". Old behavior
    // rendered literal "[数据]" placeholder; new behavior drops the chip
    // entirely so surrounding prose flows naturally.
    const citations: Citation[] = [];  // empty registry
    const { container } = renderIntl(
      <CitationsProvider citations={citations}>
        <LeadNarrative text="some text [data:art_unregistered] more text" />
      </CitationsProvider>,
    );
    const chips = container.querySelectorAll(".bh-artifact-chip");
    expect(chips.length).toBe(0);
    // Prose still rendered around where the chip would've been.
    expect(container.textContent).toContain("some text");
    expect(container.textContent).toContain("more text");
    // No literal "[数据]" placeholder in output.
    expect(container.textContent).not.toContain("[数据]");
  });
});

describe("LeadNarrative content language (P2 batch 6)", () => {

  it("marks a Chinese answer with lang=zh while streaming and when final, so it gets the CJK line length", () => {
    const text = "华东 Q3 GMV 同比下降 8.4%，主要来自线下门店。";
    const live = renderIntl(<LeadNarrative text={text} isStreaming />);
    expect(live.container.querySelector(".lead-narrative-md")?.getAttribute("lang")).toBe("zh");
    live.unmount();
    const final = renderIntl(<LeadNarrative text={text} />);
    expect(final.container.querySelector(".lead-narrative-md")?.getAttribute("lang")).toBe("zh");
  });

  it("leaves Latin answers without a lang attribute", () => {
    const english = renderIntl(<LeadNarrative text="Revenue fell 8.4% year over year." />);
    expect(english.container.querySelector(".lead-narrative-md")?.hasAttribute("lang")).toBe(false);
  });
});
