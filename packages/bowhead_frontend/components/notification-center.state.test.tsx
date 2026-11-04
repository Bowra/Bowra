import "@testing-library/jest-dom/vitest";
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderIntl } from "@/__tests__/intl-render";
import type { NotificationItemDTO } from "@/lib/api";
import { HttpStatusError } from "@/lib/api-client";

const api = vi.hoisted(() => ({
  fetchNotificationCount: vi.fn(), fetchNotifications: vi.fn(), markNotificationRead: vi.fn(),
  archiveNotification: vi.fn(), markAllNotificationsRead: vi.fn(), push: vi.fn(),
}));
vi.mock("@/lib/api", () => api);
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ push: api.push }) }));
import { NotificationCenter } from "./notification-center";

function item(id: string, actionRequired = false): NotificationItemDTO {
  return { id, actionRequired, type: "test.update", teamId: "team", category: "test", priority: "p2", title: id,
    body: null, subjectType: null, subjectId: null, actorUserId: null, actionUrl: "/", actions: [], metadata: {},
    state: "open", recipientRole: "owner", deliveredAt: "2026-11-01T00:00:00Z", readAt: null, archivedAt: null,
    createdAt: "2026-11-01T00:00:00Z", updatedAt: "2026-11-01T00:00:00Z" };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function failure(status: number) { return new HttpStatusError("notification", new Response("{}", { status })); }
const page = (items: NotificationItemDTO[], nextCursor: string | null = null) => ({ items, nextCursor });
async function open() {
  fireEvent.click(screen.getByRole("button", { expanded: false }));
  await screen.findByRole("region", { name: "Notification center" });
}
function mount() { return renderIntl(<NotificationCenter />, { locale: "en-US" }); }
function rows() { return [...document.querySelectorAll(".notification-row")]; }

beforeEach(() => {
  vi.resetAllMocks();
  api.fetchNotificationCount.mockResolvedValue({ actionRequired: 0, updatesUnread: 0, totalBadge: 0 });
  api.fetchNotifications.mockResolvedValue(page([]));
  api.markNotificationRead.mockResolvedValue(undefined);
  api.archiveNotification.mockResolvedValue(undefined);
  api.markAllNotificationsRead.mockResolvedValue(undefined);
});

describe("notification presentation from server facts", () => {
  it.each([[0, 0], [0, 3], [2, 0], [2, 3]])("shows the correct badge for %i actions and %i updates", async (actionRequired, updatesUnread) => {
    api.fetchNotificationCount.mockResolvedValue({ actionRequired, updatesUnread, totalBadge: actionRequired });
    mount();
    await waitFor(() => expect(api.fetchNotificationCount).toHaveBeenCalled());
    await waitFor(() => expect(!!document.querySelector(".notification-bell__dot")).toBe(actionRequired === 0 && updatesUnread > 0));
    expect(!!document.querySelector(".notification-bell__badge")).toBe(actionRequired > 0);
  });
  it("renders server-classified action rows even when already read", async () => {
    api.fetchNotifications.mockResolvedValue(page([{ ...item("Answer required", true), type: "routine.needs_input", readAt: "now" }, item("Result ready")]));
    mount(); await open();
    await waitFor(() => expect(rows()).toHaveLength(2));
    expect(rows()[0]).toHaveClass("action");
    expect(rows()[0]).not.toHaveClass("unread");
    expect(rows()[1]).not.toHaveClass("action");
  });
});

describe("notification read and mutation failures", () => {
  it.each([503, 500, 502])("keeps unavailable list %i retryable", async (status) => {
    api.fetchNotifications.mockRejectedValueOnce(failure(status)).mockResolvedValue(page([item("Recovered")]));
    mount(); await open();
    expect(await screen.findByText("Couldn't load notifications. Try again later.")).toBeVisible();
    expect(screen.getByRole("button", { expanded: true })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Recovered")).toBeVisible();
  });
  it("reports unknown first counts and preserves confirmed counts on later failure", async () => {
    api.fetchNotificationCount.mockRejectedValue(failure(503));
    mount(); await open();
    expect(await screen.findByText("Notification counts unavailable")).toBeVisible();
    expect(screen.queryByText("0 pending · 0 unread updates")).toBeNull();
    api.fetchNotificationCount.mockResolvedValue({ actionRequired: 2, updatesUnread: 0, totalBadge: 2 });
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("2 pending · 0 unread updates")).toBeVisible();
    api.fetchNotificationCount.mockRejectedValue(failure(503));
    fireEvent.focus(window);
    expect(await screen.findByText("Couldn't refresh notification counts. Retry to check the current counts.")).toBeVisible();
    expect(screen.getByText("2 pending · 0 unread updates")).toBeVisible();
  });
  it.each([404, 500, 503])("keeps a failed row-read %i from navigating or hiding the center", async (status) => {
    api.fetchNotifications.mockResolvedValue(page([item("Read target")]));
    api.markNotificationRead.mockRejectedValue(failure(status));
    mount(); await open(); fireEvent.click(await screen.findByText("Read target"));
    await screen.findByText(status === 404 ? "This notification is no longer available. Refresh the list." : "Couldn't mark this notification as read. Try again later.");
    expect(api.push).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { expanded: true })).toBeVisible();
    if (status !== 404) expect(rows()[0]).toHaveClass("unread");
  });
  it.each([404, 500, 503])("keeps archive failure %i scoped to the item", async (status) => {
    api.fetchNotifications.mockResolvedValue(page([item("Archive target")]));
    api.archiveNotification.mockRejectedValue(failure(status));
    mount(); await open(); await screen.findByText("Archive target");
    fireEvent.click(screen.getByRole("button", { name: "Archive notification" }));
    await screen.findByText(status === 404 ? "This notification is no longer available. Refresh the list." : "Couldn't archive this notification. Try again later.");
    expect(screen.getByRole("button", { expanded: true })).toBeVisible();
    if (status !== 404) expect(rows()).toHaveLength(1);
  });
  it("handles read-all failure, prevents duplicate submission and permits retry", async () => {
    const write = deferred<void>();
    api.fetchNotifications.mockResolvedValue(page([item("Unread")]));
    api.markAllNotificationsRead.mockReturnValueOnce(write.promise);
    mount(); await open(); fireEvent.click(screen.getByRole("tab", { name: "Updates" }));
    const button = await screen.findByRole("button", { name: "Mark all read" });
    fireEvent.click(button); fireEvent.click(button);
    expect(api.markAllNotificationsRead).toHaveBeenCalledTimes(1);
    await act(async () => write.reject(failure(503)));
    expect(await screen.findByText("Couldn't mark all notifications as read. Try again.")).toBeVisible();
    expect(rows()[0]).toHaveClass("unread");
    expect(button).not.toBeDisabled();
    fireEvent.click(button);
    await waitFor(() => expect(api.markAllNotificationsRead).toHaveBeenCalledTimes(2));
  });
});

