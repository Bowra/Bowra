import type { Audience } from "./content-share-api";

interface TeamSkillAudienceSource {
  audience?: Audience;
  visibility?: string;
  share_count?: number;
}

export function teamSkillAudience(skill: TeamSkillAudienceSource): Audience {
  if (skill.audience === "private" || skill.audience === "custom" || skill.audience === "team_wide") {
    return skill.audience;
  }
  if (skill.visibility === "team_wide") return "team_wide";
  return (skill.share_count ?? 0) > 0 ? "custom" : "private";
}
