"use client";

import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import { requestCanvasDataFocus } from "@/lib/canvas-workspace-focus";
import { useResultsWorkspaceCapabilitySnapshot } from "@/lib/use-results-workspace-capability";
import {
  Download as LucideDownload,
} from "lucide-react";
import { LucideGlyph } from "@/components/icon-refresh";
import { CharIcon } from "@/components/char-icon";

/**
 * multimodal-v1 — inline preview card for a generated **image deliverable**
 * (poster / banner from `image_generate`) inside a chat answer bubble.
 *
 * Single component = preview + download affordance (design ruling: no separate
 * file chip; the image IS the preview, the corner button is the download).
 * Backend shape (`_deliverable_inline_artifact` raster branch):
 *   { kind:"image", id, filename, mime, url (…?disposition=inline),
 *     download_url, alt, size_bytes }
 *
 * States: loading (skeleton) · loaded (img + corner download) · error
 * (degrades to a download-link row — the blob may still be fetchable as
 * attachment even when inline render fails).
 *
 * Multi-image turns (n≥2) stack vertically in array order (ChatBubbleNew
 * artifacts loop); max-height caps tall 9:16 posters on mobile (412px).
 */

export interface ChatInlineImageData {
  id: string;
  filename: string;
  mime?: string;
  url: string;
  downloadUrl: string;
  alt?: string;
  sizeBytes?: number;
}

export function ChatInlineImage({ data }: { data: ChatInlineImageData }) {
  const tAttach = useTranslations("attachment");
  const resultsT = useTranslations("results_workspace");
  const resultsWorkspaceEnabled = useResultsWorkspaceCapabilitySnapshot();
  const [state, setState] = useState<"loading" | "loaded" | "error">("loading");
  // URL change (hydrate replacement / multiple-image reuse) → reset loading state to prevent stale loaded/error.
  useEffect(() => {
    setState("loading");
  }, [data.url]);
  const alt = data.alt || data.filename || tAttach("image_alt_fallback");

  if (state === "error") {
    if (resultsWorkspaceEnabled) {
      return (
        <div
          className="chat-inline-image chat-inline-image--error"
          role="group"
          aria-label={data.filename}
        >
          <button
            type="button"
            className="chat-inline-image__error-open"
            onClick={() => requestCanvasDataFocus(data.id, {
              entryPoint: "chat",
            })}
          >
            <span aria-hidden="true"><CharIcon ch="🖼️" aria-hidden /></span>
            <span>{tAttach("image_preview_failed", { filename: data.filename })}</span>
            <span>{resultsT("open_object", { filename: data.filename })}</span>
          </button>
          <a
            href={data.downloadUrl}
            aria-label={tAttach("image_download", { filename: data.filename })}
            rel="noreferrer"
          >
            {tAttach("image_download", { filename: data.filename })}
          </a>
        </div>
      );
    }
    return (
      <a
        href={data.downloadUrl}
        rel="noreferrer"
        style={{
          display: "flex", alignItems: "center", gap: 8,
          margin: "10px 0 0", padding: "10px 14px",
          border: "1px solid var(--c-line)", borderRadius: 10,
          fontSize: "0.78125rem", color: "var(--c-ink-2)", textDecoration: "none",
          background: "var(--c-surface-2)",
        }}
      >
        <span aria-hidden="true"><CharIcon ch="🖼️" aria-hidden /></span>
        <span>{tAttach("image_preview_failed", { filename: data.filename })}</span>
      </a>
    );
  }

  const image = (
    // eslint-disable-next-line @next/next/no-img-element — same-origin authentication cookies
    // Artifact stream; bypass the next/image optimizer.
    <img
      src={data.url}
      alt={alt}
      loading="lazy"
      onLoad={() => setState("loaded")}
      onError={() => setState("error")}
      style={{
        display: "block",
        width: "100%",
        maxHeight: 420,
        objectFit: "contain",
        opacity: state === "loaded" ? 1 : 0,
        transition: "opacity 160ms ease",
        minHeight: state === "loaded" ? undefined : 160,
        background: "var(--c-surface-2)",
      }}
    />
  );

  return (
    <div
      className="chat-inline-image"
      style={{
        position: "relative",
        margin: "10px 0 0",
        border: "1px solid var(--c-line)",
        borderRadius: 10,
        overflow: "hidden",
        background: "var(--c-surface-2)",
        maxWidth: 480,
      }}
    >
      {state === "loading" ? (
        <div
          aria-hidden="true"
          style={{
            position: "absolute", inset: 0,
            display: "flex", alignItems: "center", justifyContent: "center",
            color: "var(--c-ink-3)", fontSize: "0.75rem",
            pointerEvents: "none",
          }}
        >
          {tAttach("image_loading")}
        </div>
      ) : null}
      {resultsWorkspaceEnabled ? (
        <button
          type="button"
          className="chat-inline-image__open"
          onClick={() => requestCanvasDataFocus(data.id, {
            entryPoint: "chat",
          })}
          aria-label={resultsT("open_object", { filename: data.filename })}
          title={resultsT("open_object", { filename: data.filename })}
        >
          {image}
        </button>
      ) : image}
      <a
        // ui-image-download: only appearance-refresh styles read this (P2 column material); when disabled, use the inline styles below.
        className="ui-image-download"
        href={data.downloadUrl}
        aria-label={tAttach("image_download", { filename: data.filename })}
        title={tAttach("image_download", { filename: data.filename })}
        rel="noreferrer"
        style={{
          position: "absolute", top: 8, right: 8,
          width: 40, height: 40,
          display: "flex", alignItems: "center", justifyContent: "center",
          borderRadius: 6,
          background: "color-mix(in srgb, var(--c-surface) 82%, transparent)",
          border: "1px solid var(--c-line)",
          color: "var(--c-ink-2)",
          textDecoration: "none",
          backdropFilter: "blur(4px)",
        }}
      >
        <LucideGlyph icon={LucideDownload} size={14} aria-hidden="true" />
      </a>
    </div>
  );
}
