import { createContext } from "react";
import type { ChartFollowupScope } from "./selected-context";
import type { FrameBinding } from "./prepared-frames";

/** Local prepared-data scope stays outside the persisted ChartSpec and canonical report export. */
export const ChartTimeWindowContext = createContext<{ label: string; filenameSuffix: string; beforeCapture?: () => void; followup?: { scope: ChartFollowupScope; binding: FrameBinding } } | null>(null);
