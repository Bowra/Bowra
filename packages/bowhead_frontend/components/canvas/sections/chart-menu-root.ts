"use client";

import { createContext } from "react";

/**
 * Portal container for chart overlay menus (chart-type dropdown and table action menu). Default `undefined` = mount to body.
 *
 * The chart-style settings preview inlines color variables (`--c-surface`, `--c-ink`, etc.) on its preview container.
 * Menus mounted to body cannot read them and use the page colors, showing a light menu in a dark preview. The preview uses this to
 * mount menus inside its container; `null` means the container is not mounted yet, so wait for it to appear.
 */
export const ChartMenuRootContext = createContext<HTMLElement | null | undefined>(undefined);
