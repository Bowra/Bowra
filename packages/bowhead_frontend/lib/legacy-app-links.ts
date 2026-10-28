import { unprefixAppPath } from "@/lib/base-path";
import { personalSettingsPath } from "@/lib/personal-settings-paths";

/** Query parameter that asks Settings > Memory to scroll to and highlight one memory. */
export const MEMORY_FOCUS_PARAM = "item";

const LEGACY_MEMORY_ROUTE = /^\/memory\/?(?=$|[?#])/;

/**
 * Map links persisted before a page moved to the page's current route.
 *
 * Notifications and Home items store their `action_url` in the database, so
 * rows written before the standalone `/memory` page folded into Settings still
 * point there. Rewrite at navigation time instead of migrating stored rows;
 * the query string and hash (for example `?item=<id>`) are kept.
 */
export function normalizeAppActionUrl(url: string): string {
  if (!url.startsWith("/") || url.startsWith("//")) return url;
  const route = unprefixAppPath(url);
  if (!LEGACY_MEMORY_ROUTE.test(route)) return url;
  return route.replace(LEGACY_MEMORY_ROUTE, personalSettingsPath("memory"));
}
