// Shared test helper: jsdom has no IntersectionObserver. Install a controllable fake and
// report visibility changes for observed elements by hand.
import { act } from "@testing-library/react";
import { vi } from "vitest";

interface FakeObserver {
  callback: IntersectionObserverCallback;
  targets: Set<Element>;
}

export function installFakeIntersectionObserver() {
  const observers = new Set<FakeObserver>();
  class FakeIntersectionObserver {
    private readonly entry: FakeObserver;
    constructor(callback: IntersectionObserverCallback) {
      this.entry = { callback, targets: new Set() };
      observers.add(this.entry);
    }
    observe(target: Element) {
      this.entry.targets.add(target);
    }
    unobserve(target: Element) {
      this.entry.targets.delete(target);
    }
    disconnect() {
      this.entry.targets.clear();
      observers.delete(this.entry);
    }
    takeRecords() {
      return [];
    }
  }
  vi.stubGlobal("IntersectionObserver", FakeIntersectionObserver);

  return {
    /** How many live observers are watching `target`. */
    watching(target: Element): number {
      return [...observers].filter((observer) => observer.targets.has(target)).length;
    },
    /** Report `target` as (in)visible to every observer watching it. */
    setVisible(target: Element, visible: boolean) {
      act(() => {
        for (const observer of [...observers]) {
          if (!observer.targets.has(target)) continue;
          observer.callback(
            [{ target, isIntersecting: visible, intersectionRatio: visible ? 1 : 0 } as IntersectionObserverEntry],
            observer as unknown as IntersectionObserver,
          );
        }
      });
    },
  };
}
