"use client";

import { apiJson } from "@/lib/api-client";
import { spaceHeader } from "@/lib/space-storage";

export interface SourceCategory {
  id: string;
  name: string;
  scope: "personal" | "space" | "team";
  aliases: string[];
  parent_id: string | null;
  revision: number;
}

export interface SourceNavigation {
  file_id: string;
  generation_id: string | null;
  source_version: number;
  manual_needs_review?: boolean;
  navigation_revision: number;
  description: string;
  description_kind: "manual" | "standalone" | "matrix" | "name";
  coverage: string;
  categories: Pick<SourceCategory, "id" | "name" | "parent_id" | "revision">[];
  category_options: SourceCategory[];
  can_edit: boolean;
  generation_enabled: boolean;
  generation_status?: "enabled" | "disabled" | "unavailable";
  generation_model: string | null;
  description_job: { id: string; state: string; error_code: string | null; action_id?: string | null } | null;
}

const path = (id: string) => `/api/v1/kb/files/${encodeURIComponent(id)}`;
const headers = () => ({ ...spaceHeader(), "Content-Type": "application/json" });

export function fetchSourceNavigation(id: string, signal?: AbortSignal): Promise<SourceNavigation> {
  return apiJson(`${path(id)}/navigation`, { headers: spaceHeader(), signal, cache: "no-store" });
}

export function saveSourceNavigation(data: SourceNavigation, patch: { manual_description?: string | null; category_ids?: string[] }): Promise<unknown> {
  return apiJson(`${path(data.file_id)}/navigation`, { method: "PATCH", headers: headers(), body: JSON.stringify({
    expected_generation_id: data.generation_id, expected_source_version: data.source_version, expected_revision: data.navigation_revision, ...patch,
  }) });
}

export function generateSourceDescription(data: SourceNavigation, actionId: string): Promise<unknown> {
  return apiJson(`${path(data.file_id)}/description-jobs`, { method: "POST", headers: headers(), body: JSON.stringify({
    expected_generation_id: data.generation_id, expected_source_version: data.source_version, expected_revision: data.navigation_revision, action_id: actionId,
  }) });
}

export function saveSourceCategory(input: { scope: SourceCategory["scope"]; name: string; aliases: string[]; parent_id: string | null; expected_revision?: number }, id?: string): Promise<unknown> {
  return apiJson(`/api/v1/kb/source-categories${id ? `/${encodeURIComponent(id)}` : ""}`, {
    method: id ? "PATCH" : "POST", headers: headers(), body: JSON.stringify(input),
  });
}

export function findSourceCategories(query: string): Promise<{ categories: SourceCategory[]; partial: boolean }> {
  return apiJson(`/api/v1/kb/source-categories?${new URLSearchParams({ query })}`, { headers: spaceHeader(), cache: "no-store" });
}
