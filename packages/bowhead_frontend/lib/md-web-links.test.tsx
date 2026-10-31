import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Markdown } from "./md";

describe("compact Chat web links", () => {
  it.each([
    ["bare URL", "https://example.com/articles/weather?day=30&city=tokyo#forecast", "example.com"],
    ["angle autolink", "<https://www.example.com/articles/weather>", "www.example.com"],
    ["URL label", "[https://example.com/articles/weather](https://example.com/articles/weather)", "example.com"],
    ["reported surplus parenthesis", "Weather ([https://example.com/weather/)](https://example.com/weather/))", "example.com"],
    ["multiple surplus parentheses", "[https://example.com/weather/))](https://example.com/weather/)", "example.com"],
    ["local HTTP with port", "http://127.0.0.1:8080/weather?day=30", "127.0.0.1:8080"],
    ["encoded Unicode", "<https://example.com/天气?city=东京>", "example.com"],
    ["normalized host", "[HTTPS://EXAMPLE.COM:443/weather](https://example.com/weather)", "example.com"],
    ["balanced parentheses", "<https://example.com/wiki/Weather_(Tokyo)>", "example.com"],
    ["literal closing parenthesis", "<https://example.com/weather)>", "example.com"],
  ])("compacts %s without changing its destination", (_name, markdown, host) => {
    const original = render(<Markdown markdown={markdown} />);
    const href = original.container.querySelector("a")!.getAttribute("href");
    original.unmount();
    render(<Markdown markdown={markdown} compactWebLinks linkRel="noopener noreferrer" />);
    const link = screen.getByRole("link", { name: href! });
    expect(link.textContent).toBe(host);
    expect(link).toHaveAttribute("href", href);
    expect(link).toHaveAttribute("title", href);
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it.each([
    ["named title", "[Weather forecast](https://example.com/weather)", "Weather forecast"],
    ["formatted title", "[**https://example.com/weather**](https://example.com/weather)", "https://example.com/weather"],
    ["different host", "[https://other.example/weather](https://example.com/weather)", "https://other.example/weather"],
    ["different path", "[https://example.com/a](https://example.com/b)", "https://example.com/a"],
    ["different query", "[https://example.com/?day=29](https://example.com/?day=30)", "https://example.com/?day=29"],
    ["different fragment", "[https://example.com/#a](https://example.com/#b)", "https://example.com/#a"],
    ["relative URL", "[guide](/guide/)", "guide"],
    ["other scheme", "[email](mailto:reader@example.com)", "email"],
    ["filtered URL", "[https://example.com/](javascript:alert%281%29)", "https://example.com/"],
  ])("preserves %s", (_name, markdown, label) => {
    const { container } = render(<Markdown markdown={markdown} compactWebLinks />);
    const anchor = container.querySelector("a")!;
    expect(anchor.textContent).toBe(label);
    expect(anchor).not.toHaveAttribute("data-compact-web-link");
    if (markdown.includes("javascript:")) expect(anchor.getAttribute("href") ?? "").toBe("");
  });

  it("preserves code and image labels", () => {
    const href = "https://example.com/weather";
    const { container } = render(
      <Markdown markdown={`\`${href}\`\n\n\`\`\`text\n${href}\n\`\`\`\n\n[![Forecast](/forecast.png)](${href})`} compactWebLinks />,
    );
    expect([...container.querySelectorAll("code")].map((node) => node.textContent?.trim())).toEqual([href, href]);
    expect(container.querySelector("a img")).toHaveAttribute("alt", "Forecast");
    expect(container.querySelector("[data-compact-web-link]")).toBeNull();
  });

  it("keeps distinct destinations and Chinese punctuation", () => {
    const { container } = render(
      <Markdown markdown="Sources: https://example.com/a?day=30#one， https://example.com/b?day=31#two。" compactWebLinks />,
    );
    const links = [...container.querySelectorAll("a")];
    expect(links.map((link) => link.textContent)).toEqual(["example.com", "example.com"]);
    expect(links.map((link) => link.getAttribute("href"))).toEqual(["https://example.com/a?day=30#one", "https://example.com/b?day=31#two"]);
    expect(container.textContent).toBe("Sources: example.com， example.com。");
  });

  it("leaves shared non-Chat rendering unchanged by default", () => {
    const href = "https://example.com/weather?day=30";
    render(<Markdown markdown={href} />);
    const link = screen.getByRole("link", { name: href });
    expect(link.textContent).toBe(href);
    expect(link).not.toHaveAttribute("data-compact-web-link");
  });

  it("retains the focused anchor across ordinary parent rerenders", () => {
    const markdown = "https://example.com/weather?day=30";
    const view = render(<Markdown markdown={markdown} compactWebLinks />);
    const link = screen.getByRole("link");
    link.focus();
    view.rerender(<Markdown markdown={`${markdown} updated`} compactWebLinks />);
    expect(screen.getByRole("link")).toBe(link);
    expect(link).toHaveFocus();
  });
});
