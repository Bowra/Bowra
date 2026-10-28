import type { ChartSpec } from "./types";

/** Why these rows are not a flow a Sankey can honestly draw, or `null` when they are. */
export type NotAFlowReason = "disjoint_links";

/**
 * Is this a flow network, or a list of labelled values drawn as bands?
 *
 * Independent stage-to-label rows form disconnected bands. Comparing their widths can also compare incompatible
 * quantities, such as visit counts and revenue, without representing a flow between stages.
 *
 * Criterion: **no node is shared by two links** (`disjoint_links`). Every band is independent; the Sankey adds
 * nothing but a width comparison between rows that may not even share a unit.
 *
 * ⚠ No conservation rule ("a node may not emit more than it receives"): real user-path flows have entries the data
 * does not model, so it misfires on honest charts, and it cannot tell mixed units from unmodelled entries.
 *
 * Mirrors `chart/infer.py` `_not_a_flow_reason` (new charts become tables there); this side covers charts persisted
 * before. Same column pick and link filter as `buildSankey` (`pickAxes`: categorical/date; positive flows only).
 */
export function notAFlowReason(spec: ChartSpec): NotAFlowReason | null {
  const cols = spec.columns ?? [];
  const dims = cols.filter((c) => c.type === "categorical" || c.type === "date");
  const flowCol = cols.find((c) => c.type === "numeric");
  if (dims.length < 2 || !flowCol) return null;
  const [fromKey, toKey] = [dims[0]!.key, dims[1]!.key];
  const degree = new Map<string, number>();
  let links = 0;
  for (const r of spec.rows ?? []) {
    const from = String(r[fromKey] ?? "");
    const to = String(r[toKey] ?? "");
    if (!from || !to || !(Number(r[flowCol.key] ?? 0) > 0)) continue;
    links += 1;
    degree.set(from, (degree.get(from) ?? 0) + 1);
    degree.set(to, (degree.get(to) ?? 0) + 1);
  }
  if (links && [...degree.values()].every((d) => d === 1)) return "disjoint_links";
  return null;
}
