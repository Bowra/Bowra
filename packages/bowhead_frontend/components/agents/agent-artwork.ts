import type { CSSProperties } from "react";

type AgentArtworkStyle = CSSProperties & {
  "--agent-artwork-base": string;
  "--agent-artwork-glow": string;
  "--agent-artwork-shadow": string;
};

export function agentArtworkStyle(identity: string): AgentArtworkStyle {
  let hash = 2166136261;
  for (const character of identity) {
    hash ^= character.codePointAt(0) ?? 0;
    hash = Math.imul(hash, 16777619);
  }
  const hue = Math.abs(hash) % 360;
  const accentHue = (hue + 32 + (Math.abs(hash >> 8) % 54)) % 360;
  return {
    "--agent-artwork-base": `hsl(${hue} 27% 43%)`,
    "--agent-artwork-glow": `hsl(${accentHue} 54% 76% / 0.7)`,
    "--agent-artwork-shadow": `hsl(${hue} 38% 20% / 0.48)`,
  };
}
