/**
 * Steering bubble byline (2026-11-08 dogfood F10): every bubble read "You · · interjected" because the
 * acknowledgement carried no time, and a present time was formatted with a fixed zh-CN locale.
 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";

import { SteerMessage, formatSteerTime } from "./SteerMessage";

afterEach(() => cleanup());

/** 14:05 local time, so the expectations below do not depend on the machine's time zone. */
const AT_14_05 = new Date(2026, 10, 8, 14, 5).toISOString();
/** The same calendar day, so the byline shows the time alone. */
const SAME_DAY = new Date(2026, 10, 8, 20, 0);

function byline(container: HTMLElement): string {
  return Array.from(container.querySelectorAll("div"))
    .find((node) => node.textContent?.endsWith("interjected") && node.children.length === 0)
    ?.textContent ?? "";
}

describe("SteerMessage byline", () => {
  it("leaves the time out when the acknowledgement has none: one separator, no blank segment", () => {
    // Mutation: join who · time · suffix unconditionally → "You ·  · interjected".
    const { container } = renderIntl(<SteerMessage who="You" time="" text="Only East China" />, { locale: "en-US" });
    expect(byline(container)).toBe("You · interjected");
  });

  it("shows the time between the speaker and the action when present", () => {
    const { container } = renderIntl(
      <SteerMessage who="You" time={formatSteerTime(AT_14_05, "en-US", SAME_DAY)} text="Only East China" />,
      { locale: "en-US" },
    );
    expect(byline(container)).toMatch(/^You · 2:05\sPM · interjected$/u);
  });
});

describe("formatSteerTime", () => {
  it.each([
    ["en-US", /^2:05\sPM$/u],
    ["zh-CN", /^14:05$/u],
    ["de-DE", /^14:05$/u],
    ["fr-FR", /^14:05$/u],
  ])("formats hours and minutes in the %s interface locale, like the other chat bylines", (locale, expected) => {
    expect(formatSteerTime(AT_14_05, locale, SAME_DAY)).toMatch(expected);
  });

  it("adds the date on a later day, as the user and reply bylines do", () => {
    expect(formatSteerTime(AT_14_05, "zh-CN", new Date(2026, 10, 9, 9, 0))).toBe("11月8日 14:05");
  });

  it.each([undefined, null, "", "not a time"])("returns an empty segment for %s", (ts) => {
    expect(formatSteerTime(ts, "en-US")).toBe("");
  });
});
