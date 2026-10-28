"use client";

/**
 * Switch (component unification P1, step 6, docs/design/组件统一-技术设计.md).
 *
 * `<button type="button" role="switch" aria-checked>`; the native button handles Space / Enter.
 * Legacy appearance adds no visuals: the caller supplies unchanged classes, inline styles and children (thumb span, etc.).
 * Appearance batch two: also add `ui-switch`; styles/ui-refresh.css applies the spec's
 * 34×20 track (first child is the thumb; without children, draw it on ::after).
 * Non-track switches (the Memory page's entire Auto Learn card) pass `appearance="none"`.
 *
 * ```tsx
 * <Switch checked={on} onCheckedChange={setOn} className="x-toggle" aria-label="…"><span /></Switch>
 * ```
 */
import { forwardRef, type ButtonHTMLAttributes, type MouseEvent } from "react";

export interface SwitchProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "role" | "onChange" | "type"> {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  /**
   * While saving: ignore toggles and set aria-busy / aria-disabled, but keep focusability. Using native disabled to indicate saving
   * blurs a switch just activated with Space, dropping focus to body and leaving the next keypress nowhere to go (real-browser observation, 2026-09-26).
   */
  busy?: boolean;
  /** `"track"` (default): add `ui-switch`, using the spec's track appearance; `"none"`: omit it. */
  appearance?: "track" | "none";
}

export const Switch = forwardRef<HTMLButtonElement, SwitchProps>(function Switch(
  { checked, onCheckedChange, onClick, busy = false, appearance = "track", className, ...rest },
  ref,
) {
  return (
    <button
      {...rest}
      className={appearance === "track" ? (className ? `ui-switch ${className}` : "ui-switch") : className}
      ref={ref}
      type="button"
      role="switch"
      aria-checked={checked}
      aria-busy={busy || undefined}
      aria-disabled={busy || rest["aria-disabled"] || undefined}
      onClick={(event: MouseEvent<HTMLButtonElement>) => {
        if (busy) {
          event.preventDefault();
          return;
        }
        onClick?.(event);
        if (!event.defaultPrevented) onCheckedChange(!checked);
      }}
    />
  );
});
