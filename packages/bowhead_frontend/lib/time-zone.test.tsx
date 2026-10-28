import { render, screen } from "@testing-library/react";
import { useFormatter, useTimeZone } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ClientLocaleProvider } from "@/components/client-locale-provider";
import { fetchSessions } from "@/lib/api";
import {
  detectViewerTimeZone,
  FALLBACK_TIME_ZONE,
  isValidTimeZone,
  resetViewerTimeZoneForTests,
  viewerTimeZone,
} from "@/lib/time-zone";

function mockBrowserZone(zone: string | undefined) {
  const original = Intl.DateTimeFormat.prototype.resolvedOptions;
  return vi.spyOn(Intl.DateTimeFormat.prototype, "resolvedOptions").mockImplementation(function (this: Intl.DateTimeFormat) {
    return { ...original.call(this), timeZone: zone as string };
  });
}

beforeEach(() => {
  // Pin the UI language so the formatted strings are stable on any host.
  document.cookie = "NEXT_LOCALE=en-US; path=/";
});

afterEach(() => {
  vi.restoreAllMocks();
  resetViewerTimeZoneForTests();
  document.cookie = "NEXT_LOCALE=; path=/; max-age=0";
});

describe("viewer time zone", () => {
  it("accepts IANA zones and rejects unknown or empty ones", () => {
    expect(isValidTimeZone("America/Los_Angeles")).toBe(true);
    expect(isValidTimeZone("Europe/Berlin")).toBe(true);
    expect(isValidTimeZone("Mars/Olympus_Mons")).toBe(false);
    expect(isValidTimeZone("")).toBe(false);
    expect(isValidTimeZone(undefined)).toBe(false);
  });

  it("uses the browser zone and falls back to UTC when it is missing or invalid", () => {
    expect(detectViewerTimeZone(() => "America/Los_Angeles")).toBe("America/Los_Angeles");
    expect(detectViewerTimeZone(() => "Europe/Berlin")).toBe("Europe/Berlin");
    expect(detectViewerTimeZone(() => undefined)).toBe(FALLBACK_TIME_ZONE);
    expect(detectViewerTimeZone(() => "Not/AZone")).toBe(FALLBACK_TIME_ZONE);
    expect(
      detectViewerTimeZone(() => {
        throw new Error("Intl unavailable");
      }),
    ).toBe(FALLBACK_TIME_ZONE);
  });

  it("resolves the zone once from Intl", () => {
    mockBrowserZone("Europe/Berlin");
    expect(viewerTimeZone()).toBe("Europe/Berlin");
    vi.restoreAllMocks();
    mockBrowserZone("America/Los_Angeles");
    expect(viewerTimeZone()).toBe("Europe/Berlin");
    resetViewerTimeZoneForTests();
    expect(viewerTimeZone()).toBe("America/Los_Angeles");
  });
});

function Probe() {
  const zone = useTimeZone();
  const format = useFormatter();
  // 2026-10-08 03:00Z: Oct 7 20:00 in Los Angeles, Oct 8 05:00 in Berlin, Oct 8 11:00 in Shanghai.
  const at = new Date("2026-10-08T03:00:00Z");
  return (
    <p data-testid="probe">
      {zone} | {format.dateTime(at, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23" })}
    </p>
  );
}

describe("ClientLocaleProvider time zone", () => {
  it.each([
    ["America/Los_Angeles", "Oct 7, 20:00"],
    ["Europe/Berlin", "Oct 8, 05:00"],
    ["Asia/Shanghai", "Oct 8, 11:00"],
  ])("formats app times in the viewer's zone (%s), not a fixed Shanghai zone", (zone, expected) => {
    mockBrowserZone(zone);
    render(
      <ClientLocaleProvider>
        <Probe />
      </ClientLocaleProvider>,
    );
    expect(screen.getByTestId("probe").textContent).toBe(`${zone} | ${expected}`);
  });

  it("falls back to UTC when the browser zone is invalid", () => {
    mockBrowserZone("Not/AZone");
    render(
      <ClientLocaleProvider>
        <Probe />
      </ClientLocaleProvider>,
    );
    expect(screen.getByTestId("probe").textContent).toBe("UTC | Oct 8, 03:00");
  });
});

describe("session list request", () => {
  it("sends the viewer's zone so the gateway groups by the viewer's calendar days", async () => {
    mockBrowserZone("America/Los_Angeles");
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ groups: [] }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    try {
      await fetchSessions({ workspaceId: "" });
    } finally {
      vi.unstubAllGlobals();
    }
    const init = (fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1];
    expect(new Headers(init.headers).get("X-Bowhead-Time-Zone")).toBe("America/Los_Angeles");
  });
});
