"use client";

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type FocusEvent,
  type ReactNode,
  type RefObject,
} from "react";
import {
  defaultRangeExtractor,
  useVirtualizer,
} from "@tanstack/react-virtual";

import type { SessionItem } from "@/lib/types";

export const RECOVERY_VIRTUALIZATION_THRESHOLD = 50;

export interface RecoveryRenderGroup {
  id: string;
  label: string;
  items: SessionItem[];
}

type RecoveryVirtualRow =
  | {
      key: string;
      kind: "heading";
      groupId: string;
      label: string;
    }
  | {
      key: string;
      kind: "session";
      groupId: string;
      item: SessionItem;
    };

/** Move focus to a session row; a new `token` repeats the request for the same id. */
export interface RecoveryFocusRequest {
  sessionId: string;
  token: number;
}

interface RecoveryRenderItemOptions {
  onActionMenuOpenChange?: (open: boolean) => void;
}

interface RecoveryGroupsProps {
  groups: RecoveryRenderGroup[];
  scrollRef: RefObject<HTMLDivElement | null>;
  renderItem: (
    item: SessionItem,
    options?: RecoveryRenderItemOptions,
  ) => ReactNode;
  /** Focus this session's row once it is rendered, scrolling a virtualized list to it first. */
  focusRequest?: RecoveryFocusRequest | null;
  /** The request was carried out or dropped; the owner clears it so a remount never replays it. */
  onFocusRequestSettled?: () => void;
}

/** A focus request the row never satisfies (it left the list, the page did not render) is dropped after this. */
export const RECOVERY_FOCUS_REQUEST_TIMEOUT_MS = 1_000;

