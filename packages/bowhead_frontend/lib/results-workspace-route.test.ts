import { describe, expect, it } from "vitest";

import {
  clearResultsRoute,
  isStableResultToken,
  normalizeCanonicalResultsUrl,
  parseResultsRoute,
  writeResultsExplorerRoute,
  writeResultsRoute,
} from "./results-workspace-route";

describe("Results Workspace canonical route", () => {
  it("parses report, code, and artifact routes", () => {
    expect(parseResultsRoute("result=report")).toMatchObject({
      type: "report",
      explicit: true,
      source: "canonical",
      surface: "viewer",
    });
    expect(parseResultsRoute("result=code")).toMatchObject({
      type: "code",
      source: "canonical",
    });
    expect(parseResultsRoute("result=art_123&mode=raw")).toEqual({
      type: "artifact",
      artifactId: "art_123",
      mode: "raw",
      explicit: true,
      source: "canonical",
      surface: "viewer",
    });
  });

  it("parses the stable Explorer root and selected Viewer surface", () => {
    expect(parseResultsRoute("session=s_1&results=all")).toEqual({
      type: "explorer",
      explicit: true,
      source: "canonical",
      surface: "explorer",
    });
    expect(
      parseResultsRoute("results=all&result=art_123&mode=details"),
    ).toMatchObject({
      type: "artifact",
      artifactId: "art_123",
      mode: "details",
      surface: "explorer",
    });
  });

  it("normalizes missing or invalid modes to preview", () => {
    expect(parseResultsRoute("result=art_123")).toMatchObject({
      type: "artifact",
      mode: "preview",
    });
    expect(parseResultsRoute("result=art_123&mode=execute")).toMatchObject({
      type: "artifact",
      mode: "preview",
    });
  });

  it("normalizes the canonical URL without rewriting legacy read state", () => {
    const missing = normalizeCanonicalResultsUrl(
      new URL("https://bowhead.test/?session=s_1&result=art_1"),
    );
    expect(missing.search).toBe(
      "?session=s_1&result=art_1&mode=preview",
    );

    const invalid = normalizeCanonicalResultsUrl(
      new URL(
        "https://bowhead.test/?result=art_1&mode=execute&tab=data&focus=old",
      ),
    );
    expect(invalid.search).toBe(
      "?result=art_1&mode=preview&tab=data&focus=old",
    );

    const code = normalizeCanonicalResultsUrl(
      new URL("https://bowhead.test/?result=code&mode=raw"),
    );
    expect(code.search).toBe("?result=code");
  });

  it("prefers canonical state over legacy tab and focus", () => {
    expect(
      parseResultsRoute(
        "result=art_new&mode=details&tab=data&focus=art_old",
      ),
    ).toMatchObject({
      type: "artifact",
      artifactId: "art_new",
      mode: "details",
      source: "canonical",
    });
  });

  it("keeps legacy links readable", () => {
    expect(parseResultsRoute("tab=data&focus=art_legacy")).toMatchObject({
      type: "artifact",
      artifactId: "art_legacy",
      source: "legacy",
    });
    expect(parseResultsRoute("tab=data")).toMatchObject({
      type: "legacy-data",
    });
    expect(parseResultsRoute("tab=sql")).toMatchObject({ type: "code" });
  });

  it("rejects empty, unbounded, or unsafe result tokens", () => {
    expect(parseResultsRoute("result=")).toMatchObject({ type: "invalid" });
    expect(parseResultsRoute("result=bad%20id")).toMatchObject({
      type: "invalid",
    });
    expect(
      parseResultsRoute(`result=${"a".repeat(257)}`),
    ).toMatchObject({ type: "invalid" });
    expect(isStableResultToken("art_a:b.c-1")).toBe(true);
  });

  it("writes canonical state while preserving session and report intent", () => {
    const current = new URL(
      "https://bowhead.test/bowhead/?session=s_old&report=1&tab=data&focus=art_old&locale=zh",
    );
    const next = writeResultsRoute(
      current,
      { type: "artifact", artifactId: "art_new", mode: "details" },
      "s_new",
    );
    expect(next.pathname).toBe("/bowhead/");
    expect(next.searchParams.get("session")).toBe("s_new");
    expect(next.searchParams.get("report")).toBe("1");
    expect(next.searchParams.get("locale")).toBe("zh");
    expect(next.searchParams.get("result")).toBe("art_new");
    expect(next.searchParams.get("mode")).toBe("details");
    expect(next.searchParams.has("tab")).toBe(false);
    expect(next.searchParams.has("focus")).toBe(false);
  });

  it("removes artifact-only state for report and code", () => {
    const current = new URL(
      "https://bowhead.test/?result=art_1&mode=raw&focus=art_old",
    );
    expect(
      writeResultsRoute(current, { type: "report" }).search,
    ).toBe("?result=report");
    expect(
      writeResultsRoute(current, { type: "code" }).search,
    ).toBe("?result=code");
  });

  it("writes Explorer root and Viewer routes without leaking transient state", () => {
    const current = new URL(
      "https://bowhead.test/bowhead/?session=s_old&result=art_old&mode=raw&tab=data&focus=legacy&keep=1",
    );
    const root = writeResultsExplorerRoute(current, null, "s_new");
    expect(root.search).toBe("?session=s_new&keep=1&results=all");

    const viewer = writeResultsExplorerRoute(root, {
      type: "artifact",
      artifactId: "art_new",
      mode: "preview",
    });
    expect(viewer.search).toBe(
      "?session=s_new&keep=1&results=all&result=art_new&mode=preview",
    );
    expect(writeResultsRoute(viewer, { type: "report" }).search).toBe(
      "?session=s_new&keep=1&result=report",
    );
  });

  it("never adds a session to a clean URL (the page owns ?session=)", () => {
    const clean = new URL("https://bowhead.test/bowhead/?keep=1");
    expect(writeResultsRoute(clean, { type: "report" }, "s_stale").searchParams.has("session")).toBe(false);
    expect(writeResultsExplorerRoute(clean, null, "s_stale").searchParams.has("session")).toBe(false);
    expect(clearResultsRoute(clean, "s_stale").searchParams.has("session")).toBe(false);
  });

  it("clears canonical and legacy result state without dropping unrelated intent", () => {
    const current = new URL(
      "https://bowhead.test/bowhead/?session=s_old&report=1&result=art_old&mode=raw&tab=data&focus=legacy&locale=zh",
    );
    const next = clearResultsRoute(current, "s_new");

    expect(next.pathname).toBe("/bowhead/");
    expect(next.searchParams.get("session")).toBe("s_new");
    expect(next.searchParams.get("report")).toBe("1");
    expect(next.searchParams.get("locale")).toBe("zh");
    expect(next.searchParams.has("result")).toBe(false);
    expect(next.searchParams.has("mode")).toBe(false);
    expect(next.searchParams.has("tab")).toBe(false);
    expect(next.searchParams.has("focus")).toBe(false);
    expect(next.searchParams.has("results")).toBe(false);
  });

  it("normalizes a root Explorer without inventing an artifact mode", () => {
    const normalized = normalizeCanonicalResultsUrl(
      new URL("https://bowhead.test/?results=all&mode=raw"),
    );
    expect(normalized.search).toBe("?results=all");
  });
});
