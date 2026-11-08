import { markNotificationRead, type NotificationItemDTO } from "@/lib/api";
import { setLastTeam } from "@/lib/auth-api";
import { normalizeAppActionUrl } from "@/lib/legacy-app-links";

interface Router {
  push: (href: string) => void;
}

/** Whether opening the item first switches to the Team it belongs to. */
export function opensInAnotherTeam(item: Pick<NotificationItemDTO, "scope" | "teamName" | "currentTeam">): boolean {
  return item.scope !== "personal" && Boolean(item.teamName) && item.currentTeam === false;
}

/**
 * Open a notification target: switch to the item's Team when it belongs to another one, then
 * navigate with the deployment prefix kept by the app router.
 *
 * Returns false when the browser is leaving for another Team's own address (the Team switch
 * redirected), so the caller does not navigate as well. A failed switch throws, leaving the
 * current page and Team as they were.
 */
export async function openNotificationTarget(item: NotificationItemDTO, url: string, router: Router): Promise<boolean> {
  if (opensInAnotherTeam(item)) {
    const result = await setLastTeam(item.teamId);
    if (result.redirect_url) return false;
  }
  router.push(normalizeAppActionUrl(url));
  return true;
}

/** Open an item announced outside the panel (toast or browser notification): read it, then open it. */
export async function openAnnouncedNotification(item: NotificationItemDTO, router: Router): Promise<void> {
  try {
    if (!item.readAt) await markNotificationRead(item.id);
  } catch {
    // Opening still works; the bell shows it unread until the next read.
  }
  if (item.actionUrl) await openNotificationTarget(item, item.actionUrl, router);
}
