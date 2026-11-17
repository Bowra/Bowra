import type { AgentAuthoringTool } from "./agents-api";

/** The Team Tools of one connection, as the Agent and Plugin editors group them. */
export interface ToolConnectionGroup {
  id: string;
  name: string;
  tools: AgentAuthoringTool[];
  selectable: boolean;
}

/** Stable connection identity of a Team Tool; null for Bowhead's own Tools. */
export function toolConnectionId(tool: AgentAuthoringTool): string | null {
  if (tool.source_kind !== "team_mcp") return null;
  if (tool.connection_id) return tool.connection_id;
  const parts = tool.stable_ref.split(".");
  return parts.length >= 3 && parts[0] === "teammcp" ? parts[1] : null;
}

/** Team Tools grouped by connection, connections by name and Tools by reference. */
export function groupToolConnections(tools: AgentAuthoringTool[]): ToolConnectionGroup[] {
  const grouped = new Map<string, ToolConnectionGroup>();
  for (const tool of tools) {
    const id = toolConnectionId(tool);
    if (!id) continue;
    const current = grouped.get(id);
    if (current) {
      current.tools.push(tool);
      current.selectable ||= tool.selectable;
      continue;
    }
    grouped.set(id, {
      id,
      name: tool.source_display_name || id,
      tools: [tool],
      selectable: tool.selectable,
    });
  }
  return [...grouped.values()]
    .map((group) => ({ ...group, tools: group.tools.sort((left, right) => left.stable_ref.localeCompare(right.stable_ref)) }))
    .sort((left, right) => left.name.localeCompare(right.name));
}
