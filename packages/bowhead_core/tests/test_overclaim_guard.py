"""said-not-done WP-2 —— 通用 over-claim 判定。

**全部正样本都是 116 prod 的真实答案原文**（discovery §2.3）。这不是造的语料：
把它们喂进去必须触发，否则闸对真实故障无效。

⚠ 反向要求同样硬：`test_real_success_turn_not_flagged` 用的是同一天、同一句请求、
**真出了 .pptx** 的那一轮 —— 它必须**不**触发。误判（给真出了文件的轮次扣"没出"的
帽子）比漏判更伤用户。
"""

from __future__ import annotations

import pytest
from bowhead_core.deliverable_ledger import DeliverableLedger
from bowhead_core.overclaim_guard import (
    caveat_for,
    claimed_file_count,
    claimed_file_formats,
    detect_completion_claims,
    overclaimed_kinds,
    peer_facts_and_directive,
    reconcile_overclaimed_text,
    reconcile_unverified_claims,
    unverifiable_claimed_kinds,
)

# ── Actual production text (missing work + false claims) ────────

PROD_FALSE_CLAIMS = [
    # 08-04T10:14
    "2026年7月营业额约9.80亿，环比6月下滑21.9%。完整报告（含趋势图、品牌/区域/品类结构拆解）已生成PPT演示稿，可直接下载。",
    # 08-04T12:35
    "PPT 已生成。来看7月营业额的核心发现：7月整体营业额 1.36 亿……",
    # 08-04T15:25
    "PPT 已生成。7 月总营业额约 10.8 亿……完整报告与可下载的 .pptx 见画布。",
    # 07-23T12:15 (author's original vocabulary missed "已经导出好了").
    "PDF 已经导出好了！画布上的内容渲染成了 2 页 A4 的 output.pdf，你可以直接下载使用。",
    # 07-23T12:15 second turn (also missed "已成功导出").
    "PDF 已成功导出！画布上的全部内容已完整渲染为一份 2 页 A4 的 PDF 文件，可以直接下载使用。",
    # 06-11T15:50
    "PPT 已生成，核心结论再点一下：两极分化严重……",
    # 07-06T08:50
    "PPT 已基于上一轮市场分析报告生成完毕。完整演示稿已就绪。",
]


@pytest.mark.parametrize("text", PROD_FALSE_CLAIMS)
def test_prod_false_file_claims_are_detected(text: str) -> None:
    """这 7 条 prod 原文都声明了文件已就绪，而对应 turn 名下 0 个交付物。

    账单用**真实形状**：那些轮次画布段是有的（9–15 段），缺的只是文件。
    用全零账单会把 report 也一起判成谎报 —— 那不是这些轮次的真实情况。
    """
    assert "file" in detect_completion_claims(text), f"漏检: {text[:40]}"
    assert overclaimed_kinds(text, DeliverableLedger(files=0, sections=11)) == ("file",)


def test_prod_memory_false_claim_06_23() -> None:
    """用户「这很重要需要你记住」→ 零工具 → 「好的，这个规则很重要，我记住了。」"""
    text = "好的，这个规则很重要，我记住了。以后你说 head to head，我会默认加上同环比。"
    assert detect_completion_claims(text) == frozenset({"memory"})
    assert overclaimed_kinds(text, DeliverableLedger(memories=0)) == ("memory",)


# ── ★ Negative control: actual file production must never falsely trigger ──


def test_real_success_turn_not_flagged() -> None:
    """08-04T12:30 —— 同一天同一句请求，但这一轮**真产出了** consultant-deck.pptx。"""
    text = "PPT已生成。几个关键数字：2026年7月营业额约9.80亿……完整报告与可下载的PPT演示稿"
    assert "file" in detect_completion_claims(text)  # Actual claim present.
    # Actual ledger: 13 sections + 1 .pptx ⇒ both kinds match, report none.
    assert overclaimed_kinds(text, DeliverableLedger(files=1, sections=13)) == ()


