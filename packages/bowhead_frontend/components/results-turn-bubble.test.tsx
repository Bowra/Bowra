import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

import { RESULTS_BUBBLE_MS, ResultsTurnBubble, resultsTurnItems } from "./results-turn-bubble";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const echo = (key: string, values?: Record<string, number | string>) => `${key}:${JSON.stringify(values ?? {})}`;

describe("what the reminder says (chat running state R2.6)", () => {
  it("lists delivered files, the report sections changed and the work files", () => {
    const payload = {
      outputs: [{ kind: "report", count: 1 }, { kind: "chart", count: 2 }, { kind: "presentation", count: 1 }],
      report_changes: {
        added: [{ section_id: "h", kind: "h1" }, { section_id: "a", kind: "narrative" }, { section_id: "c1", kind: "chart" }],
        updated: [{ section_id: "c", kind: "narrative" }],
        added_kinds: { h1: 1, narrative: 3, chart: 2 },
        updated_kinds: { narrative: 1 },
      },
      work_files: { count: 2 },
    };
    expect(resultsTurnItems(payload, echo, echo)).toEqual([
      'summary_output.presentation:{"count":1}',
      'turn_bubble_report:{"count":4}',
      'turn_bubble_report_figures:{"count":2}',
      'turn_bubble_work_files:{"count":2}',
    ]);
  });

  it("names a kept report without section facts as a report, and says nothing for a Turn with no results", () => {
    expect(resultsTurnItems({ outputs: [{ kind: "report", count: 1 }] }, echo, echo)).toEqual(['summary_output.report:{"count":1}']);
    expect(resultsTurnItems({}, echo, echo)).toEqual([]);
    expect(resultsTurnItems(undefined, echo, echo)).toEqual([]);
  });
});

describe("ResultsTurnBubble", () => {
  const payload = { outputs: [{ kind: "presentation", count: 1 }], work_files: { count: 2 } };

  it("fades after a few seconds", () => {
    vi.useFakeTimers();
    const onDismiss = vi.fn();
    const { getByTestId } = renderIntl(<ResultsTurnBubble finalPayload={payload} onView={() => {}} onDismiss={onDismiss} />);
    expect(getByTestId("results-turn-bubble")).toHaveTextContent("1");
    act(() => { vi.advanceTimersByTime(RESULTS_BUBBLE_MS); });
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("goes away on any other interaction, and opens Results from its own button", () => {
    const onDismiss = vi.fn();
    const onView = vi.fn();
    const { getByRole } = renderIntl(<ResultsTurnBubble finalPayload={payload} onView={onView} onDismiss={onDismiss} />);
    fireEvent.pointerDown(getByRole("button"));
    expect(onDismiss).not.toHaveBeenCalled();
    fireEvent.click(getByRole("button"));
    expect(onView).toHaveBeenCalledTimes(1);
    fireEvent.pointerDown(document.body);
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("dismisses itself when the Turn added nothing to tell", () => {
    const onDismiss = vi.fn();
    const { queryByTestId } = renderIntl(<ResultsTurnBubble finalPayload={{}} onView={() => {}} onDismiss={onDismiss} />);
    expect(queryByTestId("results-turn-bubble")).toBeNull();
    expect(onDismiss).toHaveBeenCalled();
  });
});
