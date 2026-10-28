import { createContext } from "react";
import type { FocusBinding, FocusSelection, TypedCell } from "./linked-focus";
import type { ChartSpec } from "./types";
import type { ObservationAnnotation, ObservationTarget } from "./annotations";

export interface ChartFocusContextValue {
  followupScope?: import("./selected-context").ChartFollowupScope;
  binding: FocusBinding;
  scopeSource?: ChartSpec;
  datasetDigest: string;
  selection: FocusSelection | null;
  select: ((tuple: TypedCell[] | null, observation?: ObservationTarget) => void) | null;
  pinned?: ObservationTarget | null;
  inspectAnnotation?: (annotation: ObservationAnnotation) => void;
  closeDetails?: () => void;
}
export const ChartFocusContext = createContext<ChartFocusContextValue | null>(null);
