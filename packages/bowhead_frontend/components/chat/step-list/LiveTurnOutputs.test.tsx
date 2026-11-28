import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { onCanvasOpened, onSectionAdded, onSectionPatched, resetCanvas } from "@/lib/canvas-store";
import type { Step } from "@/lib/turn-store";

import { ChatBubbleNew } from "./ChatBubbleNew";
import { LiveTurnOutputs } from "./LiveTurnOutputs";
import type { StepOutputArtifact } from "./types";

const step = (outputs: StepOutputArtifact[]) => ({ outputArtifacts: outputs }) as unknown as Step;
const narrative = (id: string, turnId: string, title: string) =>
  ({ id, kind: "narrative", markdown: `## ${title}\n\n正文`, turn_id: turnId }) as never;

beforeEach(() => resetCanvas());
afterEach(() => cleanup());

describe("LiveTurnOutputs (chat running state R2.1, R2.3)", () => {
  it("shows a delivered file while the Turn runs, updates its card for the next version, and counts work files", () => {
    renderIntl(
      <LiveTurnOutputs
        turnId="t_1"
        steps={[
          step([{ artifactId: "art_v1", filename: "季度复盘.pptx", kind: "deliverable", lineageId: "art_v1", versionNo: 1, size: 2048 }]),
          step([{ artifactId: "art_csv", filename: "sales.csv", kind: "sandbox_output" }]),
          step([{ artifactId: "art_v2", filename: "季度复盘.pptx", kind: "deliverable", lineageId: "art_v1", versionNo: 2, size: 4096 }]),
        ]}
      />,
    );
    const cards = screen.getAllByTestId("chat-inline-file");
    expect(cards).toHaveLength(1);
    expect(cards[0]).toHaveTextContent("季度复盘.pptx");
    expect(cards[0]).toHaveTextContent("已更新到第 2 版");
    // While running the line states no verdict about deliveries.
    expect(screen.getByTestId("turn-work-files")).toHaveTextContent("本轮还保存了 1 个工作文件");
  });

  it("says only how many work files were saved before anything is delivered", () => {
    renderIntl(<LiveTurnOutputs turnId="t_1" steps={[step([{ artifactId: "art_csv", kind: "sandbox_output" }])]} />);
    expect(screen.getByTestId("turn-work-files")).toHaveTextContent("已保存 1 个工作文件");
    expect(screen.queryByTestId("chat-inline-file")).toBeNull();
  });

  it("shows the report card once this Turn wrote a chapter, counting the earlier ones it revised", () => {
    onCanvasOpened({ canvas_id: "cv_1", session_id: "s_1" });
    onSectionAdded({ section: narrative("sec_old", "t_0", "季度趋势") });
    onSectionAdded({ section: narrative("sec_new", "t_1", "门店排名") });
    onSectionPatched({ section_id: "sec_old", patch: { markdown: "## 季度趋势\n\n改过" }, turnId: "t_1" });
    renderIntl(<LiveTurnOutputs turnId="t_1" steps={[]} />);
    const card = screen.getByTestId("chat-inline-report");
    expect(card).toHaveTextContent("本轮新增 1 节 · 修改 1 节");
    expect(card).toHaveTextContent("门店排名、季度趋势");
  });

  it("waits for a chapter: a chart alone may still be the chat answer's", () => {
    onCanvasOpened({ canvas_id: "cv_1", session_id: "s_1" });
    onSectionAdded({ section: { id: "sec_chart", kind: "chart", title: "月坪效", turn_id: "t_1" } as never });
    const { container } = renderIntl(<LiveTurnOutputs turnId="t_1" steps={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("does not count another Turn's edits", () => {
    onCanvasOpened({ canvas_id: "cv_1", session_id: "s_1" });
    onSectionAdded({ section: narrative("sec_old", "t_0", "季度趋势") });
    onSectionPatched({ section_id: "sec_old", patch: { markdown: "## 季度趋势\n\n改过" }, turnId: "t_0" });
    onSectionPatched({ section_id: "sec_old", patch: { markdown: "## 季度趋势\n\n再改" } });
    const { container } = renderIntl(<LiveTurnOutputs turnId="t_1" steps={[]} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("the finished answer's work-file line", () => {
  it("names the work files beside delivered files, or says nothing was delivered", () => {
    const file = { kind: "file" as const, id: "art_1", data: { id: "art_1", filename: "a.html", fileType: "HTML", downloadUrl: "/x" } };
    renderIntl(<ChatBubbleNew text="完成。" artifacts={[file]} extras={{ report: null, workFiles: 2 }} />);
    expect(screen.getByTestId("turn-work-files")).toHaveTextContent("本轮还保存了 2 个工作文件");
    cleanup();
    renderIntl(<ChatBubbleNew text="完成。" extras={{ report: null, workFiles: 3 }} />);
    expect(screen.getByTestId("turn-work-files")).toHaveTextContent("本轮没有交付文件 · 保存了 3 个工作文件");
  });
});

describe("a canvas edit on a Turn's own stream", () => {
  it("counts for that Turn even when the Turn wrote no new section", async () => {
    const { attachCanvasSSE } = await import("@/lib/canvas-sse");
    const handlers = new Map<string, (event: MessageEvent) => void>();
    const es = {
      addEventListener: (type: string, fn: (event: MessageEvent) => void) => handlers.set(type, fn),
      removeEventListener: () => {},
    };
    onCanvasOpened({ canvas_id: "cv_1", session_id: "s_1" });
    onSectionAdded({ section: narrative("sec_old", "t_0", "季度趋势") });
    attachCanvasSSE(es as unknown as EventSource, undefined, "t_2");
    handlers.get("canvas.section_patched")?.(new MessageEvent("canvas.section_patched", {
      data: JSON.stringify({ section_id: "sec_old", patch: { markdown: "## 季度趋势\n\n改过" }, rev: 2 }),
    }));
    renderIntl(<LiveTurnOutputs turnId="t_2" steps={[]} />);
    expect(screen.getByTestId("chat-inline-report")).toHaveTextContent("本轮修改 1 节");
  });
});

describe("the work-file line opens this Turn's files", () => {
  it("asks Results for the Turn's files when Results is available, and stays text otherwise", async () => {
    const caps = await import("@/lib/use-results-capabilities");
    const snapshot = vi.spyOn(caps, "useResultsCapabilitiesSnapshot");
    const { RESULTS_TURN_FILES_EVENT } = await import("@/lib/canvas-workspace-focus");
    const asked: string[] = [];
    const listen = (event: Event) => asked.push((event as CustomEvent<{ turnId: string }>).detail.turnId);
    window.addEventListener(RESULTS_TURN_FILES_EVENT, listen);
    try {
      snapshot.mockReturnValue({ workspace: true } as never);
      renderIntl(<ChatBubbleNew text="完成。" extras={{ report: null, workFiles: 2, turnId: "t_9" }} />);
      fireEvent.click(screen.getByRole("button", { name: /在成果中查看这一轮的文件/ }));
      expect(asked).toEqual(["t_9"]);
      cleanup();
      snapshot.mockReturnValue({ workspace: false } as never);
      renderIntl(<ChatBubbleNew text="完成。" extras={{ report: null, workFiles: 2, turnId: "t_9" }} />);
      expect(screen.queryByRole("button", { name: /在成果中查看这一轮的文件/ })).toBeNull();
      expect(screen.getByTestId("turn-work-files")).toHaveTextContent("本轮没有交付文件 · 保存了 2 个工作文件");
    } finally {
      window.removeEventListener(RESULTS_TURN_FILES_EVENT, listen);
      snapshot.mockRestore();
    }
  });
});

