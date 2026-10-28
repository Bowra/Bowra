/**
 * Chat workbench/report canvas batch 4b: writing progress joins the toolbar status group, active section gets an outline, read-only status badge has no menu.
 * Component-level; ReportView integration (remove banner when toolbar status exists, stop dimming the entire body) is in report-view-toolbar.test.tsx.
 */
import "@testing-library/jest-dom/vitest";

import { act, cleanup, render, renderHook, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { onCanvasDone, onSectionDone, resetCanvas } from "@/lib/canvas-store";
import zhMessages from "@/messages/zh-CN.json";
import type { ReportSection } from "@/lib/types";

import {
  ReportToolbarStatusGroup,
  StreamingToolbarStatus,
  STREAMING_ANNOUNCE_INTERVAL_MS,
  streamingOutcome,
  TERMINAL_WAIT_MS,
  useToolbarAnnouncement,
  savedCount,
  writingInterruption,
} from "./report-toolbar-status";
import { StreamingPreview, streamingSectionTitle } from "./streaming-preview";

afterEach(() => {
  cleanup();
  resetCanvas();
  vi.useRealTimers();
});

describe("useToolbarAnnouncement：写作进度最多每 10 秒一次，首条立即播；结束时按本轮终态立即播", () => {
  const label = (o: string) => ({ done: "报告已更新", stopped: "已停止", failed: "写作失败", ended: "写作已结束" })[o] ?? o;

  it("首条立即播；间隔内的变化只播最后一条，到点再播", () => {
    vi.useFakeTimers();
    const { result, rerender } = renderHook(
      ({ on, text }) => useToolbarAnnouncement(on, text, null, label),
      { initialProps: { on: false, text: null as string | null } },
    );
    expect(result.current).toBe("");
    rerender({ on: true, text: "正在更新报告 · 第 1 / 6 节" });
    expect(result.current).toBe("正在更新报告 · 第 1 / 6 节");
    rerender({ on: true, text: "正在更新报告 · 第 2 / 6 节" });
    rerender({ on: true, text: "正在更新报告 · 第 3 / 6 节" });
    expect(result.current).toBe("正在更新报告 · 第 1 / 6 节");
    act(() => {
      vi.advanceTimersByTime(STREAMING_ANNOUNCE_INTERVAL_MS - 1);
    });
    expect(result.current).toBe("正在更新报告 · 第 1 / 6 节");
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(result.current).toBe("正在更新报告 · 第 3 / 6 节");
  });

  it.each([
    ["completed", "报告已更新"],
    ["aborted", "已停止"],
    ["cancelled", "已停止"],
    ["failed", "写作失败"],
  ])("本轮终态 %s 已知：写作结束立即播「%s」，还没到点的进度作废；下一轮首条又立即播", (status, sentence) => {
    vi.useFakeTimers();
    const { result, rerender } = renderHook(
      ({ on, text, term }) => useToolbarAnnouncement(on, text, term, label),
      { initialProps: { on: true, text: "进度 1" as string | null, term: null as string | null } },
    );
    rerender({ on: true, text: "进度 2", term: null });
    rerender({ on: false, text: null, term: status });
    expect(result.current).toBe(sentence);
    act(() => {
      vi.advanceTimersByTime(STREAMING_ANNOUNCE_INTERVAL_MS * 2);
    });
    expect(result.current).toBe(sentence);
    rerender({ on: true, text: "下一轮进度", term: null });
    expect(result.current).toBe("下一轮进度");
  });

  it("画布先退出写作、本轮终态稍后才到：等到后播终态；等不到或终态不认识时播中性的「写作已结束」", () => {
    vi.useFakeTimers();
    const { result, rerender } = renderHook(
      ({ on, term }) => useToolbarAnnouncement(on, on ? "进度" : null, term, label),
      { initialProps: { on: true, term: null as string | null } },
    );
    rerender({ on: false, term: null });
    expect(result.current).toBe("进度");
    act(() => {
      vi.advanceTimersByTime(TERMINAL_WAIT_MS - 1);
    });
    rerender({ on: false, term: "aborted" });
    expect(result.current).toBe("已停止");
    act(() => {
      vi.advanceTimersByTime(TERMINAL_WAIT_MS);
    });
    expect(result.current).toBe("已停止");
    rerender({ on: true, term: null });
    rerender({ on: false, term: null });
    act(() => {
      vi.advanceTimersByTime(TERMINAL_WAIT_MS);
    });
    expect(result.current).toBe("写作已结束");
    rerender({ on: true, term: null });
    rerender({ on: false, term: "something_else" });
    act(() => {
      vi.advanceTimersByTime(TERMINAL_WAIT_MS);
    });
    expect(result.current).toBe("写作已结束");
  });

  it("F5：一挂上就不在写作（终态来自水合），不补播任何终态", () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useToolbarAnnouncement(false, null, "completed", label));
    act(() => {
      vi.advanceTimersByTime(TERMINAL_WAIT_MS * 2);
    });
    expect(result.current).toBe("");
  });

  it("streamingOutcome 只认本轮终态", () => {
    expect(streamingOutcome("completed")).toBe("done");
    expect(streamingOutcome("aborted")).toBe("stopped");
    expect(streamingOutcome("cancelled")).toBe("stopped");
    expect(streamingOutcome("failed")).toBe("failed");
    expect(streamingOutcome(null)).toBeNull();
    expect(streamingOutcome("running")).toBeNull();
  });
});

