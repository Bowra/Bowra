/**
 * Interface appearance, page-by-page acceptance A: append a stable global hook class to each CSS Module class
 * `${prefix}-${kebab-case class name}` (e.g. `eyebrow` → `ui-integ-eyebrow`).
 *
 * CSS Module class names carry build hashes, making global selectors unstable; the interface styles use `:root:root .ui-integ-*`
 * (styles/ui-refresh-pages-settings.css).
 */
export function withUiHooks<T extends Readonly<Record<string, string>>>(styles: T, prefix: string): T {
  const hooked: Record<string, string> = {};
  for (const [key, value] of Object.entries(styles)) {
    hooked[key] = `${value} ${prefix}-${toKebab(key)}`;
  }
  return hooked as T;
}

function toKebab(name: string): string {
  return name.replace(/([a-z0-9])([A-Z])/g, "$1-$2").replace(/_/g, "-").toLowerCase();
}
