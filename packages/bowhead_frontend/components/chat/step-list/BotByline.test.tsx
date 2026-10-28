/**
 * Phase 1.7-B — BotByline component test.
 *
 * Pins the rendering contract:
 *   · The brand appears once (in the sidebar), so the byline carries no logo or product name
 *   · Optional time stamp
 *   · Stable testid so DOM order assertions (BotByline above StepRow) work
 */

import { afterEach, describe, expect, it } from "vitest";
import { cleanup, screen } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { BotByline } from "./BotByline";

afterEach(() => cleanup());

describe("BotByline", () => {
  it("renders the time stamp when provided", () => {
    renderIntl(<BotByline time="14:32" />);
    // The "· 14:32" span concatenates time with the dot prefix.
    expect(screen.getByText(/14:32/)).toBeTruthy();
  });

  it("does NOT render the time when omitted", () => {
    const { container } = renderIntl(<BotByline />);
    const byline = container.querySelector(".bh-bot-byline");
    expect(byline?.textContent).not.toContain("·");
  });

  it("exposes data-testid='bot-byline' for DOM-order assertions", () => {
    renderIntl(<BotByline time="14:32" />);
    expect(screen.getByTestId("bot-byline")).toBeTruthy();
  });

  it("keeps only the turn time, without the logo or product name", () => {
    const { container } = renderIntl(<BotByline time="14:32" />);
    const byline = container.querySelector("[data-testid='bot-byline']");
    expect(byline?.textContent).toBe("14:32");
    expect(byline?.querySelector("svg")).toBeNull();
  });
});
