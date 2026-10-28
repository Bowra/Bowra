import { getCurrentLocale } from "./locale";

/** ISO → YYYY-MM-DD (date-only slice). Deliberately NOT lib/locale.ts's Intl formatter —
 *  these list rows want a stable absolute date with no locale/timezone shifting. */
export function formatDateYMD(iso: string | null): string {
  if (!iso) return "";
  const d = iso.slice(0, 10);
  return d.includes("-") ? d : iso;
}

/** French writes octets (o / Ko / Mo); the other UI languages use B / KB / MB. */
const BYTE_UNITS: Record<string, readonly [string, string, string]> = {
  fr: ["o", "Ko", "Mo"],
};

/** byte count → readable size for list-row metadata in the UI language: "944 B", "566 KB", "1.2 MB", de "1,2 MB",
 *  fr "1,2 Mo" (116 showed "566K" / "944B"). Whole kilobytes keep rows compact; knowledge base file sizes use
 *  `formatBytes` from `lib/kb-api.ts`. `locale` defaults to the current UI language. */
export function formatByteSize(n: number, locale: string = getCurrentLocale()): string {
  if (!n) return "";
  const [b, kb, mb] = BYTE_UNITS[locale.slice(0, 2)] ?? ["B", "KB", "MB"];
  const number = (value: number, digits: number) =>
    new Intl.NumberFormat(locale, { maximumFractionDigits: digits }).format(value);
  if (n < 1024) return `${number(n, 0)} ${b}`;
  if (n < 1024 * 1024) return `${number(Math.round(n / 1024), 0)} ${kb}`;
  return `${number(n / 1024 / 1024, 1)} ${mb}`;
}
