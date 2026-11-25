/**
 * Pin / Unpin in a Results file's "More actions" menu (result-pins PRD §4.1, D3): offered with `pin_result`, posts the
 * version shown, and the menu then offers Unpin.
 */
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { resetResultPinsForTests, type ResultPin } from "@/lib/result-pins";
import type { ResultCapability } from "@/lib/results-workspace-model";

import { ResultObjectActions } from "./result-object-actions";

const { toastMock } = vi.hoisted(() => ({ toastMock: vi.fn() }));
vi.mock("@/lib/use-toast", () => ({ toast: toastMock }));

const PIN: ResultPin = {
  kind: "file",
  pin_id: "art_root",
  artifact_id: "art_v2",
  title: "deck.pptx",
  version_no: 2,
  session_id: "s_1",
  opens: "session",
  changed_at: "2026-11-24T10:00:00+00:00",
  pinned_at: "2026-11-24T10:00:00+00:00",
};

const fetchMock = vi.fn();

function respond(status: number, body: unknown): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function renderActions(capabilities: ResultCapability[]) {
  return renderIntl(
    <ResultObjectActions
      artifactId="art_v2"
      capabilities={new Set(capabilities)}
      role="deliverable"
      mode="preview"
      downloadHref="/api/v1/files/art_v2"
    />,
  );
}

describe("ResultObjectActions pin", () => {
  beforeEach(() => {
    resetResultPinsForTests();
    toastMock.mockReset();
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    resetResultPinsForTests();
  });

  it("pins the version shown and then offers Unpin", async () => {
    let pinned = false;
    fetchMock.mockImplementation(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") {
        pinned = true;
        return respond(200, PIN);
      }
      return respond(200, { items: pinned ? [PIN] : [] });
    });
    renderActions(["download", "view_details", "pin_result"]);

    fireEvent.click(screen.getByRole("button", { name: "更多操作" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "置顶" }));

    await waitFor(() => expect(toastMock).toHaveBeenCalledWith({ kind: "success", text: "已置顶到侧栏" }));
    const post = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "POST");
    expect(JSON.parse(String((post?.[1] as RequestInit).body))).toEqual({ kind: "file", id: "art_v2" });

    fireEvent.click(screen.getByRole("button", { name: "更多操作" }));
    expect(await screen.findByRole("menuitem", { name: "取消置顶" })).toBeInTheDocument();
  });

  it("names the limit when the server refuses another pin", async () => {
    fetchMock.mockImplementation(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") {
        return respond(409, { detail: { error: { code: "result_pin_limit", limit: 100 } } });
      }
      return respond(200, { items: [] });
    });
    renderActions(["download", "pin_result"]);

    fireEvent.click(screen.getByRole("button", { name: "更多操作" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "置顶" }));

    await waitFor(() =>
      expect(toastMock).toHaveBeenCalledWith({
        kind: "error",
        text: "最多置顶 100 个成果。先在侧栏取消置顶一个，再置顶这个。",
      }),
    );
  });

  it("offers no Pin and loads nothing without the capability", () => {
    renderActions(["download", "view_details", "manage_in_files"]);
    fireEvent.click(screen.getByRole("button", { name: "更多操作" }));
    expect(screen.queryByRole("menuitem", { name: "置顶" })).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
