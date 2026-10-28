import type { ConfigurationJson } from "./types";

/** Preserve JSON types and complete nested values in configuration review. */
export function configurationValueText(value: ConfigurationJson): string {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}
