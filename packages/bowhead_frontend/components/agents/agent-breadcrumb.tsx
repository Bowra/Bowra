"use client";

import type { MouseEvent } from "react";
import { ChevronLeft } from "lucide-react";

import { CapabilityLink } from "@/components/capabilities/capability-link";
import { LucideGlyph } from "@/components/icon-refresh";
import { appPath } from "@/lib/base-path";

/**
 * Drill-down breadcrumb (Agents & Reports polish spec D1): "‹ Parent" in 12px ink-4. Only drill-down pages use it —
 * the sibling Marketplace / My agents / Top Charts pages switch with header tabs instead.
 */
export function AgentBreadcrumb({
  href,
  label,
  onClick,
  documentLink = false,
}: {
  href: string;
  label: string;
  onClick?: (event: MouseEvent<HTMLAnchorElement>) => void;
  /** Plain document navigation, for pages that predate client routing (release review). */
  documentLink?: boolean;
}) {
  const content = (
    <>
      <LucideGlyph icon={ChevronLeft} size={14} aria-hidden="true" />
      <span>{label}</span>
    </>
  );
  if (documentLink) return <a className="am-back" href={appPath(href)}>{content}</a>;
  return <CapabilityLink className="am-back" href={href} onClick={onClick}>{content}</CapabilityLink>;
}
