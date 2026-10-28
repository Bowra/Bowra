"""记忆重做 批2 —— FR6 存量 58 条候选的四分类 + 归并（tech-design §5.3）。

fixture 用的是 **discovery §8 逐条读完 58 条后点名过的真串**（四组矛盾、
净利率/环比各被提议 5 次、3 条垃圾、8 条本该是设置、真有价值的那几条）。
它同时是 FR3 语义判重的回归 fixture —— §5.3 说的「副产品」就是这张表。

⚠ **本模块只出报表，不代用户批准**：`keep` 桶里的条目一律保持 pending。
approve 是用户的权利，脚本代批＝把「审批队列」这个产品概念偷偷废掉。

## 突变验证：两条一开始**没打红**，都是用例本身弱，不是等价突变

- 「归并丢掉 `order_hint`」没红 —— 因为 fixture 的 id 恰好与插入序同向，
  去掉 order_hint 后按 id 兜底照样选对，用例**为了错误的理由通过**。
  把 id 取成与插入序相反才打得红。
- 「坏桶也参与归并」没红 —— 因为 fixture 里坏桶内没有任何一对近重复，
  放开分组也无事发生。补了一对措辞几乎一样的一次性要求才打得红。

★ 突变没红，先怀疑用例，其次才是等价突变。

★ 一处**刻意的不对称**，别当成 bug：
近重复在**实时写入**里只点名不合并（批1 §4.2 d-3：自动合并会静默吃掉一条真新偏好），
但在**存量清理**里用来分组、留最新。差别在于清理是**人审过才 `--apply`** 的批处理
（§5.3 明写中间要人审一次），实时写入没有那个人。
"""

from __future__ import annotations

from bowhead_core.memory_candidate_triage import (
    BUCKET_GARBAGE,
    BUCKET_KEEP,
    BUCKET_ONE_OFF,
    BUCKET_SETTINGS,
    classify_one,
    triage,
)

_UNSET = object()


def _row(cid, value, created_at, kind="preference", key=_UNSET):
    return {
        "id": cid,
        "kind": kind,
        "key": cid if key is _UNSET else key,
        "value_text": value,
        "created_at": created_at,
        "user_id": "u1",
    }


#: Exact strings identified in discovery §8, using dates recorded there.
DISCOVERY_ROWS = [
    # Two of 5 net-margin proposals (§8: 07-31 / 08-11 / 08-16 / 08-25 / 08-29).
    _row("c01", "内部盈利只看净利率", "2026-07-31"),
    _row("c02", "内部盈利只看净利率", "2026-08-11"),
    # Period-over-period comparison 5 times.
    _row("c03", "优先看环比 delta，不看绝对值", "2026-07-31"),
    _row("c04", "优先看环比 delta，不看绝对值", "2026-08-11"),
    # ★ One of four conflicts: Top 10 vs Top 5.
    _row("c05", "always display Top 10 rankings instead of Top 5", "2026-08-05"),
    _row("c06", "always display Top 5 rankings instead of Top 10", "2026-08-10"),
    # Language ×4 (§8: mutually conflicting and belong in settings).
    _row("c07", "Always respond in English", "2026-08-17"),
    _row("c08", "默认使用中文（zh-CN）回答", "2026-08-20"),
    # Appearance / first weekday.
    _row("c09", "界面用暗色 + 紧凑模式", "2026-08-12"),
    _row("c10", "周起始日设为周一", "2026-08-14"),
    # Three garbage entries individually named in §8. ★ First two **lack keys**, the actual
    # fragment-value shape; invented keys miss reality (structured candidates with meaningful keys
    # legitimately have numeric values; see case below).
    _row("c11", "1", "2026-08-02", kind="note", key=""),
    _row("c12", "most_recent_working_day", "2026-08-03", kind="note", key=""),
    _row("c13", "个人测试工作区", "2026-08-04"),
    # Valuable, belongs only in memory.
    _row("c14", "KB 结论必须标出处", "2026-08-06", kind="constraint"),
    _row("c15", "KB 未命中须如实说", "2026-08-07", kind="constraint"),
    # One-off request mistaken for long-term preference.
    _row("c16", "这轮先只看江苏", "2026-08-08"),
    _row("c17", "本次分析临时用毛利率", "2026-08-09"),
]


