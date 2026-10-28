"use client";

import { useTranslations } from "next-intl";
import React, { useEffect, useState } from "react";
import type { ReportSection } from "@/lib/types";
import { authorizeDatarcCardUrl } from "@/lib/api";

type E = Extract<ReportSection, { kind: "datarc_iframe" }>;

export function DatarcIframeSection({ data }: { data: E }) {
  const t = useTranslations("canvas");
  const [authed, setAuthed] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    authorizeDatarcCardUrl(data.cardUrl)
      .then((u) => !cancelled && setAuthed(u))
      .catch(() => !cancelled && setAuthed(data.cardUrl));
    return () => {
      cancelled = true;
    };
  }, [data.cardUrl]);

  if (!data.cardUrl) return null;
  // Wait for /authorize before mounting the iframe — an empty src reloads the whole page and triggers a console warning
  if (!authed) {
    return (
      <div className="v2-datarc-iframe-wrap">
        {data.title ? <div className="v2-datarc-iframe-head">{data.title}</div> : null}
        <div
          className="v2-datarc-iframe-loading"
          style={{
            width: "100%",
            height: data.height ?? 420,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            color: "var(--c-ink-3)",
            fontSize: "0.8125rem",
            border: "1px dashed var(--c-line)",
            borderRadius: 8,
          }}
        >
          {t("datarc_authorizing")}
        </div>
      </div>
    );
  }

  return (
    <div className="v2-datarc-iframe-wrap">
      {data.title ? <div className="v2-datarc-iframe-head">{data.title}</div> : null}
      <iframe
        src={authed}
        title={data.title || "datarc card"}
        style={{
          width: "100%",
          height: data.height ?? 420,
          border: "none",
          borderRadius: 8,
          background: "transparent",
        }}
      />
    </div>
  );
}