export function RecoveryGroups({
  groups,
  scrollRef,
  renderItem,
  focusRequest = null,
  onFocusRequestSettled,
}: RecoveryGroupsProps) {
  const rows = useMemo<RecoveryVirtualRow[]>(
    () =>
      groups.flatMap((group) => [
        {
          key: `heading:${group.id}`,
          kind: "heading" as const,
          groupId: group.id,
          label: group.label,
        },
        ...group.items.map((item) => ({
          key: `session:${item.id}`,
          kind: "session" as const,
          groupId: group.id,
          item,
        })),
      ]),
    [groups],
  );
  const sessionCount = rows.length - groups.length;
  const virtualized =
    sessionCount > RECOVERY_VIRTUALIZATION_THRESHOLD;
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [scrollMargin, setScrollMargin] = useState(0);
  const [focusedIndex, setFocusedIndex] = useState<number | null>(
    null,
  );
  const [openMenuIndex, setOpenMenuIndex] = useState<number | null>(
    null,
  );
  const rowVirtualizer = useVirtualizer({
    count: virtualized ? rows.length : 0,
    getScrollElement: () => scrollRef.current,
    estimateSize: (index) =>
      rows[index]?.kind === "heading" ? 36 : 54,
    getItemKey: (index) => rows[index]?.key ?? index,
    overscan: 8,
    scrollMargin,
    rangeExtractor: (range) => {
      const retained = new Set(defaultRangeExtractor(range));
      if (focusedIndex !== null && focusedIndex < rows.length) {
        retained.add(focusedIndex);
      }
      if (openMenuIndex !== null && openMenuIndex < rows.length) {
        retained.add(openMenuIndex);
      }
      return [...retained].sort((a, b) => a - b);
    },
  });

  // Focus a requested row (the first session of an older page). A virtualized list renders only its window, so the
  // row is scrolled into it and kept rendered (`focusedIndex` is retained by the range extractor) before focusing.
  // The request is dropped when the person moves focus themselves, or after a timeout if the row never renders.
  const [pendingFocus, setPendingFocus] = useState<RecoveryFocusRequest | null>(null);
  const handledToken = useRef<number | null>(null);
  if (focusRequest && handledToken.current !== focusRequest.token) {
    handledToken.current = focusRequest.token;
    setPendingFocus(focusRequest);
  }
  const settledRef = useRef(onFocusRequestSettled);
  settledRef.current = onFocusRequestSettled;
  const settleFocus = useCallback(() => {
    setPendingFocus(null);
    settledRef.current?.();
  }, []);
  useEffect(() => {
    if (!pendingFocus) return;
    const timer = window.setTimeout(settleFocus, RECOVERY_FOCUS_REQUEST_TIMEOUT_MS);
    // Focus moved by the person (or anything else) wins over the pending request.
    const onFocusIn = () => settleFocus();
    document.addEventListener("focusin", onFocusIn);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("focusin", onFocusIn);
    };
  }, [pendingFocus, settleFocus]);
  const virtualItems = rowVirtualizer.getVirtualItems();
  useLayoutEffect(() => {
    if (!pendingFocus) return;
    const index = rows.findIndex(
      (row) => row.kind === "session" && row.item.id === pendingFocus.sessionId,
    );
    if (index < 0) return;
    const target = Array.from(
      scrollRef.current?.querySelectorAll<HTMLElement>("[data-session-id]") ?? [],
    )
      .find((element) => element.dataset.sessionId === pendingFocus.sessionId)
      ?.querySelector<HTMLElement>(".unified-work-item-main");
    if (target) {
      // The plain list keeps its scroll position (spec 4a); the virtual one already scrolled the row into view.
      target.focus({ preventScroll: true });
      settleFocus();
      return;
    }
    if (!virtualized) return;
    setFocusedIndex(index);
    rowVirtualizer.scrollToIndex(index, { align: "auto" });
  }, [pendingFocus, rows, rowVirtualizer, scrollRef, settleFocus, virtualItems, virtualized]);

  useLayoutEffect(() => {
    if (!virtualized) return;
    const updateViewportLayout = () => {
      const nextScrollMargin = containerRef.current?.offsetTop ?? 0;
      setScrollMargin((current) =>
        current === nextScrollMargin ? current : nextScrollMargin,
      );
      // The navigation above this list can expand or collapse without the
      // session groups changing. Re-measure the virtualizer so its viewport
      // and absolute row positions follow that layout change.
      rowVirtualizer.measure();
    };
    updateViewportLayout();

    const scrollElement = scrollRef.current;
    const resizeObserver =
      typeof ResizeObserver === "undefined" || !scrollElement
        ? null
        : new ResizeObserver(updateViewportLayout);
    if (scrollElement) resizeObserver?.observe(scrollElement);

    window.addEventListener("resize", updateViewportLayout);
    return () => {
      resizeObserver?.disconnect();
      window.removeEventListener("resize", updateViewportLayout);
    };
  }, [rowVirtualizer, scrollRef, virtualized]);

  if (!virtualized) {
    return groups.map((group) => (
      <div className="unified-work-group" key={group.id}>
        <h3>{group.label}</h3>
        {group.items.map((item) => renderItem(item))}
      </div>
    ));
  }

  const clearFocusedIndex = (
    event: FocusEvent<HTMLDivElement>,
    index: number,
  ) => {
    if (
      event.relatedTarget instanceof Node &&
      event.currentTarget.contains(event.relatedTarget)
    ) {
      return;
    }
    setFocusedIndex((current) =>
      current === index ? null : current,
    );
  };

  return (
    <div
      ref={containerRef}
      className="unified-work-virtual-list"
      style={{ height: rowVirtualizer.getTotalSize() }}
      data-virtualized="true"
      data-session-count={sessionCount}
      data-scroll-margin={scrollMargin}
    >
      {rowVirtualizer.getVirtualItems().map((virtualRow) => {
        const row = rows[virtualRow.index];
        if (!row) return null;
        return (
          <div
            key={row.key}
            ref={rowVirtualizer.measureElement}
            className="unified-work-virtual-row"
            data-index={virtualRow.index}
            style={{
              transform: `translateY(${virtualRow.start - scrollMargin}px)`,
            }}
            onFocusCapture={() => setFocusedIndex(virtualRow.index)}
            onBlurCapture={(event) =>
              clearFocusedIndex(event, virtualRow.index)
            }
          >
            {row.kind === "heading" ? (
              <div className="unified-work-group">
                <h3>{row.label}</h3>
              </div>
            ) : (
              renderItem(row.item, {
                onActionMenuOpenChange: (open) =>
                  setOpenMenuIndex((current) =>
                    open
                      ? virtualRow.index
                      : current === virtualRow.index
                        ? null
                        : current,
                  ),
              })
            )}
          </div>
        );
      })}
    </div>
  );
}
