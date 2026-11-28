/** Test fixtures for the knowledge base target picker, the move dialog, the upload dialog and the Files page. */
import type { KnowledgeBaseDTO, KnowledgeBaseDirectory } from "@/lib/knowledge-bases-api";

export function knowledgeBase(over: Partial<KnowledgeBaseDTO> & Pick<KnowledgeBaseDTO, "id">): KnowledgeBaseDTO {
  return {
    is_default: false,
    home: "space",
    home_id: "ws_1",
    name: "门店运营手册",
    description: null,
    audience: "space",
    audience_level: "use",
    level: "edit",
    owner: { user_id: "u_owner", name: "王蕾" },
    can: { add_files: true, manage: false, transfer: false, delete: false },
    file_count: 0,
    failed_count: 0,
    updated_at: null,
    revision: 1,
    ...over,
  };
}

const CANNOT_ADD = { add_files: false, manage: false, transfer: false, delete: false };

/** My: default + 竞品资料; Space 商超零售场景: default (edit) + 门店运营手册 + 供应商合同 (use only); Team: default + 公司制度 (use only). */
export function directoryFixture(over: { spaceDefaultEditable?: boolean } = {}): KnowledgeBaseDirectory {
  const spaceDefaultEditable = over.spaceDefaultEditable ?? true;
  return {
    space_id: "ws_1",
    sections: {
      personal: [
        knowledgeBase({ id: "kb_my", is_default: true, home: "personal", home_id: "u_1", name: null, audience: "owner", level: "manage", owner: null }),
        knowledgeBase({ id: "kb_comp", home: "personal", home_id: "u_1", name: "竞品资料", audience: "owner", level: "manage", owner: { user_id: "u_1", name: "我" } }),
      ],
      space: [
        knowledgeBase({
          id: "kb_space",
          is_default: true,
          name: null,
          audience_level: "edit",
          level: spaceDefaultEditable ? "edit" : "use",
          owner: null,
          can: spaceDefaultEditable ? { add_files: true, manage: false, transfer: false, delete: false } : CANNOT_ADD,
        }),
        knowledgeBase({ id: "kb_ops" }),
        knowledgeBase({ id: "kb_contracts", name: "供应商合同", audience: "members", level: "use", can: CANNOT_ADD }),
      ],
      team: [
        knowledgeBase({ id: "kb_team", is_default: true, home: "team", home_id: "team_1", name: null, audience: "team", level: "use", owner: null, can: CANNOT_ADD }),
        knowledgeBase({ id: "kb_policy", home: "team", home_id: "team_1", name: "公司制度", audience: "team", level: "use", can: CANNOT_ADD }),
      ],
    },
    can_create: { personal: true, space: true, team: false },
  };
}

export const SPACES_FIXTURE = [{ id: "ws_1", name: "商超零售场景", desc: "" }];
