"""导入出处：作者明知有阻断项仍强开的痕迹（B 线批 4，跨线接口 ⑨）。

一份上传的技能清点出红项后不生效；用户可以点「仍然启用」强开（`import_status='overridden'`）。
它之后还能 promote 到团队，而团队审核者看到的是一份干净的候选——看不到作者当初是带着几条
阻断项按下去的。这个模块把那个事实算出来，供 promote 路径在**服务端**写进团队技能行。

两条硬规则：
- **只从个人实例现算**，绝不接受调用方传入的成品（客户端可传＝作者能传一份 red_count:0
  的干净出处瞒过审批，跨 session 定案）。
- 只带红项。黄的不阻断、不影响审批判断，带上只会撑大这列并把注意力引开。

接线（`_promote_skill` 里调本模块）在 skill-import 线，2026-09-10 已推 main（`d2e9d58d7`）：
锁内、所有成功腿汇合处、服务端现算、写失败不阻塞 promote。本模块只负责「算什么、算成什么样」。

呈现文案由审批面维护，口径是「作者明知有 N 条阻断项仍然启用」——不是「有 N 条错误」：
前者才是审核者要权衡的事（一个人的知情选择），后者听起来像平台的质检结论。
"""

from __future__ import annotations

import json
from typing import Any

MAX_FINDINGS = 20
MAX_BYTES = 64_000
_FINDING_FIELDS = ("id", "code", "severity", "path", "line", "subject", "message_zh")
# Only these two states merit provenance: pending cannot be promoted (lifecycle precondition returns 409);
# cleared/NULL means there were no outstanding blockers then, leaving nothing to tell reviewers.
CARRIED_STATUSES = frozenset({"overridden"})


def build_import_provenance(*, import_status: str | None, import_report_json: str | None) -> str | None:
    """(状态, 清点报告) → 落库用的 JSON 字符串；不该带痕迹时返回 None。

    输入是 skill-import 线的两个既有列，形状见其 `BundleInventory.to_dict()`。
    报告解析不出来时仍然带一条**只有状态与计数未知**的出处：作者强开这件事本身就该被看见，
    不能因为报告坏了就把它渲染成一份干净候选。
    """

    status = (import_status or "").strip()
    if status not in CARRIED_STATUSES:
        return None
    report: dict[str, Any] = {}
    if import_report_json:
        try:
            parsed = json.loads(import_report_json)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            report = parsed
    raw = report.get("findings")
    findings = [f for f in (raw if isinstance(raw, list) else []) if isinstance(f, dict)]
    red = [f for f in findings if f.get("severity") == "red"]
    # Use the report's red_count (findings may be truncated and undercount actual findings); use None if no positive count is available.
    # ★ **Never persist 0 under any circumstances**: the author did override blockers; only their number is unknown. Writing 0 renders
    # "作者明知有 0 条阻断项仍然启用", contradicting itself and excusing the author. Another workstream owns the report's JSON shape;
    # treat it as a defensive boundary: strings/floats/bools/negative values/clean reports all fall back to None, rather than yielding 0.
    raw_count = report.get("red_count")
    red_count: int | None = None
    if isinstance(raw_count, int) and not isinstance(raw_count, bool) and raw_count > 0:
        red_count = raw_count
    elif len(red) > 0:
        red_count = len(red)
    out: dict[str, Any] = {
        "status": status,
        "red_count": red_count,
        "truncated": bool(report.get("truncated")),
        "findings_digest": str(report.get("findings_digest") or "")[:128],  # sha256 hex; truncate to bound the fallback.
        "findings": [{k: f.get(k) for k in _FINDING_FIELDS if f.get(k) is not None} for f in red[:MAX_FINDINGS]],
    }
    if red_count is not None and red_count > len(out["findings"]):
        out["truncated"] = True
    text = json.dumps(out, ensure_ascii=False)
    if len(text.encode("utf-8")) > MAX_BYTES:
        out["findings"] = []
        out["truncated"] = True
        text = json.dumps(out, ensure_ascii=False)
    return text


def public_import_provenance(provenance_json: str | None) -> dict[str, Any] | None:
    """审批面读投影：审核者要判断的是「作者知情强开」这件事＋阻断项本身，不需要内部摘要键。"""

    if not provenance_json:
        return None
    try:
        data = json.loads(provenance_json)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return {
        "status": data.get("status"),
        "red_count": data.get("red_count"),
        "truncated": bool(data.get("truncated")),
        "findings": [f for f in (data.get("findings") or []) if isinstance(f, dict)],
    }
