import type { ButtonHTMLAttributes } from "react";

/** `plain`: transparent, secondary-text color (text buttons such as Dismiss / Undo, Canvas Objects Spec). */
export type ButtonVariant = "primary" | "secondary" | "danger" | "ghost" | "plain";
export type ButtonSize = "sm" | "md" | "lg";

/**
 * Generic button. Appearance uses the `settings-button` class family; styles/ui-refresh.css renders the component design spec.
 *
 * - `lg`: same as `md` under legacy appearance (also carries `settings-button--md`); 32px high under the new appearance.
 * - `loading`: writes `aria-busy`; the new appearance shows a spinner before the text. The button remains focusable,
 *   with disabling determined by the caller's `disabled`.
 */
export function Button({
  variant = "secondary",
  size = "md",
  loading = false,
  className,
  type = "button",
  style,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant;
  size?: ButtonSize;
  loading?: boolean;
}) {
  return (
    <button
      {...props}
      type={type}
      aria-busy={loading ? true : props["aria-busy"]}
      className={[
        "settings-button",
        `settings-button--${variant}`,
        size === "lg" ? "settings-button--md settings-button--lg" : `settings-button--${size}`,
        className,
      ].filter(Boolean).join(" ")}
      style={style}
    />
  );
}
