import { afterEach, expect, it } from "vitest";
import { cleanup, render } from "@testing-library/react";
import { Markdown } from "./md";

afterEach(cleanup);

it("keeps report soft breaks while Chat preserves line boundaries", () => {
  const text = "First\nSecond\n\nThird";
  const report = render(<Markdown markdown={text} />);
  expect(report.container.querySelectorAll("p")).toHaveLength(2);
  expect(report.container.querySelectorAll("br")).toHaveLength(0);
  report.unmount();
  const chat = render(<Markdown markdown={text} preserveLineBreaks />);
  expect(chat.container.querySelectorAll("p")).toHaveLength(2);
  expect(chat.container.querySelectorAll("br")).toHaveLength(1);
});

it("preserves tables, lists, inline code and fenced code when Chat shows line breaks", () => {
  const text = "- One\n- Two\n\n| A | B |\n| - | - |\n| 1 | 2 |\n\nInline `a  b`\nNext\n\n```text\nx =  1\ny =  2\n```";
  const { container } = render(<Markdown markdown={text} preserveLineBreaks />);
  expect(container.querySelectorAll("li")).toHaveLength(2);
  expect(container.querySelectorAll("table")).toHaveLength(1);
  expect(container.querySelectorAll("td")).toHaveLength(2);
  expect(container.querySelector("p code")?.textContent).toBe("a  b");
  expect(container.querySelector("pre code")?.textContent).toBe("x =  1\ny =  2\n");
  expect(container.querySelector("pre br")).toBeNull();
  expect(container.querySelectorAll("p br")).toHaveLength(1);
});
