/**
 * Two-character marks for Agent avatars.
 *
 * The first two letters of a name collided for built-ins (Compliance Analyst and Configuration Agent both read "CO"
 * on 116). A mark is now the initials of the first two words ("Compliance Analyst" → "CA"), the first two characters
 * for CJK names ("配置智能体" → "配置"), and a list resolves remaining collisions by trying further letters of the
 * same name ("Configuration Agent" → "CO" when "CA" is taken), so marks in one list stay distinct.
 */
import { createContext, useContext } from "react";

const HAN = /\p{Script=Han}/u;

function words(name: string): string[] {
  return name.split(/[^\p{L}\p{N}]+/u).filter(Boolean);
}

/** Candidate marks for one name, best first. */
function markCandidates(name: string): string[] {
  const trimmed = name.trim();
  if (!trimmed) return ["?"];
  if (HAN.test(trimmed[0] ?? "")) {
    const chars = [...trimmed].filter((ch) => HAN.test(ch));
    const out = [chars.slice(0, 2).join("")];
    for (let i = 2; i < chars.length; i += 1) out.push(`${chars[0]}${chars[i]}`);
    return out;
  }
  const parts = words(trimmed);
  if (parts.length === 0) return [[...trimmed].slice(0, 2).join("").toUpperCase()];
  const first = parts[0]!;
  const letters = parts.join("");
  const out: string[] = [];
  if (parts.length >= 2) out.push(`${first[0]}${parts[1]![0]}`);
  out.push(first.slice(0, 2));
  for (let i = 1; i < letters.length; i += 1) out.push(`${first[0]}${letters[i]}`);
  return [...new Set(out.map((mark) => mark.toUpperCase()))];
}

/** Mark for a single Agent shown on its own (detail headers). */
export function agentMark(name: string): string {
  return markCandidates(name)[0] ?? "?";
}

/** Distinct marks for the names shown together in one list, keyed by name; earlier names keep their first choice. */
export function distinctAgentMarks(names: readonly string[]): Map<string, string> {
  const used = new Set<string>();
  const out = new Map<string, string>();
  for (const name of names) {
    if (out.has(name)) continue;
    const candidates = markCandidates(name);
    const mark = candidates.find((candidate) => !used.has(candidate)) ?? candidates[0] ?? "?";
    used.add(mark);
    out.set(name, mark);
  }
  return out;
}

/** Marks resolved for the list currently on screen; avatars outside a list fall back to `agentMark`. */
export const AgentMarksContext = createContext<ReadonlyMap<string, string> | null>(null);

export function useAgentMark(name: string): string {
  const marks = useContext(AgentMarksContext);
  return marks?.get(name) ?? agentMark(name);
}
