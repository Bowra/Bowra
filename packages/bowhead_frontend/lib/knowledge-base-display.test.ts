// @vitest-environment node
import { describe, expect, it } from "vitest";

import en from "@/messages/en-US.json";
import zh from "@/messages/zh-CN.json";

import {
  audienceLabelKey,
  homeLabel,
  knowledgeBaseFileHref,
  knowledgeBaseHref,
  knowledgeBaseName,
  levelLabelKey,
  resolveKnowledgeBasePageTab,
} from "./knowledge-base-display";

function translator(catalog: { knowledge_bases: { common: Record<string, string> } }) {
  return (key: string) => catalog.knowledge_bases.common[key] ?? key;
}

describe("knowledgeBaseName", () => {
  const zhT = translator(zh);
  const enT = translator(en);
  const spaceName = (id: string) => (id === "ws_1" ? "商超零售场景" : null);

  it("renders default knowledge bases from their home", () => {
    expect(knowledgeBaseName({ is_default: true, home: "personal", home_id: "u_1", name: null }, zhT)).toBe("我的知识");
    expect(knowledgeBaseName({ is_default: true, home: "team", home_id: "t_1", name: null }, zhT)).toBe("团队知识库");
    expect(knowledgeBaseName({ is_default: true, home: "space", home_id: "ws_1", name: null }, zhT, spaceName)).toBe("商超零售场景");
    expect(knowledgeBaseName({ is_default: true, home: "personal", home_id: "u_1", name: null }, enT)).toBe("My knowledge");
  });

  it("falls back to a neutral Space name when the Space is unknown", () => {
    expect(knowledgeBaseName({ is_default: true, home: "space", home_id: "ws_9", name: null }, zhT, spaceName)).toBe("空间知识库");
  });

  it("uses the stored name of a named knowledge base", () => {
    expect(knowledgeBaseName({ is_default: false, home: "space", home_id: "ws_1", name: "门店运营手册" }, zhT, spaceName)).toBe("门店运营手册");
  });
});

describe("labels", () => {
  it("maps audiences, with the default level of all Space members", () => {
    expect(audienceLabelKey({ audience: "owner", audience_level: "use" })).toBe("audience_owner");
    expect(audienceLabelKey({ audience: "members", audience_level: "use" })).toBe("audience_members");
    expect(audienceLabelKey({ audience: "space", audience_level: "edit" })).toBe("audience_space_edit");
    expect(audienceLabelKey({ audience: "space", audience_level: "use" })).toBe("audience_space_use");
    expect(audienceLabelKey({ audience: "team", audience_level: "use" })).toBe("audience_team");
  });

  it("shows Owner only to the owner", () => {
    const kb = { level: "manage" as const, owner: { user_id: "u_1", name: "王蕾" } };
    expect(levelLabelKey(kb, "u_1")).toBe("level_owner");
    expect(levelLabelKey(kb, "u_2")).toBe("level_manage");
    expect(levelLabelKey({ level: "use", owner: null }, "u_1")).toBe("level_use");
  });

  it("names homes, the Space by its name", () => {
    const t = translator(zh);
    expect(homeLabel("personal", t)).toBe("我的");
    expect(homeLabel("space", t, "商超零售场景")).toBe("商超零售场景");
    expect(homeLabel("team", t)).toBe("团队");
  });

  it("has every common key in each locale", () => {
    expect(Object.keys(en.knowledge_bases.common).sort()).toEqual(Object.keys(zh.knowledge_bases.common).sort());
  });
});

describe("addresses", () => {
  it("builds static-export-compatible knowledge base addresses with a trailing slash", () => {
    expect(knowledgeBaseHref("kbase_1")).toBe("/knowledge/base/?id=kbase_1");
    expect(knowledgeBaseHref("kbase_1", { tab: "files" })).toBe("/knowledge/base/?id=kbase_1");
    expect(knowledgeBaseHref("kbase_1", { tab: "members" })).toBe("/knowledge/base/?id=kbase_1&tab=members");
    expect(knowledgeBaseHref("kbase_1", { fileId: "kbf_2", chunkId: "c_3" })).toBe("/knowledge/base/?id=kbase_1&file_id=kbf_2&chunk_id=c_3");
    expect(knowledgeBaseFileHref("kbf_2")).toBe("/knowledge/base/?file_id=kbf_2");
    expect(knowledgeBaseFileHref("kbf_2", "c_3")).toBe("/knowledge/base/?file_id=kbf_2&chunk_id=c_3");
  });

  it("opens Files for an unknown tab", () => {
    expect(resolveKnowledgeBasePageTab("members")).toBe("members");
    expect(resolveKnowledgeBasePageTab("settings")).toBe("settings");
    expect(resolveKnowledgeBasePageTab("bogus")).toBe("files");
    expect(resolveKnowledgeBasePageTab(null)).toBe("files");
  });
});