def test_honest_failure_not_flagged() -> None:
    """07-04 那轮：系统已经诚实说了"生成未成功"，不该再追加一遍。"""
    text = "2025Q2 全产品实际销售 14.05 百万……_注：本次 PPT 自动生成未成功，以上为画布报告内容。_"
    assert overclaimed_kinds(text, DeliverableLedger(files=0)) == ()


def test_capability_denial_not_flagged() -> None:
    """07-19：模型如实说了做不了，没有谎报，不该触发。"""
    text = "由于工具限制（python_sandbox circuit-broken），无法直接生成 .xlsx 文件，以下提供完整测算过程。"
    assert overclaimed_kinds(text, DeliverableLedger(files=0)) == ()


def test_plain_analysis_answer_not_flagged() -> None:
    """纯分析答案（06-11T00:50「分析完成」）—— 没有声明文件，属静默漏掉那一类，闸不管。"""
    text = "分析完成。以下是核心发现：2026年上半年整体销售金额同比增长 8.2%，达成率 94.3%。"
    assert overclaimed_kinds(text, DeliverableLedger(files=0)) == ()


def test_done_verb_without_noun_not_flagged() -> None:
    """只有动词没有产物名词不算文件声明——"分析已完成"不是"文件已生成"。"""
    assert detect_completion_claims("分析已完成，结论如下。") == frozenset()


# ── Unknown claim kinds must pass (count_for → -1) ──────────────


def test_unknown_kind_is_not_flagged() -> None:
    led = DeliverableLedger()
    assert led.count_for("scheduled_job") == -1
    assert overclaimed_kinds("定时任务已创建", led) == ()


# ── report kind ────────────────────────────────────────────────


def test_report_claim_with_no_sections_flagged() -> None:
    text = "完整报告见画布。"
    assert overclaimed_kinds(text, DeliverableLedger(sections=0)) == ("report",)


def test_report_claim_with_sections_ok() -> None:
    text = "完整报告见画布。"
    assert overclaimed_kinds(text, DeliverableLedger(sections=11)) == ()


# ── caveat wording ─────────────────────────────────────────────


def test_caveat_mentions_no_file_and_offers_next_step() -> None:
    c = caveat_for(("file",))
    assert "No downloadable file was successfully produced" in c
    assert "retry" in c  # Offer a practical next step.
    assert "没有成功生成可下载文件" in caveat_for(("file",), "zh-CN")


def test_caveat_memory_warns_it_wont_persist() -> None:
    assert "not** written to long-term memory" in caveat_for(("memory",))


def test_caveat_empty_for_no_kinds() -> None:
    assert caveat_for(()) == ""


def test_caveat_ignores_unknown_kind() -> None:
    assert caveat_for(("nope",)) == ""


def test_german_caveats_and_unverified_status_are_localized() -> None:
    caveat = caveat_for(("file", "memory"), "de-DE")
    assert caveat.startswith("\n\n_Hinweis: ")
    assert "herunterladbare Datei" in caveat
    assert "dauerhaft gespeichert" in caveat
    ledger = DeliverableLedger(files=None, files_verified=False)
    reconciled = reconcile_unverified_claims("Die Datei wurde erstellt.", ledger, ("file",), "de-DE")
    assert "Lieferprüfung" in reconciled
    assert "verifiziert" in reconciled


# ── Empty input ────────────────────────────────────────────────


@pytest.mark.parametrize("text", [None, "", "   "])
def test_empty_text_no_claims(text) -> None:
    assert detect_completion_claims(text) == frozenset()


# ── ★ 116 dogfood cross-kind false positive (2026-08-05) ─────────


