"use client";

/**
 * SegmentedControl (component unification P1 step 6, docs/design/组件统一-技术设计.md).
 *
 * `role="radiogroup"` + `role="radio"` (`aria-checked`). Handles behavior/aria only; supplies no visuals for legacy appearance:
 * callers pass classes, inline styles and children unchanged.
 * Appearance batch two: `appearance="segmented"` (SegmentedControl default) adds `ui-segmented` to the group;
 * `"cards"` (RadioGroup default) adds `ui-radio-cards`; styles/ui-refresh.css unifies them to the specification.
 * Callers supplying all visuals and opting out of unification pass `"none"`.
 *
 * ```tsx
 * <SegmentedControl value={mode} onValueChange={setMode} className="x-seg" aria-label="…">
 *   <Segment value="a" className={mode === "a" ? "on" : ""}>A</Segment>
 *   <Segment value="b" disabled>B</Segment>
 * </SegmentedControl>
 * ```
 *
 * Left/right (up/down for `orientation="vertical"`, all four for `"both"`) move and select, wrapping and skipping disabled items; Home/End.
 * Immediate server submission (share audience, member settings, etc.) uses `activation="manual"`: arrows only focus; Space / Enter / click select.
 * With no selected item (value matches no segment), Tab lands on the first enabled segment without selecting it.
 * Clicking any segment (including the selected one) calls `onValueChange`, matching the pre-migration onClick semantics.
 */
import {
  createContext,
  forwardRef,
  useContext,
  useMemo,
  type ButtonHTMLAttributes,
  type ForwardedRef,
  type HTMLAttributes,
  type MouseEvent,
  type ReactElement,
  type Ref,
} from "react";

import { CompositeGroup, CompositeOption, uiClassName } from "./composite-selection";

interface SegmentedContextValue {
  value: string | null;
  select: (value: string) => void;
}

const SegmentedContext = createContext<SegmentedContextValue | null>(null);

export interface SegmentedControlProps<T extends string = string>
  extends Omit<HTMLAttributes<HTMLDivElement>, "role" | "onChange" | "defaultValue"> {
  value: T | null;
  onValueChange: (value: T) => void;
  /** `"both"`: all four arrows work (right/down = next, left/up = previous), for card-style radio selection. */
  orientation?: "horizontal" | "vertical" | "both";
  /**
   * Saving: ignore selection and mark aria-busy / aria-disabled; keep segments focusable. Using disabled for Saving
   * makes the segment just selected by an arrow lose focus to body (real-browser check, 2026-09-26, share audience).
   */
  busy?: boolean;
  /** Appearance: `"segmented"` segments, `"cards"` card-style radios, `"none"` opts out of unification. */
  appearance?: "segmented" | "cards" | "none";
  /** See CompositeGroupProps.activation. */
  activation?: "automatic" | "manual";
}

const APPEARANCE_CLASS = { segmented: "ui-segmented", cards: "ui-radio-cards", none: null } as const;

function SegmentedControlInner<T extends string = string>(
  { value, onValueChange, busy = false, appearance = "segmented", className, ...rest }: SegmentedControlProps<T>,
  ref: ForwardedRef<HTMLDivElement>,
) {
  const ctx = useMemo<SegmentedContextValue>(
    () => ({
      value,
      select: (next) => {
        if (busy) return;
        onValueChange(next as T);
      },
    }),
    [value, onValueChange, busy],
  );
  return (
    <SegmentedContext.Provider value={ctx}>
      <CompositeGroup
        {...rest}
        className={uiClassName(APPEARANCE_CLASS[appearance], className)}
        aria-busy={busy || undefined}
        aria-disabled={busy || rest["aria-disabled"] || undefined}
        ref={ref}
        containerRole="radiogroup"
        itemRole="radio"
        selectedAttr="aria-checked"
        onSelectValue={ctx.select}
      />
    </SegmentedContext.Provider>
  );
}

export const SegmentedControl = forwardRef(SegmentedControlInner) as <T extends string = string>(
  props: SegmentedControlProps<T> & { ref?: Ref<HTMLDivElement> },
) => ReactElement;

export interface SegmentProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "value" | "role"> {
  value: string;
  "data-active"?: string | boolean;
}

export const Segment = forwardRef<HTMLButtonElement, SegmentProps>(function Segment({ value, onClick, ...rest }, ref) {
  const ctx = useContext(SegmentedContext);
  if (!ctx) throw new Error("Segment must be rendered inside <SegmentedControl>");
  return (
    <CompositeOption
      {...rest}
      ref={ref}
      value={value}
      role="radio"
      aria-checked={ctx.value === value}
      onClick={(event: MouseEvent<HTMLButtonElement>) => {
        onClick?.(event);
        if (!event.defaultPrevented) ctx.select(value);
      }}
    />
  );
});
