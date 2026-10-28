type PageTitleListener = () => void;

interface PageTitleRegistration {
  token: symbol;
  ownerId: string;
  title: string | null;
}

export interface PageTitleRegistrationHandle {
  update: (title: string | null | undefined) => void;
  unregister: () => void;
}

const registrations: PageTitleRegistration[] = [];
const listeners = new Set<PageTitleListener>();

/**
 * The most recent registration that has a title. A registration without one (for example a report canvas that has
 * no title yet) defers to the one beneath it, such as the page's own title registered by AppShell.
 */
export function getPageTitleSnapshot(): string | null {
  for (let index = registrations.length - 1; index >= 0; index -= 1) {
    const title = registrations[index]!.title;
    if (title) return title;
  }
  return null;
}

export function getServerPageTitleSnapshot(): null {
  return null;
}

export function subscribePageTitle(listener: PageTitleListener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function registerPageTitle(
  ownerId: string,
  title: string | null | undefined,
): PageTitleRegistrationHandle {
  const normalizedOwnerId = ownerId.trim();
  if (!normalizedOwnerId) {
    throw new Error("Page title ownerId must not be empty");
  }
  if (
    process.env.NODE_ENV !== "production" &&
    registrations.some((entry) => entry.ownerId === normalizedOwnerId)
  ) {
    console.warn(
      `[page-title-store] duplicate active owner: ${normalizedOwnerId}`,
    );
  }

  const previous = getPageTitleSnapshot();
  const registration: PageTitleRegistration = {
    token: Symbol(normalizedOwnerId),
    ownerId: normalizedOwnerId,
    title: title?.trim() || null,
  };
  registrations.push(registration);
  notifyIfChanged(previous);

  let active = true;
  return {
    update: (nextTitle) => {
      if (!active) return;
      const beforeUpdate = getPageTitleSnapshot();
      registration.title = nextTitle?.trim() || null;
      notifyIfChanged(beforeUpdate);
    },
    unregister: () => {
      if (!active) return;
      active = false;
      const beforeCleanup = getPageTitleSnapshot();
      const index = registrations.findIndex(
        (entry) => entry.token === registration.token,
      );
      if (index >= 0) registrations.splice(index, 1);
      notifyIfChanged(beforeCleanup);
    },
  };
}

function notifyIfChanged(previous: string | null): void {
  if (getPageTitleSnapshot() === previous) return;
  for (const listener of listeners) listener();
}
