"use client";

import { useEffect } from "react";

type FocusedItemScrollOpts<T> = {
  focusId: string | null;
  items: T[] | null;
  loading?: boolean;
  dataAttribute: string;
  searchValue: string;
  getItemId: (item: T) => string;
  setSearchValue: (value: string) => void;
  setSelectedItem: (item: T) => void;
};

export function useFocusedItemScroll<T>({
  focusId,
  items,
  loading = false,
  dataAttribute,
  searchValue,
  getItemId,
  setSearchValue,
  setSelectedItem,
}: FocusedItemScrollOpts<T>): void {
  useEffect(() => {
    if (!focusId || loading || !items) return;
    const match = items.find((item) => getItemId(item) === focusId);
    if (!match) {
      if (searchValue !== focusId) setSearchValue(focusId);
      return;
    }

    setSelectedItem(match);
    requestAnimationFrame(() => {
      document
        .querySelector(`[${dataAttribute}="${escapeCssAttributeValue(focusId)}"]`)
        ?.scrollIntoView({ block: "center", behavior: "smooth" });
    });
  }, [
    dataAttribute,
    focusId,
    getItemId,
    items,
    loading,
    searchValue,
    setSearchValue,
    setSelectedItem,
  ]);
}

function escapeCssAttributeValue(value: string): string {
  const css = typeof CSS !== "undefined" ? CSS : null;
  if (css && typeof css.escape === "function") return css.escape(value);
  return value.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
}
