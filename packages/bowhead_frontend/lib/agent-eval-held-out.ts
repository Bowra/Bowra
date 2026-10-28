/**
 * Held-out cases (alignment loop A6, R16). A held-out case is hidden from Coach except as a pass count, so it
 * shows whether a change really improved the Agent. The flag lives in `expected_contract.held_out` and is saved
 * with the suite like any other case field; only the Owner can change it. Each aligned point keeps one
 * held-out case: holding out another case of the same point moves the flag there.
 */

interface CaseLike {
  expected_contract: Record<string, unknown>;
}

export function isHeldOut(item: CaseLike): boolean {
  return item.expected_contract?.held_out === true;
}

/** Ids of the case's points that came from the alignment loop (the ones held-out cases check). */
export function alignedPointIds(item: CaseLike): string[] {
  const points = item.expected_contract?.points;
  if (!Array.isArray(points)) return [];
  return points.flatMap((point) => {
    if (typeof point !== "object" || point === null) return [];
    const { id, origin } = point as { id?: unknown; origin?: { loop_id?: unknown } | null };
    return typeof id === "string" && origin && typeof origin.loop_id === "string" && origin.loop_id ? [id] : [];
  });
}

function withHeldOut<T extends CaseLike>(item: T, on: boolean): T {
  const rest = { ...(item.expected_contract ?? {}) };
  delete rest.held_out;
  // Off is stored as absent, the server default, so a case that was never held out keeps its hash.
  return { ...item, expected_contract: on ? { ...rest, held_out: true } : rest };
}

/** Turn a case's held-out flag on or off; turning it on moves the flag from other cases of the same aligned points. */
export function setHeldOut<T extends CaseLike>(cases: T[], index: number, on: boolean): T[] {
  const shared = new Set(alignedPointIds(cases[index]));
  return cases.map((item, candidate) => {
    if (candidate === index) return withHeldOut(item, on);
    if (on && isHeldOut(item) && alignedPointIds(item).some((id) => shared.has(id))) return withHeldOut(item, false);
    return item;
  });
}

/** Whether turning this case's flag off leaves one of its aligned points without any held-out case. */
export function isOnlyHeldOut(cases: CaseLike[], index: number): boolean {
  const item = cases[index];
  if (!item || !isHeldOut(item)) return false;
  return alignedPointIds(item).some((id) =>
    !cases.some((other, candidate) => candidate !== index && isHeldOut(other) && alignedPointIds(other).includes(id)));
}