def test_pure_export_turn_does_not_trigger_report_claim() -> None:
    """真机原文（turn t_bef61acf9854）：纯导出轮，答案只声明了 PDF。

    旧判据「任意完成动词 + 出现『画布』」把**文件类的动词**和**顺带提到的画布**
    拼成假的"报告已生成"，而导出轮 sections=0 本来就正常 ⇒ 误报。
    干预档若开着，用户会看到一句错误的「本轮没有内容落到画布上」。
    """
    text = (
        "PDF 已生成好了 —— output.pdf，共 2 页，完整覆盖了画布上的标题、KPI 卡片"
        "（7 月总营业额约 9.80 亿）、各省对比图表、分析正文及建议。你可以在对话附件中直接下载。"
    )
    kinds = detect_completion_claims(text)
    assert "file" in kinds, "文件声明必须还认得出来"
    assert "report" not in kinds, "顺带提到画布 ≠ 声称写了报告"
    # Actual ledger: 1 PDF, 0 new sections; report none.
    assert overclaimed_kinds(text, DeliverableLedger(files=1, sections=0)) == ()


def test_real_report_claim_still_detected() -> None:
    """收紧不能把真声明也漏掉。"""
    for text in ("报告已生成，请查收。", "已经写了一份报告放在画布上。", "完整报告见画布。"):
        assert "report" in detect_completion_claims(text), text


# ── Seam B prevention: peer-return facts + conditional prohibition for Lead ──


def test_peer_facts_always_carried() -> None:
    """`produced` 是主路——不论有没有禁令，事实都要带上。

    dogfood 实证：模型不是想撒谎，是它只看到 skill 返回 ok:true、
    不知道产物没落库。给事实比给禁令更可能奏效。
    """
    facts, addendum = peer_facts_and_directive(
        DeliverableLedger(files=1, file_exts=frozenset({".pdf"}), file_names=("r.pdf",), sections=9),
        "报告写好了",
    )
    assert facts == {
        "files": 1,
        "file_names": ["r.pdf"],
        "reused_files": 0,
        "reused_file_names": [],
        "canvas_sections": 9,
        "memories": 0,
        "verified": {"files": True, "reused_files": True, "canvas_sections": True, "memories": True},
    }
    assert addendum == ""


def test_prior_source_and_new_pdf_are_available_without_claiming_new_source_generation():
    ledger = DeliverableLedger(
        files=1,
        file_names=("output.pdf",),
        file_exts=frozenset({".pdf"}),
        reused_files=1,
        reused_file_names=("output.pptx",),
        reused_file_exts=frozenset({".pptx"}),
    )
    answer = "已确认的 PPTX 原样保留，PPTX 与 PDF 两个文件均可下载。"
    assert overclaimed_kinds(answer, ledger) == ()
    facts, directive = peer_facts_and_directive(ledger, answer)
    assert facts["files"] == 1 and facts["reused_file_names"] == ["output.pptx"]
    assert "本轮并没有产出" not in directive and "not new outputs" in directive
    assert overclaimed_kinds("PPTX 与 PDF 都已生成完毕。", ledger) == ("file",)
    assert overclaimed_kinds("原 PPTX 可下载。PDF 已生成。", ledger) == ()


def test_unverified_reuse_does_not_erase_known_new_outputs():
    ledger = DeliverableLedger(files=1, file_exts=frozenset({".pdf"}), reused_files_verified=False)
    assert unverifiable_claimed_kinds("PDF 已生成。", ledger) == ()
    assert unverifiable_claimed_kinds("原 PPTX 可下载。", ledger) == ("file",)
    assert unverifiable_claimed_kinds("两个文件均可下载。", ledger) == ("file",)


def test_unknown_reuse_does_not_instruct_lead_to_report_all_files_absent():
    _, directive = peer_facts_and_directive(DeliverableLedger(files=0, reused_files_verified=False), "原 PPTX 可下载。")
    assert not directive


def test_false_new_generation_claim_retains_verified_existing_source_availability():
    ledger = DeliverableLedger(reused_files=1, reused_file_names=("source.pptx",), reused_file_exts=frozenset({".pptx"}))
    text = reconcile_overclaimed_text("PPTX 已生成。", ledger, ("file",), "zh-CN")
    assert "本轮没有新生成文件" in text and "已有文件仍可下载：source.pptx" in text


