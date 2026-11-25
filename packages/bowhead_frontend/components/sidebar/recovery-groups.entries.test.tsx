/** Mixed groups (result pins): a pinned group's rows keep the given order, carry the icon flag, and count toward virtualization. */
import { cleanup, render } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ResultPin } from "@/lib/result-pins";
import type { SessionItem } from "@/lib/types";

import { RecoveryGroups, type RecoveryRenderGroup } from "./recovery-groups";

const session = (id: string): SessionItem => ({ id, title: id, intents: [], time: "1d", state: "done" });
const pin: ResultPin = {
  kind: "file",
  pin_id: "art_root",
  artifact_id: "art_v2",
  title: "deck.pptx",
  session_id: "s_1",
  opens: "session",
  changed_at: "2026-11-24T10:00:00+00:00",
  pinned_at: "2026-11-24T10:00:00+00:00",
};

function renderGroups(groups: RecoveryRenderGroup[]) {
  const scrollRef = createRef<HTMLDivElement>();
  const renderItem = vi.fn((item: SessionItem, options?: { withIcon?: boolean }) => (
    <div key={item.id} data-row={`session:${item.id}`} data-icon={String(Boolean(options?.withIcon))} />
  ));
  const renderResult = vi.fn((result: ResultPin, options?: { withIcon?: boolean }) => (
    <div key={result.pin_id} data-row={`result:${result.pin_id}`} data-icon={String(Boolean(options?.withIcon))} />
  ));
  const view = render(
    <div ref={scrollRef}>
      <RecoveryGroups groups={groups} scrollRef={scrollRef} renderItem={renderItem} renderResult={renderResult} />
    </div>,
  );
  return { ...view, renderItem, renderResult };
}

afterEach(cleanup);

describe("RecoveryGroups mixed entries", () => {
  it("renders the notice, then results and conversations in the given order, all with icons", () => {
    const { container } = renderGroups([
      {
        id: "pinned",
        label: "Pinned",
        items: [session("s_1")],
        entries: [{ kind: "result", pin }, { kind: "session", item: session("s_1") }],
        notice: <p data-row="notice">failed</p>,
      },
      { id: "today", label: "Today", items: [session("s_2")] },
    ]);

    expect(Array.from(container.querySelectorAll("[data-row]")).map((node) => node.getAttribute("data-row"))).toEqual([
      "notice",
      "result:art_root",
      "session:s_1",
      "session:s_2",
    ]);
    expect(container.querySelector("[data-row='result:art_root']")?.getAttribute("data-icon")).toBe("true");
    expect(container.querySelector("[data-row='session:s_1']")?.getAttribute("data-icon")).toBe("true");
    // Conversations in ordinary groups stay without icons.
    expect(container.querySelector("[data-row='session:s_2']")?.getAttribute("data-icon")).toBe("false");
  });

  it("counts pinned results toward the virtualization threshold", () => {
    const sessions = Array.from({ length: 50 }, (_, index) => session(`s_${index}`));
    const { container } = renderGroups([
      {
        id: "pinned",
        label: "Pinned",
        items: [],
        entries: [{ kind: "result", pin }],
      },
      { id: "today", label: "Today", items: sessions },
    ]);
    const list = container.querySelector("[data-virtualized='true']");
    expect(list?.getAttribute("data-session-count")).toBe("51");
  });
});
