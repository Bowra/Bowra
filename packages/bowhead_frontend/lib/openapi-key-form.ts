/** What an Agent's CI pipeline needs (Agent CI/CD PRD R9): the Agent version endpoints, nothing else. */
export const AGENT_CI_SCOPES: readonly string[] = ["agent_versions:read", "agent_versions:write"];

/** Matches the server default for a new key without an expiry date. */
const DEFAULT_EXPIRY_DAYS = 30;

/** A `datetime-local` value in the browser's timezone, minute precision. */
export function localDateTime(date: Date): string {
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** A new key's expiry is prefilled with the default, 30 days from now, so the date is visible before saving. */
export function defaultKeyExpiry(now: Date = new Date()): string {
  const date = new Date(now);
  date.setDate(date.getDate() + DEFAULT_EXPIRY_DAYS);
  return localDateTime(date);
}

/** `?preset=agent_ci` (a link from the developer docs or a bookmark); any other value is no preset. */
export function keyFormPreset(search: string): "agent_ci" | null {
  return new URLSearchParams(search).get("preset") === "agent_ci" ? "agent_ci" : null;
}

/**
 * The query string with the one-time `preset` removed and everything else kept, so a reload or a
 * locale change does not reopen the key form over what the admin typed.
 */
export function withoutKeyFormPreset(search: string): string {
  const params = new URLSearchParams(search);
  params.delete("preset");
  const rest = params.toString();
  return rest ? `?${rest}` : "";
}