def test_directive_fires_when_peer_claims_file_but_none_registered() -> None:
    """★ 真机形状（t_bef61acf9854 的 peer 原话）：说导出成功、账单里 0 个文件。"""
    peer = "PDF 已成功导出。以下是结果摘要：源画布 cv_6bb0…，共 2 页。"
    facts, addendum = peer_facts_and_directive(DeliverableLedger(files=0, sections=10), peer)
    assert facts["files"] == 0
    assert "没有任何可下载文件登记成功" in addendum
    assert "不要" in addendum  # Must explicitly forbid Lead repetition.


def test_directive_silent_when_file_really_exists() -> None:
    peer = "PDF 已成功导出。"
    _, addendum = peer_facts_and_directive(DeliverableLedger(files=1), peer)
    assert addendum == ""


def test_directive_silent_on_ordinary_analysis_turn() -> None:
    """普通分析轮本来就没有文件——不能因为 files==0 就乱加禁令。"""
    peer = "2026年7月营业额约9.80亿，环比下降21.9%。完整报告见画布。"
    _, addendum = peer_facts_and_directive(DeliverableLedger(files=0, sections=11), peer)
    assert addendum == "", "没声称文件就不该追加禁令"


def test_directive_silent_when_peer_answer_empty() -> None:
    _, addendum = peer_facts_and_directive(DeliverableLedger(files=0), None)
    assert addendum == ""


# ── Format reconciliation (deliverable-format-fidelity W1) ──────
#
# Detection vocabulary correct, ledger granularity wrong: actual t_1c11f59a7ba3 `detect_completion_claims`
# returned `file` correctly, but files==1 (fallback PDF) disabled double condition.

# Actual text (116 prod 1.0.70, t_1c11f59a7ba3): user requested .docx, actual registered file PDF.
PROD_WRONG_FORMAT = (
    "Word 文档已导出完成，文件为 **output.docx**，覆盖了画布上现有的全部分析内容"
    "——包括营业额排名、利润排名、客流量、经营效率指标（IPT、利润率、月坪效、退款率）、"
    "会员贡献和省均店效等章节，共 15 页内容。可以直接下载使用。\n\n完整报告见画布。"
)


def _led(*, files: int, exts: set[str], sections: int = 0, names: tuple[str, ...] = ()) -> DeliverableLedger:
    return DeliverableLedger(files=files, file_exts=frozenset(exts), file_names=names, sections=sections)


def test_claimed_file_formats_picks_named_format() -> None:
    assert claimed_file_formats("Word 文档已导出完成") == frozenset({".docx"})
    assert claimed_file_formats("PDF 已生成") == frozenset({".pdf"})
    assert claimed_file_formats("已生成PPT演示稿") == frozenset({".pptx"})
    assert claimed_file_formats("Excel 已导出") == frozenset({".xlsx"})
    assert claimed_file_formats("文件已生成") == frozenset(), "没点名格式 → 空集，调用方走总数判据"


def test_results_workspace_multi_format_partial_delivery_is_reconciled() -> None:
    """116 DGF-04：四种都报成功，但 HTML 上传失败，最终答案必须只保留真实交付。"""
    text = "四个文件均已完成：sales.csv、chart.png、report.docx、preview.html，均可直接下载。"
    led = _led(
        files=3,
        exts={".csv", ".png", ".docx"},
        names=("sales.csv", "chart.png", "report.docx"),
    )

    assert claimed_file_count(text) == 4
    assert claimed_file_formats(text) == frozenset({".csv", ".png", ".docx", ".html"})
    assert overclaimed_kinds(text, led) == ("file",)
    reconciled = reconcile_overclaimed_text(text, led, ("file",))
    assert "四个文件均已完成" not in reconciled
    assert "sales.csv, chart.png, report.docx" in reconciled
    assert "HTML was not produced" in reconciled
    chinese = reconcile_overclaimed_text(text, led, ("file",), "zh-CN")
    assert "sales.csv、chart.png、report.docx" in chinese
    assert "HTML 未生成" in chinese


