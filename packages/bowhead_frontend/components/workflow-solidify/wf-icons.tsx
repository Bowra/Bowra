"use client";
// wf-icons.tsx — workflow-solidify line icons (ported verbatim from Claude Design handoff
// icons.jsx P-map + solidify-shared SP-map, merged). Lucide-style, currentColor stroke.
import React from "react";

// merged path registry: base (icons.jsx) ∪ solidify extras (solidify-shared SP).
const P: Record<string, string> = {
  // base
  plus: "M5 12h14|M12 5v14",
  menu: "M4 6h16|M4 12h16|M4 18h16",
  search: "circle:11,11,7|M21 21l-4.3-4.3",
  chevronDown: "M6 9l6 6 6-6",
  chevronRight: "M9 6l6 6-6 6",
  chevronLeft: "M15 6l-6 6 6 6",
  x: "M18 6 6 18|M6 6l12 12",
  check: "M20 6 9 17l-5-5",
  rotateCw: "M21 12a9 9 0 1 1-3.36-7M21 3v6h-6",
  copy: "rect:9,9,13,13,2|M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1",
  code: "M16 18l6-6-6-6|M8 6l-6 6 6 6",
  info: "circle:12,12,10|M12 16v-4|M12 8h.01",
  arrowUpRight: "M7 7h10v10|M7 17 17 7",
  alertTriangle: "M21.73 18l-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3z|M12 9v4|M12 17h.01",
  clock: "circle:12,12,10|M12 6v6l4 2",
  lock: "rect:3,11,21,21,2|M7 11V7a5 5 0 0 1 10 0v4",
  slash: "circle:12,12,10|M4.93 4.93l14.14 14.14",
  trash: "M3 6h18|M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2|M10 11v6|M14 11v6",
  pin: "M12 17v5|M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z",
  // solidify extras (SP)
  crystal: "M6 3h12l4 6-10 13L2 9z|M11 3 8 9l4 13 4-13-3-6|M2 9h20",
  flask: "M9 3h6|M10 3v6.5L4.7 18A2 2 0 0 0 6.4 21h11.2a2 2 0 0 0 1.7-3L14 9.5V3|M7.5 15h9",
  route: "circle:6,19,3|circle:18,5,3|M12 19h4.5a3.5 3.5 0 0 0 0-7h-8a3.5 3.5 0 0 1 0-7H12",
  layers: "M12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.84z|M2 12.18l8.58 3.9a2 2 0 0 0 1.66 0l8.58-3.9|M2 16.18l8.58 3.9a2 2 0 0 0 1.66 0l8.58-3.9",
  user: "M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2|circle:12,7,4",
  users: "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2|circle:9,7,4|M22 21v-2a4 4 0 0 0-3-3.87|M16 3.13a4 4 0 0 1 0 7.75",
  globe: "circle:12,12,10|M2 12h20|M12 2a15 15 0 0 1 0 20 15 15 0 0 1 0-20",
  bot: "rect:3,11,21,21,2|M7 11V7a5 5 0 0 1 10 0v4|circle:9,16,1|circle:15,16,1|M12 3v2",
  hash: "M4 9h16|M4 15h16|M10 3 8 21|M16 3l-2 18",
  pencil: "M12 20h9|M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z",
  target: "circle:12,12,10|circle:12,12,6|circle:12,12,2",
  listChecks: "M11 6h10|M11 12h10|M11 18h10|M3 6l1.5 1.5L7 4|M3 13l1.5 1.5L7 11",
  sliders: "M4 21v-7|M4 10V3|M12 21v-9|M12 8V3|M20 21v-5|M20 12V3|M1 14h6|M9 8h6|M17 16h6",
  scissors: "circle:6,6,3|circle:6,18,3|M20 4 8.12 15.88|M14.47 14.48 20 20|M8.12 8.12 12 12",
  play: "M5 3l14 9-14 9z",
  arrowRight: "M5 12h14|M13 6l6 6-6 6",
  cornerRight: "M15 10l5 5-5 5|M4 4v7a4 4 0 0 0 4 4h12",
  eye: "M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z|circle:12,12,3",
  eyeOff: "M9.88 9.88a3 3 0 0 0 4.24 4.24|M10.73 5.08A10.43 10.43 0 0 1 12 5c6.5 0 10 7 10 7a13.16 13.16 0 0 1-1.67 2.68|M6.61 6.61A13.526 13.526 0 0 0 2 12s3.5 7 10 7a9.74 9.74 0 0 0 5.39-1.61|M2 2l20 20",
  powerOff: "M18.36 6.64a9 9 0 1 1-12.73 0|M12 2v10",
  sparkle: "M9.94 3.66 11 7l1.06-3.34A1 1 0 0 1 13 3l3.34 1.06L13 5.12 16.34 6.18 13 7.24 11.94 10.58 10.88 7.24 7.54 6.18 10.88 5.12z",
  minusCircle: "circle:12,12,10|M8 12h8",
  helpCircle: "circle:12,12,10|M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3|M12 17h.01",
};

function seg(d: string, i: number) {
  if (d.startsWith("circle:")) {
    const [cx, cy, r] = d.slice(7).split(",").map(Number);
    return React.createElement("circle", { key: i, cx, cy, r });
  }
  if (d.startsWith("rect:")) {
    const [x, y, x2, y2, rx] = d.slice(5).split(",").map(Number);
    return React.createElement("rect", { key: i, x, y, width: x2 - x, height: y2 - y, rx });
  }
  return React.createElement("path", { key: i, d });
}

export interface WfIconProps {
  name: string;
  s?: number;
  stroke?: number;
  style?: React.CSSProperties;
  className?: string;
}

export function WfIcon({ name, s = 18, stroke = 2, style, className }: WfIconProps) {
  const raw = P[name];
  if (!raw) return null;
  return React.createElement(
    "svg",
    {
      width: s, height: s, viewBox: "0 0 24 24", fill: "none",
      stroke: "currentColor", strokeWidth: stroke, strokeLinecap: "round" as const,
      strokeLinejoin: "round" as const, "aria-hidden": true, className, style,
    },
    raw.split("|").map(seg),
  );
}
