"use client";

import type { ReactNode } from "react";

import { ChevronDown, ChevronRight } from "@/components/icons";
import styles from "./section-heading.module.css";

/**
 * C5 · section heading: 13/650 title, 12 ink3 count, at most one trailing text action. A collapsible
 * heading is one 28-high button with a leading line chevron (down open / right closed) and
 * aria-expanded; never ▶ characters or trailing arrows.
 */
export function SectionHeading({
  title,
  count,
  icon,
  collapsible = false,
  open = true,
  onToggle,
  action,
  controls,
  id,
  level = 3,
}: {
  title: ReactNode;
  count?: ReactNode;
  icon?: ReactNode;
  collapsible?: boolean;
  open?: boolean;
  onToggle?: () => void;
  action?: ReactNode;
  /** ID of the collapsible content. */
  controls?: string;
  id?: string;
  level?: 2 | 3 | 4;
}) {
  const Heading = `h${level}` as const;
  const label = (
    <>
      {icon ? <span className={styles.icon} aria-hidden>{icon}</span> : null}
      <Heading className={styles.title} id={id}>
        {title}
        {count !== undefined && count !== null && count !== "" ? <span className={styles.count}>{count}</span> : null}
      </Heading>
    </>
  );
  return (
    <div className={styles.heading}>
      {collapsible ? (
        <button type="button" className={styles.toggle} aria-expanded={open} aria-controls={controls} onClick={onToggle}>
          <span className={styles.chevron} aria-hidden>{open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</span>
          {label}
        </button>
      ) : label}
      {action}
    </div>
  );
}

/** C5 + content: a section with consistent 10px spacing. */
export function CanvasSection({ children, label, labelledBy }: { children: ReactNode; label?: string; labelledBy?: string }) {
  return <section className={styles.section} aria-label={labelledBy ? undefined : label} aria-labelledby={labelledBy}>{children}</section>;
}

/** C6 · setting row (switches and segments apply immediately; text fields use the save bar). */
export function FieldRow({ title, description, control, titleId, className, help }: { title: ReactNode; help?: ReactNode; description?: ReactNode; control: ReactNode; titleId?: string; className?: string }) {
  return (
    <div className={className ? `${styles.row} ${className}` : styles.row}>
      <span className={styles.rowText}>
        <span className={styles.labelRow}><span className={styles.rowTitle} id={titleId}>{title}</span>{help}</span>
        {description ? <span className={styles.rowDescription}>{description}</span> : null}
      </span>
      <span className={styles.rowControl}>{control}</span>
    </div>
  );
}
