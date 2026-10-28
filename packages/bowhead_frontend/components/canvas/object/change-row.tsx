"use client";

import { Fragment, useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";

import type { ChangeValue } from "@/lib/canvas-change-value";
import styles from "./canvas-object.module.css";

const LONG_TEXT = 160;

/** Renders one ChangeValue by kind (C7 value rules). */
export function ChangeValueView({ value }: { value: ChangeValue }) {
  const t = useTranslations("canvas_object");
  const [expanded, setExpanded] = useState(false);
  if (value.kind === "empty") return <span className={styles.valueEmpty}>{t("value_empty")}</span>;
  if (value.kind === "toggle") return <>{t(value.on ? "value_on" : "value_off")}</>;
  if (value.kind === "enum") return <>{value.label}</>;
  if (value.kind === "number") return <>{value.text}</>;
  if (value.kind === "list") return (
    <span className={styles.chips}>
      {value.items.map((item, index) => (
        <span key={`${item.label}-${index}`} className={styles.chip} data-change={item.change}>
          {item.change === "added" ? `+ ${item.label}` : item.label}
          {item.change ? <span className="sr-only">{t(item.change === "added" ? "value_added" : "value_removed")}</span> : null}
        </span>
      ))}
    </span>
  );
  if (value.kind === "diff") return (
    <span>
      {value.segments.map((segment, index) => segment.op === "equal" ? <span key={index}>{segment.text}</span>
        : segment.op === "insert" ? <ins key={index} className={styles.inserted}>{segment.text}</ins>
          : <del key={index} className={styles.deleted}>{segment.text}</del>)}
    </span>
  );
  const long = Array.from(value.text).length > LONG_TEXT || value.text.split("\n").length > 3;
  return (
    <>
      <span className={long && !expanded ? styles.valueClamp : undefined}>{value.text}</span>
      {long ? (
        <button type="button" className={styles.valueMore} aria-expanded={expanded} onClick={() => setExpanded((open) => !open)}>
          {t(expanded ? "value_collapse" : "value_expand")}
        </button>
      ) : null}
    </>
  );
}

export type ChangeRowState = "pending" | "accepted" | "dismissed" | "conflict" | "unsupported";

/**
 * C7 · change row: location + before / after (+ source) + optional actions. r3, hairline outline.
 * Values are rendered by type, never as JSON. Medium and narrow containers stack the pairs (already
 * the default single-column layout).
 */
export function ChangeRow({
  location,
  locationNote,
  badge,
  trailing,
  before,
  after,
  beforeLabel,
  strikeBefore = true,
  afterLabel,
  rows,
  state = "pending",
  selectable,
  actions,
  children,
  id,
}: {
  location: ReactNode;
  locationNote?: ReactNode;
  badge?: ReactNode;
  /** Right side of the location line (e.g. Undo / Restore on collapsed rows). */
  trailing?: ReactNode;
  before?: ChangeValue;
  after?: ChangeValue;
  beforeLabel?: ReactNode;
  /** Strike the “before” value (document changes); suggestions show the current value plainly. */
  strikeBefore?: boolean;
  afterLabel?: ReactNode;
  /** Extra labelled rows after the pair, such as the data source. */
  rows?: Array<{ label: ReactNode; value: ReactNode }>;
  state?: ChangeRowState;
  selectable?: { checked: boolean; onChange: (checked: boolean) => void; label: string; disabled?: boolean };
  actions?: ReactNode;
  children?: ReactNode;
  id?: string;
}) {
  const t = useTranslations("canvas_object");
  const hasPair = before !== undefined || after !== undefined || (rows?.length ?? 0) > 0;
  return (
    <div className={styles.changeRow} data-state={state} data-selectable={selectable ? "true" : undefined} id={id} tabIndex={id ? -1 : undefined}>
      {selectable ? (
        <input
          type="checkbox"
          className={styles.changeCheck}
          checked={selectable.checked}
          disabled={selectable.disabled}
          aria-label={selectable.label}
          onChange={(event) => selectable.onChange(event.target.checked)}
        />
      ) : null}
      <span className={styles.changeLocation}>
        {location}
        {locationNote ? <span className={styles.changeLocationNote}>{locationNote}</span> : null}
        {badge}
        {trailing ? <><span className={styles.changeSpacer} />{trailing}</> : null}
      </span>
      {hasPair ? (
        <dl className={styles.changeValues}>
          {before !== undefined ? <>
            <dt>{beforeLabel ?? t("before")}</dt>
            <dd className={before.kind === "list" || before.kind === "diff" ? undefined : strikeBefore ? styles.valueOld : styles.valueCurrent}><ChangeValueView value={before} /></dd>
          </> : null}
          {after !== undefined ? <>
            <dt>{afterLabel ?? t("after")}</dt>
            <dd className={styles.valueNew}><ChangeValueView value={after} /></dd>
          </> : null}
          {rows?.map((row, index) => <Fragment key={index}><dt>{row.label}</dt><dd>{row.value}</dd></Fragment>)}
        </dl>
      ) : null}
      {children}
      {actions ? <span className={styles.changeActions}>{actions}</span> : null}
    </div>
  );
}
