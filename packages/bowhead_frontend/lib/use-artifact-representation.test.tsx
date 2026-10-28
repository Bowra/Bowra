import "@testing-library/jest-dom/vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useArtifactRepresentation } from "./use-artifact-representation";
import {
  FilePreviewer,
  type ArtifactItem,
} from "@/components/canvas/data-tab-files";
import type { ResultsViewerMode } from "./results-workspace-route";

const auth = vi.hoisted(() => ({
  me: { id: "u1", team_id: "team", auth_session_generation: "generation1" },
}));
vi.mock("./auth/auth-session-context", () => ({
  useOptionalAuthSession: () => ({ status: "authenticated", me: auth.me }),
}));
vi.mock("next-intl", () => ({ useTranslations: () => (key: string) => key }));
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  auth.me = {
    id: "u1",
    team_id: "team",
    auth_session_generation: "generation1",
  };
});
const item: ArtifactItem = {
  id: "art_a",
  filename: "rows.csv",
  mime: "text/csv",
  size: 20,
  source: "upload",
  created_at: "2026-09-08T00:00:00Z",
  preview_available: true,
};
const props = {
  sessionId: "s_1",
  artifactId: item.id,
  scope: "session" as const,
  mode: "preview" as ResultsViewerMode,
  profile: "table" as const,
};
function result(
  mode: string,
  id = item.id,
  text = "raw original",
  revision = "r1",
) {
  return new Response(
    JSON.stringify({
      representation: mode,
      revision,
      family: "text",
      mode: "text",
      text,
      details: mode === "details" ? { ...item, id } : undefined,
    }),
    { status: 200 },
  );
}
function error(code: string, status = 422) {
  return new Response(
    JSON.stringify({ detail: { error: { code, message: "private path" } } }),
    { status },
  );
}
function deferred() {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

describe("independent representations", () => {
  it.each([
    ["session", "export_copy_legacy_source_missing"],
    ["report", "export_copy_legacy_source_missing"],
    ["session", "export_copy_current"],
    ["session", "unverified"],
    ["session", undefined],
  ] as const)("keeps catalog trust %s/%s when an older Details response carries storage trust", async (scope, trust) => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({
      representation: "details", revision: "details-1", family: "pdf",
      details: { ...item, filename: "renamed.pdf", trust: "trusted" },
    }), { status: 200 }));
    vi.stubGlobal("fetch", fetcher);
    render(<FilePreviewer item={{ ...item, trust }} sessionId="s_1" scope={scope} viewerMode="details" />);
    const trustRow = (await screen.findByText("detail_trust")).closest("div")!;
    expect(within(trustRow).getByText(trust ?? "—")).toBeInTheDocument();
    const filenameRow = screen.getByText("detail_filename").closest("div")!;
    expect(within(filenameRow).getByText("renamed.pdf")).toBeInTheDocument();
    expect(fetcher.mock.calls).toHaveLength(1);
  });

  it("keeps the loaded representation when focus returns from a child frame", async () => {
    vi.spyOn(document, "hasFocus").mockReturnValue(true);
    const fetcher = vi.fn(async (input: RequestInfo | URL) => result(String(input).split("/").at(-1)!));
    vi.stubGlobal("fetch", fetcher);
    const hook = renderHook(() => useArtifactRepresentation({ ...props, mode: "raw" }));
    await waitFor(() => expect(hook.result.current.data?.representation).toBe("raw"));
    const loaded = hook.result.current.data;
    const calls = fetcher.mock.calls.length;
    await act(async () => { fireEvent.blur(window); fireEvent.focus(window); });
    expect(hook.result.current.data).toBe(loaded);
    expect(hook.result.current.loading).toBe(false);
    expect(fetcher).toHaveBeenCalledTimes(calls);
  });

  it("rechecks authorization after the visible browser window loses focus", async () => {
    const focused = vi.spyOn(document, "hasFocus").mockReturnValue(true);
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
    let denied = false;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => denied
      ? error("artifact_scope_unavailable", 404) : result(String(input).split("/").at(-1)!)));
    const hook = renderHook(() => useArtifactRepresentation({ ...props, mode: "raw" }));
    await waitFor(() => expect(hook.result.current.data?.representation).toBe("raw"));
    focused.mockReturnValue(false);
    act(() => fireEvent.blur(window));
    denied = true;
    focused.mockReturnValue(true);
    act(() => fireEvent.focus(window));
    expect(hook.result.current.data).toBeNull();
    await waitFor(() => expect(hook.result.current.scopeUnavailable).toBe(true));
  });

  it("coalesces visibility restoration and focus into one fresh authorization check", async () => {
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
    const fetcher = vi.fn(async (input: RequestInfo | URL) => result(String(input).split("/").at(-1)!));
    vi.stubGlobal("fetch", fetcher);
    const hook = renderHook(() => useArtifactRepresentation({ ...props, mode: "raw" }));
    await waitFor(() => expect(hook.result.current.data?.representation).toBe("raw"));
    visibility.mockReturnValue("hidden");
    act(() => fireEvent(document, new Event("visibilitychange")));
    const calls = fetcher.mock.calls.length;
    // A focus event delivered before visibility restoration cannot consume the recheck.
    await act(async () => fireEvent.focus(window));
    expect(fetcher).toHaveBeenCalledTimes(calls);
    visibility.mockReturnValue("visible");
    act(() => fireEvent(document, new Event("visibilitychange")));
    await waitFor(() => expect(hook.result.current.data?.representation).toBe("raw"));
    expect(fetcher).toHaveBeenCalledTimes(calls + 2);
    await act(async () => fireEvent.focus(window));
    expect(fetcher).toHaveBeenCalledTimes(calls + 2);
    hook.unmount();
    await act(async () => fireEvent.focus(window));
    expect(fetcher).toHaveBeenCalledTimes(calls + 2);
  });

  it("reuses Raw only after a successful fresh metadata check", async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL) => result(String(input).split("/").at(-1)!));
    vi.stubGlobal("fetch", fetcher);
    const hook = renderHook((p) => useArtifactRepresentation(p), { initialProps: { ...props, mode: "raw" as ResultsViewerMode } });
    await waitFor(() => expect(hook.result.current.data?.representation).toBe("raw"));
    hook.rerender({ ...props, mode: "details" });
    await waitFor(() => expect(hook.result.current.data?.representation).toBe("details"));
    hook.rerender({ ...props, mode: "raw" });
    await waitFor(() => expect(hook.result.current.data?.representation).toBe("raw"));
    expect(fetcher.mock.calls.filter(([url]) => String(url).endsWith("/raw"))).toHaveLength(1);
    expect(fetcher.mock.calls.filter(([url]) => String(url).endsWith("/details"))).toHaveLength(3);
  });

  it("identity changes dispose the old renderer/fullscreen state", async () => {
    let pending = false;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => pending
      ? new Promise<Response>(() => {}) : Promise.resolve(result(String(input).split("/").at(-1)!))));
    const view = render(<FilePreviewer item={item} sessionId="s_1" scope="session" viewerMode="raw" />);
    await screen.findByText("raw original");
    fireEvent.click(screen.getByRole("button", { name: "open_full" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    pending = true;
    auth.me = { ...auth.me, id: "u2" };
    view.rerender(<FilePreviewer item={item} sessionId="s_1" scope="session" viewerMode="raw" />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByText("raw original")).not.toBeInTheDocument();
  });

  it("offers recovery from a parser failure, Raw/Details work, fullscreen does not refetch", async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      const mode = String(input).split("/").at(-1)!;
      return mode === "preview"
        ? error("artifact_representation_corrupt")
        : result(mode);
    });
    vi.stubGlobal("fetch", fetcher);
    render(<FilePreviewer item={item} sessionId="s_1" scope="session" />);
    const failure = await screen.findByRole("alert");
    expect(failure).toHaveTextContent("error_corrupt");
    expect(failure).not.toHaveTextContent("private path");
    expect(
      within(failure).getByRole("link", { name: "download" }),
    ).toHaveAttribute("href", "/api/v1/files/art_a");
    fireEvent.click(within(failure).getByRole("button", { name: "mode_raw" }));
    expect(await screen.findByText("raw original")).toBeInTheDocument();
    const calls = fetcher.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "open_full" }));
    expect(await screen.findByRole("dialog")).toHaveTextContent("raw original");
    expect(fetcher).toHaveBeenCalledTimes(calls);
    fireEvent.keyDown(document, { key: "Escape" });
    fireEvent.click(screen.getByRole("tab", { name: "mode_details" }));
    expect(await screen.findByText(item.id)).toBeInTheDocument();
    expect(
      fetcher.mock.calls.some(([url]) =>
        String(url).endsWith("/representations/raw"),
      ),
    ).toBe(true);
  });

  it.each(["success", "failure"])(
    "ignores late preview %s after switching to Raw, even if fetch ignores abort",
    async (outcome) => {
      const pending = deferred();
      let previewSignal: AbortSignal | undefined;
      vi.stubGlobal(
        "fetch",
        vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
          const mode = String(input).split("/").at(-1)!;
          if (mode === "preview") {
            previewSignal = init?.signal as AbortSignal;
            return pending.promise;
          }
          return Promise.resolve(result(mode));
        }),
      );
      const hook = renderHook((p) => useArtifactRepresentation(p), {
        initialProps: props,
      });
      await waitFor(() => expect(previewSignal).toBeDefined());
      hook.rerender({ ...props, mode: "raw" });
      expect(hook.result.current.data).toBeNull();
      await waitFor(() =>
        expect(hook.result.current.data?.representation).toBe("raw"),
      );
      expect(previewSignal?.aborted).toBe(true);
      await act(async () =>
        pending.resolve(
          outcome === "success"
            ? result("preview", item.id, "stale")
            : error("artifact_scope_unavailable", 404),
        ),
      );
      expect(hook.result.current.data?.representation).toBe("raw");
      expect(hook.result.current.error).toBeNull();
    },
  );

  it("reauthorizes cached modes and purges every representation on revocation", async () => {
    let denied = false;
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      const mode = String(input).split("/").at(-1)!;
      return denied ? error("artifact_scope_unavailable", 404) : result(mode);
    });
    vi.stubGlobal("fetch", fetcher);
    const hook = renderHook((p) => useArtifactRepresentation(p), {
      initialProps: { ...props, mode: "raw" as ResultsViewerMode },
    });
    await waitFor(() =>
      expect(hook.result.current.data?.representation).toBe("raw"),
    );
    hook.rerender({ ...props, mode: "details" });
    await waitFor(() =>
      expect(hook.result.current.data?.representation).toBe("details"),
    );
    denied = true;
    hook.rerender({ ...props, mode: "raw" });
    expect(hook.result.current.data).toBeNull();
    await waitFor(() =>
      expect(hook.result.current.scopeUnavailable).toBe(true),
    );
    denied = false;
    act(() => hook.result.current.retry());
    await waitFor(() =>
      expect(hook.result.current.data?.representation).toBe("raw"),
    );
    expect(
      fetcher.mock.calls.filter(([url]) => String(url).endsWith("/raw")),
    ).toHaveLength(2);
  });

  it("network failure during reauthorization cannot display cached content", async () => {
    let fail = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (fail) throw new TypeError("Failed to fetch");
        return result(String(input).split("/").at(-1)!);
      }),
    );
    const hook = renderHook((p) => useArtifactRepresentation(p), {
      initialProps: { ...props, mode: "raw" as ResultsViewerMode },
    });
    await waitFor(() =>
      expect(hook.result.current.data?.representation).toBe("raw"),
    );
    hook.rerender({ ...props, mode: "details" });
    await waitFor(() =>
      expect(hook.result.current.data?.representation).toBe("details"),
    );
    fail = true;
    hook.rerender({ ...props, mode: "raw" });
    await waitFor(() => expect(hook.result.current.error).toBe("network"));
    expect(hook.result.current.data).toBeNull();
  });

  it.each(["scope", "object", "principal"])(
    "masks cached data immediately on %s changes",
    async (change) => {
      let block = false;
      const fetcher = vi.fn((input: RequestInfo | URL) => {
        if (block) return new Promise<Response>(() => {});
        return Promise.resolve(result(String(input).split("/").at(-1)!));
      });
      vi.stubGlobal("fetch", fetcher);
      const hook = renderHook(
        (p: Parameters<typeof useArtifactRepresentation>[0]) =>
          useArtifactRepresentation(p),
        { initialProps: { ...props, mode: "raw" } },
      );
      await waitFor(() =>
        expect(hook.result.current.data?.representation).toBe("raw"),
      );
      block = true;
      if (change === "principal")
        auth.me = { ...auth.me, auth_session_generation: "generation2" };
      hook.rerender({
        ...props,
        mode: "raw",
        scope: change === "scope" ? "report" : "session",
        artifactId: change === "object" ? "art_b" : item.id,
      });
      expect(hook.result.current.data).toBeNull();
      expect(hook.result.current.loading).toBe(true);
      if (change === "scope")
        expect(String(fetcher.mock.calls.at(-1)?.[0])).toBe(
          "/api/v1/canvas/by-session/s_1/artifacts/art_a/representations/details",
        );
    },
  );

  it("changed metadata revision invalidates the cached Raw", async () => {
    let revision = "r1";
    const fetcher = vi.fn(async (input: RequestInfo | URL) =>
      result(String(input).split("/").at(-1)!, item.id, revision, revision),
    );
    vi.stubGlobal("fetch", fetcher);
    const hook = renderHook((p) => useArtifactRepresentation(p), {
      initialProps: { ...props, mode: "raw" as ResultsViewerMode },
    });
    await waitFor(() => expect(hook.result.current.data?.text).toBe("r1"));
    hook.rerender({ ...props, mode: "details" });
    await waitFor(() =>
      expect(hook.result.current.data?.representation).toBe("details"),
    );
    revision = "r2";
    hook.rerender({ ...props, mode: "raw" });
    await waitFor(() => expect(hook.result.current.data?.text).toBe("r2"));
    expect(
      fetcher.mock.calls.filter(([url]) => String(url).endsWith("/raw")),
    ).toHaveLength(2);
  });
});
