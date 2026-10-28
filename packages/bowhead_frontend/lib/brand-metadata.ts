
import { PRODUCT_NAME } from "@/lib/product-identity";
import { appPath } from "@/lib/base-path";
import {
  CANONICAL_BOWHEAD_ICON_PATH,
  canonicalEffectiveThemeFallback,
  isCanonicalBrandBundle,
  type EffectiveBrand,
  type EffectiveThemeResponse,
} from "@/lib/theme-config";

export const BRAND_METADATA_TITLE_ID = "bowhead-document-title";
export const BRAND_METADATA_FAVICON_ID = "bowhead-runtime-favicon";
export const BRAND_METADATA_OWNED_ATTR = "data-bowhead-metadata-owned";
const BRAND_METADATA_SOURCE_ATTR = "data-bowhead-metadata-source";
const BRAND_METADATA_TEAM_ATTR = "data-bowhead-metadata-team";
const BRAND_METADATA_REVISION_ATTR = "data-bowhead-metadata-revision";
const LEGACY_FAVICON_SELECTOR = [
  "link[data-bowhead-public-favicon]",
  "link[data-bowhead-brand-favicon]",
].join(", ");

export type BrandMetadataSource =
  | "canonical"
  | "public-bootstrap"
  | "authenticated-cache"
  | "live";

export interface BrandMetadataState {
  applicationName: string;
  pageTitle: string | null;
  documentTitle: string;
  faviconHref: string;
  source: BrandMetadataSource;
  teamId: string | null;
  revisionId: string | null;
}

export interface ApplyMetadataResult {
  titleCreated: boolean;
  titleChanged: boolean;
  faviconCreated: boolean;
  faviconChanged: boolean;
  legacyNodesRemoved: number;
}

export function composeDocumentTitle({
  applicationName,
  pageTitle,
  whiteLabel = false,
}: Pick<BrandMetadataState, "applicationName" | "pageTitle"> & {
  /**
   * Public (signed-out) pages under a custom brand are fully white-labelled: without a page title, use only the application name, without "— Verified Intelligence".
   * Same rule as the gateway first-paint injection `static_localized.public_document_title`; the product brand is unchanged.
   */
  whiteLabel?: boolean;
}): string {
  const normalizedApplicationName = applicationName.trim() || PRODUCT_NAME;
  const normalizedPageTitle = pageTitle?.trim() || null;
  if (normalizedPageTitle) return `${normalizedPageTitle} · ${normalizedApplicationName}`;
  return whiteLabel && normalizedApplicationName !== PRODUCT_NAME
    ? normalizedApplicationName
    : `${normalizedApplicationName} — Verified Intelligence`;
}

export function resolveBrandMetadata(
  bundle: EffectiveThemeResponse,
  pageTitle: string | null | undefined,
  source: BrandMetadataSource,
  options: { whiteLabel?: boolean } = {},
): BrandMetadataState {
  const brand = bundle.brand ?? canonicalEffectiveThemeFallback().brand!;
  const applicationName = brand.applicationName.trim() || PRODUCT_NAME;
  const normalizedPageTitle = pageTitle?.trim() || null;
  return {
    applicationName,
    pageTitle: normalizedPageTitle,
    documentTitle: composeDocumentTitle({
      applicationName,
      pageTitle: normalizedPageTitle,
      whiteLabel: options.whiteLabel,
    }),
    faviconHref: resolveFaviconHref(bundle, brand),
    source,
    teamId: bundle.team?.id ?? null,
    revisionId: bundle.team?.revisionId ?? null,
  };
}

