/**
 * Report history "Turn N" numbering. Without session ordinals (shared reports, report library) prior reports
 * are numbered among the turns that wrote the report; with the chat page's session ordinals (P2 batch 3)
 * they use the session turn number, the same one the topbar caption shows.
 */
import "@testing-library/jest-dom/vitest";
import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { SessionTurnOrdinalsContext } from "@/lib/session-turn-ordinals";
import type { ReportSection } from "@/lib/types";

import { ReportHistory } from "./report-history";

const section = (id: string, turnId: string, text: string, superseded = true) =>
  ({ id, kind: "h1", text, turn_id: turnId, superseded }) as unknown as ReportSection;

// Turns 1 and 3 of the session wrote reports that were superseded by the report of turn 5.
const superseded = [section("s1", "t_1", "第一版"), section("s3", "t_3", "第二版")];
const all = [...superseded, section("s5", "t_5", "当前版", false)];

function rowLabels(): string[] {
  return Array.from(document.querySelectorAll(".hrow .turn"), (el) => el.textContent ?? "");
}

describe("ReportHistory turn numbering", () => {
  it("numbers by report-writing turns without session ordinals", () => {
    renderIntl(<ReportHistory superseded={superseded} allSections={all} renderArchived={() => null} />);
    expect(screen.getByTestId("report-history")).toBeInTheDocument();
    expect(rowLabels()).toEqual(["第 1 轮", "第 2 轮"]);
  });

  it("uses the session turn ordinals (same as the topbar) when the chat page provides them", () => {
    const ordinals = new Map([["t_1", 1], ["t_2", 2], ["t_3", 3], ["t_4", 4], ["t_5", 5]]);
    renderIntl(
      <SessionTurnOrdinalsContext.Provider value={ordinals}>
        <ReportHistory superseded={superseded} allSections={all} renderArchived={() => null} />
      </SessionTurnOrdinalsContext.Provider>,
    );
    expect(rowLabels()).toEqual(["第 1 轮", "第 3 轮"]);
  });

  it("keeps one numbering for all rows when a prior report's turn is unknown to the session", () => {
    renderIntl(
      <SessionTurnOrdinalsContext.Provider value={new Map([["t_3", 3]])}>
        <ReportHistory superseded={superseded} allSections={all} renderArchived={() => null} />
      </SessionTurnOrdinalsContext.Provider>,
    );
    expect(rowLabels()).toEqual(["第 1 轮", "第 2 轮"]);
  });
});
