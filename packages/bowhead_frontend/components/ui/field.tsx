import type { CSSProperties, ReactNode } from "react";

import { Badge } from "@/components/ui/badge";
import styles from "./section-heading.module.css";

/** Form field: label + control + hint/error. Controls use `fieldDescribedBy` to associate hints and errors. */
export function Field({
  id,
  label,
  hint,
  error,
  children,
  style,
  className,
  suggestion,
  help,
}: {
  id: string;
  label: ReactNode;
  help?: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  children: ReactNode;
  style?: CSSProperties;
  /** Classes appended after settings-field (legacy classes moved from inline styles, UI-refresh hooks). */
  className?: string;
  /**
   * C6 · the field has a pending AI suggestion: accent outline plus a “Suggested” badge; activating the
   * badge reveals the matching row in the AI suggestions section.
   */
  suggestion?: { label: string; onReveal: () => void };
}) {
  const hintId = hint ? `${id}-hint` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  return (
    <div className={["settings-field", className, suggestion ? styles.suggested : null].filter(Boolean).join(" ")} style={style}>
      {suggestion || help ? (
        <span className={styles.labelRow}>
          <label className="settings-field__label" htmlFor={id}>{label}</label>
          {help}
          {suggestion ? <SuggestionBadge label={suggestion.label} onReveal={suggestion.onReveal} /> : null}
        </span>
      ) : (
        <label className="settings-field__label" htmlFor={id}>
          {label}
        </label>
      )}
      {children}
      {hint ? (
        <div id={hintId} className="settings-field__hint">
          {hint}
        </div>
      ) : null}
      {error ? (
        <div id={errorId} className="settings-field__error" role="alert">
          {error}
        </div>
      ) : null}
    </div>
  );
}

export function fieldDescribedBy(id: string, opts: { hint?: boolean; error?: boolean }) {
  return [
    opts.hint ? `${id}-hint` : null,
    opts.error ? `${id}-error` : null,
  ].filter(Boolean).join(" ") || undefined;
}

/** C6 · “Suggested” badge: reveals the matching row in the AI suggestions section. */
export function SuggestionBadge({ label, onReveal }: { label: string; onReveal: () => void }) {
  return (
    <button type="button" className={styles.suggestionBadge} onClick={onReveal}>
      <Badge tone="accent" size="sm">{label}</Badge>
    </button>
  );
}