def _buckets(rows=DISCOVERY_ROWS):
    return {t.candidate_id: t for t in triage(rows).triaged}


def test_only_unambiguous_garbage_is_auto_rejected():
    """§8 点名了三条垃圾，但只有**形状上不会误伤**的两条被自动判掉：
    `"1"`（纯数字）与 `个人测试工作区`（点名 marker）。

    ★ `most_recent_working_day` **故意不判**：它与 `revenue_in_millions`
    （一条正当的短偏好值）形状完全一样，没有模型就分不开。拿形状当判据会把真偏好
    判成 `low_quality` 并写进 30 天抑制 —— 分不开就不分，留给报表上的人审
    （§5.3 本来就要求中间人审一次）。**误杀比多留一条更伤**，这条纪律在这里也成立。
    """
    b = _buckets()
    for cid in ("c11", "c13"):
        assert b[cid].bucket == BUCKET_GARBAGE, (cid, b[cid])
    assert b["c12"].bucket == BUCKET_KEEP


def test_settings_family_covers_language_appearance_and_week_start():
    """§8「本该是设置」8 条＝语言 ×4 + 暗色紧凑 + 柔和色系 + 周起始日。
    进记忆只会和设置打架（用户在设置里改了，记忆还在说旧的）。"""
    b = _buckets()
    for cid in ("c07", "c08", "c09", "c10"):
        assert b[cid].bucket == BUCKET_SETTINGS, (cid, b[cid])


def test_one_off_requests_are_separated_from_real_preferences():
    b = _buckets()
    assert b["c16"].bucket == BUCKET_ONE_OFF
    assert b["c17"].bucket == BUCKET_ONE_OFF


def test_the_valuable_ones_stay_pending_for_the_user_to_decide():
    """★ 脚本**不代批**。这两条是 §8 判为「真有价值、且只能是记忆」的那一类。"""
    b = _buckets()
    for cid in ("c14", "c15"):
        assert b[cid].bucket == BUCKET_KEEP
        assert b[cid].superseded_by_candidate_id is None


def test_a_claim_proposed_five_times_collapses_to_the_newest():
    """净利率与环比各被提议 5 次 —— 归并后每组只留最新那条 pending。"""
    b = _buckets()
    assert b["c01"].superseded_by_candidate_id == "c02"
    assert b["c02"].superseded_by_candidate_id is None
    assert b["c03"].superseded_by_candidate_id == "c04"
    assert b["c04"].superseded_by_candidate_id is None


def test_the_top10_vs_top5_contradiction_resolves_to_the_newer_one():
    """★ §8「最硬的一条」：候选池里存着自相矛盾的规则，两条都还挂着 pending。
    「这四组如果被全部采纳，模型会精分。」归并按时间取最新（08-10 的 Top 5 胜）。"""
    b = _buckets()
    assert b["c05"].superseded_by_candidate_id == "c06"
    assert "Top 5" in b["c06"].value_text
    assert b["c06"].superseded_by_candidate_id is None


def test_two_different_preferences_are_not_merged_just_because_they_share_words():
    """归并的误伤方向是「把两条不同的真偏好并成一条」。这两条都以 `KB ` 开头、
    都很短，但说的是两件事 —— 必须各自保留。"""
    b = _buckets()
    assert b["c14"].superseded_by_candidate_id is None
    assert b["c15"].superseded_by_candidate_id is None


def test_only_bad_buckets_and_superseded_rows_are_rejected():
    """`rejects()` 是 `--apply` 真正要动的集合：三个坏桶 + 组内被取代的。
    没被取代的 `keep` 一条都不许动。"""
    report = triage(DISCOVERY_ROWS)
    reject_ids = {t.candidate_id for t in report.rejects()}
    assert {"c11", "c13"} <= reject_ids  # Garbage (c12 indistinguishable by shape; human review).
    assert {"c07", "c08", "c09", "c10"} <= reject_ids  # settings
    assert {"c16", "c17"} <= reject_ids  # one-off
    assert {"c01", "c03", "c05"} <= reject_ids  # Superseded older entries.
    assert reject_ids.isdisjoint({"c12", "c14", "c15", "c02", "c04", "c06"})


