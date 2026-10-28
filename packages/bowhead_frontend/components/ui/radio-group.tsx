"use client";

/**
 * RadioGroup / Radio: card-style single selection (component unification P1, step 6, docs/design/组件统一-技术设计.md).
 *
 * Share the SegmentedControl / Segment implementation (`role="radiogroup"` + `role="radio"`, arrows move and select,
 * Home/End, skip disabled items, roving tabindex, `busy`); the names simply better describe selecting one card from a group.
 * Defaults to `appearance="cards"` (`ui-radio-cards`). Do not add separate logic here.
 */
import { forwardRef, type ReactElement, type Ref } from "react";

import { SegmentedControl, type SegmentedControlProps } from "./segmented-control";

export type RadioGroupProps<T extends string = string> = SegmentedControlProps<T>;

function RadioGroupInner<T extends string = string>(
  { appearance = "cards", ...rest }: RadioGroupProps<T>,
  ref: Ref<HTMLDivElement>,
) {
  return <SegmentedControl<T> {...rest} appearance={appearance} ref={ref} />;
}

export const RadioGroup = forwardRef(RadioGroupInner) as <T extends string = string>(
  props: RadioGroupProps<T> & { ref?: Ref<HTMLDivElement> },
) => ReactElement;

export { Segment as Radio, type SegmentProps as RadioProps } from "./segmented-control";
