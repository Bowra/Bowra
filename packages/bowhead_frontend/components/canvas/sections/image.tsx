"use client";

import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";
import { useArtifactHref } from "@/lib/canvas-pm-adapter/canvas-context";
import type { ReportSection } from "@/lib/types";
import {
  Download as LucideDownload,
} from "lucide-react";
import { LucideGlyph } from "@/components/icon-refresh";

/**
 * Image Section (multimodal-v1) — AI-generated image (poster / banner /
 * promo art from `image_generate` → `add_image`).
 *
 * - Loads via GET /api/v1/files/{artifact_id}?disposition=inline — raster
 *   whitelist (png/jpeg/webp) + nosniff enforced gateway-side; same-origin
 *   cookie auth, owner-checked.
 * - Card chrome follows html-artifact.tsx (head strip + body); image itself
 *   is read-only — caption/alt patchable only via canvas-writer tool.
 * - max-height keeps tall posters (9:16) from dominating the report column;
 *   `object-fit: contain` preserves aspect ratio.
 */

type E = Extract<ReportSection, { kind: "image" }>;

export function ImageSection({ data }: { data: E }) {
  const t = useTranslations("canvas");
  const [state, setState] = useState<"loading" | "loaded" | "error">("loading");
  // Artifact change (canvas-writer replaces the image) → reset loading state to avoid stale loaded/error.
  useEffect(() => {
    setState("loading");
  }, [data.artifact_id]);
  // content-permissions: report views use permission mediation (visible to shared viewers), otherwise /api/v1/files/{id}.
  const inlineUrl = useArtifactHref(data.artifact_id, { inline: true });
  const downloadUrl = useArtifactHref(data.artifact_id);
  const alt = data.alt || data.caption || t("image_alt_fallback");

  return (
    <div className="v2-image-section-wrap" data-export-render-state={state}>
      <div
        style={{
          position: "relative",
          border: "1px solid var(--c-line)",
          borderRadius: 8,
          overflow: "hidden",
          background: "var(--c-surface-2)",
        }}
      >
        {state === "error" ? (
          <div
            style={{
              padding: 28,
              display: "flex", alignItems: "center", justifyContent: "center",
              color: "var(--c-ink-3)", fontSize: "0.75rem",
            }}
          >
            {t("image_load_failed")} ·{" "}
            <a
              href={downloadUrl}
              style={{ color: "var(--c-ink-2)", marginLeft: 4 }}
              rel="noreferrer"
            >
              {t("try_download")}
            </a>
          </div>
        ) : (
          <>
            {state === "loading" ? (
              <div
                aria-hidden="true"
                style={{
                  position: "absolute", inset: 0,
                  display: "flex", alignItems: "center", justifyContent: "center",
                  color: "var(--c-ink-3)", fontSize: "0.75rem",
                }}
              >
                {t("loading")}
              </div>
            ) : null}
            {/* eslint-disable-next-line @next/next/no-img-element — streaming artifact
                inline resource (same-origin auth cookie), bypassing the next/image optimizer */}
            <img
              src={inlineUrl}
              alt={alt}
              loading="lazy"
              onLoad={() => setState("loaded")}
              onError={() => setState("error")}
              style={{
                display: "block",
                width: "100%",
                maxHeight: 560,
                objectFit: "contain",
                background: "var(--c-surface-2)",
                opacity: state === "loaded" ? 1 : 0,
                transition: "opacity 160ms ease",
                minHeight: state === "loaded" ? undefined : 180,
              }}
            />
            <a
              // ui-image-download: the refreshed styles restyle it (P2 bar material) over the inline base styles below.
              className="ui-image-download"
              href={downloadUrl}
              aria-label={t("download_image")}
              title={t("download_image")}
              rel="noreferrer"
              style={{
                position: "absolute", top: 8, right: 8,
                width: 28, height: 28,
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
          </>
        )}
      </div>
      {data.caption ? (
        <div
          style={{
            marginTop: 6,
            fontSize: "0.75rem",
            lineHeight: 1.5,
            color: "var(--c-ink-3)",
          }}
        >
          {data.caption}
        </div>
      ) : null}
    </div>
  );
}