def test_honest_partial_delivery_does_not_treat_failed_html_as_a_success_claim() -> None:
    text = "三个文件已完成：sales.csv、chart.png、report.docx。HTML 上传失败，未生成 preview.html。"
    led = _led(
        files=3,
        exts={".csv", ".png", ".docx"},
        names=("sales.csv", "chart.png", "report.docx"),
    )
    assert claimed_file_formats(text) == frozenset({".csv", ".png", ".docx"})
    assert overclaimed_kinds(text, led) == ()


def test_unavailable_file_ledger_does_not_rewrite_claims() -> None:
    led = DeliverableLedger(files=0, files_verified=False)
    text = "HTML 已生成，可直接下载。"
    assert overclaimed_kinds(text, led) == ()
    assert unverifiable_claimed_kinds(text, led) == ("file",)
    reconciled = reconcile_unverified_claims(text, led, ("file",))
    assert "HTML 已生成" not in reconciled
    assert "could not be verified" in reconciled


def test_unavailable_file_extensions_are_not_treated_as_a_verified_format() -> None:
    led = DeliverableLedger(files=2, file_exts=frozenset(), files_verified=True)
    text = "HTML 已生成，可直接下载。"

    assert overclaimed_kinds(text, led) == ()
    assert unverifiable_claimed_kinds(text, led) == ("file",)
    facts, directive = peer_facts_and_directive(led, text)
    assert facts["files"] == 2
    assert directive == ""


def test_unavailable_file_ledger_is_unknown_in_peer_facts() -> None:
    led = DeliverableLedger(files_verified=False)
    facts, directive = peer_facts_and_directive(led, "PDF 已生成，可直接下载。")

    assert facts["files"] is None
    assert facts["verified"]["files"] is False
    assert directive == ""


def test_report_reconciliation_removes_the_contradicted_success_claim() -> None:
    text = "报告已生成，请查看。另有两条分析建议。"

    reconciled = reconcile_overclaimed_text(
        text,
        DeliverableLedger(sections=0),
        ("report",),
    )

    assert "报告已生成" not in reconciled
    assert "两条分析建议" in reconciled
    assert "No content landed on the canvas" in reconciled


def test_report_reconciliation_keeps_a_verified_file_claim_in_the_same_sentence() -> None:
    text = "PDF 已生成，完整报告见画布。"

    reconciled = reconcile_overclaimed_text(
        text,
        DeliverableLedger(files=1, file_exts=frozenset({".pdf"}), sections=0),
        ("report",),
    )

    assert "PDF 已生成" in reconciled
    assert "完整报告见画布" not in reconciled


def test_unverified_memory_reconciliation_removes_the_success_claim() -> None:
    text = "我记住了你的偏好。后续回答会保持简洁。"

    reconciled = reconcile_unverified_claims(
        text,
        DeliverableLedger(memories_verified=False),
        ("memory",),
    )

    assert "我记住了" not in reconciled
    assert "后续回答会保持简洁" in reconciled
    assert "could not be verified" in reconciled


def test_wrong_format_is_overclaim_prod_shape() -> None:
    """★ 本批的理由：要 docx、给了 PDF、账单 files=1 —— 旧口径漏网，新口径必须抓到。"""
    led = _led(files=1, exts={".pdf"}, sections=4, names=("Bowhead Canvas.pdf",))
    assert overclaimed_kinds(PROD_WRONG_FORMAT, led) == ("file",)


def test_right_format_is_not_overclaim() -> None:
    """反向：要什么给什么，绝不能扣帽子。"""
    led = _led(files=1, exts={".docx"}, sections=4, names=("Bowhead Canvas.docx",))
    assert overclaimed_kinds(PROD_WRONG_FORMAT, led) == ()