describe("ReportToolbarStatusGroup：读屏区域常驻，终态只看本轮结果", () => {
  const wrap = (on: boolean, term: string | null) => (
    <NextIntlClientProvider locale="zh-CN" messages={zhMessages}>
      <ReportToolbarStatusGroup isStreaming={on} turnId={null} turnTerminalStatus={term} idle={<span>徽标</span>} />
    </NextIntlClientProvider>
  );

  it("写作开始前区域就在；写作中播进度；正常完成但有被中断标记的节（is_partial）仍播「报告已更新」", async () => {
    const { rerender } = renderIntl(<ReportToolbarStatusGroup isStreaming={false} turnId={null} idle={<span>徽标</span>} />);
    const live = screen.getByTestId("report-toolbar-announcer");
    expect(live).toHaveAttribute("role", "status");
    expect(live).toHaveAttribute("aria-live", "polite");
    expect(live.textContent).toBe("");
    rerender(wrap(true, null));
    expect(screen.getByTestId("report-toolbar-announcer")).toBe(live);
    await waitFor(() => expect(live.textContent).toBe("进行中"));
    // Backend abort_open_section can mark unclosed sections is_partial even on normal completion: the canvas contains an interrupted section.
    onSectionDone({ section_id: "sec_open", payload: {}, is_partial: true });
    rerender(wrap(false, "completed"));
    expect(screen.getByTestId("report-toolbar-announcer")).toBe(live);
    expect(live.textContent).toBe("报告已更新");
    expect(screen.getByText("徽标")).toBeInTheDocument();
  });

  it("本轮被停止 / 失败时，即使画布没有错误、是由兜底标成完成，也按本轮结果播", () => {
    const { rerender } = render(wrap(true, null));
    const live = screen.getByTestId("report-toolbar-announcer");
    onCanvasDone({ errors: [] });
    rerender(wrap(false, "aborted"));
    expect(live.textContent).toBe("已停止");
    rerender(wrap(true, null));
    rerender(wrap(false, "failed"));
    expect(live.textContent).toBe("写作失败");
  });
});

