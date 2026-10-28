"use client";

import type { ReactNode } from "react";
import { useTranslations } from "next-intl";

import { Check, AlertCircle, CircleDot, Eye, MessageCircleQuestion } from "@/components/icons";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import styles from "./canvas-object.module.css";

/**
 * C11 · object-level status vocabulary. Every word maps to one tone, one icon and at most one tab
 * marker (C1), so the badge in the toolbar and the marker on the tab always agree.
 */
export type CanvasObjectStatus =
  | "generating"
  | "review"
  | "needs_input"
  | "publishing"
  | "published"
  | "failed"
  | "cancelled"
  | "draft"
  | "creating"
  | "created"
  | "read_only"
  | "unconfirmed"
  | "enabled"
  | "disabled";

/** C1 · tab markers. Normal and published objects carry no marker. */
export type CanvasObjectMarker = "dirty" | "running" | "attention" | "failed";

interface StatusDefinition {
  tone: BadgeTone;
  icon: "spinner" | "check" | "attention" | "question" | "alert" | "eye" | null;
  marker: CanvasObjectMarker | null;
}

const STATUS: Record<CanvasObjectStatus, StatusDefinition> = {
  generating: { tone: "accent", icon: "spinner", marker: "running" },
  review: { tone: "warning", icon: "attention", marker: "attention" },
  needs_input: { tone: "warning", icon: "question", marker: "attention" },
  publishing: { tone: "accent", icon: "spinner", marker: "running" },
  published: { tone: "positive", icon: "check", marker: null },
  failed: { tone: "danger", icon: "alert", marker: "failed" },
  cancelled: { tone: "neutral", icon: null, marker: null },
  draft: { tone: "neutral", icon: null, marker: null },
  creating: { tone: "accent", icon: "spinner", marker: "running" },
  created: { tone: "positive", icon: "check", marker: null },
  read_only: { tone: "neutral", icon: "eye", marker: null },
  unconfirmed: { tone: "warning", icon: null, marker: "attention" },
  enabled: { tone: "positive", icon: null, marker: null },
  disabled: { tone: "neutral", icon: null, marker: null },
};

/** The tab marker that corresponds to an object status (one table for C1 and C11). */
export function objectStatusMarker(status: CanvasObjectStatus | null | undefined): CanvasObjectMarker | null {
  return status ? STATUS[status].marker : null;
}

export function Spinner({ size = "small", label }: { size?: "small" | "large"; label?: string }) {
  return (
    <span
      className={styles.spinner}
      data-size={size}
      role={label ? "status" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
    />
  );
}

function StatusIcon({ icon }: { icon: StatusDefinition["icon"] }): ReactNode {
  if (icon === "spinner") return <Spinner />;
  if (icon === "check") return <Check size={14} aria-hidden />;
  if (icon === "attention") return <CircleDot size={14} aria-hidden />;
  if (icon === "question") return <MessageCircleQuestion size={14} aria-hidden />;
  if (icon === "alert") return <AlertCircle size={14} aria-hidden />;
  if (icon === "eye") return <Eye size={14} aria-hidden />;
  return null;
}

/** C11 · semantic light background + icon + fixed word. */
export function ObjectStatusBadge({ status, label }: { status: CanvasObjectStatus; label?: string }) {
  const t = useTranslations("canvas_object.status");
  const definition = STATUS[status];
  return (
    <Badge tone={definition.tone} size="sm" role="status">
      <StatusIcon icon={definition.icon} />
      {label ?? t(status)}
    </Badge>
  );
}

/** C1 marker graphic; the accessible word lives on the tab name. */
export function ObjectMarker({ marker }: { marker: CanvasObjectMarker }) {
  if (marker === "dirty") return <span className={styles.markerDirty} aria-hidden />;
  return (
    <span
      className={`${styles.marker} ${marker === "running" ? styles.markerRunning : marker === "attention" ? styles.markerAttention : styles.markerFailed}`}
      aria-hidden
    >
      {marker === "running" ? <Spinner /> : marker === "attention" ? <CircleDot size={14} /> : <AlertCircle size={14} />}
    </span>
  );
}
