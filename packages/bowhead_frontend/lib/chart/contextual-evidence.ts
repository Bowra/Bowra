import type { EvidenceNote, EvidencePlanV1 } from "./presentation-plan";

/** Read only the closed source statement already used by annotation admission. */
export function sourceNoteStatement(note: EvidenceNote): { text: string; period: string; source: Record<string, unknown> } | null {
  if (note.availability !== "available" || !note.excerpt) return null;
  try {
    const source = JSON.parse(note.excerpt);
    if (!source || typeof source !== "object" || Array.isArray(source) || !["event", "coverage"].includes(source.role)) return null;
    const bounds = source.role === "event" ? ["at"] : ["lower", "upper"];
    if (Object.keys(source).sort().join() !== ["role", "time_key", "measure_keys", "text", ...bounds].sort().join()) return null;
    if (typeof source.time_key !== "string" || !source.time_key || typeof source.text !== "string" || !source.text.trim() || JSON.stringify(source.measure_keys) !== JSON.stringify(note.measure_keys)) return null;
    if (!bounds.every((key) => (typeof source[key] === "string" && source[key].trim()) || (typeof source[key] === "number" && Number.isFinite(source[key])))) return null;
    return { text: source.text, period: bounds.map((key) => String(source[key])).join(" – "), source };
  } catch { return null; }
}

/** Mirror the producer boundary; references are literal context, not causal proof. */
export function validAnnotationEvidence(plan: EvidencePlanV1): boolean {
  return (plan.annotations ?? []).every((item) => {
    if (item.availability === "unavailable") return true;
    const role = item.role ?? "observation", ids = item.note_ids;
    if (ids === undefined && role === "observation") return true;
    if (plan.version < 2 || !Array.isArray(ids) || !ids.length || ids.length > 3 || new Set(ids).size !== ids.length) return false;
    const notes = ids.map((id) => plan.evidence_notes.find((note) => note.id === id));
    if (notes.some((note) => !note || note.availability !== "available" || !note.measure_keys.includes(item.measure_key))) return false;
    if (role === "observation") return !item.label || notes.some((note) => note!.excerpt!.includes(item.label!));
    if (!["event", "coverage"].includes(role) || notes.length !== 1) return false;
    const statement = sourceNoteStatement(notes[0]!);
    if (!statement) return false;
    const source = statement.source;
    if (source.role !== role || source.time_key !== item.observation_keys[0] || source.text !== item.label) return false;
    return role === "event" ? item.kind === "point" && item.tuple[0] === source.at : item.kind === "interval" && item.lower === source.lower && item.upper === source.upper;
  });
}
