import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render } from "@testing-library/react";

afterEach(() => {
  cleanup();
  vi.doUnmock("@/lib/product-identity");
  vi.resetModules();
  window.__BOWHEAD_BASE_PATH__ = undefined;
});

it("prefixes the Business Datarc logo with the current browser entry path", async () => {
  vi.doMock("@/lib/product-identity", () => ({ BUILD_EDITION: "business" }));
  window.__BOWHEAD_BASE_PATH__ = "/bowhead";
  const { DatarcMark } = await import("./data-source-icons");

  const { container } = render(<DatarcMark />);

  expect(container.querySelector("img")?.getAttribute("src")).toBe(
    "/bowhead/data-sources/datarc.png",
  );
});

it("uses the licensed database icon for Community Datarc connections", async () => {
  vi.doMock("@/lib/product-identity", () => ({ BUILD_EDITION: "community" }));
  const { DatarcMark } = await import("./data-source-icons");
  const { container } = render(<DatarcMark size={25} />);
  const icon = container.querySelector('svg[data-source-icon="datarc"]');
  expect(icon?.getAttribute("width")).toBe("25");
  expect(icon?.getAttribute("height")).toBe("25");
  expect(icon?.getAttribute("aria-hidden")).toBe("true");
  expect(container.querySelector("img")).toBeNull();
});

it("draws the Tavily mark inline in the theme ink colour so it stays legible in dark mode", async () => {
  const { TavilyMark } = await import("./data-source-icons");

  const { container } = render(<TavilyMark />);

  const svg = container.querySelector("svg.bh-tavily-mark") as SVGSVGElement;
  expect(container.querySelector("img")).toBeNull();
  expect(svg.getAttribute("aria-hidden")).toBe("true");
  expect(svg.style.color).toBe("var(--c-ink)");
  expect(svg.querySelector("path")?.getAttribute("fill")).toBe("currentColor");
});
