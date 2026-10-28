"use client";

import type { ReactNode } from "react";
import styles from "./canvas-object.module.css";

/** Shows its content only on narrow canvases, where the toolbar row gives way to one object row (C1/C2). */
export function NarrowStatus({ children }: { children: ReactNode }) {
  return <span className={styles.narrowStatus}>{children}</span>;
}
