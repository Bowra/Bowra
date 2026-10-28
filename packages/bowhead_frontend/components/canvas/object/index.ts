/** Canvas object components C1–C11, C13, C14 (Claude Design “Canvas Objects Spec”). */
export { CanvasObjectTabs, CanvasObjectIcon, canvasPanelId, canvasTabId, canvasSwitchShortcut, type CanvasObjectKind, type CanvasTab } from "./object-tabs";
export { CanvasOpenMenu, CanvasOpenMenuItems, hasOpenMenuItems, type CanvasOpenDocument, type CanvasOpenDocuments, type CanvasOpenMenuModel } from "./open-menu";
export { CanvasObjectToolbar, CanvasToolbarActions, SaveState, ToolbarAction, formatSavedTime, type CanvasSaveState } from "./toolbar";
export { CanvasFooterPortal, CanvasObjectSurface, useCanvasObjectFooter, useReportCanvasSave, type CanvasSaveReport } from "./surface";
export { CanvasObjectHeader } from "./header";
export { ChangeRow, ChangeValueView, type ChangeRowState } from "./change-row";
export { CanvasObjectFooter, type CanvasFooterPrimary, type CanvasFooterSecondary } from "./footer";
export { StateBlock, type StateBlockAction } from "./state-block";
export { ObjectMarker, ObjectStatusBadge, Spinner, objectStatusMarker, type CanvasObjectMarker, type CanvasObjectStatus } from "./status-badge";
export { canvasCloseGuardOptions, useCanvasCloseGuard } from "./close-guard";
export { NarrowStatus } from "./narrow-status";
