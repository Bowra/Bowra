"""Report trust computation — the single authoritative implementation (shared core).

`compute_report_trust` aggregates a report's section tree into verified/unverified/sources counts —
the data behind the "✓ N% 已验证 · V/T · K 来源" indicator. It lived in the gateway IM layer; it now
lives here so the canvas composer (core) can compute it ONCE at compose-time, over the persisted
section tree (raw markers intact, before the i18n number-render strip), and ride the result on the
`canvas.done` event. IM channels prefer that authoritative value instead of
re-deriving it from the lossy event stream (stripped numbers / dropped sections). The gateway and the
frontend (`report-trust.ts`) keep the same algorithm — this is its one Python home.

citation-audit M1-4 口径（M3-2 起是唯一口径）: verified = 有效 `[N]` refs（1 ≤ N ≤ sources
entries）; unverified = 结构化 `unverified_numbers` 字段（narrative + insight_card）。
verified==0 → pct=None（不显误导性 0%；M2 dogfood 实证：纯编造报告走这条 → TrustStrip
整条隐藏而不是撒谎说 100%）。退役 gen2 标记计数分支已随 M3 读端下线删除。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# NUMBERED_RE: a bare `[N]` reference (1-3 digits) into the sources list —— gen3
# Sole live format. citation-audit M3-2: retired _NUM_SRC_RE/_NUM_PLAIN_RE (gen2
# counts) removed when read side retired.
_NUMBERED_RE = re.compile(r"\[(\d{1,3})\]")


@dataclass(frozen=True)
class ReportTrustStats:
    """Aggregate verification stats for a report's section tree (port of ``ReportTrustStats``)."""

    verified: int
    unverified: int
    sources: int
    verified_pct: int | None  # 0-100; None when there are no claims at all

    @property
    def silent(self) -> bool:
        """The trust line hides entirely when there's nothing to say — no verifiable claims and no
        sources (mirrors ``trust-strip.tsx:44``: ``verifiedPct === null && sources === 0``). This is
        what keeps casual chat-mode turns from showing an empty/misleading trust line (§4.3)."""
        return self.verified_pct is None and self.sources == 0


def _collect_strings(value: Any, out: list[str]) -> None:
    """Recursively collect every string value in a section (string/list/dict) — faithful port.

    Per-string scanning (not ``json.dumps`` of the whole tree): a serialized array literal like
    ``{"a":[5]}`` would otherwise be mis-matched by the ``[N]`` regex (report-trust.ts note)."""
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, list):
        for v in value:
            _collect_strings(v, out)
    elif isinstance(value, dict):
        for v in value.values():
            _collect_strings(v, out)


#: Maximum structured unverified_numbers items **rendered**, display cap, not count cap.
UNVERIFIED_LIST_CAP = 20


def apply_unverified(payload: dict[str, Any], items: list[str], *, cap: int = UNVERIFIED_LIST_CAP) -> None:
    """把未核验清单写进 section payload —— **列表与总数必须成对写**。

    ⚠ 这个 helper 存在的理由：这一对字段有五个地方要维护（两个写入端、分章投影、
    `_chapter_payloads`、`split_section`），复审实测我第一版只覆盖了其中一个。
    分开写迟早漏，漏了的后果是可信度百分比朝**乐观**方向说谎。

    空列表要把两个键**一起清掉**：`unverified_numbers: []` 是「清除 stale 警示」的
    既有约定，若只清列表、留下旧的 `unverified_total`，`patch_section` 的浅合并
    会让一份本该 100% 的报告显示 3%，而界面上一个警示都没有（复审实测）。
    """
    clean = [str(x)[:60] for x in items if isinstance(x, str) and x.strip()]
    if not clean:
        payload["unverified_numbers"] = []
        payload.pop("unverified_total", None)
        return
    payload["unverified_numbers"] = clean[:cap]
    payload["unverified_total"] = len(clean)