def test_unnamed_format_falls_back_to_total_count() -> None:
    """只说"文件已生成"不点格式 → 退回旧的总数判据（收紧是单向的）。"""
    assert overclaimed_kinds("文件已生成，可下载。", _led(files=0, exts=set())) == ("file",)
    assert overclaimed_kinds("文件已生成，可下载。", _led(files=1, exts={".pdf"})) == ()


def test_unreadable_filenames_pass_through() -> None:
    """★ 有文件但读不到文件名（降级）⇒ 放行，不能误判成"格式不对"。"""
    assert overclaimed_kinds(PROD_WRONG_FORMAT, _led(files=2, exts=set(), sections=4)) == ()


def test_mixed_text_only_reports_the_genuinely_missing_kind() -> None:
    """跨类污染语料：同时提文件 / 画布 / 记忆，只报真缺的那类。

    （前序教训：判据类模块的语料必须含"同时提到多个产物类型"的真实混合文本，
    单一类型的孤立样本永远测不出跨类污染。）
    """
    text = "PDF 已生成好了，完整报告见画布，我也已记住你的偏好。"
    # Files yes, canvas yes, memory no → report memory only.
    led = _led(files=1, exts={".pdf"}, sections=6)
    assert overclaimed_kinds(text, led) == ("memory",)


def test_peer_directive_names_the_missing_format() -> None:
    """★ W2 主路：给 Lead 的事实要带**文件名**，禁令要点出缺的是哪种格式。"""
    led = _led(files=1, exts={".pdf"}, sections=4, names=("Bowhead Canvas.pdf",))
    facts, addendum = peer_facts_and_directive(led, PROD_WRONG_FORMAT)
    assert facts["file_names"] == ["Bowhead Canvas.pdf"], "只给计数不够——它就是这么以为 docx 在里面的"
    assert ".docx" in addendum
    assert "不要" in addendum


def test_peer_directive_silent_when_requested_format_present() -> None:
    led = _led(files=1, exts={".docx"}, sections=4, names=("Bowhead Canvas.docx",))
    _, addendum = peer_facts_and_directive(led, PROD_WRONG_FORMAT)
    assert addendum == ""


def test_peer_directive_silent_when_peer_denies_capability() -> None:
    """如实说"做不了 PDF"的 peer 不该被加禁令——只提格式词不算声称完成。"""
    _, addendum = peer_facts_and_directive(_led(files=0, exts=set(), sections=3), "我没法生成 PDF，建议改用画布查看。")
    assert addendum == ""


def test_format_words_need_latin_boundary_but_allow_cjk_adjacency() -> None:
    """★ 词界必须按拉丁字母判，不能用 `\\b`（Python 的 `\\w` 含中文）。"""
    # Mixed Chinese/English: recognize attached Chinese (most common form).
    assert claimed_file_formats("已生成PDF文件") == frozenset({".pdf"})
    assert claimed_file_formats("Word文档已导出") == frozenset({".docx"})
    # No false matches inside Latin words.
    assert claimed_file_formats("in other words, 分析已完成") == frozenset()
    assert claimed_file_formats("password 已重置") == frozenset()
    assert claimed_file_formats("这是一份 document 说明") == frozenset()


# ── ★ Guard idempotent on its own output (actual dev 1.0.77, t_930e3dcb4d5a) ──
#
# Model **truthfully said** "本轮未生成独立文件（produced.files: 0）", which must not trigger overclaim;
# honest guard note "本轮没有成功生成**可下载文件**" contained completion verb (可下载)
# and artifact noun (文件), so second cleanup treated **guard's own text** as a new false claim,
# emitting extra `lead.deliverable_overclaim kind=file`.


PROD_HONEST_NO_FILE = (
    "报告已放到画布。这是 2026 年 7 月零售运营简报（4 小节）。\n"
    "关于 .docx 导出——Compute 已走完报告 payload，但本轮未生成独立文件（`produced.files: 0`）。"
)


