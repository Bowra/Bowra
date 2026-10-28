import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useEffect, useState, useSyncExternalStore } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { SourceRef } from "@/lib/canvas-pm-adapter/citation-types";

const cacheSpies = vi.hoisted(() => ({
  clearArtifact: vi.fn(),
  clearKb: vi.fn(),
  evictArtifact: vi.fn(),
}));

vi.mock("@/lib/canvas-pm-adapter/artifact-preview-cache", () => ({
  clearArtifactPreviewCache: cacheSpies.clearArtifact,
  evictArtifactPreview: cacheSpies.evictArtifact,
}));
vi.mock("@/lib/canvas-pm-adapter/kb-chunk-cache", () => ({
  clearKbChunkCache: cacheSpies.clearKb,
}));

import {
  SourceAccessRegistryProvider,
  useSourceAccess,
  useSourceAccessActions,
} from "./source-access-registry";
import {
  getCanvasSnapshot,
  hydrateCanvas,
  redactSourceIdentityInView,
  resetCanvas,
  subscribeCanvas,
} from "./canvas-store";

const ALIAS = "source-ref-v1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
const SOURCE: SourceRef = {
  id: ALIAS,
  source_alias: ALIAS,
  access_state: "linked_accessible",
  generic_type: "document",
  ordinal: 2,
  safe_label: "Private board notes",
  safe_query: "private query",
  safe_preview: "private preview",
  safe_url: "/api/v1/report-artifacts/art-safe/download",
  safe_preview_url: "/api/v1/report-artifacts/art-safe/preview",
};

function Probe({ source = SOURCE }: { source?: SourceRef }) {
  const access = useSourceAccess(source);
  const { check, revoke } = useSourceAccessActions();
  return (
    <div>
      <button type="button" onClick={() => void check(source)}>check</button>
      <button type="button" onClick={() => revoke(source)}>revoke</button>
      <button type="button" onClick={() => void Promise.all([check(source), check(source)])}>check-twice</button>
      <output data-testid="status">{access?.status}</output>
      <output data-testid="generation">{access?.generation}</output>
      <output data-testid="detail">{JSON.stringify(access?.source)}</output>
      <output data-testid="preview">{JSON.stringify(access?.preview)}</output>
    </div>
  );
}

function renderProbe(source = SOURCE, onRedactAlias?: (sourceAlias: string) => void) {
  return render(
    <SourceAccessRegistryProvider onRedactAlias={onRedactAlias}>
      <Probe source={source} />
    </SourceAccessRegistryProvider>,
  );
}

/**
 * Use `check` only (without `useSourceAccess`) — **isolate** registry cache behavior from hook's
 * revoke-when-source-declares-unavailable side effect, otherwise testing their combined effect.
 * Next button simulates source criteria changing mid-session (access revoked/granted).
 */
function CheckProbe({ sources }: { sources: SourceRef[] }) {
  const { check } = useSourceAccessActions();
  const [index, setIndex] = useState(0);
  const [last, setLast] = useState("");
  return (
    <div>
      <button type="button" onClick={() => void check(sources[index]).then((r) => setLast(r.status))}>
        probe
      </button>
      <button type="button" onClick={() => setIndex((i) => i + 1)}>next-source</button>
      <output data-testid="last">{last}</output>
    </div>
  );
}

function renderCheckProbe(sources: SourceRef[]) {
  return render(
    <SourceAccessRegistryProvider>
      <CheckProbe sources={sources} />
    </SourceAccessRegistryProvider>,
  );
}

function ScopedProbe({ scopeKey }: { scopeKey: string }) {
  return (
    <SourceAccessRegistryProvider scopeKey={scopeKey}>
      <Probe />
    </SourceAccessRegistryProvider>
  );
}

beforeEach(() => {
  for (const spy of Object.values(cacheSpies)) spy.mockClear();
});

afterEach(() => {
  cleanup();
  resetCanvas();
  vi.unstubAllGlobals();
});

