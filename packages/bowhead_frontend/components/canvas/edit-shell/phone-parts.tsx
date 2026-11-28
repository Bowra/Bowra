"use client";

/**
 * The editors' phone frame (canvas GPhoneBar, GPhoneSave, G9, WP1–WP5): a top bar with the file, its
 * state and 退出编辑, and a save bar whose first row names the unsaved changes and opens 修改记录
 * as a sheet. Phones keep this bar at the bottom instead of the desktop top-bar save controls.
 */

import type { ReactNode } from "react";
import { useTranslations } from "next-intl";
import {
  Check as LucideCheck,
  ChevronUp as LucideChevronUp,
  CircleAlert as LucideCircleAlert,
  LoaderCircle as LucideLoaderCircle,
  X as LucideX,
} from "lucide-react";

import { LucideGlyph } from "@/components/icon-refresh";
import { Button } from "@/components/ui/button";
import type { ShellButton, ShellStatus } from "./editor-shell";
import styles from "./phone-parts.module.css";

export function PhoneTopBar({ filename, status, onExit, onCloseFullView }: {
  filename: string;
  status: ShellStatus;
  onExit: () => void;
  onCloseFullView?: () => void;
}) {
  const t = useTranslations("edit_shell");
  const icon = status.icon === "saving" ? LucideLoaderCircle : status.icon === "saved" ? LucideCheck : status.icon === "error" ? LucideCircleAlert : null;
  return (
    <header className={styles.topBar}>
      <div className={styles.fileText}>
        <h2 className={styles.fileName} title={filename}>{filename}</h2>
        <p className={styles.status} role="status" data-tone={status.tone}>
          {status.icon === "dirty" ? <span className={styles.statusDot} aria-hidden="true" /> : null}
          {icon ? <LucideGlyph icon={icon} size={14} aria-hidden="true" className={status.icon === "saving" ? styles.spin : undefined} /> : null}
          <span className={styles.statusText}>{status.text}</span>
        </p>
      </div>
      <Button variant="plain" className={styles.touch} onClick={onExit}>{t("exit")}</Button>
      {onCloseFullView ? (
        <button type="button" className={`v2-icon-btn ${styles.iconTouch}`} aria-label={t("close_full_view")} title={t("close_full_view")} onClick={onCloseFullView}>
          <LucideGlyph icon={LucideX} size={16} aria-hidden="true" />
        </button>
      ) : null}
    </header>
  );
}

export interface PhoneSaveBarProps {
  /** 「3 处未保存 · 查看修改记录」: what is unsaved, opening 修改记录. */
  summary?: { lead: string; tone: "warning" | "danger"; onOpen: () => void };
  /** A failed save or an invalid change, said under the summary. */
  message?: ReactNode;
  discard?: ShellButton & { danger?: boolean };
  primary?: ShellButton;
  /** After a save: what was saved, and 继续修改. */
  saved?: { text: string; tone: "positive" | "danger"; action?: ShellButton };
}

export function PhoneSaveBar({ summary, message, discard, primary, saved }: PhoneSaveBarProps) {
  const t = useTranslations("edit_shell");
  if (!summary && !saved && !message) return null;
  return (
    <div className={styles.saveBar} role="region" aria-label={t("phone_save_region")}>
      {saved ? (
        <>
          <p className={styles.savedLine} role="status" data-tone={saved.tone}>
            <LucideGlyph icon={saved.tone === "danger" ? LucideCircleAlert : LucideCheck} size={18} aria-hidden="true" />
            <span>{saved.text}</span>
          </p>
          {saved.action ? (
            <Button className={styles.touch} data-shell-action="continue" onClick={saved.action.onClick} loading={saved.action.busy} disabled={saved.action.disabled}>
              {saved.action.label}
            </Button>
          ) : null}
        </>
      ) : (
        <>
          {summary ? (
            <button type="button" className={styles.summary} data-tone={summary.tone} aria-haspopup="dialog"
              aria-label={t("phone_summary_label", { lead: summary.lead })} onClick={summary.onOpen}>
              <span className={styles.summaryDot} aria-hidden="true" />
              <span className={styles.summaryText}>{summary.lead}{t("phone_summary_separator")}{t("phone_view_changes")}</span>
              <LucideGlyph icon={LucideChevronUp} size={18} aria-hidden="true" />
            </button>
          ) : null}
          {message ? <p className={styles.message} role="alert">{message}</p> : null}
          {discard || primary ? (
            <div className={styles.actions}>
              {discard ? (
                <Button className={styles.touch} variant={discard.danger ? "danger" : "secondary"} disabled={discard.disabled} onClick={discard.onClick}>{discard.label}</Button>
              ) : null}
              {primary ? (
                <Button className={styles.touch} variant="primary" disabled={primary.disabled} loading={primary.busy} title={primary.title} onClick={primary.onClick}>{primary.label}</Button>
              ) : null}
            </div>
          ) : null}
        </>
      )}
    </div>
  );
}
