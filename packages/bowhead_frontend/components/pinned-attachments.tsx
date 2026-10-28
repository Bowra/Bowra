"use client";

/**
 * PinnedAttachmentsStrip — list this session's pinned attachments above the composer.
 * Click 🗑 to unpin (later turns no longer inject the stub into Lead's system prompt); retain the artifact itself.
 * Pinning affects session context and does not transfer artifact ownership.
 *
 * chat-file-download #2: chips are now `AttachmentChip` (same component +
 * download state machine as the sent-message attachment strip / ChatInlineFile)
 * instead of bespoke read-only markup — this strip was previously the one
 * surface with zero download affordance at all. Click chip body → download,
 * click × → unpin; independent affordances on the same chip (× stopPropagation-
 * guarded in AttachmentChip).
 */

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";

import { appPath } from "@/lib/base-path";
import {
  unpinAttachmentFromSession,
  fetchPinnedAttachments,
  type PinnedAttachmentDTO,
} from "@/lib/api";
import { AttachmentChip, type AttachmentItem } from "@/components/chat/attachment-chip";
import { CharIcon } from "@/components/char-icon";

interface Props {
  sessionId: string | null;
  /** Any change triggers a refetch—the parent may pass turnId or a bump counter;
   *  turnId changes when a new turn completes, letting newly uploaded attachment stubs appear in the chip list immediately. */
  refreshKey?: string | number;
}


export function PinnedAttachmentsStrip({ sessionId, refreshKey = 0 }: Props) {
  const t = useTranslations("pinned");
  const [items, setItems] = useState<PinnedAttachmentDTO[]>([]);
  const [loading, setLoading] = useState(false);
  const [collapsed, setCollapsed] = useState(false);

  const refresh = useCallback(async () => {
    if (!sessionId) {
      setItems([]);
      return;
    }
    setLoading(true);
    try {
      const got = await fetchPinnedAttachments(sessionId);
      setItems(got);
    } catch (e) {
      console.warn("pinned fetch failed:", e);
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    void refresh();
  }, [refresh, refreshKey]);

  const onUnpin = async (id: string) => {
    if (!sessionId) return;
    // Optimistic: remove from the UI immediately; restore on failure
    const snapshot = items;
    setItems((xs) => xs.filter((x) => x.id !== id));
    try {
      await unpinAttachmentFromSession(sessionId, id);
    } catch (e) {
      console.warn("unpin failed:", e);
      setItems(snapshot);
    }
  };

  if (!sessionId || items.length === 0) {
    return null;
  }

  return (
    <div
      className="v2-pinned-strip"
      style={{
        display: "flex",
        alignItems: "center",
        // ψ-G r2: alignContent (not alignItems) controls multi-row cross-axis
        // gap; keeps single-row center alignment (Eng review P1-6).
        alignContent: "flex-start",
        gap: 6,
        padding: "6px 10px",
        flexWrap: "wrap",
        background: "var(--c-surface-subtle, var(--c-surface))",
        // ψ-G r2: scroll when many pinned (54 pin case); iOS momentum scroll.
        maxHeight: 120,
        overflowY: "auto",
        WebkitOverflowScrolling: "touch",
      }}
    >
      <button
        type="button"
        onClick={() => setCollapsed((v) => !v)}
        title={collapsed ? t("expand") : t("collapse")}
        style={{
          all: "unset",
          fontSize: "0.6875rem",
          color: "var(--c-ink-3)",
          cursor: "pointer",
          fontFamily: "var(--font-v2-mono)",
          marginRight: 4,
        }}
      >
        <CharIcon ch="📌" /> {t("count_pinned", { count: items.length })} <CharIcon ch={collapsed ? "▸" : "▾"} />
      </button>
      {!collapsed &&
        items.map((it) => {
          const item: AttachmentItem = {
            localId: it.id,
            fileId: it.id,
            filename: it.filename,
            size: it.size,
            mime: it.mime,
            status: "done",
            downloadUrl: appPath(`/api/v1/files/${it.id}`),
          };
          return (
            <AttachmentChip
              key={it.id}
              item={item}
              onRemove={() => onUnpin(it.id)}
              removeLabel={t("unpin_title")}
            />
          );
        })}
      {loading && (
        <span style={{ fontSize: "0.625rem", color: "var(--c-ink-3)", fontFamily: "var(--font-v2-mono)" }}>
          {t("loading")}
        </span>
      )}
    </div>
  );
}
