"use client";

/**
 * Pin / Unpin for a surface that lists results (Results, Files, Reports): pinned state from the shared list, the
 * request, its toast and the PRD §4.5 failure sentences in one place.
 */
import { useCallback, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { track } from "@/lib/metrics";
import {
  findResultPin,
  pinResult,
  resultPinFailure,
  resultPinLimit,
  unpinResult,
  useResultPins,
  type ResultPinKind,
} from "@/lib/result-pins";
import { toast } from "@/lib/use-toast";
import { withHttpFailureReason } from "@/lib/user-facing-error";

export type ResultPinSurface = "results" | "files" | "reports" | "sidebar";

export interface ResultPinActions {
  isPinned: (kind: ResultPinKind, id: string | null | undefined) => boolean;
  isBusy: (kind: ResultPinKind, id: string | null | undefined) => boolean;
  /** Menu label for the current state: Pin or Unpin. */
  label: (kind: ResultPinKind, id: string | null | undefined) => string;
  toggle: (kind: ResultPinKind, id: string) => Promise<void>;
}

export function useResultPinActions(surface: ResultPinSurface, { enabled = true }: { enabled?: boolean } = {}): ResultPinActions {
  const t = useTranslations("result_pins");
  const pins = useResultPins(undefined, { enabled });
  const [busyKeys, setBusyKeys] = useState<ReadonlySet<string>>(() => new Set());
  const busyRef = useRef(new Set<string>());

  const isPinned = useCallback(
    (kind: ResultPinKind, id: string | null | undefined) => Boolean(findResultPin(pins.pins, kind, id)),
    [pins.pins],
  );
  const isBusy = useCallback(
    (kind: ResultPinKind, id: string | null | undefined) => Boolean(id) && busyKeys.has(`${kind}:${id}`),
    [busyKeys],
  );
  const label = useCallback(
    (kind: ResultPinKind, id: string | null | undefined) => (isPinned(kind, id) ? t("unpin") : t("pin")),
    [isPinned, t],
  );

  const toggle = useCallback(
    async (kind: ResultPinKind, id: string) => {
      const key = `${kind}:${id}`;
      if (busyRef.current.has(key)) return;
      const existing = findResultPin(pins.pins, kind, id);
      const action = existing ? "unpin" : "pin";
      busyRef.current.add(key);
      setBusyKeys(new Set(busyRef.current));
      try {
        if (existing) await unpinResult(kind, existing.pin_id);
        else await pinResult(kind, id);
        track("bowhead_result_pin_total", { action, kind, surface });
        toast({ kind: "success", text: existing ? t("unpinned_toast") : t("pinned_toast") });
      } catch (error) {
        track("bowhead_result_pin_total", { action: `${action}_failed`, kind, surface });
        toast({ kind: "error", text: resultPinErrorText(t, error) });
      } finally {
        busyRef.current.delete(key);
        setBusyKeys(new Set(busyRef.current));
      }
    },
    [pins.pins, surface, t],
  );

  return { isPinned, isBusy, label, toggle };
}

/** The PRD §4.5 sentence for a refused Pin/Unpin. */
export function resultPinErrorText(
  t: ReturnType<typeof useTranslations<"result_pins">>,
  error: unknown,
): string {
  switch (resultPinFailure(error)) {
    case "gone":
      return t("error_gone");
    case "limit":
      return t("error_limit", { limit: resultPinLimit(error) });
    case "unavailable":
      return t("error_unavailable");
    default:
      return withHttpFailureReason(t("error_failed"), error);
  }
}
