"use client";

import type { ReactNode } from "react";

import { PageFrame } from "@/components/shell/page-frame";

/**
 * Personal capability pages (Skills & Workflows, My Connections) in the shared PageFrame (Header, Language & Paging
 * Spec §2, §6 #10): the shared top bar, the 1120 work column by default and the standard header placement.
 */
export function PersonalCapabilityPage({
  mobileTitle,
  width = "table",
  mobileActions,
  children,
}: {
  /** Page name: the top-bar titles and the page H1. */
  mobileTitle: string;
  /** Content-width tier (Page Layout Spec: form 880 / table 1120). */
  width?: "form" | "table";
  /** Phone top-bar actions (the header actions move there below 768). */
  mobileActions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <PageFrame title={mobileTitle} width={width} mobileActions={mobileActions}>
      {children}
    </PageFrame>
  );
}
