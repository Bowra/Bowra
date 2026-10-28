import { readFileSync } from "node:fs";
import path from "node:path";

import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({
  value: {
    bundle: {
      team: { configurationType: "bowhead" },
    },
    brand: {
      applicationName: "Bowhead",
      logoAlt: "Bowhead",
      logoOnLight: { kind: "text", text: "Bowhead" },
      logoOnDark: { kind: "text", text: "Bowhead" },
      mark: { kind: "generatedInitial", text: "B" },
      favicon: { kind: "generatedInitial", text: "B" },
    },
  },
}));

vi.mock("@/components/brand-provider", () => ({
  useBrand: () => state.value,
}));

import { BrandGraphic, BrandIdentity } from "./brand-identity";

afterEach(() => {
  cleanup();
  state.value.bundle.team.configurationType = "bowhead";
  state.value.brand.applicationName = "Bowhead";
  state.value.brand.logoOnLight = { kind: "text", text: "Bowhead" };
  state.value.brand.mark = { kind: "generatedInitial", text: "B" };
});

describe("BrandGraphic", () => {
  it("renders the Bowhead whale for the canonical compact mark", () => {
    const { container } = render(<BrandGraphic variant="mark" size={28} />);

    expect(container.querySelector("svg")).not.toBeNull();
    expect(container.querySelector(".brand-graphic__initial")).toBeNull();
  });

  it("keeps the Bowhead whale when a team customizes styles but not its identity", () => {
    state.value.bundle.team.configurationType = "custom";

    const { container } = render(<BrandGraphic variant="mark" size={28} />);

    expect(container.querySelector("svg")).not.toBeNull();
    expect(container.querySelector(".brand-graphic__initial")).toBeNull();
  });

  it("keeps generated initials for a genuinely custom brand", () => {
    state.value.bundle.team.configurationType = "custom";
    state.value.brand.applicationName = "Arctic Data";
    state.value.brand.mark = { kind: "generatedInitial", text: "A" };

    const { container } = render(<BrandGraphic variant="mark" size={28} />);

    expect(container.querySelector("svg")).toBeNull();
    expect(container.querySelector(".brand-graphic__initial")?.textContent).toBe(
      "A",
    );
  });

  it("keeps the dark fallback selector more specific than the initial display rule", () => {
    const css = readFileSync(
      path.resolve(__dirname, "../styles/brand-identity.css"),
      "utf8",
    );

    expect(css).toMatch(
      /\.brand-graphic--logo\s*>\s*\.brand-graphic__dark\s*\{\s*display:\s*none;/,
    );
  });
});

describe("BrandIdentity", () => {
  it("renders the effective application name beside the shared graphic", () => {
    state.value.bundle.team.configurationType = "custom";
    state.value.brand.applicationName = "Northstar";
    state.value.brand.logoOnLight = { kind: "generatedInitial", text: "N" };

    const { container } = render(<BrandIdentity size={28} />);

    expect(container.textContent).toContain("Northstar");
    expect(container.querySelector(".brand-graphic__initial")?.textContent).toBe(
      "N",
    );
  });

  it("keeps a branded accessible name when only the mark is shown", () => {
    state.value.brand.applicationName = "Northstar";

    const { container } = render(
      <BrandIdentity variant="mark" showName={false} />,
    );

    expect(container.querySelector(".brand-identity")?.getAttribute("aria-label"))
      .toBe("Northstar");
  });
});