describe("第 4b 批余项：写作的暂停与中断（只取前端自己能确定的事实）", () => {
  const group = (props: Partial<Parameters<typeof ReportToolbarStatusGroup>[0]>) => (
    <NextIntlClientProvider locale="zh-CN" messages={zhMessages}>
      <ReportToolbarStatusGroup isStreaming={false} turnId={null} idle={<span>徽标</span>} {...props} />
    </NextIntlClientProvider>
  );

  it("writingInterruption：只有停止与失败算打断", () => {
    expect(writingInterruption("aborted")).toBe("stopped");
    expect(writingInterruption("cancelled")).toBe("stopped");
    expect(writingInterruption("failed")).toBe("failed");
    expect(writingInterruption("completed")).toBeNull();
    expect(writingInterruption(null)).toBeNull();
  });

  it("写作中连接断开、正在重连：转圈换成静止的暂停标记，文字「输出已暂停，正在重新连接」；连上后恢复", async () => {
    const { rerender } = render(group({ isStreaming: true, connection: "reconnecting" }));
    const root = screen.getByTestId("report-toolbar-streaming");
    expect(root).toHaveAttribute("data-state", "paused");
    expect(root).toHaveClass("is-paused");
    expect(root.querySelector(".rc-spinner")).toBeNull();
    expect(root.querySelector(".rc-toolbar-stream-paused")).toHaveAttribute("aria-hidden", "true");
    expect(root).toHaveTextContent("输出已暂停，正在重新连接");
    await waitFor(() => expect(screen.getByTestId("report-toolbar-announcer")).toHaveTextContent("输出已暂停，正在重新连接"));
    rerender(group({ isStreaming: true, connection: null }));
    expect(screen.getByTestId("report-toolbar-streaming")).not.toHaveAttribute("data-state");
    expect(screen.getByTestId("report-toolbar-streaming").querySelector(".rc-spinner")).not.toBeNull();
  });

  it("放弃重连：写作进度换成「连接中断，已写的部分已保存」+「刷新报告」，立即播报，之后不再被「写作已结束」覆盖", () => {
    vi.useFakeTimers();
    const onRefresh = vi.fn();
    const { rerender } = render(group({ isStreaming: true }));
    const live = screen.getByTestId("report-toolbar-announcer");
    rerender(group({ isStreaming: true, connection: "lost", onRefresh }));
    expect(screen.queryByTestId("report-toolbar-streaming")).toBeNull();
    const note = screen.getByTestId("report-toolbar-outcome");
    expect(note).toHaveAttribute("data-outcome", "lost");
    expect(note).toHaveClass("is-error");
    expect(note).toHaveTextContent("连接中断，已写的部分已保存");
    expect(live).toHaveTextContent("连接中断，已写的部分已保存");
    // Fallback later marks the canvas complete: this turn has no terminal state; formerly it announced “写作已结束” after 1.5 seconds.
    rerender(group({ isStreaming: false, connection: "lost", onRefresh }));
    act(() => {
      vi.advanceTimersByTime(TERMINAL_WAIT_MS * 2);
    });
    expect(live).toHaveTextContent("连接中断，已写的部分已保存");
    screen.getByRole("button", { name: "刷新报告" }).click();
    expect(onRefresh).toHaveBeenCalledTimes(1);
    expect(screen.getByText("徽标")).toBeInTheDocument();
  });

  it.each([
    ["stopped", "已停止，已写的部分已保存", false],
    ["failed", "写作中断，已写的部分已保存", true],
  ] as const)("上一次写作 %s：不在写作时徽标旁常驻说明；写作中不显示", (kind, text, isError) => {
    const { rerender } = render(group({ interruption: kind }));
    const note = screen.getByTestId("report-toolbar-outcome");
    expect(note).toHaveTextContent(text);
    expect(note.classList.contains("is-error")).toBe(isError);
    expect(screen.queryByRole("button", { name: "刷新报告" })).toBeNull();
    rerender(group({ interruption: kind, isStreaming: true }));
    expect(screen.queryByTestId("report-toolbar-outcome")).toBeNull();
  });

  it.each([
    ["stopped", 3, "已停止，已保存 3 节"],
    ["failed", 1, "写作中断，已保存 1 节"],
    ["stopped", 0, "已停止，已写的部分已保存"],
    ["failed", null, "写作中断，已写的部分已保存"],
    ["stopped", undefined, "已停止，已写的部分已保存"],
  ] as const)("本轮落库节数（done 的 report_sections_saved）：%s、%s ⇒「%s」（0 与缺字段不写数字）", (kind, n, text) => {
    render(group({ interruption: kind, sectionsSaved: n }));
    expect(screen.getByTestId("report-toolbar-outcome")).toHaveTextContent(text);
  });

  it("英文按单复数写节数", () => {
    renderIntl(
      <ReportToolbarStatusGroup isStreaming={false} turnId={null} idle={null} interruption="stopped" sectionsSaved={1} />,
      { locale: "en-US" },
    );
    expect(screen.getByTestId("report-toolbar-outcome")).toHaveTextContent("Stopped. 1 section saved");
    cleanup();
    renderIntl(
      <ReportToolbarStatusGroup isStreaming={false} turnId={null} idle={null} interruption="failed" sectionsSaved={4} />,
      { locale: "en-US" },
    );
    expect(screen.getByTestId("report-toolbar-outcome")).toHaveTextContent("Writing interrupted. 4 sections saved");
  });

  it("savedCount：只有正整数写进说明", () => {
    expect(savedCount(2)).toBe(true);
    expect(savedCount(0)).toBe(false);
    expect(savedCount(null)).toBe(false);
    expect(savedCount(undefined)).toBe(false);
    expect(savedCount(1.5)).toBe(false);
  });

  it("没有连接问题、没有打断时：与第 4b 批相同（只有徽标，没有说明）", () => {
    render(group({}));
    expect(screen.getByText("徽标")).toBeInTheDocument();
    expect(screen.queryByTestId("report-toolbar-outcome")).toBeNull();
  });
});

