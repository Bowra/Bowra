import type { Step } from "./turn-store";

/** Exact declared inputs only. An incomplete historical preview provides no revision proof. */
export function sandboxInputArtifactIds(codeInput: Record<string, unknown>, preview: unknown): string[] {
  if (Array.isArray(codeInput.input_artifact_ids)) {
    return codeInput.input_artifact_ids.filter((id): id is string => typeof id === "string").slice(0, 32);
  }
  try {
    const args = typeof preview === "string" ? JSON.parse(preview) : null;
    let inner = args && typeof args === "object" && "input" in args ? args.input : args;
    if (typeof inner === "string") inner = JSON.parse(inner);
    if (!inner?.inputs || typeof inner.inputs !== "object" || Array.isArray(inner.inputs)) return [];
    const ids = Object.values(inner.inputs).flatMap((item) => {
      const id = item && typeof item === "object" ? (item as Record<string, unknown>).artifact_id : null;
      return typeof id === "string" && id.startsWith("art_") ? [id] : [];
    });
    return [...new Set(ids)].slice(0, 32);
  } catch {
    return [];
  }
}

/** Final answer selection only; trace steps and retained files keep every original output. */
export function selectFinalInlineArtifacts(raw: unknown[], steps: readonly Step[]): unknown[] {
  const identities = new Map<string, string>();
  for (const entry of raw) {
    if (!entry || typeof entry !== "object") continue;
    const file = entry as Record<string, unknown>;
    if (typeof file.id === "string" && typeof file.filename === "string" && typeof file.mime === "string") {
      identities.set(file.id, JSON.stringify([file.filename, file.mime]));
    }
  }
  const superseded = new Set<string>();
  for (const step of steps) {
    if (step.toolName !== "python_sandbox" || step.state !== "done") continue;
    const outputs = step.outputArtifacts ?? [];
    const inputs = step.codeInput?.inputArtifactIds ?? [];
    for (const output of outputs) {
      if (output.dataRole === "stdout") continue;
      const identity = identities.get(output.artifactId);
      if (!identity || identity !== JSON.stringify([output.filename, output.mime])) continue;
      const parents = new Set(inputs.filter((id) => id !== output.artifactId && identities.get(id) === identity));
      const siblings = outputs.filter((item) => JSON.stringify([item.filename, item.mime]) === identity);
      if (parents.size === 1 && siblings.length === 1) {
        for (const parent of parents) superseded.add(parent);
      }
    }
  }
  return raw.filter((entry) => !entry || typeof entry !== "object" || !superseded.has((entry as { id: string }).id));
}
