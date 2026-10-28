import { act, cleanup, fireEvent, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderIntl } from "@/__tests__/intl-render";
import zh from "@/messages/zh-CN.json";
import { QueuedReferenceEvidence } from "./QueuedReferenceEvidence";

const props = { workspaceId: "ws_1", sessionId: "session-1", submissionId: "submission-1" };
function facts(state = "queued", visible = true, id = props.submissionId) {
  return {
    facts_version: 1, turn_id: "original-parent",
    source_locator: { kind: "queued_submission", submission_id: id, accepted_parent_turn_id: "original-parent", turn_id: state === "materialized" ? "child-1" : null, text_spans: [{ text: "private task" }] },
    queue: { state }, content_included: true,
    references: [{ resource_kind: "skill", resource_visibility: visible ? "visible" : "unavailable", label: "杜邦分析", parameters: { period: "private quarter" } }],
    evidence_completeness: { status: "partial" }, execution_evidence_turn_id: state === "materialized" ? "child-1" : null,
  };
}
const response = (body: unknown = facts(), status = 200) => new Response(JSON.stringify(body), { status });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); delete window.__BOWHEAD_BASE_PATH__; });

describe("queued source with the real HTTP reader", () => {
  it("reads the original submission on demand and requests text and parameters explicitly", async () => {
    window.__BOWHEAD_BASE_PATH__ = "/bowhead";
    const fetch = vi.fn().mockImplementation(async () => response());
    vi.stubGlobal("fetch", fetch);
    const { container } = renderIntl(<QueuedReferenceEvidence {...props} />);
    expect(fetch).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "排队内容" }));
    await screen.findByText("杜邦分析");
    expect(fetch.mock.calls[0][0]).toBe("/bowhead/api/v1/chat/sessions/session-1/reference-queue/submission-1/evidence?workspace_id=ws_1");
    expect(fetch.mock.calls[0][1]).toMatchObject({ cache: "no-store" });
    expect(container.textContent).not.toContain("private");
    fireEvent.click(screen.getByRole("button", { name: "查看正文和参数" }));
    await screen.findByText("private task");
    expect(container.textContent).toContain("private quarter");
    expect(fetch.mock.calls[1][0]).toContain("&include_content=true");
    expect(screen.queryByText("已完成执行")).toBeNull();
    expect(container.textContent).not.toContain("original-parent");
    fireEvent.click(screen.getByRole("button", { name: "收起" }));
    expect(container.textContent).not.toContain("private");
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "排队内容" }));
  });

  it.each(["queued", "cancelled", "materialized"])("renders %s acceptance facts without claiming execution", async state => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(facts(state))));
    const { container } = renderIntl(<QueuedReferenceEvidence {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "排队内容" }));
    await screen.findByText(zh.reference_evidence[`queue_${state}` as keyof typeof zh.reference_evidence]);
    expect(container.textContent).not.toContain("已完成执行");
  });

  it("hides revoked names and parameters and clears old text before a failed refresh", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(response()).mockResolvedValueOnce(response(facts("queued", false))).mockResolvedValueOnce(response({}, 403));
    vi.stubGlobal("fetch", fetch);
    const { container } = renderIntl(<QueuedReferenceEvidence {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "排队内容" }));
    await screen.findByText("杜邦分析");
    fireEvent.click(screen.getByRole("button", { name: "查看正文和参数" }));
    await screen.findByText("private task");
    expect(container.textContent).not.toContain("杜邦分析");
    expect(container.textContent).not.toContain("private quarter");
    fireEvent.click(screen.getByRole("button", { name: "刷新记录" }));
    expect(container.textContent).not.toContain("private");
    await screen.findByText(zh.reference_evidence.empty);
  });

  it("discards old responses on source change and close", async () => {
    let resolve!: (value: Response) => void;
    const fetch = vi.fn().mockImplementationOnce(() => new Promise<Response>(done => { resolve = done; })).mockResolvedValueOnce(response(facts("queued", true, "submission-2")));
    vi.stubGlobal("fetch", fetch);
    const { container, rerender } = renderIntl(<QueuedReferenceEvidence {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "排队内容" }));
    rerender(<NextIntlClientProvider locale="zh-CN" messages={zh}><QueuedReferenceEvidence {...props} submissionId="submission-2" /></NextIntlClientProvider>);
    expect(fetch.mock.calls[0][1].signal.aborted).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "排队内容" }));
    await screen.findByText("杜邦分析");
    fireEvent.click(screen.getByRole("button", { name: "收起" }));
    await act(async () => { resolve(response()); });
    expect(container.textContent).not.toContain("private");
    expect(screen.queryByText("杜邦分析")).toBeNull();
  });

  it("does not reopen a closed panel when its pending response arrives", async () => {
    let resolve!: (value: Response) => void;
    vi.stubGlobal("fetch", vi.fn().mockImplementation(() => new Promise<Response>(done => { resolve = done; })));
    const { container } = renderIntl(<QueuedReferenceEvidence {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "排队内容" }));
    fireEvent.click(screen.getByRole("button", { name: "收起" }));
    await act(async () => { resolve(response()); });
    expect(screen.getByRole("button", { name: "排队内容" }).getAttribute("aria-expanded")).toBe("false");
    expect(container.textContent).not.toContain("杜邦分析");
  });

  it("rejects a different submission instead of displaying its private content", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(facts("queued", true, "other-submission"))));
    renderIntl(<QueuedReferenceEvidence {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "排队内容" }));
    await screen.findByRole("alert");
    expect(screen.queryByText("杜邦分析")).toBeNull();
  });
});
