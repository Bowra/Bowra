import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const canvas = { title: null as string | null, sections: [] as unknown[], bufferedLeadSectionIds: new Set<string>() };
vi.mock("@/lib/canvas-store", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getCanvasSnapshot: () => canvas,
  subscribeCanvas: () => () => {},
}));

import { renderIntl } from "@/__tests__/intl-render";
import { CANVAS_OPEN_RESULT_EVENT, type CanvasOpenResultDetail } from "@/lib/canvas-workspace-focus";
import { reportChangesOf } from "@/lib/report-changes";

import { ChatBubbleNew } from "./ChatBubbleNew";
import { ChatInlineReport } from "./ChatInlineReport";

afterEach(() => {
  cleanup();
  canvas.title = null;
  canvas.sections = [];
});

const changes = reportChangesOf({
  report_changes: {
    added: [
      { section_id: "sec_h1", kind: "h1", title: "华东 Q3 销售额下滑归因" },
      { section_id: "sec_a", kind: "narrative", title: "核心结论" },
      { section_id: "sec_chart", kind: "chart", title: "月坪效前十" },
      { section_id: "sec_b", kind: "narrative", title: "品类拆解" },
    ],
    updated: [{ section_id: "sec_c", kind: "narrative", title: "门店排名" }],
    added_kinds: { h1: 1, narrative: 2, chart: 1 },
    updated_kinds: { narrative: 1 },
  },
})!;

describe("ChatInlineReport (chat running state R2.2)", () => {
  it("names the report, what this Turn changed and the sections", () => {
    canvas.title = "华东 Q3 销售额下滑归因";
    renderIntl(<ChatInlineReport changes={changes} />);
    const card = screen.getByTestId("chat-inline-report");
    expect(card).toHaveTextContent("报告「华东 Q3 销售额下滑归因」");
    // The title is the report's frame; the chart is counted apart and not listed with the chapters.
    expect(card).toHaveTextContent("本轮新增 2 节、1 张图 · 修改 1 节");
    expect(card).toHaveTextContent("核心结论、品类拆解、门店排名");
    expect(card).toHaveTextContent("打开到这一节");
  });

  it("is titled as Results titles the report: its h1 first, then the canvas title", () => {
    // 116, 1.7.89: a report composed from a helper's payload has no canvas title; its h1 carries it.
    canvas.sections = [{ id: "sec_h1", kind: "h1", text: "销售额 Top10 门店 · 月坪效看板" }];
    renderIntl(<ChatInlineReport changes={changes} />);
    expect(screen.getByTestId("chat-inline-report")).toHaveTextContent("报告「销售额 Top10 门店 · 月坪效看板」");
    cleanup();
    canvas.sections = [{ id: "sec_h1", kind: "h1", text: "旧标题", superseded: true }];
    canvas.title = "画布标题";
    renderIntl(<ChatInlineReport changes={changes} />);
    expect(screen.getByTestId("chat-inline-report")).toHaveTextContent("报告「画布标题」");
  });

  it("opens the report in Results at the first changed section", () => {
    const opened: CanvasOpenResultDetail[] = [];
    const listen = (event: Event) => opened.push((event as CustomEvent<CanvasOpenResultDetail>).detail);
    window.addEventListener(CANVAS_OPEN_RESULT_EVENT, listen);
    renderIntl(<ChatInlineReport changes={changes} />);
    fireEvent.click(screen.getByRole("button", { name: /在成果中打开到这一节/ }));
    // The visible words beside the card do the same (116, 1.7.89: they did nothing).
    fireEvent.click(screen.getByTestId("chat-inline-report-go"));
    window.removeEventListener(CANVAS_OPEN_RESULT_EVENT, listen);
    expect(opened).toEqual([
      { target: { type: "report", sectionId: "sec_a" }, entryPoint: "chat" },
      { target: { type: "report", sectionId: "sec_a" }, entryPoint: "chat" },
    ]);
  });

  it("counts sections past the ones it names, and reads untitled reports and sections plainly", () => {
    const many = reportChangesOf({
      report_changes: {
        added: [1, 2, 3, 4].map((n) => ({ section_id: `sec_${n}`, kind: "narrative", title: n === 2 ? "" : `第${n}部分` })),
        updated: [],
        added_kinds: { narrative: 6 },
      },
    })!;
    renderIntl(<ChatInlineReport changes={many} />);
    const card = screen.getByTestId("chat-inline-report");
    expect(card).toHaveTextContent(/^报告/);
    expect(card).toHaveTextContent("本轮新增 6 节");
    expect(card).toHaveTextContent("第1部分、第3部分、第4部分 等 6 节");
  });

  it("speaks the answer language's copy", () => {
    renderIntl(<ChatInlineReport changes={changes} />, { locale: "en-US" });
    expect(screen.getByTestId("chat-inline-report")).toHaveTextContent("This turn: 2 sections and 1 chart added · 1 section revised");
  });
});

it("says the report was updated when only its title changed, and opens there", () => {
  const titleOnly = reportChangesOf({ report_changes: { added: [], updated: [{ section_id: "sec_h1", kind: "h1", title: "新标题" }], updated_kinds: { h1: 1 } } })!;
  renderIntl(<ChatInlineReport changes={titleOnly} />);
  expect(screen.getByTestId("chat-inline-report")).toHaveTextContent("本轮更新了报告");
  expect(titleOnly.firstSectionId).toBe("sec_h1");
});

describe("the answer bubble", () => {
  it("shows the report card after the answer once it has finished streaming", () => {
    renderIntl(<ChatBubbleNew text="华东下滑 8%。" isStreaming extras={{ report: changes, workFiles: 0 }} />);
    expect(screen.queryByTestId("chat-inline-report")).toBeNull();
    cleanup();
    renderIntl(<ChatBubbleNew text="华东下滑 8%。" extras={{ report: changes, workFiles: 0 }} />);
    expect(screen.getByTestId("chat-inline-report")).toBeInTheDocument();
    cleanup();
    renderIntl(<ChatBubbleNew text="华东下滑 8%。" />);
    expect(screen.queryByTestId("chat-inline-report")).toBeNull();
  });
});
