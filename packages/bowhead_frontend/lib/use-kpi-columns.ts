"use client";

import { useEffect, useRef, useState } from "react";

import { kpiColumns } from "@/lib/kpi-grid";

/**
 * Measures a KPI row and returns its balanced column count (`data-cols`), or null until it has been measured
 * (the CSS then uses the width-independent `data-max-cols` fallback). Follows the row's width as the canvas resizes.
 */
export function useKpiColumns<T extends HTMLElement>(count: number) {
  const ref = useRef<T | null>(null);
  const [columns, setColumns] = useState<number | null>(null);

  useEffect(() => {
    const element = ref.current;
    if (!element || typeof ResizeObserver === "undefined") return;
    const measure = () => {
      const width = element.clientWidth;
      setColumns(width > 0 ? kpiColumns(count, width) : null);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [count]);

  return { ref, columns };
}