describe("StreamingToolbarStatus", () => {
  it("可见文字对读屏隐藏并交给状态组；给了停止回调才有「停止」", () => {
    const onCancel = vi.fn();
    const onProgress = vi.fn();
    renderIntl(<StreamingToolbarStatus turnId={null} onCancel={onCancel} onProgress={onProgress} />);
    const root = screen.getByTestId("report-toolbar-streaming");
    expect(root.querySelector(".rc-toolbar-stream-text")).toHaveAttribute("aria-hidden", "true");
    // Report writing has not started: use the same source as the banner and say “进行中”.
    expect(onProgress).toHaveBeenLastCalledWith("进行中");
    screen.getByRole("button", { name: "停止" }).click();
    expect(onCancel).toHaveBeenCalledTimes(1);
    cleanup();
    renderIntl(<StreamingToolbarStatus turnId={null} />);
    expect(screen.queryByRole("button", { name: "停止" })).toBeNull();
  });
});

describe("正在写的章节（StreamingPreview）", () => {
  const SECS = [
    { id: "sec_a", kind: "narrative", markdown: "## 闭店影响\n闭店影响约占 1.2 个百分点" },
    { id: "sec_b", kind: "narrative", markdown: "没有标题的一段" },
  ] as unknown as ReportSection[];

  it("streamingSectionTitle 只认第一行的 markdown 标题", () => {
    expect(streamingSectionTitle("## 闭店影响\n正文")).toBe("闭店影响");
    expect(streamingSectionTitle("# 标题 #")).toBe("标题");
    expect(streamingSectionTitle("正文 ## 不是标题")).toBeNull();
  });

  it("outlined block, one “正在写…” label and two static skeleton lines; untitled sections read “正在写这一节”", () => {
    const { container } = renderIntl(<StreamingPreview sections={SECS} />);
    const blocks = container.querySelectorAll(".sp-block");
    expect(blocks).toHaveLength(2);
    expect(blocks[0]).toHaveClass("sp-block--writing");
    expect(blocks[0]).toHaveAttribute("aria-busy", "true");
    expect(blocks[0].querySelector(".sp-writing-label")?.textContent).toBe("正在写「闭店影响」");
    expect(blocks[1].querySelector(".sp-writing-label")?.textContent).toBe("正在写这一节");
    expect(blocks[0].querySelectorAll(".sp-skeleton > span")).toHaveLength(2);
    expect(blocks[0].querySelector(".sp-skeleton")).toHaveAttribute("aria-hidden", "true");
  });
});
