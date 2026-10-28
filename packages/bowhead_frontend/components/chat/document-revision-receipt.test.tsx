/**
 * Document revision receipt (Chat Cards Spec §4) and the Composer revision target bar (§9): both name the real
 * draft and its number, never a task ID, and the receipt follows the Turn through generating → ready / failed.
 */
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { DocumentRevisionReceipt, documentRevisionReceiptState } from "@/components/chat/document-revision-receipt";
import { RevisionTargetBar } from "@/components/chat/revision-target-bar";
import { copyComposerDocumentRevision } from "@/lib/composer-document-revision";
import { subscribeDocumentCanvasRequests } from "@/lib/document-canvas-requests";
import { dispatchToTurnStore } from "@/lib/turn-store-dispatch";
import { _resetAllTurnsForTesting, getTurnSnapshot } from "@/lib/turn-store";

afterEach(() => {
  cleanup();
  _resetAllTurnsForTesting();
});

const receipt = { title: "Q2 运营复盘.pptx", version: 2 };
const card = () => screen.getByTestId("document-revision-receipt");

describe("DocumentRevisionReceipt", () => {
  it("generating: names the draft it is based on and says the original is kept", () => {
    renderIntl(<DocumentRevisionReceipt receipt={receipt} state="generating" />);
    expect(card()).toHaveAttribute("data-state", "generating");
    expect(card()).toHaveTextContent("正在生成新版");
    expect(card()).toHaveTextContent("Q2 运营复盘.pptx · 基于第 2 版草稿");
    expect(card()).toHaveTextContent("生成后会在画布中等你审阅，原草稿保留。");
  });

  it("ready: names the new draft number and offers Review in Canvas", () => {
    const onOpen = vi.fn();
    renderIntl(<DocumentRevisionReceipt receipt={receipt} state="ready" onOpen={onOpen} />);
    expect(card()).toHaveTextContent("第 3 版草稿已生成");
    expect(card()).toHaveTextContent("Q2 运营复盘.pptx");
    fireEvent.click(screen.getByRole("button", { name: "在画布中审阅" }));
    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it("an older receipt settles into one row: draft from → to", () => {
    renderIntl(<DocumentRevisionReceipt receipt={receipt} state="ready" settled onOpen={vi.fn()} />, { locale: "en-US" });
    expect(card()).toHaveClass("ui-card--row");
    expect(card()).toHaveTextContent("Revised Q2 运营复盘.pptx · draft 2 to 3");
    expect(screen.getByRole("button", { name: "Open in Canvas" })).toBeTruthy();
  });

  it("failed and paused Turns say what happened to the draft", () => {
    renderIntl(<DocumentRevisionReceipt receipt={receipt} state="failed" />);
    expect(card()).toHaveTextContent("没有生成新版：Q2 运营复盘.pptx · 第 2 版草稿保持不变");
    cleanup();
    renderIntl(<DocumentRevisionReceipt receipt={receipt} state="awaiting_input" />);
    expect(card()).toHaveTextContent("修改要求已发送");
    expect(card()).toHaveTextContent("Q2 运营复盘.pptx · 第 2 版草稿");
  });

  it.each([
    [{ running: true, failed: false, terminalStatus: null, hasSuccessorDraft: false }, "generating"],
    [{ running: false, failed: false, terminalStatus: "completed", hasSuccessorDraft: true }, "ready"],
    // A completed Turn that produced no successor draft did not make a new version.
    [{ running: false, failed: false, terminalStatus: "completed", hasSuccessorDraft: false }, "failed"],
    [{ running: false, failed: false, terminalStatus: "clarification_pending", hasSuccessorDraft: false }, "awaiting_input"],
    [{ running: false, failed: true, terminalStatus: "error", hasSuccessorDraft: true }, "failed"],
    [{ running: false, failed: false, terminalStatus: "aborted", hasSuccessorDraft: false }, "failed"],
  ] as const)("state from the Turn outcome %j → %s", (turn, expected) => {
    expect(documentRevisionReceiptState(turn)).toBe(expected);
  });

  it("generating sweeps an indeterminate bar and shows the elapsed time; ready and sent show the Turn time", () => {
    renderIntl(<DocumentRevisionReceipt receipt={receipt} state="generating" meta="42 秒" />);
    expect(card()).toHaveTextContent("42 秒");
    expect(card().querySelector(".ui-card__progress--indeterminate")).not.toBeNull();
    cleanup();
    renderIntl(<DocumentRevisionReceipt receipt={receipt} state="ready" meta="14:36" onOpen={vi.fn()} />);
    expect(card()).toHaveTextContent("14:36");
    cleanup();
    renderIntl(<DocumentRevisionReceipt receipt={receipt} state="awaiting_input" meta="14:31" />);
    expect(card()).toHaveTextContent("14:31");
  });

  it("the failed row uses the failure intent; the history row's action is an accent ghost button", () => {
    renderIntl(<DocumentRevisionReceipt receipt={receipt} state="failed" />);
    expect(card()).toHaveClass("ui-card--failure", "ui-card--row");
    cleanup();
    renderIntl(<DocumentRevisionReceipt receipt={receipt} state="ready" settled onOpen={vi.fn()} />);
    const open = screen.getByRole("button", { name: "在画布中打开" });
    expect(open).toHaveClass("settings-button--ghost");
    expect(open).not.toHaveClass("ui-card__btn--plain");
  });
});

describe("turn_started carries the receipt facts (live == replay)", () => {
  const started = { turn_id: "t_rev", session_id: "s_1", document_revision: receipt };

  it.each(["live", "replay"] as const)("%s dispatch stores the same title and number", (source) => {
    dispatchToTurnStore("turn_started", started, source, "t_rev");
    expect(getTurnSnapshot("t_rev")?.documentRevision).toEqual(receipt);
  });

  it("keeps the first receipt and ignores malformed ones", () => {
    dispatchToTurnStore("turn_started", { ...started, document_revision: { title: "", version: 1 } }, "live", "t_bad");
    expect(getTurnSnapshot("t_bad")?.documentRevision).toBeUndefined();
    dispatchToTurnStore("turn_started", started, "live", "t_rev");
    dispatchToTurnStore("turn_started", { ...started, document_revision: { title: "Other.pptx", version: 5 } }, "replay", "t_rev");
    expect(getTurnSnapshot("t_rev")?.documentRevision).toEqual(receipt);
  });
});

describe("RevisionTargetBar", () => {
  const target = { kind: "document_revision" as const, version: 1 as const, session_id: "s_1", job_id: "djob_abc", draft_digest: "a".repeat(64) };

  it("shows the real draft title and number, never the task ID, and opens the draft", () => {
    const requests: unknown[] = [];
    const stop = subscribeDocumentCanvasRequests((request) => requests.push(request));
    const revision = copyComposerDocumentRevision({ handoffId: "h", target, display: { title: "Q2 运营复盘.pptx", version: 2 } });
    renderIntl(<RevisionTargetBar revision={revision} removeDisabled={false} onRemove={vi.fn()} />);
    const bar = screen.getByTestId("composer-document-revision");
    expect(bar).toHaveTextContent("正在修改Q2 运营复盘.pptx第 2 版草稿");
    expect(bar).not.toHaveTextContent("djob_");
    expect(screen.getByRole("button", { name: "不再修改Q2 运营复盘.pptx" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "在画布中打开Q2 运营复盘.pptx" }));
    expect(requests).toEqual([{ sessionId: "s_1", jobId: "djob_abc" }]);
    stop();
  });

  it("keeps the generic label for handoffs saved without a name", () => {
    renderIntl(<RevisionTargetBar revision={copyComposerDocumentRevision({ handoffId: "h", target })} removeDisabled onRemove={vi.fn()} />, { locale: "en-US" });
    expect(screen.getByTestId("composer-document-revision")).toHaveTextContent("RevisingDocument draft");
    expect(screen.queryByRole("button", { name: /Open .* in Canvas/ })).toBeNull();
  });

  it.each([
    { title: "", version: 1 }, { title: "x", version: 0 }, { title: "x", version: 1.5 }, { title: "x", extra: true }, "x",
  ])("rejects a malformed display %j", (display) => {
    expect(() => copyComposerDocumentRevision({ handoffId: "h", target, display })).toThrow();
  });
});
