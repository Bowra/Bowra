/**
 * rehypeCardCitations: decoration path from [N] to clickable citation chips (citation-audit M3).
 *
 * This path previously had **zero test coverage**, while M3 establishes [N] as the sole trust signal:
 * correct decoration determines whether users can trust the indication that a number has a source.
 */

import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { Markdown } from "@/lib/md";

const md = (markdown: string) =>
  render(<Markdown markdown={markdown} enableCardCitations />).container;

describe("rehypeCardCitations", () => {
  it("正文里的 [N] 渲成可点 chip", () => {
    const c = md("营收 100亿 [1]。");
    const chips = c.querySelectorAll(".rc-cardcite");
    expect(chips).toHaveLength(1);
    expect(chips[0].textContent).toBe("1");
    expect(chips[0].getAttribute("data-source-idx")).toBe("0");
  });

  it("代码块里的 a[1] 是数组下标，不装饰（impl review 安全面 P1-1 顺藤）", () => {
    // Without filtering, observed rendering is x = a<sup class="rc-cardcite">1</sup>: array index becomes a clickable citation.
    // Backend strip_untrusted_numbered deliberately preserves a[1] inside fences (A-P2-3);
    // decorating it in frontend would break parity.
    const c = md("正文 100亿 [1]。\n\n```python\nx = a[1]\ny = b[2]\n```");
    const code = c.querySelector("code");
    expect(code?.querySelectorAll(".rc-cardcite")).toHaveLength(0);
    expect(code?.textContent).toContain("a[1]");
    expect(code?.textContent).toContain("b[2]");
    // Body citations must still be decorated (avoid blanket filtering)
    expect(c.querySelectorAll(".rc-cardcite")).toHaveLength(1);
  });

  it("行内代码 `arr[1]` 同样不装饰", () => {
    const c = md("见 `arr[1]` 的写法。");
    expect(c.querySelectorAll(".rc-cardcite")).toHaveLength(0);
    expect(c.querySelector("code")?.textContent).toBe("arr[1]");
  });
});
