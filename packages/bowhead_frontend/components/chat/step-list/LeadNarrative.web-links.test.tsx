import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import messages from "@/messages/zh-CN.json";
import { renderIntl } from "@/__tests__/intl-render";
import { CitationsProvider, LeadNarrative } from "./LeadNarrative";

describe("Chat web references", () => {
  it("keeps completed streaming and replayed labels identical alongside citations", () => {
    const text = "[Forecast](https://example.com/weather) https://example.com/weather?day=30 [cite:1]";
    const content = (isStreaming: boolean) => (
      <NextIntlClientProvider locale="zh-CN" messages={messages}>
        <CitationsProvider citations={[{ seq: 1, artifactId: "art_weather", label: "Weather data", kind: "data_ref" }]}>
          <LeadNarrative text={text} isStreaming={isStreaming} />
        </CitationsProvider>
      </NextIntlClientProvider>
    );
    const view = render(content(true));
    const snapshot = () =>
      [...view.container.querySelectorAll("a")].map((link) => ({
        text: link.textContent,
        href: link.getAttribute("href"),
        title: link.getAttribute("title"),
        label: link.getAttribute("aria-label"),
      }));
    const live = snapshot();
    expect(live.map((link) => link.text)).toEqual(["Forecast", "example.com"]);
    expect(view.container.querySelector(".bh-artifact-chip")).toBeTruthy();
    view.rerender(content(false));
    expect(snapshot()).toEqual(live);
    view.unmount();
    const replay = render(content(false));
    expect(
      [...replay.container.querySelectorAll("a")].map((link) => ({
        text: link.textContent,
        href: link.getAttribute("href"),
        title: link.getAttribute("title"),
        label: link.getAttribute("aria-label"),
      })),
    ).toEqual(live);
    expect(replay.container.querySelector(".bh-artifact-chip")).toBeTruthy();
  });

  it("accepts an incomplete streaming link before its destination closes", () => {
    const view = renderIntl(<LeadNarrative text="Source: [https://example.com/weather](https://example.com/" isStreaming />);
    expect(view.container.textContent).toContain("https://example.com/weather");
    view.rerender(<LeadNarrative text="Source: [https://example.com/weather](https://example.com/weather)" isStreaming />);
    expect(view.container.querySelector("a")?.textContent).toBe("example.com");
  });
});