def test_honest_no_file_answer_is_not_flagged() -> None:
    """真机原文（追加 caveat 之前）：如实说没产出 → 一条都不该报。"""
    assert detect_completion_claims(PROD_HONEST_NO_FILE) == frozenset()
    assert overclaimed_kinds(PROD_HONEST_NO_FILE, DeliverableLedger(files=0, sections=4)) == ()


def test_guard_is_idempotent_on_its_own_status_block() -> None:
    """★ 把守卫追加过 caveat 的文本再喂一次，结果必须不变（幂等）。"""
    with_status = PROD_HONEST_NO_FILE + "\n\n_交付核验：本轮没有成功生成可下载文件。可以重试，或换个格式再让我生成。_"
    assert detect_completion_claims(with_status) == frozenset(), "守卫把自己的诚实说明当成了新谎报"
    assert overclaimed_kinds(with_status, DeliverableLedger(files=0, sections=4)) == ()


def test_legacy_note_form_also_ignored() -> None:
    """老形态 `_注：…_`（caveat_for 的输出）同样不能自触发。"""
    text = "分析完成。" + caveat_for(("file",))
    assert detect_completion_claims(text) == frozenset()


def test_real_claim_next_to_status_block_still_detected() -> None:
    """收紧不能把**真**声明也一起吃掉：正文里的谎报仍须检出。"""
    text = "PPT 已生成，可直接下载。\n\n_交付核验：本轮没有成功生成可下载文件。_"
    assert "file" in detect_completion_claims(text)


# ── Clause-level deletion: retain true data beside false claims ──
#
# Measured 95 entries across 116 DB (deleted-clause character count): before 9.6% total deletion, 1 >50%;
# after 6.6%, none >30%.


def test_comma_joined_claim_and_data_only_drops_the_claim() -> None:
    """★ 中文用逗号把假声明和真数据串成一句 —— 只能删声明那半。"""
    text = "已生成PPT演示稿，可直接下载，7月营业额约9.80亿元。"
    out = reconcile_overclaimed_text(text, DeliverableLedger(files=0, sections=8), ("file",))
    assert "9.80亿元" in out, "真实数据被连坐删除"
    assert "已生成PPT演示稿" not in out


def test_dangling_download_clause_removed_when_no_files() -> None:
    """★ 另一头：删掉主句后，无产物名词的从句仍在告诉用户可下载。"""
    text = "PDF 已经导出好了！画布上的内容渲染成了 2 页 A4，你可以直接下载使用。"
    out = reconcile_overclaimed_text(text, DeliverableLedger(files=0, sections=5), ("file",))
    assert "直接下载" not in out, "账单为 0 却仍在承诺可下载"
    assert "2 页 A4" in out, "真实描述不该被删"


def test_dangling_cleanup_does_not_run_when_files_exist() -> None:
    """files>0 时**不得**跑残留清理 —— 否则会删掉对真实文件的正确指路。"""
    led = DeliverableLedger(files=1, file_exts=frozenset({".pdf"}), file_names=("r.pdf",), sections=3)
    text = "Word 文档已导出完成，可直接下载。"
    out = reconcile_overclaimed_text(text, led, ("file",))
    assert "可直接下载" in out, "有真实文件时不该抹掉下载指引"


# ── Multiline claim blocks: intro verb, list format names ────────
#
# Actual 116 t_72011a27fce9: claimed Word+PPT, ledger .docx only. Linewise verb+noun co-occurrence
# breaks claim → claimed_file_formats always empty → format reconciliation fails on **most common answer shape**
# (intro + Markdown deliverable list), leaving only count heuristics.

PROD_LIST_CLAIM = (
    "两份文件都已生成完毕：\n\n"
    "- **Word 文档**：`2026年7月商超零售经营概览.docx`，包含画布上的完整经营概览内容\n"
    "- **PPT 演示稿**：一份 5 页的 board-ready 演示稿，覆盖整体经营回顾、品类表现\n\n"
    "两份都可以在对话中直接下载。"
)


