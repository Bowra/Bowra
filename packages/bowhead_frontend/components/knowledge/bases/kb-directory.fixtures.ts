/** Test fixtures for the knowledge base directory, the New and Delete dialogs and the Knowledge page. */
import type { KbFileDTO } from "@/lib/kb-api";
import type { KnowledgeBaseDTO, KnowledgeBaseDirectory } from "@/lib/knowledge-bases-api";

const NONE = { add_files: false, manage: false, transfer: false, delete: false };
const EDIT = { add_files: true, manage: false, transfer: false, delete: false };
const OWNER = { add_files: true, manage: true, transfer: true, delete: true };

export function kbFixture(over: Partial<KnowledgeBaseDTO> & Pick<KnowledgeBaseDTO, "id">): KnowledgeBaseDTO {
  return {
    is_default: false,
    home: "space",
    home_id: "ws_1",
    name: "门店运营手册",
    description: null,
    audience: "space",
    audience_level: "use",
    level: "use",
    owner: { user_id: "u_owner", name: "王蕾" },
    can: NONE,
    file_count: 0,
    failed_count: 0,
    updated_at: "2026-11-20T08:00:00Z",
    revision: 1,
    ...over,
  };
}

/**
 * ① as drawn: My (default + 竞品资料), the Space 商超零售场景 (default with a failure, 门店运营手册 owned by me,
 * 供应商合同 members only and editable) and the Team (default, 公司制度, 医药临床资料). The viewer is u_1.
 */
export function directoryFixture(over: Partial<KnowledgeBaseDirectory> = {}): KnowledgeBaseDirectory {
  return {
    space_id: "ws_1",
    sections: {
      personal: [
        kbFixture({
          id: "kb_my",
          is_default: true,
          home: "personal",
          home_id: "u_1",
          name: null,
          description: "你上传的个人资料",
          audience: "owner",
          level: "manage",
          owner: { user_id: "u_1", name: "我" },
          can: { add_files: true, manage: true, transfer: false, delete: false },
          file_count: 12,
        }),
        kbFixture({
          id: "kb_comp",
          home: "personal",
          home_id: "u_1",
          name: "竞品资料",
          description: "竞品发布会材料和价格表",
          audience: "owner",
          level: "manage",
          owner: { user_id: "u_1", name: "我" },
          can: { add_files: true, manage: true, transfer: false, delete: true },
          file_count: 5,
        }),
      ],
      space: [
        kbFixture({
          id: "kb_space",
          is_default: true,
          name: null,
          description: "空间成员共用的资料",
          audience_level: "edit",
          level: "edit",
          owner: null,
          can: EDIT,
          file_count: 38,
          failed_count: 1,
        }),
        kbFixture({
          id: "kb_ops",
          description: "开店、巡检、陈列标准",
          level: "manage",
          owner: { user_id: "u_1", name: "我" },
          can: OWNER,
          file_count: 21,
        }),
        kbFixture({
          id: "kb_contracts",
          name: "供应商合同",
          description: "采购合同与补充协议",
          audience: "members",
          level: "edit",
          can: EDIT,
          file_count: 7,
        }),
      ],
      team: [
        kbFixture({
          id: "kb_team",
          is_default: true,
          home: "team",
          home_id: "team_1",
          name: null,
          description: "团队共用资料",
          audience: "team",
          level: "use",
          owner: null,
          file_count: 3,
        }),
        kbFixture({ id: "kb_policy", home: "team", home_id: "team_1", name: "公司制度", description: "人事、报销和差旅制度", audience: "team", file_count: 26 }),
        kbFixture({ id: "kb_clinic", home: "team", home_id: "team_1", name: "医药临床资料", audience: "members", file_count: 14 }),
      ],
    },
    can_create: { personal: true, space: true, team: false },
    ...over,
  };
}

export const SPACES = [{ id: "ws_1", name: "商超零售场景", desc: "", role: "member" as const }];

export function kbFileFixture(over: Partial<KbFileDTO> & Pick<KbFileDTO, "file_id">): KbFileDTO {
  return {
    artifact_id: `art_${over.file_id}`,
    filename: `${over.file_id}.pdf`,
    mime_type: "application/pdf",
    size_bytes: 1024,
    scope: "space",
    scope_id: "ws_1",
    workspace_id: "ws_1",
    team_id: "team_1",
    uploaded_by: "u_1",
    uploaded_at: "2026-11-25T08:00:00Z",
    parse_status: "done",
    parse_progress_pct: null,
    parse_layer: null,
    parse_error: null,
    parse_duration_ms: null,
    chunks_count: 3,
    cite_count: 0,
    last_cited_at: null,
    use_llm_marker: false,
    doc_type_id: null,
    ...over,
  } as KbFileDTO;
}