def test_grouping_only_happens_inside_the_keep_bucket():
    """坏桶里的条目本来就要 reject，再分组只会让报表把「因为重复被丢」和
    「因为是垃圾被丢」混在一起，人审时看不清该信哪个理由。

    ★ fixture 里**必须有一对坏桶内的近重复**，否则放开分组也无事发生，
    这条用例就是死的（第一版就是这样，突变没打红才发现）。
    `c16` / `c18` 是两条措辞几乎一样的一次性要求。
    """
    rows = [*DISCOVERY_ROWS, _row("c18", "这轮先只看江苏省", "2026-08-08")]
    b = {t.candidate_id: t for t in triage(rows).triaged}
    # Positive control: these **are** near-duplicates, merged if placed in keep.
    from bowhead_core.memory_write_policy import is_near_duplicate

    assert is_near_duplicate("这轮先只看江苏", "这轮先只看江苏省")
    for cid in ("c07", "c08", "c11", "c12", "c13", "c16", "c17", "c18"):
        assert b[cid].superseded_by_candidate_id is None, cid


def test_classify_one_is_deterministic_and_needs_no_db():
    """判据是纯函数 —— 能直接问「这句该去哪个桶」，不必先起一个库。"""
    assert classify_one(value_text="KPI 卡片中营收以百万（¥M）展示")[0] == BUCKET_KEEP
    assert classify_one(value_text="这轮先只看江苏")[0] == BUCKET_ONE_OFF
    assert classify_one(value_text="以后都用英文回答")[0] == BUCKET_SETTINGS
    assert classify_one(value_text="1")[0] == BUCKET_GARBAGE


# ═══ Timestamp ties (seen on the first real run) ════════════════


def _tie_rows():
    """Top 10 / Top 5 那组矛盾，两条**同一个时间戳**——生产里同批入队就是这样。

    ★ id 故意取成**与插入序相反**（先插的叫 `cZ`、后插的叫 `cA`）：
    id 若与插入序同向，去掉 `order_hint` 后按 id 兜底照样能选对，
    这条用例就会**为了错误的理由通过**（第一版就是这样，突变没打红才发现）。
    """
    return [
        {**_row("cZ", "always display Top 10 rankings instead of Top 5", "2026-08-05T10:00:00"), "order_hint": 1},
        {**_row("cA", "always display Top 5 rankings instead of Top 10", "2026-08-05T10:00:00"), "order_hint": 2},
    ]


def test_a_timestamp_tie_is_broken_by_insertion_order_not_by_uuid():
    """★ 真机复现：`created_at` 只精确到秒，同批入队会打平，
    这时「取最新」会悄悄退化成「按 uuid 随机取一条」——报表照样印着「取最新」，
    人却看不出方向选反了。第一次真跑就把 Top 10 / Top 5 解成了 Top 10（旧的）。
    传了 `order_hint`（sqlite rowid）之后，后插入的 Top 5 才真的胜出。"""
    b = {t.candidate_id: t for t in triage(_tie_rows()).triaged}
    assert b["cZ"].superseded_by_candidate_id == "cA"
    assert "Top 5" in b["cA"].value_text
    assert b["cA"].superseded_by_candidate_id is None


def test_a_timestamp_tie_is_flagged_for_human_review():
    """打平时「取最新」其实**没有判据**。报表必须把这几对单独印出来交人审，
    不能让人以为是按时间选的 —— 否则脚本在替人做一个它做不了的判断。"""
    report = triage(_tie_rows())
    ties = report.ties()
    assert [t.candidate_id for t in ties] == ["cZ"]
    assert ties[0].tie_with_keeper is True


def test_distinct_timestamps_are_not_flagged_as_ties():
    """positive control：日期不同的那些组不该被标成 tie，否则报表天天喊狼来了。"""
    assert triage(DISCOVERY_ROWS).ties() == []


# ═══ Actual gaps found in review ═══════════════════════════════


