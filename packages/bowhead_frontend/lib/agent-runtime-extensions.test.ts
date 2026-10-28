import { describe, expect, it } from "vitest";
import { emptyRuntimeExtensions, optionalSkillsNeedPrivateAudience, readOptionalSkillCatalog, readRuntimeExtensions } from "./agent-runtime-extensions";

describe("Optional method editor contract", () => {
  it("preserves supported policy fields and detaches editable arrays", () => {
    const policy = emptyRuntimeExtensions();
    policy.skill_read.resource_allowlist = [{ source_kind: "builtin", id: "builtin:dupont", version: null }];
    const result = readRuntimeExtensions(policy);
    expect(result).toEqual(policy);
    if (result !== null && result !== "unsupported") result.skill_read.resource_allowlist.length = 0;
    expect(policy.skill_read.resource_allowlist).toHaveLength(1);
    expect(readRuntimeExtensions(null)).toBeNull();
    expect(readRuntimeExtensions(undefined)).toBeNull();
  });

  it.each(["version", "unknown_field", "invoke", "empty_enabled", "too_many", "duplicate", "undeclared", "bad_hash"])("keeps %s opaque instead of rebuilding a weaker policy", (change) => {
    const policy = emptyRuntimeExtensions();
    expect(readRuntimeExtensions(policy)).toEqual(policy);
    const value = policy as unknown as Record<string, unknown>;
    switch (change) {
      case "version": value.schema_version = 2; break;
      case "unknown_field": value.future = true; break;
      case "invoke": Object.assign(policy.skill_read, { invoke: true }); break;
      case "empty_enabled": policy.skill_read.enabled = true; break;
      case "too_many": policy.skill_read.resource_allowlist = Array.from({ length: 33 }, (_, i) => ({ source_kind: "builtin", id: `builtin:m${i}` })); break;
      case "duplicate": policy.skill_read.resource_allowlist = [{ source_kind: "builtin", id: "same" }, { source_kind: "builtin", id: "same" }]; break;
      case "undeclared": policy.skill_read.resource_allowlist = [{ source_kind: "user", id: "personal" }]; break;
      case "bad_hash": policy.skill_read.resource_allowlist = [{ source_kind: "builtin", id: "method", version: "latest" }]; break;
    }
    expect(readRuntimeExtensions(value)).toBe("unsupported");
  });

  it("keeps personal selectors private even while optional use is disabled", () => {
    const policy = emptyRuntimeExtensions();
    policy.skill_read.allowed_sources.push("user");
    policy.skill_read.resource_allowlist = [{ source_kind: "user", id: "personal" }];
    expect(optionalSkillsNeedPrivateAudience(policy, "private")).toBe(false);
    expect(optionalSkillsNeedPrivateAudience(policy, "teamwide")).toBe(true);
  });

  it.each(["agent", "offset", "personal_audience", "unpinned_resource", "unknown_field"])("rejects %s catalog data before offering a selection", (change) => {
    const value = { kind: "agent_optional_skill_catalog", schema_version: 1, agent_id: "osa_1", next_offset: 1,
      items: [{ resource: { kind: "skill", source_kind: "user", id: "personal", version: `sha256:${"a".repeat(64)}` }, label: "Method", description: "", allowed_audiences: ["private"] }] };
    expect(readOptionalSkillCatalog(value, "osa_1", 0)).toEqual(value);
    switch (change) {
      case "agent": value.agent_id = "osa_2"; break;
      case "offset": value.next_offset = 0; break;
      case "personal_audience": value.items[0].allowed_audiences.push("teamwide"); break;
      case "unpinned_resource": Reflect.deleteProperty(value.items[0].resource, "version"); break;
      case "unknown_field": Object.assign(value.items[0].resource, { command: ["run"] }); break;
    }
    expect(() => readOptionalSkillCatalog(value, "osa_1", 0)).toThrow("optional_skill_catalog_invalid");
  });
});
