/** A pending "focus this row" request is dropped instead of firing late (review follow-up to the Show older focus fix). */

import { act, cleanup, render } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { SessionItem } from "@/lib/types";

import {
  RECOVERY_FOCUS_REQUEST_TIMEOUT_MS,
  RecoveryGroups,
  type RecoveryFocusRequest,
  type RecoveryRenderGroup,
} from "./recovery-groups";

const item = (id: string): SessionItem => ({ id, title: id, intents: [], time: "1d", state: "done" });
const group = (...ids: string[]): RecoveryRenderGroup => ({ id: "older", label: "Older", items: ids.map(item) });

function harness(groups: RecoveryRenderGroup[], focusRequest: RecoveryFocusRequest | null, onSettled = vi.fn()) {
  const scrollRef = createRef<HTMLDivElement>();
  const ui = (g: RecoveryRenderGroup[], request: RecoveryFocusRequest | null) => (
    <>
      <button type="button" data-testid="elsewhere">elsewhere</button>
      <div ref={scrollRef}>
        <RecoveryGroups
          groups={g}
          scrollRef={scrollRef}
          focusRequest={request}
          onFocusRequestSettled={onSettled}
          renderItem={(session) => (
            <div key={session.id} data-session-id={session.id}>
              <button type="button" className="unified-work-item-main">{session.title}</button>
            </div>
          )}
        />
      </div>
    </>
  );
  const view = render(ui(groups, focusRequest));
  return { ...view, onSettled, rerenderWith: (g: RecoveryRenderGroup[], request: RecoveryFocusRequest | null) => view.rerender(ui(g, request)) };
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("RecoveryGroups focus request", () => {
  it("focuses the row and reports the request settled", () => {
    // The request arrives on a later render, as from WorkRecovery (the scroll container is attached by then).
    const { onSettled, rerenderWith } = harness([group("s_1", "s_2")], null);
    rerenderWith([group("s_1", "s_2")], { sessionId: "s_2", token: 1 });
    expect(document.activeElement?.textContent).toBe("s_2");
    expect(onSettled).toHaveBeenCalledTimes(1);
  });

  it("drops a request whose row does not appear in time: a later render never steals focus", () => {
    vi.useFakeTimers();
    const request = { sessionId: "s_late", token: 1 };
    const { onSettled, rerenderWith } = harness([group("s_1")], request);
    expect(document.activeElement).toBe(document.body);
    act(() => {
      vi.advanceTimersByTime(RECOVERY_FOCUS_REQUEST_TIMEOUT_MS);
    });
    expect(onSettled).toHaveBeenCalledTimes(1);
    rerenderWith([group("s_1", "s_late")], request);
    expect(document.activeElement).toBe(document.body);
  });

  it("drops the request when the person moves focus elsewhere first", () => {
    const request = { sessionId: "s_late", token: 1 };
    const { getByTestId, onSettled, rerenderWith } = harness([group("s_1")], request);
    act(() => {
      getByTestId("elsewhere").focus();
    });
    expect(onSettled).toHaveBeenCalledTimes(1);
    rerenderWith([group("s_1", "s_late")], request);
    expect(document.activeElement).toBe(getByTestId("elsewhere"));
  });
});
