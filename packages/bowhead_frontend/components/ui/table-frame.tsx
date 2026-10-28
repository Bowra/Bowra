import type { CSSProperties, ReactNode } from "react";

/**
 * List/table frame: on narrow screens only the frame scrolls horizontally, preventing tables from widening the page.
 *
 * `ui-table-frame` gets its rules from `styles/ui-refresh.css` (Appearance Batch Four: content-surface background,
 * 0.5px hairline frame, `--r-4` radius) on top of `settings-table-frame` and caller classes.
 */
export function TableFrame({
  children,
  style,
  className,
}: {
  children: ReactNode;
  style?: CSSProperties;
  className?: string;
}) {
  return (
    <div className={`settings-table-frame ui-table-frame${className ? ` ${className}` : ""}`} style={style}>
      {children}
    </div>
  );
}
