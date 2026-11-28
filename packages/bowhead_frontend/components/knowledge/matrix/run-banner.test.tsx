import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { renderIntl } from "@/__tests__/intl-render";
import { NextIntlClientProvider } from "next-intl";
import messages from "@/messages/zh-CN.json";
import type { MatrixRunVM } from "@/lib/kb-matrix/types";
import { RunBanners } from "./run-banner";

const run = (id: string, status: MatrixRunVM["status"]): MatrixRunVM => ({ runId: id, status, cellsTotal: 4, cellsDone: status === "succeeded" ? 4 : 0, cellsFailed: status === "failed" ? 2 : 0, etaS: 60, initiatedBy: "u1", skippedEdited: 0, scopeKind: "column", canCancel: true });
const props = { columns: [], cancelling: false, onCancel: vi.fn(), onViewFailed: vi.fn() };
beforeEach(() => { vi.useFakeTimers(); vi.clearAllMocks(); });
afterEach(() => { cleanup(); vi.useRealTimers(); });

it("opens historical tasks as one collapsed summary and retains failure actions after collapse", () => {
  renderIntl(<RunBanners {...props} runs={[1, 2, 3, 4].map((n) => run(String(n), n === 4 ? "failed" : "succeeded"))} />);
  const summary = screen.getByTestId("kb-matrix-task-summary");
  expect(summary).toHaveTextContent("运行记录");
  expect(summary).toHaveTextContent("1 个任务需要处理");
  expect(summary.parentElement).not.toHaveAttribute("open");
  expect(screen.getByRole("status")).toBeEmptyDOMElement();
  fireEvent.click(summary);
  fireEvent.click(screen.getByRole("button", { name: "查看失败" }));
  expect(props.onViewFailed).toHaveBeenCalledWith("4");
  fireEvent.click(summary);
  act(() => vi.advanceTimersByTime(5000));
  expect(summary).toHaveTextContent("1 个任务需要处理");
});

it("confirms an observed completion once, then keeps its history without replaying success", () => {
  const view = renderIntl(<RunBanners {...props} runs={[run("r1", "running")]} />);
  view.rerender(<NextIntlClientProvider locale="zh-CN" messages={messages}><RunBanners {...props} runs={[run("r1", "succeeded")]} /></NextIntlClientProvider>);
  expect(screen.getByTestId("kb-matrix-task-summary")).toHaveTextContent("本次已完成 1 个任务");
  act(() => vi.advanceTimersByTime(3001));
  view.rerender(<NextIntlClientProvider locale="zh-CN" messages={messages}><RunBanners {...props} runs={[run("r1", "succeeded")]} /></NextIntlClientProvider>);
  expect(screen.getByTestId("kb-matrix-task-summary")).toHaveTextContent("运行记录");
  view.unmount();
  renderIntl(<RunBanners {...props} runs={[run("r1", "succeeded")]} />);
  expect(screen.getByRole("status")).toBeEmptyDOMElement();
});

it("keeps the cancel control for a running task inside the summary", () => {
  renderIntl(<RunBanners {...props} runs={[run("r1", "running")]} />);
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(props.onCancel).toHaveBeenCalledWith("r1");
});

it("labels a trial run with its file count before the scope", () => {
  renderIntl(<RunBanners {...props} runs={[{ ...run("r1", "running"), scopeKind: "all_pending", trial: true, trialFiles: 3 }, { ...run("r2", "running"), scopeKind: "failed" }]} />);
  const [trial, failed] = screen.getAllByTestId("kb-matrix-runbanner");
  expect(trial).toHaveTextContent(/^试跑 3 份文件 · 全部未完成 · 正在运行/);
  expect(failed).toHaveTextContent(/^失败的格子 · 正在运行/);
});

it("says a finished run skipped the automatic retry for an outage", () => {
  renderIntl(<RunBanners {...props} runs={[{ ...run("r1", "succeeded"), cellsDone: 1, cellsFailed: 3, errorCode: "provider_outage" }, { ...run("r2", "succeeded"), cellsDone: 3, cellsFailed: 1 }]} />);
  const [outage, plain] = screen.getAllByTestId("kb-matrix-runbanner");
  expect(outage).toHaveTextContent(messages.kbMatrix.banner_done_outage);
  expect(plain).not.toHaveTextContent(messages.kbMatrix.banner_done_outage);
});

it("says another member's run stopped because the matrix was made private (S8)", () => {
  renderIntl(<RunBanners {...props} runs={[{ ...run("r1", "cancelled"), cellsDone: 1, errorCode: "matrix_became_private" }, { ...run("r2", "cancelled"), cellsDone: 2 }]} />);
  const [stopped, cancelled] = screen.getAllByTestId("kb-matrix-runbanner");
  expect(stopped).toHaveTextContent("已停止：矩阵已改为仅创建者可见，其他成员发起的运行不再继续 · 已完成的 1 格保留");
  expect(cancelled).toHaveTextContent("已取消 · 已完成的 2 格保留");
});
