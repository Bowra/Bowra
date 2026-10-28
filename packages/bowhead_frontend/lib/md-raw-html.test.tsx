import { afterEach, expect, it } from "vitest";
import { cleanup, render } from "@testing-library/react";
import { Markdown, MarkdownInline } from "./md";

afterEach(cleanup);

it("turns <br> inside a table cell into a line break instead of literal text", () => {
  const text = "| Step | Detail |\n| - | - |\n| 1 | First line<br>second line<br/>third |";
  const { container } = render(<Markdown markdown={text} preserveLineBreaks />);
  const cell = container.querySelectorAll("td")[1];
  expect(cell.querySelectorAll("br")).toHaveLength(2);
  expect(cell.textContent).toBe("First linesecond linethird");
  expect(container.textContent).not.toContain("<br");
});

it("renders a raw heading line as a heading and keeps the following Markdown", () => {
  const { container } = render(<Markdown markdown={"<h1>Agent review</h1>\n\nThe **answer** follows."} />);
  expect(container.querySelector("h1")?.textContent).toBe("Agent review");
  expect(container.querySelector("p strong")?.textContent).toBe("answer");
  expect(container.textContent).not.toContain("<h1>");
});

it("never renders arbitrary HTML: attributes, scripts and unknown tags are dropped", () => {
  const text = 'Click <a href="javascript:alert(1)" onclick="x()">here</a> <img src=x onerror=alert(1)> <b style="color:red">bold</b><script>alert(2)</script> done &amp; dusted';
  const { container } = render(<Markdown markdown={text} />);
  expect(container.querySelector("a, img, script")).toBeNull();
  const bold = container.querySelector("b");
  expect(bold?.textContent).toBe("bold");
  expect(bold?.attributes).toHaveLength(0);
  expect(container.textContent).toBe("Click here  bold done & dusted");
});

it("keeps prose that only looks like a tag literal", () => {
  const { container } = render(<Markdown markdown={"Returns a List<String> or <T> value."} />);
  expect(container.textContent).toBe("Returns a List<String> or <T> value.");
});

it("keeps a heading tag inside a table cell inline, without nesting blocks", () => {
  const text = "| A |\n| - |\n| <h2>Title</h2> text |";
  const { container } = render(<Markdown markdown={text} />);
  expect(container.querySelector("td h2")).toBeNull();
  expect(container.querySelector("td strong")?.textContent).toBe("Title");
});

it("applies the same rules to inline Markdown", () => {
  const { container } = render(<MarkdownInline markdown={"one<br>two <em>three</em>"} />);
  expect(container.querySelectorAll("br")).toHaveLength(1);
  expect(container.querySelector("em")?.textContent).toBe("three");
  expect(container.textContent).toBe("onetwo three");
});
