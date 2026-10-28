import { readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";

import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DEVELOPMENT_ROUTE_PREFIXES, appRoutePolicy } from "@/lib/app-route-policy";
import { developmentRoutesEnabled } from "@/lib/development-route-gate";

import DevelopmentRouteLayout from "./development-route-layout";

const NOT_FOUND = "NEXT_NOT_FOUND";

vi.mock("next/navigation", () => ({
  notFound: () => {
    throw new Error(NOT_FOUND);
  },
}));

const APP_GROUP_DIR = path.resolve(__dirname, "../app/(app)");
const PRODUCTION_DOCKERFILE = path.resolve(__dirname, "../../../deploy/Dockerfile.app");
/**
 * Imported through a template literal: the production image deletes this
 * segment before `next build` type-checks the project, so a literal import
 * would no longer resolve there.
 */
const SPIKE_SEGMENT = "spike";

/** Top-level `(app)` segments that the route policy lets bypass authentication. */
function developmentSegments(): string[] {
  return readdirSync(APP_GROUP_DIR)
    .filter((name) => statSync(path.join(APP_GROUP_DIR, name)).isDirectory())
    .filter((name) => appRoutePolicy(`/${name}/`).auth === "development-bypass");
}

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("development route gate", () => {
  it("is closed in a production build without the e2e flag", () => {
    expect(developmentRoutesEnabled({ NODE_ENV: "production" })).toBe(false);
    expect(
      developmentRoutesEnabled({ NODE_ENV: "production", NEXT_PUBLIC_BOWHEAD_E2E: "0" }),
    ).toBe(false);
  });

  it("is open in development and in the e2e production build", () => {
    expect(developmentRoutesEnabled({ NODE_ENV: "development" })).toBe(true);
    expect(developmentRoutesEnabled({ NODE_ENV: "test" })).toBe(true);
    expect(
      developmentRoutesEnabled({ NODE_ENV: "production", NEXT_PUBLIC_BOWHEAD_E2E: "1" }),
    ).toBe(true);
  });

  it("covers every authentication-bypassing segment with the shared layout", async () => {
    const segments = new Set([
      ...developmentSegments(),
      ...DEVELOPMENT_ROUTE_PREFIXES.map((prefix) => prefix.slice(1)),
    ]);
    expect(segments.size).toBeGreaterThan(0);
    for (const segment of segments) {
      const layout = (await import(`../app/(app)/${segment}/layout.tsx`)) as {
        default: unknown;
      };
      expect(layout.default, segment).toBe(DevelopmentRouteLayout);
    }
  });

  it("renders a spike route as 404 in production", async () => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("NEXT_PUBLIC_BOWHEAD_E2E", "");
    const { default: SpikeLayout } = (await import(`../app/(app)/${SPIKE_SEGMENT}/layout.tsx`)) as {
      default: typeof DevelopmentRouteLayout;
    };

    expect(() =>
      render(
        <SpikeLayout>
          <main>spike fixture</main>
        </SpikeLayout>,
      ),
    ).toThrow(NOT_FOUND);
    expect(screen.queryByText("spike fixture")).toBeNull();
  });

  it("renders a spike route in the e2e production build", async () => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("NEXT_PUBLIC_BOWHEAD_E2E", "1");
    const { default: SpikeLayout } = (await import(`../app/(app)/${SPIKE_SEGMENT}/layout.tsx`)) as {
      default: typeof DevelopmentRouteLayout;
    };

    render(
      <SpikeLayout>
        <main>spike fixture</main>
      </SpikeLayout>,
    );
    expect(screen.getByText("spike fixture")).toBeTruthy();
  });

  it("is excluded from the production image build", () => {
    const dockerfile = readFileSync(PRODUCTION_DOCKERFILE, "utf8");
    const firstBuild = dockerfile.indexOf("npm run build");
    expect(firstBuild).toBeGreaterThan(0);
    for (const prefix of DEVELOPMENT_ROUTE_PREFIXES) {
      const removal = dockerfile.indexOf(`RUN rm -rf "app/(app)${prefix}"`);
      expect(removal, prefix).toBeGreaterThan(0);
      expect(removal, prefix).toBeLessThan(firstBuild);
    }
  });
});
