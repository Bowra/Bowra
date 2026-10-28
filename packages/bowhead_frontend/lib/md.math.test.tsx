import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { Markdown } from "./md";

afterEach(cleanup);

describe("chat Markdown formulas", () => {
  it("renders display math while leaving amounts and fenced source as text", () => {
    const { container } = render(
      <Markdown
        markdown={
          "营业收入 $100，成本 $80。\n\n$$\\text{净利率}=\\frac{10}{100}=10\\%$$\n\n```text\n$$raw$$\n```"
        }
      />,
    );

    expect(container.querySelector(".katex")).not.toBeNull();
    expect(container.querySelector(".katex-mathml math")).not.toBeNull();
    expect(container.querySelector("p")?.textContent).toContain("$100，成本 $80");
    expect(container.querySelector("pre code")?.textContent).toContain("$$raw$$");
  });

  it("does not turn an array subscript inside math into a citation", () => {
    const { container } = render(<Markdown markdown={"$$\\text{a[1]}$$"} enableCardCitations />);
    expect(container.querySelector(".katex")).not.toBeNull();
    expect(container.querySelector("sup.rc-cardcite")).toBeNull();
  });
});
