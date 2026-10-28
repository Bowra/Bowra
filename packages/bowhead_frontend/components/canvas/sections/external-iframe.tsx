"use client";

import { useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { embedFrame } from "@/lib/embed-frame";
import { safeHref } from "@/lib/safe-href";
import type { ReportSection } from "@/lib/types";
import { CharIcon, CharIconText } from "@/components/char-icon";
import { GoogleFileCard } from "@/components/canvas/sections/google-file-card";

/**
 * #125 Case A: External URL iframe embed.
 *
 * Only http(s) pages are framed, inside the shared embed sandbox (see
 * `embedFrame`). Sites with X-Frame-Options: DENY / CSP frame-ancestors fail
 * silently (white iframe) — the "open in new tab" link is the escape hatch.
 */

type E = Extract<ReportSection, { kind: "external_iframe" }>;

export function ExternalIframeSection({ data }: { data: E }) {
  // Files the Lead wrote to Google render as a card with a snapshot; the live preview loads on demand.
  if (data.provider === "google_workspace") return <GoogleFileCard data={data} />;
  return <PlainExternalIframe data={data} />;
}

function PlainExternalIframe({ data }: { data: E }) {
  const t = useTranslations("external_iframe");
  const [loadErr, setLoadErr] = useState(false);
  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const h = data.height ?? 420;
  const frame = embedFrame(data.url);
  const href = safeHref(data.url);

  return (
    <div className="v2-external-iframe-wrap" style={{
      border: "1px solid var(--c-line)", borderRadius: 8,
      overflow: "hidden",
    }}>
      <div style={{
        display: "flex", alignItems: "center",
        padding: "6px 12px",
        fontSize: "0.75rem", color: "var(--c-ink-2)",
        background: "var(--c-surface-2)",
        borderBottom: "1px solid var(--c-line)",
      }}>
        <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {data.title}
        </span>
        {href ? (
          <a
            href={href}
            target="_blank" rel="noreferrer"
            style={{ fontSize: "0.625rem", color: "var(--c-ink-3)", textDecoration: "none", marginLeft: 8 }}
            title={t("open_new_tab")}
          >
            <CharIcon ch="↗" />
          </a>
        ) : null}
      </div>
      {!frame || loadErr ? (
        <div style={{
          height: h, display: "flex", alignItems: "center", justifyContent: "center",
          background: "var(--c-surface)", color: "var(--c-ink-2)", fontSize: "0.75rem",
          textAlign: "center", padding: 16, flexDirection: "column", gap: 8,
        }}>
          <div>{frame ? t("blocked") : t("unsupported_url")}</div>
          {href ? (
            <a href={href} target="_blank" rel="noreferrer" style={{
              color: "var(--c-accent)",
            }}><CharIconText ch="→" at="end">{t("open_new_tab_link")}</CharIconText></a>
          ) : null}
        </div>
      ) : (
        <iframe
          ref={iframeRef}
          src={frame.src}
          title={data.title}
          sandbox={frame.sandbox}
          referrerPolicy="strict-origin-when-cross-origin"
          loading="lazy"
          onError={() => setLoadErr(true)}
          style={{
            width: "100%", height: h, border: "none",
            background: "white",
          }}
        />
      )}
    </div>
  );
}