describe("notification asynchronous view ownership", () => {
  it("ignores an old tab response after a new tab has loaded", async () => {
    const old = deferred<ReturnType<typeof page>>();
    api.fetchNotifications.mockImplementation((tab) => tab === "action" ? old.promise : Promise.resolve(page([item("New update")])));
    mount(); await open(); fireEvent.click(screen.getByRole("tab", { name: "Updates" }));
    await screen.findByText("New update");
    await act(async () => old.resolve(page([item("Old action", true)], "old-cursor")));
    expect(screen.queryByText("Old action")).toBeNull();
    expect(screen.getByText("New update")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  });
  it("ignores an old tab error and finally while the new tab is loading", async () => {
    const old = deferred<ReturnType<typeof page>>(), next = deferred<ReturnType<typeof page>>();
    api.fetchNotifications.mockImplementation((tab) => tab === "action" ? old.promise : next.promise);
    mount(); await open(); fireEvent.click(screen.getByRole("tab", { name: "Updates" }));
    await act(async () => old.reject(failure(503)));
    expect(screen.queryByText("Couldn't load notifications. Try again later.")).toBeNull();
    expect(screen.getByText("Loading…")).toBeVisible();
    await act(async () => next.resolve(page([item("Current")])));
    expect(screen.getByText("Current")).toBeVisible();
  });
  it("ignores stale count responses", async () => {
    const old = deferred<{ actionRequired: number; updatesUnread: number; totalBadge: number }>();
    api.fetchNotificationCount.mockReturnValueOnce(old.promise).mockResolvedValue({ actionRequired: 2, updatesUnread: 0, totalBadge: 2 });
    mount(); await open();
    await screen.findByText("2 pending · 0 unread updates");
    await act(async () => old.resolve({ actionRequired: 9, updatesUnread: 0, totalBadge: 9 }));
    expect(screen.getByText("2 pending · 0 unread updates")).toBeVisible();
  });
  it("deduplicates pages and ignores pagination after changing tabs", async () => {
    const more = deferred<ReturnType<typeof page>>();
    api.fetchNotifications.mockImplementation((tab, cursor) => tab === "updates" ? Promise.resolve(page([item("Current update")])) : cursor ? more.promise : Promise.resolve(page([item("First")], "next")));
    mount(); await open();
    const button = await screen.findByRole("button", { name: "Load more" });
    fireEvent.click(button); fireEvent.click(button);
    expect(api.fetchNotifications.mock.calls.filter((args) => args[1] === "next")).toHaveLength(1);
    fireEvent.click(screen.getByRole("tab", { name: "Updates" }));
    await screen.findByText("Current update");
    await act(async () => more.resolve(page([item("First"), item("Late page")])));
    expect(screen.queryByText("Late page")).toBeNull();
    expect(rows()).toHaveLength(1);
  });
  it("keeps read-all results with the initiating tab", async () => {
    const write = deferred<void>();
    api.fetchNotifications.mockImplementation((tab) => Promise.resolve(page([item(tab)])));
    api.markAllNotificationsRead.mockReturnValue(write.promise);
    mount(); await open(); fireEvent.click(screen.getByRole("tab", { name: "All" }));
    fireEvent.click(await screen.findByRole("button", { name: "Mark all read" }));
    fireEvent.click(screen.getByRole("tab", { name: "Updates" }));
    await screen.findByText("updates");
    await act(async () => write.resolve());
    expect(rows()[0]).toHaveClass("unread");
    expect(api.markAllNotificationsRead).toHaveBeenCalledWith("all");
  });
  it("does not navigate after the read finishes in a closed view", async () => {
    const write = deferred<void>();
    api.fetchNotifications.mockResolvedValue(page([item("Pending read")]));
    api.markNotificationRead.mockReturnValue(write.promise);
    mount(); await open(); fireEvent.click(await screen.findByText("Pending read"));
    fireEvent.click(screen.getByRole("button", { expanded: true }));
    await act(async () => write.resolve());
    expect(api.push).not.toHaveBeenCalled();
  });
  it("ignores a pending list after close and unmount", async () => {
    const pending = deferred<ReturnType<typeof page>>();
    api.fetchNotifications.mockReturnValue(pending.promise);
    const mounted = mount(); await open();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("region", { name: "Notification center" })).toBeNull();
    mounted.unmount();
    await act(async () => pending.resolve(page([item("Late")]))) ;
    expect(screen.queryByText("Late")).toBeNull();
  });
});

