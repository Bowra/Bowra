/**
 * Button class contract: the base layer styles settings-button--{variant,size}; styles/ui-refresh.css also
 * recognizes --lg and aria-busy. lg keeps --md so the base-layer padding still applies.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Button } from "./button";

describe("Button", () => {
  it("默认是次按钮、md 尺寸、type=button", () => {
    render(<Button>取消</Button>);
    const button = screen.getByRole("button", { name: "取消" });
    expect(button.className).toBe("settings-button settings-button--secondary settings-button--md");
    expect(button.getAttribute("type")).toBe("button");
    expect(button.hasAttribute("aria-busy")).toBe(false);
  });

  it("lg carries both --md (base layer) and --lg", () => {
    render(<Button size="lg" variant="primary">保存</Button>);
    const classes = screen.getByRole("button", { name: "保存" }).classList;
    expect(classes.contains("settings-button--md")).toBe(true);
    expect(classes.contains("settings-button--lg")).toBe(true);
    expect(classes.contains("settings-button--primary")).toBe(true);
  });

  it("loading 写 aria-busy，按钮仍可点、可聚焦", () => {
    render(<Button loading>保存中</Button>);
    const button = screen.getByRole("button", { name: "保存中" });
    expect(button.getAttribute("aria-busy")).toBe("true");
    expect((button as HTMLButtonElement).disabled).toBe(false);
  });
});
