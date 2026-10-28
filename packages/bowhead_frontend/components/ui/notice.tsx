import type { CSSProperties, ReactNode } from "react";

/** Inline notice; `error` uses an assertive alert, other tones a polite status. */
export function Notice({
  tone = "info",
  children,
  style,
  className,
}: {
  tone?: "info" | "success" | "error" | "muted";
  children: ReactNode;
  style?: CSSProperties;
  /** Classes appended after settings-notice (legacy classes moved from inline styles, UI-refresh hooks). */
  className?: string;
}) {
  const isError = tone === "error";
  return (
    <div
      className={`settings-notice settings-notice--${tone}${className ? ` ${className}` : ""}`}
      role={isError ? "alert" : "status"}
      aria-live={isError ? "assertive" : "polite"}
      style={style}
    >
      {children}
    </div>
  );
}
