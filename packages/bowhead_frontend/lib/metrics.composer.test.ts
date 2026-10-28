import { afterEach, describe, expect, it, vi } from "vitest";

import {
  trackComposerEvent,
  trackComposerTransportFailure,
} from "@/lib/metrics";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Composer product telemetry", () => {
  it("uses the authenticated durable endpoint and reuses an explicit retry id", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const eventId = "018f0cc3-e4a2-4b21-a109-001122334455";

    const returned = trackComposerEvent(
      {
        event_name: "composer_planning_toggled",
        enabled: true,
        entry: "toolbar",
        surface: "workbench",
        composer_mode: "comfortable",
      },
      eventId,
    );
    await Promise.resolve();

    expect(returned).toBe(eventId);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/metrics/composer-event");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toMatchObject({
      event_id: eventId,
      event_name: "composer_planning_toggled",
      enabled: true,
      entry: "toolbar",
      surface: "workbench",
      composer_mode: "comfortable",
    });
  });

  it("reports only browser transport errors with bounded context", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    expect(trackComposerTransportFailure(
      new TypeError("Failed to fetch private URL"),
      {
        surface: "home",
        modelControlBatch: "a",
        hasAttachments: true,
      },
    )).toBe(true);
    expect(trackComposerTransportFailure(
      Object.assign(new Error("server"), { status: 503 }),
      {
        surface: "home",
        modelControlBatch: "a",
        hasAttachments: false,
      },
    )).toBe(false);
    await Promise.resolve();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const payload = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(payload).toMatchObject({
      event_name: "composer_send_result",
      result: "transport_error",
      model_control_batch: "a",
      has_attachments: true,
      surface: "home",
    });
    expect(JSON.stringify(payload)).not.toContain("private URL");
  });

  it("reports attachment lifecycle with bounded fields only", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    trackComposerEvent({
      event_name: "composer_attachment_action",
      action: "upload",
      result: "failed",
      kind: "bundle",
      size_band: "large",
      surface: "workbench",
      composer_mode: "compact",
    });
    await Promise.resolve();

    const payload = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(payload).toMatchObject({
      event_name: "composer_attachment_action",
      action: "upload",
      result: "failed",
      kind: "bundle",
      size_band: "large",
    });
    expect(Object.keys(payload)).not.toContain("filename");
    expect(Object.keys(payload)).not.toContain("file_id");
    expect(Object.keys(payload)).not.toContain("mime");
  });
});