describe("notification replacement and pagination controls", () => {
  it("deduplicates an overlapping page by stable notification ID", async () => {
    api.fetchNotifications.mockResolvedValueOnce(page([item("First")], "next"))
      .mockResolvedValueOnce(page([item("First"), item("Second")]));
    mount(); await open();
    fireEvent.click(await screen.findByRole("button", { name: "Load more" }));
    await screen.findByText("Second");
    expect(screen.getAllByText("First")).toHaveLength(1);
    expect(rows()).toHaveLength(2);
  });
  it("ignores a previous opening's response after reopening the same tab", async () => {
    const old = deferred<ReturnType<typeof page>>();
    api.fetchNotifications.mockReturnValueOnce(old.promise).mockResolvedValue(page([item("New opening")]));
    mount(); await open();
    fireEvent.click(screen.getByRole("button", { expanded: true }));
    await open(); await screen.findByText("New opening");
    await act(async () => old.resolve(page([item("Old opening")])));
    expect(screen.queryByText("Old opening")).toBeNull();
  });
  it("ignores an obsolete count failure after a successful newer read", async () => {
    const old = deferred<never>();
    api.fetchNotificationCount.mockReturnValueOnce(old.promise).mockResolvedValue({ actionRequired: 1, updatesUnread: 0, totalBadge: 1 });
    mount(); await open(); await screen.findByText("1 pending · 0 unread updates");
    await act(async () => old.reject(failure(404)));
    expect(screen.getByRole("button", { expanded: true })).toBeVisible();
  });
  it("guards same-tick row clicks while marking read", async () => {
    const write = deferred<void>();
    api.fetchNotifications.mockResolvedValue(page([item("One action")]));
    api.markNotificationRead.mockReturnValue(write.promise);
    mount(); await open();
    const row = await screen.findByText("One action");
    fireEvent.click(row); fireEvent.click(row);
    expect(api.markNotificationRead).toHaveBeenCalledTimes(1);
    await act(async () => write.resolve());
    expect(api.push).toHaveBeenCalledTimes(1);
  });
  it("refreshes the current view after an archive completed in another tab", async () => {
    const write = deferred<void>();
    api.fetchNotifications.mockImplementation((tab) => Promise.resolve(page([item(tab)])));
    api.archiveNotification.mockReturnValue(write.promise);
    mount(); await open(); await screen.findByText("action");
    fireEvent.click(screen.getByRole("button", { name: "Archive notification" }));
    fireEvent.click(screen.getByRole("tab", { name: "Updates" }));
    await screen.findByText("updates");
    await act(async () => write.resolve());
    expect(api.archiveNotification).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("updates")).toBeVisible();
    expect(screen.queryByText("action")).toBeNull();
  });
});