describe("SourceAccessRegistry", () => {
  it("clears stale details synchronously while probing, then restores only the successful generation", async () => {
    let resolveResponse: ((value: unknown) => void) | undefined;
    vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve) => { resolveResponse = resolve; })));
    renderProbe();

    fireEvent.click(screen.getByRole("button", { name: "check" }));
    expect(screen.getByTestId("status")).toHaveTextContent("checking");
    expect(screen.getByTestId("detail").textContent).not.toContain("Private board notes");
    expect(screen.getByTestId("detail").textContent).not.toContain("private query");
    expect(screen.getByTestId("detail").textContent).not.toContain("private preview");

    resolveResponse?.({
      ok: true,
      json: async () => ({ family: "text", mode: "text", text: "fresh preview" }),
    });
    await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("linked_accessible"));
    expect(screen.getByTestId("detail")).toHaveTextContent("Private board notes");
    expect(screen.getByTestId("preview")).toHaveTextContent("fresh preview");
  });

  it("turns 401, 403 and 404 into the same unavailable state and evicts preview caches", async () => {
    for (const status of [401, 403, 404]) {
      cleanup();
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status }));
      renderProbe();
      fireEvent.click(screen.getByRole("button", { name: "check" }));
      await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("linked_unavailable"));
      expect(screen.getByTestId("detail").textContent).not.toContain("Private board notes");
      expect(screen.getByTestId("detail").textContent).not.toContain("private query");
      expect(screen.getByTestId("detail").textContent).not.toContain("private preview");
    }
    expect(cacheSpies.evictArtifact).toHaveBeenCalledWith(ALIAS);
    expect(cacheSpies.clearKb).toHaveBeenCalled();
  });

  it("redacts the current Canvas memory copy after an authorization failure", async () => {
    hydrateCanvas({
      canvas: { id: "cv_1", sessionId: "s_1", title: "Report" },
      sections: [{
        id: "sec_sources",
        kind: "sources",
        entries: [{ ...SOURCE }],
      }] as never,
      sourceRefs: [SOURCE],
    });
    const onRedactAlias = vi.fn(redactSourceIdentityInView);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 403 }));
    renderProbe(SOURCE, onRedactAlias);

    fireEvent.click(screen.getByRole("button", { name: "check" }));
    await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("linked_unavailable"));

    const snapshot = getCanvasSnapshot();
    expect(onRedactAlias).toHaveBeenCalledWith(ALIAS);
    expect(JSON.stringify(snapshot.sections)).not.toContain("Private board notes");
    expect(JSON.stringify(snapshot.sections)).not.toContain("private query");
    expect(JSON.stringify(snapshot.sourceRefs)).not.toContain("private preview");
    expect(snapshot.sourceRefs[0]).toEqual({
      id: ALIAS,
      source_alias: ALIAS,
      access_state: "linked_unavailable",
      generic_type: "document",
      ordinal: 2,
    });
  });

  it("瞬时故障（503、网络错误）不抹来源身份：先 503 再恢复 200，下次悬停能重新可查看", async () => {
    hydrateCanvas({
      canvas: { id: "cv_1", sessionId: "s_1", title: "Report" },
      sections: [{ id: "sec_sources", kind: "sources", entries: [{ ...SOURCE }] }] as never,
      sourceRefs: [SOURCE],
    });
    const onRedactAlias = vi.fn(redactSourceIdentityInView);
    const fetchSpy = vi.fn()
      .mockResolvedValueOnce({ ok: false, status: 503 })
      .mockRejectedValueOnce(new Error("network down"))
      .mockResolvedValue({ ok: true, json: async () => ({ family: "text", mode: "text", text: "fresh" }) });
    vi.stubGlobal("fetch", fetchSpy);
    // Read source from store as canvas does: redaction makes next check see linked_unavailable, preventing all future probes
    function StoreProbe() {
      const snapshot = useSyncExternalStore(subscribeCanvas, getCanvasSnapshot, getCanvasSnapshot);
      return <Probe source={snapshot.sourceRefs[0] as SourceRef} />;
    }
    render(
      <SourceAccessRegistryProvider onRedactAlias={onRedactAlias}>
        <StoreProbe />
      </SourceAccessRegistryProvider>,
    );

    fireEvent.click(screen.getByRole("button", { name: "check" }));
    await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("linked_unavailable"));
    fireEvent.click(screen.getByRole("button", { name: "check" }));
    await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("linked_unavailable"));
    expect(onRedactAlias).not.toHaveBeenCalled();
    expect(getCanvasSnapshot().sourceRefs[0]).toMatchObject({ access_state: "linked_accessible", safe_label: "Private board notes" });

    fireEvent.click(screen.getByRole("button", { name: "check" }));
    await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("linked_accessible"));
    expect(fetchSpy).toHaveBeenCalledTimes(3);
    expect(screen.getByTestId("preview")).toHaveTextContent("fresh");
  });

  it("负对照：403 / 404 / 410 / 401 仍然抹来源身份", async () => {
    for (const status of [401, 403, 404, 410]) {
      cleanup();
      const onRedactAlias = vi.fn();
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status }));
      renderProbe(SOURCE, onRedactAlias);
      fireEvent.click(screen.getByRole("button", { name: "check" }));
      await waitFor(() => expect(onRedactAlias).toHaveBeenCalledWith(ALIAS));
      expect(screen.getByTestId("status")).toHaveTextContent("linked_unavailable");
    }
  });

  it("deduplicates concurrent probes for the same report alias", async () => {
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ family: "text", mode: "text", text: "fresh" }),
    });
    vi.stubGlobal("fetch", fetchSpy);
    renderProbe();
    fireEvent.click(screen.getByRole("button", { name: "check-twice" }));
    await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("linked_accessible"));
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it("已判定的结果被复用——重复 check 不再重新探测", async () => {
    // Formerly inFlight deduplicated only **concurrent** calls, deleting after resolve ⇒ every hover of same reference
    // reset to checking and fetched 20-row preview again, showing spinner each time,
    // with returned content changing popover dimensions and causing floating-ui flip ⇒ jitter.
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ family: "text", mode: "text", text: "fresh" }),
    });
    vi.stubGlobal("fetch", fetchSpy);
    renderProbe();

    fireEvent.click(screen.getByRole("button", { name: "check" }));
    await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("linked_accessible"));
    expect(fetchSpy).toHaveBeenCalledTimes(1);

    // Second and third hover
    fireEvent.click(screen.getByRole("button", { name: "check" }));
    fireEvent.click(screen.getByRole("button", { name: "check" }));
    await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("linked_accessible"));

    expect(fetchSpy).toHaveBeenCalledTimes(1);
    // Must not reset to checking (source of spinner + dimension changes → jitter)
    expect(screen.getByTestId("status")).toHaveTextContent("linked_accessible");
  });

  it("探测失败不缓存——下次悬停仍会重试", async () => {
    // Network glitches / 5xx / permissions not yet effective are **nondeterministic** failures; caching makes temporary faults permanently unavailable.
    const fetchSpy = vi.fn().mockRejectedValue(new Error("network down"));
    vi.stubGlobal("fetch", fetchSpy);
    renderProbe();

    fireEvent.click(screen.getByRole("button", { name: "check" }));
    await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("linked_unavailable"));
    expect(fetchSpy).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "check" }));
    await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(2));
  });

  it("会话中途拿到授权 → 缓存失效并重新探测（不刷新也能自愈）", async () => {
    // Cache formerly keyed only by alias; early-return criteria also include access_state / safe_preview_url.
    // Thus prior unavailable conclusion permanently blocked genuinely granted later access:
    // measured 0 fetches regardless of hovers; recovery required page refresh.
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ family: "text", mode: "text", text: "fresh" }),
    });
    vi.stubGlobal("fetch", fetchSpy);
    const denied: SourceRef = { ...SOURCE, access_state: "linked_unavailable", safe_preview_url: undefined };
    renderCheckProbe([denied, SOURCE]);

    fireEvent.click(screen.getByRole("button", { name: "probe" }));
    await waitFor(() => expect(screen.getByTestId("last")).toHaveTextContent("linked_unavailable"));
    expect(fetchSpy).not.toHaveBeenCalled(); // Early return; no request should occur

    fireEvent.click(screen.getByRole("button", { name: "next-source" }));
    fireEvent.click(screen.getByRole("button", { name: "probe" }));

    await waitFor(() => expect(screen.getByTestId("last")).toHaveTextContent("linked_accessible"));
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it("会话中途被撤权 → 不复用上一次的 accessible 结论（安全向）", async () => {
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ family: "text", mode: "text", text: "fresh" }),
    });
    vi.stubGlobal("fetch", fetchSpy);
    const revoked: SourceRef = { ...SOURCE, access_state: "linked_unavailable", safe_preview_url: undefined };
    renderCheckProbe([SOURCE, revoked]);

    fireEvent.click(screen.getByRole("button", { name: "probe" }));
    await waitFor(() => expect(screen.getByTestId("last")).toHaveTextContent("linked_accessible"));

    fireEvent.click(screen.getByRole("button", { name: "next-source" }));
    fireEvent.click(screen.getByRole("button", { name: "probe" }));

    // Changed criteria ⇒ invalidate old conclusion, redetermine unavailable (triggering redaction flow)
    await waitFor(() => expect(screen.getByTestId("last")).toHaveTextContent("linked_unavailable"));
  });

  it("只有 access_state 变（preview URL 原样留着）也必须重判", async () => {
    // Isolate this one of three criteria: revocation may change only access_state while leaving safe_preview_url
    // in payload. Fingerprint missing access_state would swallow this entire class of changes.
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ family: "text", mode: "text", text: "fresh" }),
    });
    vi.stubGlobal("fetch", fetchSpy);
    const deniedWithUrl: SourceRef = { ...SOURCE, access_state: "linked_unavailable" };
    renderCheckProbe([deniedWithUrl, SOURCE]);

    fireEvent.click(screen.getByRole("button", { name: "probe" }));
    await waitFor(() => expect(screen.getByTestId("last")).toHaveTextContent("linked_unavailable"));
    expect(fetchSpy).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "next-source" }));
    fireEvent.click(screen.getByRole("button", { name: "probe" }));

    await waitFor(() => expect(screen.getByTestId("last")).toHaveTextContent("linked_accessible"));
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it("只有 safe_preview_url 从无到有（access_state 一直 accessible）也必须重判", async () => {
    // Last individually isolated criterion. Early-return branch reads it, so fingerprint must include it —
    // otherwise accessible-marked sources without preview URLs become permanently unavailable.
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ family: "text", mode: "text", text: "fresh" }),
    });
    vi.stubGlobal("fetch", fetchSpy);
    const noUrlYet: SourceRef = { ...SOURCE, safe_preview_url: undefined };
    renderCheckProbe([noUrlYet, SOURCE]);

    fireEvent.click(screen.getByRole("button", { name: "probe" }));
    await waitFor(() => expect(screen.getByTestId("last")).toHaveTextContent("linked_unavailable"));
    expect(fetchSpy).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "next-source" }));
    fireEvent.click(screen.getByRole("button", { name: "probe" }));

    await waitFor(() => expect(screen.getByTestId("last")).toHaveTextContent("linked_accessible"));
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it("探测**途中**被撤权 → 不复用那条 in-flight 结论", async () => {
    // inFlight also deduplicates by alias, with **same** problem as result cache:
    // if criteria change during probe, second check gets promise calculated under old criteria.
    let resolveResponse: ((value: unknown) => void) | undefined;
    vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve) => { resolveResponse = resolve; })));
    const revoked: SourceRef = { ...SOURCE, access_state: "linked_unavailable", safe_preview_url: undefined };
    renderCheckProbe([SOURCE, revoked]);

    fireEvent.click(screen.getByRole("button", { name: "probe" })); // Pending
    fireEvent.click(screen.getByRole("button", { name: "next-source" }));
    fireEvent.click(screen.getByRole("button", { name: "probe" }));

    // Before first response arrives, second check must immediately early-return under new criteria
    await waitFor(() => expect(screen.getByTestId("last")).toHaveTextContent("linked_unavailable"));
    expect(resolveResponse).toBeTypeOf("function"); // Confirm first request is still pending
  });

  it("判据没变时仍然只探测一次 —— 修 key 不能把「每次 hover 重查」放回来", async () => {
    // Reverse guard: fingerprint uses only **presence** of safe_preview_url, not full string; otherwise signed-parameter
    // preview URLs differ every time, making cache ineffective.
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ family: "text", mode: "text", text: "fresh" }),
    });
    vi.stubGlobal("fetch", fetchSpy);
    const sameCriteria: SourceRef = { ...SOURCE, safe_preview_url: `${SOURCE.safe_preview_url}?sig=xyz` };
    renderCheckProbe([SOURCE, sameCriteria]);

    fireEvent.click(screen.getByRole("button", { name: "probe" }));
    await waitFor(() => expect(screen.getByTestId("last")).toHaveTextContent("linked_accessible"));
    fireEvent.click(screen.getByRole("button", { name: "next-source" }));
    fireEvent.click(screen.getByRole("button", { name: "probe" }));
    await waitFor(() => expect(screen.getByTestId("last")).toHaveTextContent("linked_accessible"));

    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it("revoke 之后缓存失效，重新探测", async () => {
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ family: "text", mode: "text", text: "fresh" }),
    });
    vi.stubGlobal("fetch", fetchSpy);
    renderProbe();

    fireEvent.click(screen.getByRole("button", { name: "check" }));
    await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("linked_accessible"));
    fireEvent.click(screen.getByRole("button", { name: "revoke" }));
    fireEvent.click(screen.getByRole("button", { name: "check" }));

    await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(2));
  });

  it("does not restore a stale successful response after the generation is revoked", async () => {
    let resolveResponse: ((value: unknown) => void) | undefined;
    vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve) => { resolveResponse = resolve; })));
    renderProbe();
    fireEvent.click(screen.getByRole("button", { name: "check" }));
    fireEvent.click(screen.getByRole("button", { name: "revoke" }));
    resolveResponse?.({
      ok: true,
      json: async () => ({ family: "text", mode: "text", text: "must not render" }),
    });

    await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("linked_unavailable"));
    expect(screen.getByTestId("preview").textContent).not.toContain("must not render");
    expect(screen.getByTestId("detail").textContent).not.toContain("Private board notes");
  });

  it("does not restore a response started before the report scope changed", async () => {
    let resolveOldResponse: ((value: unknown) => void) | undefined;
    vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve) => { resolveOldResponse = resolve; })));
    const view = render(<ScopedProbe scopeKey="report-a" />);
    fireEvent.click(screen.getByRole("button", { name: "check" }));
    expect(screen.getByTestId("status")).toHaveTextContent("checking");

    view.rerender(<ScopedProbe scopeKey="report-b" />);
    await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("linked_accessible"));
    // The server projection can still say accessible before the new scope is
    // probed, but old business details must not be restored by the late result.
    const oldJson = vi.fn(async () => ({ family: "text", mode: "text", text: "old-scope secret" }));
    resolveOldResponse?.({
      ok: true,
      json: oldJson,
    });

    await waitFor(() => expect(oldJson).toHaveBeenCalled());
    expect(screen.getByTestId("preview").textContent).not.toContain("old-scope secret");
  });

  it("fails closed without a current authorization endpoint and clears caches on unmount", async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    const { unmount } = renderProbe({
      ...SOURCE,
      safe_preview_url: undefined,
    });
    fireEvent.click(screen.getByRole("button", { name: "check" }));
    await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("linked_unavailable"));
    expect(fetchSpy).not.toHaveBeenCalled();
    unmount();
    expect(cacheSpies.clearArtifact).toHaveBeenCalled();
    expect(cacheSpies.clearKb).toHaveBeenCalled();
  });
  it("判据变成注定不可用后，旧判据的在途探测晚回来也不能把来源改回可访问", async () => {
    let resolveResponse: ((value: unknown) => void) | undefined;
    vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve) => { resolveResponse = resolve; })));
    const noPreview: SourceRef = { ...SOURCE, safe_preview_url: undefined };
    renderCheckProbe([SOURCE, noPreview]);

    fireEvent.click(screen.getByRole("button", { name: "probe" }));
    fireEvent.click(screen.getByRole("button", { name: "next-source" }));
    fireEvent.click(screen.getByRole("button", { name: "probe" }));
    await waitFor(() => expect(screen.getByTestId("last")).toHaveTextContent("linked_unavailable"));

    resolveResponse?.({ ok: true, json: async () => ({ family: "text", mode: "text", text: "STALE PREVIEW" }) });
    await new Promise((resolve) => setTimeout(resolve, 20));
    // Old probe invalidated: its own caller also gets unavailable, without old preview returning.
    expect(screen.getByTestId("last")).toHaveTextContent("linked_unavailable");
  });

  it("不可用来源在面板里打开不会和撤销互相触发（报告页曾无限更新崩进恢复模式）", async () => {
    // Same structure as real report page: source comes from canvas store, open panel checks when source changes,
    // registry revocation erases that source from canvas store.
    const unavailable: SourceRef = {
      id: ALIAS,
      source_alias: ALIAS,
      access_state: "linked_unavailable",
      generic_type: "query_result",
      ordinal: 1,
    };
    hydrateCanvas({
      canvas: { id: "cv_1", sessionId: "s_1", title: "Report" },
      sections: [{ id: "sec_sources", kind: "sources", entries: [{ ...unavailable }] }] as never,
      sourceRefs: [{ ...unavailable }],
    });
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    let checks = 0;
    function OpenPanel() {
      const canvas = useSyncExternalStore(subscribeCanvas, getCanvasSnapshot);
      const ref = canvas.sourceRefs[0];
      const access = useSourceAccess(ref);
      const { check } = useSourceAccessActions();
      useEffect(() => {
        checks += 1;
        void check(ref);
      }, [ref, check]);
      return <output data-testid="status">{access?.status}</output>;
    }
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <SourceAccessRegistryProvider onRedactAlias={redactSourceIdentityInView}>
        <OpenPanel />
      </SourceAccessRegistryProvider>,
    );

    await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("linked_unavailable"));
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(checks).toBeLessThanOrEqual(2);
    expect(errors.mock.calls.join("\n")).not.toMatch(/Maximum update depth/);
    expect(fetchSpy).not.toHaveBeenCalled();
    errors.mockRestore();
  });

  it("已经抹过的来源再撤销一次，画布 store 不再通知更新", () => {
    hydrateCanvas({
      canvas: { id: "cv_1", sessionId: "s_1", title: "Report" },
      sections: [{ id: "sec_sources", kind: "sources", entries: [{ ...SOURCE }] }] as never,
      sourceRefs: [SOURCE],
    });
    redactSourceIdentityInView(ALIAS);
    const redacted = getCanvasSnapshot();
    const listener = vi.fn();
    const unsubscribe = subscribeCanvas(listener);
    redactSourceIdentityInView(ALIAS);
    unsubscribe();
    expect(listener).not.toHaveBeenCalled();
    expect(getCanvasSnapshot()).toBe(redacted);
  });
});
