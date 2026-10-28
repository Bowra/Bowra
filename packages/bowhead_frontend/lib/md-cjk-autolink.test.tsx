/**
 * Bare URLs immediately followed by Chinese punctuation must not absorb the punctuation into the link.
 *
 * 116 1.3.49: a web-search answer wrote “（来源: https://…html）”; all three source-link hrefs included
 * `%EF%BC%89` (fullwidth closing parenthesis), causing 404s on click. Assert the href users actually click.
 */
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Markdown } from "./md";

function hrefs(markdown: string): string[] {
  const { container } = render(<Markdown markdown={markdown} />);
  return [...container.querySelectorAll("a")].map((a) => a.getAttribute("href") ?? "");
}

describe("裸链接在中文标点处截断", () => {
  it("★全角右括号不进 href，且仍显示在正文里", () => {
    const md = "武汉发放消费券。（来源: https://www.ctdsb.net/c1666_202609/2863753.html）";
    const { container } = render(<Markdown markdown={md} />);
    const a = container.querySelector("a");
    expect(a?.getAttribute("href")).toBe("https://www.ctdsb.net/c1666_202609/2863753.html");
    expect(a?.textContent).toBe("https://www.ctdsb.net/c1666_202609/2863753.html");
    expect(container.textContent).toContain("2863753.html）");
  });

  it("带查询串的地址后紧跟逗号、句号与汉字", () => {
    expect(hrefs("见 https://mp.weixin.qq.com/s?__biz=MzA5&mid=26&idx=1，后续。")).toEqual([
      "https://mp.weixin.qq.com/s?__biz=MzA5&mid=26&idx=1",
    ]);
    expect(hrefs("详见https://www.chinanews.com.cn/cj/2026/09-23/10702052.shtml。")).toEqual([
      "https://www.chinanews.com.cn/cj/2026/09-23/10702052.shtml",
    ]);
  });

  it("地址里的汉字本身不截", () => {
    expect(hrefs("参考 https://zh.wikipedia.org/wiki/中文 这一页")).toEqual([
      encodeURI("https://zh.wikipedia.org/wiki/中文"),
    ]);
  });

  it("作者写的 [文字](地址) 不动", () => {
    expect(hrefs("[新闻（全文）](https://example.com/a)")).toEqual(["https://example.com/a"]);
  });
});
