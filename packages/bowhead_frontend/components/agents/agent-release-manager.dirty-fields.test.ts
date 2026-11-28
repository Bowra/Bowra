import { describe, expect, it } from "vitest";

import { editorDraftDirtyFields } from "./agent-release-manager";

const SAVED = {
  displayName: "门店周报",
  description: "一句话",
  sourceLocale: "zh-CN",
  listingExtras: {},
  systemPrompt: "你是门店周报助手。",
  maxIterations: 10,
  capabilityProtocolBlock: false,
  model: "analyst",
  outputSchema: "",
  primaryCapabilityRef: null,
  pluginIds: [],
  builtinPluginIds: [],
  toolConnectionIds: [],
  toolRefs: ["datarc_query"],
  skillNames: [],
  skillGrants: [],
  extensionFields: {},
  knowledgeAccess: null,
  audienceMode: "private",
  userIds: [],
  groupIds: [],
};

const print = (value: object) => JSON.stringify(value);

describe("editorDraftDirtyFields (the trial save rule)", () => {
  it("names only 工作说明 when just the instructions changed", () => {
    expect(editorDraftDirtyFields(print(SAVED), print({ ...SAVED, systemPrompt: "你是门店周报助手。只用查到的数字。" }))).toEqual(["instructions"]);
  });

  it("groups each field by the name the Owner sees, in page order", () => {
    expect(editorDraftDirtyFields(print(SAVED), print({
      ...SAVED,
      audienceMode: "custom",
      userIds: ["u_2"],
      toolRefs: ["datarc_query", "datarc_schema"],
      description: "新的一句话",
      maxIterations: 12,
    }))).toEqual(["description", "resources", "audience", "advanced"]);
  });

  it("has nothing to save when the draft is unchanged", () => {
    expect(editorDraftDirtyFields(print(SAVED), print(SAVED))).toEqual([]);
  });
});
