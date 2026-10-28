"use client";

import { referenceCatalogItemIdentity } from "./reference-catalog-api";
import { useEffect, useRef, useState } from "react";
import { ApiError } from "./api-client";
import {
  composerDraftScopeSignature,
  type ComposerPersistenceScope,
} from "./composer-persistence-scope";
import {
  getReferenceCatalog,
  type ReferenceCatalogResponse,
  type ReferenceCatalogSelection,
} from "./reference-catalog-api";

export type ReferenceCatalogFailure =
  "load_failed" | "refresh_required" | "context_changed" | "invalid_input";
type State = {
  key: string;
  status: "idle" | "loading" | "loaded" | "error";
  data: ReferenceCatalogResponse | null;
  error: ReferenceCatalogFailure | null;
  loadingMore: boolean;
};
type ActiveRequest = {
  key: string;
  controller: AbortController;
  data: ReferenceCatalogResponse | null;
  loadingMore: boolean;
};
const EMPTY: State = {
  key: "",
  status: "idle",
  data: null,
  error: null,
  loadingMore: false,
};

function failure(error: unknown): ReferenceCatalogFailure {
  if (error instanceof ApiError) {
    if (error.code === "reference_catalog_response_invalid")
      return "refresh_required";
    if (
      error.code === "reference_catalog_cursor_invalid" ||
      error.code === "reference_scope_unavailable"
    )
      return "context_changed";
    if (
      error.code === "reference_input_invalid" ||
      error.code === "reference_request_too_large"
    )
      return "invalid_input";
  }
  return "load_failed";
}

/** Menu-local reader, with no persistent cache or permission reuse. A null
 * selection closes it; it has no effect on the existing text send path.
 */
export function useReferenceCatalog(
  scope: ComposerPersistenceScope | null,
  selection: ReferenceCatalogSelection | null,
  locale: string,
) {
  const serializedSelection =
    selection === null ? "" : JSON.stringify(selection);
  const signature = composerDraftScopeSignature(scope);
  const key =
    signature && serializedSelection
      ? JSON.stringify([signature, locale, serializedSelection])
      : "";
  const [state, setState] = useState<State>(EMPTY);
  const [reload, setReload] = useState(0);
  const active = useRef<ActiveRequest | null>(null);

  useEffect(() => {
    if (!key || !scope) {
      setState(EMPTY);
      return;
    }
    const current: ActiveRequest = {
      key,
      controller: new AbortController(),
      data: null,
      loadingMore: false,
    };
    active.current = current;
    const scopeSnapshot = { ...scope };
    const selectionSnapshot = JSON.parse(
      serializedSelection,
    ) as ReferenceCatalogSelection;
    setState({
      key,
      status: "loading",
      data: null,
      error: null,
      loadingMore: false,
    });
    const timer = setTimeout(() => {
      void getReferenceCatalog(
        scopeSnapshot,
        selectionSnapshot,
        current.controller.signal,
      )
        .then((data) => {
          if (current.controller.signal.aborted || active.current !== current)
            return;
          current.data = data;
          setState({
            key,
            status: "loaded",
            data,
            error: null,
            loadingMore: false,
          });
        })
        .catch((error: unknown) => {
          if (current.controller.signal.aborted || active.current !== current)
            return;
          setState({
            key,
            status: "error",
            data: null,
            error: failure(error),
            loadingMore: false,
          });
        });
    }, 120);
    return () => {
      clearTimeout(timer);
      current.controller.abort();
      if (active.current === current) active.current = null;
    };
    // key contains the complete scope and selection; object identity changes
    // alone must not restart the debounce on every editor render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, reload]);

  const visible = !key
    ? EMPTY
    : state.key === key
      ? state
      : { ...EMPTY, key, status: "loading" as const };

  const loadMore = async () => {
    const current = active.current;
    const previous = current?.data;
    if (
      !scope ||
      !selection ||
      !key ||
      current?.key !== key ||
      current.controller.signal.aborted ||
      !previous?.next_cursor ||
      current.loadingMore
    )
      return;
    current.loadingMore = true;
    setState((old) => (old.key === key ? { ...old, loadingMore: true } : old));
    try {
      const next = await getReferenceCatalog(
        { ...scope },
        JSON.parse(serializedSelection),
        current.controller.signal,
        previous.next_cursor,
      );
      if (current.controller.signal.aborted || active.current !== current)
        return;
      if (
        next.catalog_generation !== previous.catalog_generation ||
        JSON.stringify(next.entry_support) !==
          JSON.stringify(previous.entry_support)
      ) {
        throw new ApiError(409, "Reference catalog changed", {
          code: "reference_catalog_cursor_invalid",
        });
      }
      const groups = previous.groups.map((group, index) => {
        const incoming = next.groups[index];
        if (incoming.id !== group.id || incoming.error !== group.error)
          throw new ApiError(409, "Reference catalog changed", {
            code: "reference_catalog_cursor_invalid",
          });
        const ids = new Set(group.items.map(referenceCatalogItemIdentity));
        if (
          incoming.items.some((item) =>
            ids.has(referenceCatalogItemIdentity(item)),
          )
        )
          throw new ApiError(502, "Overlapping catalog page", {
            code: "reference_catalog_response_invalid",
          });
        return { ...group, items: [...group.items, ...incoming.items] };
      });
      if (next.next_cursor === previous.next_cursor)
        throw new ApiError(502, "Repeated catalog cursor", {
          code: "reference_catalog_response_invalid",
        });
      const data = { ...next, groups };
      current.data = data;
      setState({
        key,
        status: "loaded",
        data,
        error: null,
        loadingMore: false,
      });
    } catch (error) {
      if (current.controller.signal.aborted || active.current !== current)
        return;
      current.data = null;
      setState({
        key,
        status: "error",
        data: null,
        error: failure(error),
        loadingMore: false,
      });
    } finally {
      current.loadingMore = false;
    }
  };

  return {
    status: visible.status,
    data: visible.data,
    error: visible.error,
    loadingMore: visible.loadingMore,
    loadMore,
    retry: () => {
      active.current?.controller.abort();
      if (active.current) active.current.data = null;
      setState({ ...EMPTY, key, status: key ? "loading" : "idle" });
      setReload((value) => value + 1);
    },
  };
}
