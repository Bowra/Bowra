"use client";

import type { ReactNode } from "react";

import styles from "./canvas-object.module.css";

/**
 * C4 · object header: eyebrow (type icon + object type [+ format]), title 17/650 on one line with the
 * full name in the tooltip, and a subtitle with scale and origin. Never holds buttons or internal IDs.
 */
export function CanvasObjectHeader({
  icon,
  eyebrow,
  title,
  subtitle,
  titleId,
}: {
  icon: ReactNode;
  eyebrow: ReactNode;
  title: string;
  subtitle?: ReactNode;
  titleId?: string;
}) {
  return (
    <header className={styles.header}>
      <span className={styles.eyebrow}>{icon}{eyebrow}</span>
      <h2 id={titleId} className={styles.title} title={title}>{title}</h2>
      {subtitle ? <p className={styles.subtitle}>{subtitle}</p> : null}
    </header>
  );
}
