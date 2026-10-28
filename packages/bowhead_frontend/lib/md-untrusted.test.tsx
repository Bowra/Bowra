/**
 * Two options for untrusted text (source excerpts): `allowImages={false}` omits <img> (no automatic external requests; use alt + link instead),
 * and `linkRel` sets the full rel. Defaults retain existing chat/report behavior (render images, rel="noreferrer").
 */
import "@testing-library/jest-dom/vitest";
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Markdown } from "./md";

const MD = "![像素](https://attacker.example/pixel.png) ![](https://attacker.example/noalt.png) ![js](javascript:alert(1)) [链接](https://example.com/x)";

describe("Markdown 的不可信文字选项", () => {
  it("默认（对话与报告）：图片照旧渲染，链接 rel 照旧", () => {
    const { container } = render(<Markdown markdown={MD} />);
    expect(container.querySelectorAll("img")).toHaveLength(3);
    expect(container.querySelector("a[href='https://example.com/x']")).toHaveAttribute("rel", "noreferrer");
  });

  it("allowImages=false：没有 <img>，图片写成 alt 文字的链接（没有 alt 时写地址），javascript: 地址被去掉", () => {
    const { container } = render(<Markdown markdown={MD} allowImages={false} linkRel="noopener noreferrer" />);
    expect(container.querySelectorAll("img")).toHaveLength(0);
    const images = Array.from(container.querySelectorAll("[data-md-image]"));
    expect(images.map((el) => el.textContent)).toEqual(["像素", "https://attacker.example/noalt.png", "js"]);
    expect(images[0]).toHaveAttribute("href", "https://attacker.example/pixel.png");
    expect(images[0]).toHaveAttribute("rel", "noopener noreferrer");
    expect(images[0]).toHaveAttribute("target", "_blank");
    expect(images[2].getAttribute("href") ?? "").not.toMatch(/javascript:/i);
    expect(container.querySelector("a[href='https://example.com/x']")).toHaveAttribute("rel", "noopener noreferrer");
  });
});
