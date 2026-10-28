import type { Page } from "@playwright/test";

/** Phones switch between Chat and the canvas pane; with Canvas objects open the pane tab reads “成果 · N”. */
export const mobilePaneTab = (page: Page) => page.getByRole("tablist", { name: /^对话 \/ / }).getByRole("tab").nth(1);
