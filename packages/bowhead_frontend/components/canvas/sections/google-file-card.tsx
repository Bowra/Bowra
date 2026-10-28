"use client";

import { useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { ExternalLink, File, FileText, Presentation, Table } from "lucide-react";

import { ChatCard, ChatCardButton } from "@/components/ui/card";
import { Segment, SegmentedControl } from "@/components/ui/segmented-control";
import { embedFrame } from "@/lib/embed-frame";
import { Markdown } from "@/lib/md";
import { safeHref } from "@/lib/safe-href";
import type { ReportSection } from "@/lib/types";
import styles from "./google-file-card.module.css";

type Card = Extract<ReportSection, { kind: "external_iframe" }>;
type View = "snapshot" | "live";

const KIND_ICON = { document: FileText, spreadsheet: Table, presentation: Presentation } as const;

/**
 * A file the Lead wrote to Google (Canvas Objects Spec part 2 §11): an info ChatCard whose meta is the write time
 * and whose footer opens the file in Google in a new tab. Content comes in two layers so every browser sees
 * something:
 *
 * 1. default: the snapshot Bowhead read back right after the write (no third-party cookies needed);
 * 2. on demand: Google's live read-only preview (4:3, at most 420 high), which needs the browser's Google session
 *    inside the iframe — Chrome/Edge allow it, Safari/Firefox/Brave usually do not. A cross-origin frame reports
 *    "loaded" even when Google shows a sign-in or refusal page, so the card never treats loading as success: a
 *    hint under the frame always says what to do when nothing shows.
 */
export function GoogleFileCard({ data }: { data: Card }) {
  const t = useTranslations("google_preview");
  const locale = useLocale();
  const [view, setView] = useState<View>("snapshot");
  const live = view === "live";
  const href = safeHref(data.open_url ?? data.url);
  const frame = live ? embedFrame(data.url) : null;
  const kindLabel = t(data.file_kind ? `kind_${data.file_kind}` : "kind_unknown");
  const writtenAt = data.snapshot_at ? formatWriteTime(data.snapshot_at, locale) : null;
  const snapshot = data.snapshot;

  const openInGoogle = href ? (
    <ChatCardButton variant="secondary" size="md" icon={ExternalLink} href={href} target="_blank" rel="noopener noreferrer">
      {t("open_in_google")}<span className="ui-card__sr-only">{t("opens_new_tab")}</span>
    </ChatCardButton>
  ) : null;

  return (
    <ChatCard
      intent="info"
      data-testid="google-file-card"
      icon={data.file_kind ? KIND_ICON[data.file_kind] : File}
      eyebrow={kindLabel}
      meta={writtenAt}
      title={data.title}
      live="off"
      footerStart={(
        <>
          {!snapshot && !live ? (
            <ChatCardButton variant="secondary" size="md" onClick={() => setView("live")}>{t("live_preview")}</ChatCardButton>
          ) : null}
          {openInGoogle}
        </>
      )}
    >
      {snapshot ? (
        <div className={styles.controls}>
          <SegmentedControl value={view} onValueChange={(next: View) => setView(next)} aria-label={t("view_label")}>
            <Segment value="snapshot">{t("snapshot")}</Segment>
            <Segment value="live">{t("live_preview")}</Segment>
          </SegmentedControl>
          {!live && snapshot.format === "table" && snapshot.range ? <span className={styles.hint}>{t("range_label", { range: snapshot.range })}</span> : null}
          {!live && snapshot.format === "markdown" && snapshot.part === "written" ? <span className={styles.hint}>{t("written_label")}</span> : null}
        </div>
      ) : !live ? (
        <p className="ui-card__text">{t("no_snapshot")}</p>
      ) : null}
      {live ? (
        frame ? (
          <iframe
            className={styles.frame}
            src={frame.src}
            title={data.title}
            sandbox={frame.sandbox}
            referrerPolicy="strict-origin-when-cross-origin"
          />
        ) : null
      ) : snapshot?.format === "markdown" ? (
        <div className={styles.snapshot} data-testid="google-file-snapshot">
          <Markdown markdown={snapshot.content} allowImages={false} linkRel="noopener noreferrer" />
        </div>
      ) : snapshot?.format === "table" ? (
        <div className={styles.snapshot} data-testid="google-file-snapshot">
          <table className={styles.table}>
            <tbody>
              {snapshot.rows.map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {row.map((cell, cellIndex) => <td key={cellIndex}>{cell}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {live ? <p className={styles.hint} data-testid="google-live-hint">{t("live_preview_hint")}</p> : null}
      {!live && snapshot?.truncated ? <p className={styles.hint}>{t("snapshot_truncated")}</p> : null}
    </ChatCard>
  );
}

/** “10月5日 14:10” / “Oct 5, 14:10”: the write time in the card meta. */
function formatWriteTime(value: string, locale: string): string | null {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return new Intl.DateTimeFormat(locale, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
}