export function applyOwnedDocumentMetadata(
  state: BrandMetadataState,
  targetDocument?: Document,
): ApplyMetadataResult {
  const doc =
    targetDocument ??
    (typeof document === "undefined" ? undefined : document);
  if (!doc) {
    return {
      titleCreated: false,
      titleChanged: false,
      faviconCreated: false,
      faviconChanged: false,
      legacyNodesRemoved: 0,
    };
  }

  let legacyNodesRemoved = 0;
  for (const legacyNode of doc.querySelectorAll(LEGACY_FAVICON_SELECTOR)) {
    legacyNode.remove();
    legacyNodesRemoved += 1;
  }

  const titleResult = ensureOwnedElement<HTMLTitleElement>(
    doc,
    BRAND_METADATA_TITLE_ID,
    "title",
  );
  let titleChanged = titleResult.created;
  titleChanged =
    setAttributeIfChanged(
      titleResult.element,
      BRAND_METADATA_OWNED_ATTR,
      "",
    ) || titleChanged;
  titleChanged =
    setDiagnosticAttributes(titleResult.element, state) || titleChanged;
  if (titleResult.element.textContent !== state.documentTitle) {
    titleResult.element.textContent = state.documentTitle;
    titleChanged = true;
  }

  const faviconResult = ensureOwnedElement<HTMLLinkElement>(
    doc,
    BRAND_METADATA_FAVICON_ID,
    "link",
  );
  let faviconChanged = faviconResult.created;
  faviconChanged =
    setAttributeIfChanged(
      faviconResult.element,
      BRAND_METADATA_OWNED_ATTR,
      "",
    ) || faviconChanged;
  faviconChanged =
    setDiagnosticAttributes(faviconResult.element, state) || faviconChanged;
  faviconChanged =
    setAttributeIfChanged(faviconResult.element, "rel", "icon") ||
    faviconChanged;
  faviconChanged =
    setOptionalAttribute(
      faviconResult.element,
      "type",
      isSvgHref(state.faviconHref) ? "image/svg+xml" : null,
    ) || faviconChanged;
  faviconChanged =
    setAttributeIfChanged(
      faviconResult.element,
      "href",
      state.faviconHref,
    ) || faviconChanged;

  return {
    titleCreated: titleResult.created,
    titleChanged,
    faviconCreated: faviconResult.created,
    faviconChanged,
    legacyNodesRemoved,
  };
}

function resolveFaviconHref(
  bundle: EffectiveThemeResponse,
  brand: EffectiveBrand,
): string {
  if (isCanonicalBrandBundle(bundle)) {
    return appPath(CANONICAL_BOWHEAD_ICON_PATH);
  }
  if (brand.favicon.kind === "asset") {
    return appPath(brand.favicon.path);
  }
  const rawInitial =
    brand.favicon.kind === "generatedInitial"
      ? brand.favicon.text
      : (brand.favicon.text ?? brand.applicationName);
  const initial =
    Array.from(rawInitial.trim())[0] ??
    Array.from(brand.applicationName.trim())[0] ??
    "?";
  return generatedInitialFavicon(initial.toUpperCase());
}

function generatedInitialFavicon(initial: string): string {
  const svg = [
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">',
    '<rect width="64" height="64" rx="14" fill="#161310"/>',
    `<text x="32" y="42" text-anchor="middle" font-family="system-ui,sans-serif" font-size="34" font-weight="700" fill="#FAF9F6">${escapeXml(initial)}</text>`,
    "</svg>",
  ].join("");
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

function escapeXml(value: string): string {
  return value.replace(
    /[&<>"']/g,
    (character) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&apos;",
      })[character] ?? character,
  );
}

function ensureOwnedElement<T extends HTMLElement>(
  doc: Document,
  id: string,
  tagName: "title" | "link",
): { element: T; created: boolean } {
  const existing = doc.getElementById(id);
  if (existing?.tagName.toLowerCase() === tagName) {
    return { element: existing as T, created: false };
  }
  existing?.remove();
  const element = doc.createElement(tagName) as unknown as T;
  element.id = id;
  doc.head.append(element);
  return { element, created: true };
}

function setDiagnosticAttributes(
  element: HTMLElement,
  state: BrandMetadataState,
): boolean {
  let changed = setAttributeIfChanged(
    element,
    BRAND_METADATA_SOURCE_ATTR,
    state.source,
  );
  changed =
    setOptionalAttribute(
      element,
      BRAND_METADATA_TEAM_ATTR,
      state.teamId,
    ) || changed;
  changed =
    setOptionalAttribute(
      element,
      BRAND_METADATA_REVISION_ATTR,
      state.revisionId,
    ) || changed;
  return changed;
}

function setAttributeIfChanged(
  element: Element,
  name: string,
  value: string,
): boolean {
  if (element.getAttribute(name) === value) return false;
  element.setAttribute(name, value);
  return true;
}

function setOptionalAttribute(
  element: Element,
  name: string,
  value: string | null,
): boolean {
  if (value === null) {
    if (!element.hasAttribute(name)) return false;
    element.removeAttribute(name);
    return true;
  }
  return setAttributeIfChanged(element, name, value);
}

function isSvgHref(href: string): boolean {
  return (
    href.startsWith("data:image/svg+xml") ||
    href.split(/[?#]/, 1)[0]?.endsWith(".svg") === true
  );
}
