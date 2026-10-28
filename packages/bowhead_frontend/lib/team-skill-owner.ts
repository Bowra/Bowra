import type { TeamSkillPublic } from "@/lib/workflow-solidify-api";

/**
 * Who a team Skill belongs to, for display: the creator's name from the list API, or
 * `fallback` (a localized "Team member" label). Never the raw `usr_*` id — older
 * servers echoed the id as the name when the creator could not be resolved.
 */
export function teamSkillOwnerName(
  skill: Pick<TeamSkillPublic, "created_by" | "created_by_name">,
  fallback: string,
): string {
  const name = skill.created_by_name?.trim();
  return name && name !== skill.created_by?.trim() ? name : fallback;
}
