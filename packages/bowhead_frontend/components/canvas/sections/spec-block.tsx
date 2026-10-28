"use client";

/**
 * SpecBlock — S3: unified renderer entry + registry for structured-graph embedded blocks.
 *
 * Establish the L2 pattern: one `spec_block` section kind carries structured graphs (attribution chains/metric trees/flowcharts),
 * dispatching by `format` to registered renderers. First format = "graph" (directed graph; trees are a special case).
 *
 * Why not mermaid: 75MB (transitive d3/cytoscape/katex), with a default skin that is off-brand.
 * Use structured nodes/edges + dagre layout (~45KB) + custom SVG instead (design tokens, dark-mode support,
 * graph numbers can join the verified system). New format = one SPEC_RENDERERS entry.
 *
 * Render failure gracefully falls back to JSON source, never a blank screen.
 */

import { useEffect, useMemo, useState, type ComponentType } from "react";
import { useTranslations } from "next-intl";
import type { ReportSection } from "@/lib/types";

type SpecBlockData = Extract<ReportSection, { kind: "spec_block" }>;

interface LaidOutNode {
  id: string;
  label: string;
  emphasis?: "default" | "highlight" | "muted";
  x: number;
  y: number;
  w: number;
  h: number;
}
interface LaidOutEdge {
  d: string;
  label?: string;
  lx: number;
  ly: number;
}
interface GraphLayout {
  width: number;
  height: number;
  nodes: LaidOutNode[];
  edges: LaidOutEdge[];
}

const NODE_H = 36;

function labelWidth(s: string): number {
  let w = 0;
  for (const ch of s) w += /[一-鿿＀-￯]/.test(ch) ? 14 : 7.4;
  return Math.max(64, Math.min(240, Math.ceil(w) + 26));
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function computeGraphLayout(dagre: any, data: SpecBlockData): GraphLayout {
  const g = new dagre.graphlib.Graph();
  g.setGraph({
    rankdir: data.direction === "LR" ? "LR" : "TB",
    nodesep: 26,
    ranksep: 42,
    marginx: 6,
    marginy: 6,
  });
  g.setDefaultEdgeLabel(() => ({}));
  const byId = new Map((data.nodes || []).map((n) => [n.id, n]));
  for (const n of data.nodes || []) {
    g.setNode(n.id, { width: labelWidth(n.label), height: NODE_H });
  }
  for (const e of data.edges || []) {
    if (byId.has(e.from) && byId.has(e.to)) g.setEdge(e.from, e.to, { width: 1, height: 1 });
  }
  dagre.layout(g);

  const nodes: LaidOutNode[] = (data.nodes || []).map((n) => {
    const gn = g.node(n.id) || { x: 0, y: 0, width: 80, height: NODE_H };
    return { id: n.id, label: n.label, emphasis: n.emphasis, x: gn.x, y: gn.y, w: gn.width, h: gn.height };
  });
  const edges: LaidOutEdge[] = (data.edges || [])
    .filter((e) => byId.has(e.from) && byId.has(e.to))
    .map((e) => {
      const ge = g.edge(e.from, e.to) || { points: [] };
      const pts: { x: number; y: number }[] = ge.points || [];
      const d = pts.length ? "M" + pts.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" L") : "";
      const mid = pts[Math.floor(pts.length / 2)] || { x: 0, y: 0 };
      return { d, label: e.label, lx: mid.x, ly: mid.y };
    });
  const graph = g.graph();
  return { width: Math.max(1, graph.width || 1), height: Math.max(1, graph.height || 1), nodes, edges };
}

function GraphRenderer({ data }: { data: SpecBlockData }) {
  const t = useTranslations("spec_block");
  const [layout, setLayout] = useState<GraphLayout | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Stable data-content key (avoid recalculation on every render).
  const key = useMemo(() => JSON.stringify({ d: data.direction, n: data.nodes, e: data.edges }), [data]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const mod = await import("@dagrejs/dagre");
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const dagre = (mod as any).default ?? mod;
        const result = computeGraphLayout(dagre, data);
        if (!cancelled) {
          setLayout(result);
          setError(null);
        }
      } catch (e) {
        // Layout-failure marker only: UI displays localized render_failed, without the layout library's raw wording.
        if (!cancelled) setError("layout_failed");
        void e;
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  if (error) return <SpecBlockFallback data={data} error={error} />;
  if (!layout) return <div className="rc-spec-loading" aria-busy />;

  const { width, height, nodes, edges } = layout;
  return (
    <div className="rc-spec-graph">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        width={width}
        height={height}
        role="img"
        aria-label={t("diagram_aria")}
        preserveAspectRatio="xMidYMid meet"
      >
        <defs>
          <marker id="rc-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0,0 L10,5 L0,10 z" fill="var(--c-ink-3)" />
          </marker>
        </defs>
        {edges.map((e, i) => (
          <g key={i}>
            {e.d ? <path d={e.d} className="rc-spec-edge" markerEnd="url(#rc-arrow)" /> : null}
            {e.label ? (
              <text x={e.lx} y={e.ly - 3} className="rc-spec-edge-label" textAnchor="middle">
                {e.label}
              </text>
            ) : null}
          </g>
        ))}
        {nodes.map((n) => (
          <g key={n.id} transform={`translate(${n.x - n.w / 2}, ${n.y - n.h / 2})`}>
            <rect
              width={n.w}
              height={n.h}
              rx={8}
              className={
                "rc-spec-node" +
                (n.emphasis === "highlight" ? " rc-spec-node--hl" : n.emphasis === "muted" ? " rc-spec-node--muted" : "")
              }
            />
            <text x={n.w / 2} y={n.h / 2} className="rc-spec-node-label" textAnchor="middle" dominantBaseline="central">
              {n.label}
            </text>
          </g>
        ))}
      </svg>
    </div>
  );
}

/** Format registry: add new structured-graph formats with one entry here only. */
const SPEC_RENDERERS: Record<string, ComponentType<{ data: SpecBlockData }>> = {
  graph: GraphRenderer,
};

function SpecBlockFallback({ data, error }: { data: SpecBlockData; error?: string }) {
  const t = useTranslations("spec_block");
  return (
    <div className="rc-spec-fallback">
      <div className="rc-spec-fallback-note">
        <strong>{t("fallback_title")}</strong>
        <span>{error ? t("render_failed") : t("unsupported_format", { format: data.format })}</span>
      </div>
      <pre className="rc-spec-source">
        <code>{JSON.stringify({ nodes: data.nodes, edges: data.edges }, null, 2)}</code>
      </pre>
    </div>
  );
}

export function SpecBlock({ data }: { data: SpecBlockData }) {
  const Renderer = SPEC_RENDERERS[data.format];
  return (
    <div className="rc-spec-block" data-spec-format={data.format}>
      {data.title ? <div className="rc-block-title">{data.title}</div> : null}
      {Renderer ? <Renderer data={data} /> : <SpecBlockFallback data={data} />}
    </div>
  );
}