def unverified_count(section: dict[str, Any]) -> int:
    """这一段有几个未核验数字 —— 可信度那条的**分母**。

    优先用 `unverified_total`（真实条数）：列表在写入端被截到 `UNVERIFIED_LIST_CAP`
    条只为少渲几条，拿它当分母会让 30 条记成 20 条 ⇒ 百分比朝「更可信」的方向虚高。
    一个朝乐观方向说谎的可信度指标，比没有这个指标更糟。

    ⚠ **列表为空时一律返回 0，不看 total**：`patch_section` 是浅合并，清空列表而
    没清掉总数的段会留下陈旧 total（复审实测 100% 反向崩成 3%，且界面给不出对应内容）。
    total 比列表还短（数据坏了）时同样退回列表长度——只朝「更不可信」的方向兜底。
    """
    un = section.get("unverified_numbers")
    if not isinstance(un, list) or not un:
        return 0
    total = section.get("unverified_total")
    if isinstance(total, bool) or not isinstance(total, (int, float)):
        return len(un)
    # JS Number.isInteger(30.0) true, Python isinstance(int) false;
    # both implementations must agree: test integer value, not type (re-review parity caught it).
    if total != int(total) or int(total) < len(un) or int(total) > _UNVERIFIED_TOTAL_SANE_MAX:
        return len(un)
    return int(total)


#: Sane unverified_total bound; larger means corrupt field (re-review TS constructed 1e21);
#: then use list length, fallback only toward less trustworthy.
_UNVERIFIED_TOTAL_SANE_MAX = 1_000_000


def compute_report_trust(sections: list[dict[str, Any]]) -> ReportTrustStats:
    """Compute verification stats over a canvas ``ReportSection`` tree (port of computeReportTrust).

    ``sections`` are the section payload dicts (canvas section tree). Pure function — same algorithm
    as the frontend. Compute it over RAW sections (markers intact); once numbers are render-stripped
    the src/plain `[num:…]` signal is gone and only `[N]` + sources survive.
    """
    verified = 0
    unverified = 0
    sources = 0
    strings: list[str] = []
    numbered: list[int] = []

    def walk(lst: list[Any]) -> None:
        nonlocal sources, unverified
        for s in lst:
            if not isinstance(s, dict):
                continue
            kind = s.get("kind")
            if kind == "sources":
                entries = s.get("entries")
                if isinstance(entries, list):
                    sources = len(entries)
                continue
            if kind == "section":
                body = s.get("body")
                if isinstance(body, list):
                    walk(body)
                continue
            _collect_strings(s, strings)
            if kind in ("narrative", "insight_card"):
                # insight_card: impl review SEC-P1; composer now deterministically computes field
                # from card text (previously peer plain-gen2 amber rendering bore responsibility).
                un = s.get("unverified_numbers")
                if isinstance(un, list):
                    unverified += unverified_count(s)

    walk(sections)

    # citation-audit M1-4: new default only gen3; verified=valid [N] references,
    # unverified=structured unverified_numbers field.
    # citation-audit M3-2: gen2 marker-count branch (and kill-switch decision) removed with read side;
    # M1 emitter stopped, M2 existing data cleaned (all six production-family assertions zero), branch never triggered.
    for text in strings:
        for m in _NUMBERED_RE.finditer(text):
            numbered.append(int(m.group(1)))

    # a `[N]` ref is verified only when it has a backing sources entry (1 ≤ N ≤ sources).
    verified += sum(1 for n in numbered if 1 <= n <= sources)

    total = verified + unverified
    if total == 0:
        pct = None
    elif verified == 0:
        # citation-audit M1-4 (impl review C-P2-2): no percentage when verified==0;
        # unmigrated data (no [N], old unverified field remains) shows misleading 0%.
        # Still show unverified count (warnings visible, hide only %). M2 dogfood: entirely fabricated report
        # (zero [N]) follows path → whole TrustStrip hidden, never lies.
        pct = None
    else:
        pct = round(verified / total * 100)
    return ReportTrustStats(verified=verified, unverified=unverified, sources=sources, verified_pct=pct)


def trust_payload(sections: list[dict[str, Any]]) -> dict[str, Any]:
    """``compute_report_trust`` as the plain dict ridden on the ``canvas.done`` event, so IM channels
    read the authoritative compose-time trust instead of re-deriving from the lossy
    (stripped / dropped) event stream."""
    stats = compute_report_trust(sections)
    return {"verified": stats.verified, "unverified": stats.unverified, "sources": stats.sources, "verified_pct": stats.verified_pct}