def test_the_garbage_rule_is_too_narrow_to_steal_rows_with_a_real_destination():
    """★ 初版的垃圾判据太宽，把「有明确去处」的条目抢走了：`用中文回答` 判成
    low_quality 而不是 settings，`毛利率优先` / `Top 5` 直接被当垃圾拒掉并写进 30 天抑制
    （中文偏好经常短于 6 个字）。

    修法**不是**把它挪到最后就算了 —— 而是把两条不可靠的规则整个删掉：
    长度阈值、以及「像不像标识符」。留下的只有纯数字与点名 marker，**不会误伤**。
    （挪顺序也做了，但那只是保险；根因是判据本身太宽。突变实测：现在把垃圾判据
    挪回最前面，这些用例**不会**变红 —— 因为它已经咬不到这些串了。）
    """
    assert classify_one(value_text="用中文回答")[0] == BUCKET_SETTINGS
    assert classify_one(value_text="毛利率优先")[0] == BUCKET_KEEP
    assert classify_one(value_text="Top 5")[0] == BUCKET_KEEP
    assert classify_one(value_text="revenue_in_millions")[0] == BUCKET_KEEP
    # Positive control: actual garbage remains garbage.
    assert classify_one(value_text="1")[0] == BUCKET_GARBAGE
    assert classify_one(value_text="most_recent_working_day")[0] == BUCKET_KEEP  # Inseparable by shape; human review.
    # ★ Numeric values with meaningful keys are **legitimate**, not garbage: `{key:'kpi_top_n', value:'5'}`
    # is a standard structured preference. Value-only judgment rejects it and adds 30-day suppression.
    assert classify_one(value_text="5", key="kpi_top_n")[0] == BUCKET_KEEP
    assert classify_one(value_text="1000000", key="money_unit_scale")[0] == BUCKET_KEEP


def test_topic_is_not_a_ui_theme():
    """★ `主题` 在中文里绝大多数时候是「话题/题目」不是「UI 主题」。
    裸词放进设置族会把内容偏好判成外观设置并拒写。"""
    assert classify_one(value_text="报告主题按季度拆")[0] == BUCKET_KEEP
    assert classify_one(value_text="分析主题固定为营收")[0] == BUCKET_KEEP
    # Positive control: actual appearance settings still blocked.
    assert classify_one(value_text="界面用暗色 + 紧凑模式")[0] == BUCKET_SETTINGS


def test_a_three_way_duplicate_chain_has_no_dangling_pointer():
    """★ 净利率真实被提议了 **5 次**，而 fixture 每组只有两条 —— 链式取代一开始没测到。
    初版是 a→b、b→c：报表印着「a 被 b 取代」，而 b 自己也在 reject 列表里，
    人审看到的是一个即将被删掉的目标。"""
    rows = [
        {**_row("a", "内部盈利只看净利率", "2026-07-31"), "order_hint": 1},
        {**_row("b", "内部盈利就只看净利率", "2026-08-11"), "order_hint": 2},
        {**_row("c", "内部盈利只看净利率哦", "2026-08-16"), "order_hint": 3},
    ]
    report = triage(rows)
    b = {t.candidate_id: t for t in report.triaged}
    keeper_ids = {t.candidate_id for t in report.triaged if t.superseded_by_candidate_id is None}
    assert b["a"].superseded_by_candidate_id == "c"
    assert b["b"].superseded_by_candidate_id == "c"
    assert b["c"].superseded_by_candidate_id is None
    # Every superseded target must be **retained**, never an intermediate entry also slated for deletion.
    reject_ids = {t.candidate_id for t in report.rejects()}
    for t in report.triaged:
        if t.superseded_by_candidate_id:
            assert t.superseded_by_candidate_id in keeper_ids
            assert t.superseded_by_candidate_id not in reject_ids


def test_classification_reads_the_key_because_structured_candidates_hide_meaning_there():
    """★ 与 gate 同一个缺口的第二处落点：`{key:'answer_language', value:'English'}`
    单看 value 只是个普通英文词，会被判成「真价值、交用户裁」——
    而语言候选恰恰是本 sprint 要清的最大一桶（58 条里 8 条）。"""
    assert classify_one(value_text="English")[0] == BUCKET_KEEP
    assert classify_one(value_text="English", key="answer_language")[0] == BUCKET_SETTINGS
    assert classify_one(value_text="Monday", key="week_start_day")[0] == BUCKET_SETTINGS


