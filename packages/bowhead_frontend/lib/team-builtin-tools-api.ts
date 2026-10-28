import { apiFetch, readApiError } from "./api-client";

export type TeamBuiltinTool = {
  id: string;
  provider: "tavily" | "datarc" | "powerbi" | "shopify";
  name: string;
  display_name: string;
  endpoint: string;
  key_hint: string | null;
  status: "enabled" | "disabled";
  configured: true;
  created_at: string;
  updated_at: string;
  scope_state: "unknown" | "verified" | "invalid";
  scope: {
    key_id?: string;
    key_name?: string;
    expired_at?: string | null;
    permissions?: string[];
    workspaces?: Array<{ id: string; name: string; desc?: string }>;
  };
  scope_checked_at: string | null;
  supported_tools: string[];
  shop_domain?: string;
  shop_name?: string;
  granted_scopes?: string[];
  connection_revision?: number;
  tenant_id?: string;
  client_id?: string;
  workspace_id?: string;
  dataset_id?: string;
};

async function checked(response: Response): Promise<Response> {
  if (response.ok) return response;
  const error = await readApiError(response);
  // Carry code through: message in the new {detail:{code}} format is generic; callers need code to map specific notices.
  throw Object.assign(new Error(error.message), { status: error.status, code: error.code });
}

export async function listBuiltinTools(): Promise<TeamBuiltinTool[]> {
  const response = await apiFetch("/api/v1/team-builtin-tools");
  return (await checked(response).then((item) => item.json())) as TeamBuiltinTool[];
}

export async function configureTavily(api_key: string, enabled: boolean): Promise<TeamBuiltinTool> {
  const response = await apiFetch("/api/v1/team-builtin-tools/tavily", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ api_key, enabled }) });
  return (await checked(response).then((item) => item.json())) as TeamBuiltinTool;
}

export async function setTavilyEnabled(enabled: boolean): Promise<TeamBuiltinTool> {
  const response = await apiFetch("/api/v1/team-builtin-tools/tavily/status", { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled }) });
  return (await checked(response).then((item) => item.json())) as TeamBuiltinTool;
}

export type DatarcToolInput = { name: string; endpoint: string; connection_key: string; enabled?: boolean };

export async function createDatarcTool(input: DatarcToolInput): Promise<TeamBuiltinTool> {
  const response = await apiFetch("/api/v1/team-builtin-tools/datarc", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  return (await checked(response).then((item) => item.json())) as TeamBuiltinTool;
}

export async function updateDatarcTool(id: string, input: Partial<Omit<DatarcToolInput, "enabled">>): Promise<TeamBuiltinTool> {
  const response = await apiFetch(`/api/v1/team-builtin-tools/datarc/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  return (await checked(response).then((item) => item.json())) as TeamBuiltinTool;
}

export async function setDatarcEnabled(id: string, enabled: boolean): Promise<TeamBuiltinTool> {
  const response = await apiFetch(`/api/v1/team-builtin-tools/datarc/${encodeURIComponent(id)}/status`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  return (await checked(response).then((item) => item.json())) as TeamBuiltinTool;
}

export async function refreshDatarcScope(id: string): Promise<TeamBuiltinTool> {
  const response = await apiFetch(`/api/v1/team-builtin-tools/datarc/${encodeURIComponent(id)}/scope/refresh`, { method: "POST" });
  return (await checked(response).then((item) => item.json())) as TeamBuiltinTool;
}

export async function deleteDatarcTool(id: string): Promise<void> {
  await checked(await apiFetch(`/api/v1/team-builtin-tools/datarc/${encodeURIComponent(id)}`, { method: "DELETE" }));
}

export type PowerBiToolInput = {
  name: string;
  tenant_id: string;
  client_id: string;
  client_secret: string;
  workspace_id: string;
  dataset_id: string;
  enabled?: boolean;
};

export async function createPowerBiTool(input: PowerBiToolInput): Promise<TeamBuiltinTool> {
  return (await checked(await apiFetch("/api/v1/team-builtin-tools/powerbi", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input),
  })).then((item) => item.json())) as TeamBuiltinTool;
}

export async function updatePowerBiTool(id: string, input: Partial<PowerBiToolInput>): Promise<TeamBuiltinTool> {
  return (await checked(await apiFetch(`/api/v1/team-builtin-tools/powerbi/${encodeURIComponent(id)}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input),
  })).then((item) => item.json())) as TeamBuiltinTool;
}

export async function setPowerBiEnabled(id: string, enabled: boolean): Promise<void> {
  await checked(await apiFetch(`/api/v1/team-builtin-tools/powerbi/${encodeURIComponent(id)}/status`, {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled }),
  }));
}

export async function deletePowerBiTool(id: string): Promise<void> {
  await checked(await apiFetch(`/api/v1/team-builtin-tools/powerbi/${encodeURIComponent(id)}`, { method: "DELETE" }));
}

export type PowerBiTemplate = { id: string; name: string; dax_template: string };

export async function listPowerBiTemplates(id: string): Promise<PowerBiTemplate[]> {
  return (await checked(await apiFetch(`/api/v1/team-builtin-tools/powerbi/${encodeURIComponent(id)}/templates`)).then((item) => item.json())) as PowerBiTemplate[];
}

export async function createPowerBiTemplate(id: string, name: string, dax_template: string): Promise<void> {
  await checked(await apiFetch(`/api/v1/team-builtin-tools/powerbi/${encodeURIComponent(id)}/templates`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, dax_template }),
  }));
}

export async function deletePowerBiTemplate(id: string, templateId: string): Promise<void> {
  await checked(await apiFetch(`/api/v1/team-builtin-tools/powerbi/${encodeURIComponent(id)}/templates/${encodeURIComponent(templateId)}`, { method: "DELETE" }));
}


export type ShopifyToolInput = { name: string; shop_domain: string; client_id: string; client_secret: string; enabled?: boolean };

export async function createShopifyTool(input: ShopifyToolInput): Promise<TeamBuiltinTool> {
  return (await checked(await apiFetch("/api/v1/team-builtin-tools/shopify", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input),
  })).then((item) => item.json())) as TeamBuiltinTool;
}

export async function updateShopifyTool(id: string, input: Partial<Omit<ShopifyToolInput, "shop_domain" | "enabled">> & { connection_revision: number }): Promise<TeamBuiltinTool> {
  return (await checked(await apiFetch(`/api/v1/team-builtin-tools/shopify/${encodeURIComponent(id)}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input),
  })).then((item) => item.json())) as TeamBuiltinTool;
}

export async function setShopifyEnabled(id: string, enabled: boolean, connection_revision: number): Promise<void> {
  await checked(await apiFetch(`/api/v1/team-builtin-tools/shopify/${encodeURIComponent(id)}/status`, {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled, connection_revision }),
  }));
}

export async function deleteShopifyTool(id: string): Promise<void> {
  await checked(await apiFetch(`/api/v1/team-builtin-tools/shopify/${encodeURIComponent(id)}`, { method: "DELETE" }));
}

export async function createShopifyPlugin(id: string): Promise<{ id: string }> {
  return (await checked(await apiFetch(`/api/v1/team-builtin-tools/shopify/${encodeURIComponent(id)}/plugin`, { method: "POST" })).then((item) => item.json())) as { id: string };
}

export async function createShopifyContentPlugin(toolId: string): Promise<{ id: string }> {
  return (await checked(await apiFetch(`/api/v1/team-builtin-tools/shopify/${encodeURIComponent(toolId)}/content-plugin`, { method: "POST" })).then((item) => item.json())) as { id: string };
}
