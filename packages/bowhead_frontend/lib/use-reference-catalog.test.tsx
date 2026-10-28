import { StrictMode } from "react";
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import fixtures from "../../../contracts/chat-references/catalog-v1.json";
import { ApiError, apiJson } from "./api-client";
import type { ComposerPersistenceScope } from "./composer-persistence-scope";
import type { ReferenceCatalogSelection } from "./reference-catalog-api";
import { useReferenceCatalog } from "./use-reference-catalog";

vi.mock("./api-client", async (original) => ({
  ...(await original<typeof import("./api-client")>()),
  apiJson: vi.fn(),
}));
const scope: ComposerPersistenceScope = {
  userId: "user_one",
  teamId: "team_one",
  workspaceId: "ws_one",
  requestedSessionId: null,
  authSessionGeneration: "generation_one",
};
const selection: ReferenceCatalogSelection = {
  trigger: "@",
  query: "",
  entryMode: "ordinary",
  selectedRefs: [],
};
const page = (ids: number[], cursor: string | null = null) => {
  const data = structuredClone(fixtures.cases[0].response) as any;
  const template = data.groups[0].items[0];
  data.groups[0].items = ids.map((id) => ({
    ...template,
    id: `osa_${id}`,
    label: `Agent ${id}`,
    resource: {
      ...template.resource,
      id: `osa_${id}`,
      version: `arv_${id}`,
      assignment_id: `asa_${id}`,
      release_id: `arl_${id}`,
    },
  }));
  data.next_cursor = cursor;
  return data;
};
const pending = () => {
  let resolve!: (value: unknown) => void;
  const promise = new Promise<unknown>((done) => {
    resolve = done;
  });
  return { promise, resolve };
};
const tick = async (ms = 120) => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
};
beforeEach(() => {
  vi.useFakeTimers();
  vi.mocked(apiJson).mockReset();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("reference catalog request lifecycle", () => {
  it("debounces changing queries and keeps closed menus idle", async () => {
    vi.mocked(apiJson).mockResolvedValue(page([1]));
    const { result, rerender } = renderHook(
      ({ query }: { query: ReferenceCatalogSelection | null }) =>
        useReferenceCatalog(scope, query, "zh-CN"),
      { initialProps: { query: null as ReferenceCatalogSelection | null } },
    );
    await tick();
    expect(result.current.status).toBe("idle");
    expect(apiJson).not.toHaveBeenCalled();
    rerender({ query: selection });
    await tick(119);
    expect(apiJson).not.toHaveBeenCalled();
    rerender({ query: { ...selection, query: "利润" } });
    await tick(119);
    expect(apiJson).not.toHaveBeenCalled();
    await tick(1);
    expect(apiJson).toHaveBeenCalledTimes(1);
    expect(
      JSON.parse(vi.mocked(apiJson).mock.calls[0][1]!.body as string).query,
    ).toBe("利润");
    expect(result.current.data?.groups[0].items[0].id).toBe("osa_1");
    rerender({ query: null });
    expect(result.current.data).toBeNull();
    expect(result.current.status).toBe("idle");
  });

  it.each([
    "userId",
    "teamId",
    "workspaceId",
    "requestedSessionId",
    "authSessionGeneration",
  ] as const)(
    "immediately hides old data and aborts late pages after %s changes",
    async (field) => {
      const late = pending();
      vi.mocked(apiJson)
        .mockResolvedValueOnce(
          page(
            Array.from({ length: 20 }, (_, i) => i),
            "next",
          ),
        )
        .mockReturnValueOnce(late.promise)
        .mockResolvedValueOnce(page([99]));
      const renders: { current: ComposerPersistenceScope; ids: string[] }[] =
        [];
      const { result, rerender } = renderHook(
        ({ current }) => {
          const state = useReferenceCatalog(current, selection, "zh-CN");
          renders.push({
            current,
            ids: state.data?.groups[0].items.map((item) => item.id) ?? [],
          });
          return state;
        },
        { initialProps: { current: scope } },
      );
      await tick();
      let more!: Promise<void>;
      act(() => {
        more = result.current.loadMore();
      });
      const signal = vi.mocked(apiJson).mock.calls[1][1]!.signal!;
      const changed = { ...scope, [field]: "different" };
      rerender({ current: changed });
      expect(signal.aborted).toBe(true);
      expect(
        renders
          .filter((render) => render.current === changed)
          .every((render) => !render.ids.length),
      ).toBe(true);
      await tick();
      await act(async () => {
        late.resolve(page([20]));
        await more;
      });
      expect(
        result.current.data?.groups[0].items.map((item) => item.id),
      ).toEqual(["osa_99"]);
    },
  );

  it.each(["query", "agent", "locale"])(
    "drops late initial results after %s changes",
    async (change) => {
      const late = pending();
      vi.mocked(apiJson)
        .mockReturnValueOnce(late.promise)
        .mockResolvedValueOnce(page([2]));
      const { result, rerender } = renderHook(
        ({ query, locale }) => useReferenceCatalog(scope, query, locale),
        { initialProps: { query: selection, locale: "zh-CN" } },
      );
      await tick();
      const next =
        change === "query"
          ? { ...selection, query: "new" }
          : change === "agent"
            ? {
                ...selection,
                selectedRefs: [page([1]).groups[0].items[0].resource],
              }
            : selection;
      rerender({
        query: next,
        locale: change === "locale" ? "en-US" : "zh-CN",
      });
      await tick();
      await act(async () => {
        late.resolve(page([1]));
        await late.promise;
      });
      expect(result.current.data?.groups[0].items[0].id).toBe("osa_2");
    },
  );

  it("does not refetch for equivalent editor renders and handles StrictMode", async () => {
    vi.mocked(apiJson).mockResolvedValue(page([1]));
    const { result, rerender } = renderHook(
      ({ current, query }) => useReferenceCatalog(current, query, "zh-CN"),
      {
        initialProps: { current: scope, query: selection },
        wrapper: StrictMode,
      },
    );
    await tick();
    rerender({
      current: { ...scope },
      query: { ...selection, selectedRefs: [] },
    });
    await tick();
    expect(apiJson).toHaveBeenCalledTimes(1);
    expect(result.current.status).toBe("loaded");
  });

  it("serializes pagination and appends only the matching generation", async () => {
    const next = pending();
    vi.mocked(apiJson)
      .mockResolvedValueOnce(
        page(
          Array.from({ length: 20 }, (_, i) => i),
          "next",
        ),
      )
      .mockReturnValueOnce(next.promise);
    const { result } = renderHook(() =>
      useReferenceCatalog(scope, selection, "zh-CN"),
    );
    await tick();
    let one!: Promise<void>, two!: Promise<void>;
    act(() => {
      one = result.current.loadMore();
      two = result.current.loadMore();
    });
    expect(apiJson).toHaveBeenCalledTimes(2);
    expect(result.current.loadingMore).toBe(true);
    await act(async () => {
      next.resolve(page([20]));
      await Promise.all([one, two]);
    });
    expect(result.current.data?.groups[0].items).toHaveLength(21);
    expect(result.current.data?.next_cursor).toBeNull();
    await act(async () => {
      await result.current.loadMore();
    });
    expect(apiJson).toHaveBeenCalledTimes(2);
  });

  it.each(["changed", "overlap", "network"])(
    "clears uncertain %s pagination and requires an explicit retry",
    async (error) => {
      const next = page([error === "overlap" ? 0 : 20]);
      if (error === "changed") next.catalog_generation = "b".repeat(64);
      const mock = vi.mocked(apiJson).mockResolvedValueOnce(
        page(
          Array.from({ length: 20 }, (_, i) => i),
          "next",
        ),
      );
      if (error === "network")
        mock.mockRejectedValueOnce(
          new ApiError(503, "internal account secret"),
        );
      else mock.mockResolvedValueOnce(next);
      mock.mockResolvedValueOnce(page([42]));
      const { result } = renderHook(() =>
        useReferenceCatalog(scope, selection, "zh-CN"),
      );
      await tick();
      await act(async () => {
        await result.current.loadMore();
      });
      expect(result.current.data).toBeNull();
      expect(result.current.status).toBe("error");
      expect(result.current.error).toBe(
        error === "changed"
          ? "context_changed"
          : error === "overlap"
            ? "refresh_required"
            : "load_failed",
      );
      await tick(1000);
      expect(apiJson).toHaveBeenCalledTimes(2);
      act(() => result.current.retry());
      await tick();
      expect(result.current.data?.groups[0].items[0].id).toBe("osa_42");
    },
  );

  it("aborts an unresolved request on unmount", async () => {
    const late = pending();
    vi.mocked(apiJson).mockReturnValue(late.promise);
    const { unmount } = renderHook(() =>
      useReferenceCatalog(scope, selection, "zh-CN"),
    );
    await tick();
    const signal = vi.mocked(apiJson).mock.calls[0][1]!.signal!;
    unmount();
    expect(signal.aborted).toBe(true);
    await act(async () => {
      late.resolve(page([1]));
      await late.promise;
    });
  });
});