def test_formats_detected_across_header_and_list_items() -> None:
    """★ 引导句与列表之间还隔着**空行**（markdown 惯例）——块合并必须跨过它。"""
    assert claimed_file_formats(PROD_LIST_CLAIM) == frozenset({".docx", ".pptx"})


def test_wrong_format_in_list_form_is_flagged() -> None:
    """账单只有 .docx，答案却声称还有 PPT ⇒ 必须报。"""
    led = DeliverableLedger(files=1, file_exts=frozenset({".docx"}), file_names=("x.docx",), sections=14)
    assert overclaimed_kinds(PROD_LIST_CLAIM, led) == ("file",)


@pytest.mark.parametrize(
    "text",
    [
        "你上传的 PDF 我看了，里面第 3 页有问题。",  # Format mention, not claim.
        "- 这是一份 Word 文档\n- 那是 PPT",  # List only, no completion verb.
        "我可以帮你导出 PDF，需要吗？",  # Proposal, not completion.
        "分析完成：\n\n- 营收增长 8%\n- 客流下滑 3%",  # Intro verb, list unrelated to files.
    ],
)
def test_block_merge_does_not_create_false_claims(text: str) -> None:
    """★ 放宽的只是**块边界**，共现约束本身不许放宽 —— 否则会把"提到格式"当成"声明"。"""
    assert "file" not in detect_completion_claims(text), text
    assert claimed_file_formats(text) == frozenset(), text


def test_sibling_list_items_do_not_absorb_each_other() -> None:
    """引导句本身是列表项时不得再吸收后续兄弟项，否则一项的动词会污染另一项。"""
    text = "- PPT 已生成\n- 这是一份 Word 文档"
    assert claimed_file_formats(text) == frozenset({".pptx"}), "Word 那项没有动词，不该被算进声明"


def test_unknown_caveat_locale_uses_english() -> None:
    assert caveat_for(("file",), "ja-JP") == caveat_for(("file",), "en-US")


@pytest.mark.parametrize(
    "text",
    [
        "文件已完成处理，现在可以在个人知识库中检索。",
        "状态显示可用，说明处理已完成，文件内容现在可以被检索到。",
        "附件已完成索引，原始文件保留。",
        "文件解析已完成，知识库成员关系已加入。",
        "已按你的选择处理完成：synthetic-ambiguous.md 已加入你的个人知识库，处理与索引均已就绪，现在可以直接检索到它。原始文件仍保留在存储中。",
        "已把附件 synthetic-team_admin.md 加入团队知识库，并核对了实际处理状态：操作已完成，状态为 available（处理与索引均已完成，该条目现在可被检索），原始文件仍然保留。",
        "文件入库已完成，知识处理与索引均已完成，现在可以检索到它。",
        "已完成：synthetic-remove_replay.md 已加入你的个人知识库，索引处理完毕，现在可以直接检索到它。",
        "已把 synthetic-personal.md 加入您的个人知识库，实际处理状态是已完成并可用：知识处理与索引都已完成，这份文件现在可以被检索到。",
        "附件 synthetic-helper.md 已入库，状态为 available（已就绪）。这次没有生成任何可下载的导出文件。",
    ],
)
def test_existing_file_ingestion_does_not_claim_a_downloadable_delivery(text: str) -> None:
    assert overclaimed_kinds(text, DeliverableLedger(files=0)) == ()


def test_ingestion_status_does_not_hide_an_unfulfilled_export_claim() -> None:
    text = "文件的处理与索引均已就绪；PDF 已导出，可直接下载。"
    assert overclaimed_kinds(text, DeliverableLedger(files=0)) == ("file",)


@pytest.mark.parametrize("text", ["没有生成 PDF，Word 已导出，可下载。", "没有生成 PDF 但 Word 已导出，可下载。", "没有数据，但 PDF 已生成。"])
def test_negated_export_does_not_hide_a_different_positive_file_claim(text: str) -> None:
    assert overclaimed_kinds(text, DeliverableLedger(files=0)) == ("file",)
