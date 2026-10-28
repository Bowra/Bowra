/**
 * Badge / StatusDot contract (component unification P1, appearance batch two, docs/design/组件统一-技术设计.md).
 * styles/ui-refresh.css supplies the visuals; lock the class contract here: tone × size, caller classes retained at the end
 * (the base layer still styles the legacy classes), and status dots hidden from screen readers.
 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { Badge, StatusDot } from "./badge";
import { Radio, RadioGroup } from "./radio-group";

afterEach(() => {
  cleanup();
});

describe("Badge", () => {
  it("defaults to a neutral md badge", () => {
    render(<Badge>管理员</Badge>);
    expect(screen.getByText("管理员").className).toBe("ui-badge ui-badge--neutral ui-badge--md");
  });

  it("maps tone and size to classes and keeps the legacy class last", () => {
    render(
      <Badge tone="accent" size="sm" className="team-role-badge team-role-badge--creator" title="owner">
        负责人
      </Badge>,
    );
    const badge = screen.getByText("负责人");
    expect(badge.tagName).toBe("SPAN");
    expect(badge.className).toBe("ui-badge ui-badge--accent ui-badge--sm team-role-badge team-role-badge--creator");
    expect(badge.getAttribute("title")).toBe("owner");
  });

  it.each(["neutral", "accent", "positive", "warning", "danger", "outline"] as const)("tone %s", (tone) => {
    render(<Badge tone={tone}>x</Badge>);
    expect(screen.getByText("x").classList.contains(`ui-badge--${tone}`)).toBe(true);
  });
});

describe("StatusDot", () => {
  it("is decorative and carries its tone", () => {
    const { container } = render(<StatusDot tone="positive" />);
    const dot = container.querySelector("span")!;
    expect(dot.getAttribute("aria-hidden")).toBe("true");
    expect(dot.className).toBe("ui-status-dot ui-status-dot--positive");
  });
});

describe("RadioGroup", () => {
  it("defaults to the card appearance (ui-radio-cards) with the caller class after it", () => {
    render(
      <RadioGroup value="a" onValueChange={() => {}} className="cards" aria-label="Scope">
        <Radio value="a">A</Radio>
      </RadioGroup>,
    );
    expect(screen.getByRole("radiogroup", { name: "Scope" }).className).toBe("ui-radio-cards cards");
    expect(screen.getByRole("radio", { name: "A" }).getAttribute("aria-checked")).toBe("true");
  });
});