def test_dedup_still_compares_values_only_not_key_plus_value():
    """⚠ 分类看 key+value，**判重只比 value**：key 不同但说的是同一件事，
    正是该合并的那种（存量里 35 条重复主张全是这个形态）。
    若判重也带上 key，两条不同 key 的同义条目永远合不掉。"""
    rows = [
        {**_row("a", "内部盈利只看净利率", "2026-07-31"), "key": "profit_metric", "order_hint": 1},
        {**_row("b", "内部盈利只看净利率", "2026-08-11"), "key": "margin_rule", "order_hint": 2},
    ]
    b = {t.candidate_id: t for t in triage(rows).triaged}
    assert b["a"].superseded_by_candidate_id == "b"


def test_dedup_never_crosses_a_user_or_workspace_or_kind_boundary():
    """★★ 归并的代表条目若是**全局一张表**，A 用户的候选会被判成 B 用户候选的重复
    而被 reject —— 而脚本默认处理所有用户（`--user` 不传就是全部），
    在 116 生产库上跑一次就是跨租户删数据。"""
    same_text = "内部盈利只看净利率"
    rows = [
        {
            "id": "uA",
            "user_id": "A",
            "workspace_id": None,
            "kind": "preference",
            "key": "k",
            "value_text": same_text,
            "created_at": "2026-07-31",
            "order_hint": 1,
        },
        {
            "id": "uB",
            "user_id": "B",
            "workspace_id": None,
            "kind": "preference",
            "key": "k",
            "value_text": same_text,
            "created_at": "2026-08-11",
            "order_hint": 2,
        },
        {
            "id": "wA",
            "user_id": "A",
            "workspace_id": "ws_a",
            "kind": "preference",
            "key": "k",
            "value_text": same_text,
            "created_at": "2026-08-12",
            "order_hint": 3,
        },
        {
            "id": "kA",
            "user_id": "A",
            "workspace_id": None,
            "kind": "constraint",
            "key": "k",
            "value_text": same_text,
            "created_at": "2026-08-13",
            "order_hint": 4,
        },
    ]
    b = {t.candidate_id: t for t in triage(rows).triaged}
    for cid in ("uA", "uB", "wA", "kA"):
        assert b[cid].superseded_by_candidate_id is None, cid
    # Positive control: still merge within the same (user, space, kind).
    rows.append(
        {
            "id": "uA2",
            "user_id": "A",
            "workspace_id": None,
            "kind": "preference",
            "key": "k2",
            "value_text": same_text,
            "created_at": "2026-08-20",
            "order_hint": 5,
        }
    )
    b2 = {t.candidate_id: t for t in triage(rows).triaged}
    assert b2["uA"].superseded_by_candidate_id == "uA2"


def test_created_at_mixes_iso_T_and_sqlite_space_separators():
    """★ 这张表里两种分隔符**并存**（`candidates.py` 自己的注释记着，别处查询全用
    `datetime()` 包）。按原始串排序时 `' '(0x20) < 'T'(0x54)` ⇒ 更新的那条排到前面，
    「取最新」又一次选反方向，而且 `tie_with_keeper` 比的也是原始串，
    连人审提示都不会给 —— 就是 Top10/Top5 那个方向反转换了个门进来。"""
    rows = [
        {
            "id": "older",
            "user_id": "u",
            "workspace_id": None,
            "kind": "preference",
            "key": "k",
            "value_text": "同一句话",
            "created_at": "2026-08-10T08:00:00",
            "order_hint": 1,
        },
        {
            "id": "newer",
            "user_id": "u",
            "workspace_id": None,
            "kind": "preference",
            "key": "k",
            "value_text": "同一句话",
            "created_at": "2026-08-10 09:00:00",
            "order_hint": 2,
        },
    ]
    b = {t.candidate_id: t for t in triage(rows).triaged}
    assert b["older"].superseded_by_candidate_id == "newer"
    assert b["newer"].superseded_by_candidate_id is None
