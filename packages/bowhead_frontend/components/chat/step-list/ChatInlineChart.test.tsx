/**
 * A chart delivered in chat is edited in place: the earlier answer's figure
 * shows the newest saved revision, live and after a reload.
 */
import "@testing-library/jest-dom/vitest";

import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { attachCanvasSSE } from "@/lib/canvas-sse";
import { getCanvasSnapshot, onCanvasOpened, onSectionAdded, resetCanvas } from "@/lib/canvas-store";
import type { ChartSpec } from "@/lib/chart/types";
import { recordChatFigureRevision, resetChatFigureRevisions } from "@/lib/chat-figure-revisions";
import type { ReportSection } from "@/lib/types";

import { ChatBubbleNew } from "./ChatBubbleNew";
import { ChatInlineChart } from "./ChatInlineChart";

vi.mock("next/dynamic", () => ({
  default: () =>
    function ChartBlockStub({ spec }: { spec: { title?: string } }) {
      return <div data-testid="chart-block">{spec.title}</div>;
    },
}));

afterEach(() => {
  cleanup();
  resetCanvas();
  resetChatFigureRevisions();
});

const spec = (title: string) => ({ kind: "bar", title }) as unknown as ChartSpec;

class FakeEventSource {
  handlers = new Map<string, (e: MessageEvent) => void>();
  addEventListener(type: string, handler: (e: MessageEvent) => void) {
    this.handlers.set(type, handler);
  }
  removeEventListener(type: string) {
    this.handlers.delete(type);
  }
  emit(type: string, payload: unknown) {
    this.handlers.get(type)?.(new MessageEvent(type, { data: JSON.stringify(payload) }));
  }
}

describe("ChatInlineChart — in-place chart edits", () => {
  it("a later Turn's chat-only update replaces the earlier figure without entering Canvas", () => {
    onCanvasOpened({ canvas_id: "cv_1", session_id: "s_1" });
    render(<ChatBubbleNew text="Primes per hundred." artifacts={[{ kind: "chart", id: "sec_chat", spec: spec("Primes") }]} />);
    expect(screen.getByTestId("chart-block")).toHaveTextContent("Primes");

    const es = new FakeEventSource();
    attachCanvasSSE(es as unknown as EventSource);
    act(() => {
      es.emit("canvas.section_patched", {
        canvas_id: "cv_1",
        section_id: "sec_chat",
        kind: "chart",
        rev: 2,
        hidden_for_chat_mode: true,
        patch: { id: "sec_chat", kind: "chart", rev: 2, spec: spec("Primes (green)") },
      });
    });
    expect(screen.getByTestId("chart-block")).toHaveTextContent("Primes (green)");
    expect(getCanvasSnapshot().sections).toEqual([]);
  });

  it("keeps the newest revision from live updates and the history projection", () => {
    const { rerender } = render(<ChatInlineChart spec={spec("Delivered")} sectionId="sec_1" />);
    act(() => recordChatFigureRevision("sec_1", spec("Edited"), 2));
    expect(screen.getByTestId("chart-block")).toHaveTextContent("Edited");
    act(() => recordChatFigureRevision("sec_1", spec("Stale"), 1));
    expect(screen.getByTestId("chart-block")).toHaveTextContent("Edited");
    // A reload returns the saved revision with its rev; a newer one wins.
    rerender(<ChatInlineChart spec={spec("Reloaded")} sectionId="sec_1" rev={3} />);
    expect(screen.getByTestId("chart-block")).toHaveTextContent("Reloaded");
  });

  it("a report chart also shown in chat follows its loaded Canvas revision", () => {
    onCanvasOpened({ canvas_id: "cv_1", session_id: "s_1" });
    act(() => {
      onSectionAdded({ section: { id: "sec_report", kind: "chart", rev: 4, spec: spec("Canvas edit") } as unknown as ReportSection });
    });
    render(<ChatInlineChart spec={spec("Delivered")} sectionId="sec_report" rev={3} />);
    expect(screen.getByTestId("chart-block")).toHaveTextContent("Canvas edit");
  });
});
