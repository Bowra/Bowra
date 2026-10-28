import { describe, expect, it } from "vitest";

import type { KbRecordDTO, Variant } from "@/lib/kb-knowledge-tables-api";

import { locateExcerpt } from "@/components/knowledge/matrix/cite-popover";
import { buildCell, canManageTemplate, classifyWsRole, docTypeIdFromViewId, viewIdFor } from "./template-view";

function rec(fields: Record<string, Variant[]>, over: Partial<KbRecordDTO> = {}): KbRecordDTO {
  return {
    record_id: "rec_1",
    doc_type_id: "dt_1",
    file_id: "f_1",
    workspace_id: "ws_1",
    fields,
    verify_status: "ready",
    edited_by_user: [],
    extractor_model: "m",
    extraction_duration_ms: null,
    extracted_at: "2026-07-15T10:00:00",
    last_modified_at: "2026-07-15T10:00:00",
    last_modified_by: "u",
    ...over,
  };
}

function variant(normalized: Record<string, unknown>, over: Partial<Variant> = {}): Variant {
  return {
    raw: null,
    normalized,
    effective_at: null,
    is_primary: true,
    extracted_by: "tier1",
    confidence: 0.9,
    source_excerpt: "本协议项下年度采购总金额为人民币壹佰贰拾捌万元整",
    chunk_id: "ck_1",
    page: 3,
    ...over,
  };
}

describe("buildCell", () => {
  it("raw_string 哨兵 → needs_review（不符列类型）", () => {
    const r = rec({ 金额: [variant({ raw_string: "面议（按季度结算）" })] });
    const cell = buildCell(r, "金额", r.extracted_at);
    expect(cell?.status).toBe("needs_review");
    expect(cell?.value).toBe("面议（按季度结算）");
  });

  it("amount 型 normalized → done + 数值", () => {
    const r = rec({ 金额: [variant({ amount: 1280000 })] });
    const cell = buildCell(r, "金额", r.extracted_at);
    expect(cell?.status).toBe("done");
    expect(cell?.value).toBe("1280000");
    expect(cell?.chunkId).toBe("ck_1");
  });

  it("edited_by_user 字段名列表 → per-cell edited；approved → verified 行级映射", () => {
    const r = rec(
      { 金额: [variant({ amount: 1 })] },
      { edited_by_user: ["金额"], verify_status: "approved" },
    );
    const cell = buildCell(r, "金额", r.extracted_at);
    expect(cell?.edited).toBe(true);
    expect(cell?.verified).toBe(true);
  });

  it("字段缺失 → undefined（渲染 empty「—」）", () => {
    const r = rec({});
    expect(buildCell(r, "金额", r.extracted_at)).toBeUndefined();
  });
});

describe("locateExcerpt（M1 两级定位：规范化 → 精确子串）", () => {
  it("空白差异不影响命中，返回原文偏移的高亮切片", () => {
    const content = "第四条 价款与结算 4.1 本协议项下年度采购总金额为人民币壹佰贰拾捌万元整（¥1,280,000），乙方按月开具发票。";
    const hit = locateExcerpt(content, "本协议项下 年度采购总金额为人民币壹佰贰拾捌万元整");
    expect(hit).not.toBeNull();
    expect(hit!.hit).toContain("年度采购总金额");
    expect(content).toContain(hit!.hit);
  });

  it("未命中 → null（走降级小字）", () => {
    expect(locateExcerpt("完全无关的文本", "本协议项下年度采购总金额")).toBeNull();
    expect(locateExcerpt("任意", undefined)).toBeNull();
  });
});

describe("view id round-trip", () => {
  it("tpl_ 前缀合成 id", () => {
    expect(viewIdFor("dt_x")).toBe("tpl_dt_x");
    expect(docTypeIdFromViewId("tpl_dt_x")).toBe("dt_x");
    expect(docTypeIdFromViewId("mx_real")).toBeNull();
  });
});

describe("classifyWsRole（镜像服务端 normalize_workspace_role 别名）", () => {
  it("ws_admin 等价原始角色 → isAdmin+canEdit", () => {
    for (const raw of ["ws_admin", "admin", "owner", "workspace_admin", "ADMIN", " Owner "]) {
      expect(classifyWsRole(raw)).toEqual({ canEdit: true, isAdmin: true });
    }
  });
  it("普通成员 → canEdit 但非 admin", () => {
    expect(classifyWsRole("member")).toEqual({ canEdit: true, isAdmin: false });
    expect(classifyWsRole("user")).toEqual({ canEdit: true, isAdmin: false });
  });
  it("viewer/read_only → 非 canEdit 非 admin", () => {
    for (const raw of ["viewer", "readonly", "read_only", "READ_ONLY"]) {
      expect(classifyWsRole(raw)).toEqual({ canEdit: false, isAdmin: false });
    }
  });
  it("空/未知 → canEdit fail-open、非 admin", () => {
    // Unknown roles affect only admin classification (conservative false); canEdit is true for non-viewers (member default)
    expect(classifyWsRole(null)).toEqual({ canEdit: true, isAdmin: false });
    expect(classifyWsRole(undefined)).toEqual({ canEdit: true, isAdmin: false });
    expect(classifyWsRole("")).toEqual({ canEdit: true, isAdmin: false });
  });
});

describe("canManageTemplate（镜像服务端 _require_admin_or_creator）", () => {
  it("admin 可管理任意模板（含他人创建）", () => {
    expect(canManageTemplate("u_other", "u_me", true)).toBe(true);
    expect(canManageTemplate(null, "u_me", true)).toBe(true);
  });
  it("非 admin 仅可管理自己创建的", () => {
    expect(canManageTemplate("u_me", "u_me", false)).toBe(true);
    expect(canManageTemplate("u_other", "u_me", false)).toBe(false);
  });
  it("uid 或 created_by 缺失 → 保守 false（非 admin 时）", () => {
    expect(canManageTemplate("u_me", null, false)).toBe(false);
    expect(canManageTemplate(null, "u_me", false)).toBe(false);
    expect(canManageTemplate("", "", false)).toBe(false);
  });
});

describe("spaceEditPermsFrom（由会话与空间角色算编辑权限，不发请求）", () => {
  it("团队管理员：可编辑、按管理员处理", async () => {
    const { spaceEditPermsFrom } = await import("./template-view");
    expect(spaceEditPermsFrom({ id: "u1", is_team_admin: true }, "viewer")).toEqual({ canEdit: true, uid: "u1", isAdmin: true });
  });
  it("空间角色决定编辑与管理员；viewer 只读", async () => {
    const { spaceEditPermsFrom } = await import("./template-view");
    expect(spaceEditPermsFrom({ id: "u1" }, "ws_admin")).toEqual({ canEdit: true, uid: "u1", isAdmin: true });
    expect(spaceEditPermsFrom({ id: "u1" }, "member")).toEqual({ canEdit: true, uid: "u1", isAdmin: false });
    expect(spaceEditPermsFrom({ id: "u1" }, "viewer")).toEqual({ canEdit: false, uid: "u1", isAdmin: false });
  });
});
