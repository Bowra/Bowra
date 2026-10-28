"""`bi_ask_question` 前置校验 —— 提前识别 query 里 workspace 不认的实体，防 agent
循环发垃圾 query.

背景：
    用户问 "为什么 2024 年华东地区营业额同比下滑" —— workspace 的 `城市` 维度里
    只有 `上海 / 北京 / 江苏` 这种省市值，没有 `华东地区` 聚合概念. 直接发给 datarc
    NL2SQL 会返 `type=image` 但 URL 指向通用 discover 页（无有效数据），agent 看到
    "有数据" 误以为成功，反复问了 7 次。加 preflight 一次性识别 "华东不在维度值里"
    返 warning，agent 第一次就能改口"江苏/浙江/安徽/上海分查"。

现状（批1/c8 之后）：两 collection 距离检查（check_entities/_top1_distance）已整体
删除（docstring 自认零调用方的 M1 遗产）——本模块只剩 token 抽取原语
（extract_candidate_entities / is_noise_word），由 tool_registry 消费.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


# Only contiguous 2-6-character Chinese tokens (proper nouns / business terms); English/numbers use another path.
_CN_TOKEN_RE = re.compile(r"[\u4e00-\u9fa5]{2,8}")

# Generic noise words: do not query workspace, avoiding entities such as "今年/同比/环比".
_STOP_WORDS = frozenset(
    {
        # Time.
        "今年",
        "去年",
        "前年",
        "今日",
        "昨日",
        "明日",
        "今月",
        "上月",
        "下月",
        "本月",
        "本周",
        "上周",
        "下周",
        "近三天",
        "近七天",
        "近一天",
        "近一月",
        "近一年",
        "近七日",
        "近半年",
        "近三月",
        "近三年",
        "去年同期",
        "同比",
        "环比",
        "增长率",
        "变化率",
        "变化量",
        "对比",
        "比较",
        "比率",
        "增速",
        # Aggregation / qualifiers.
        "前三",
        "前五",
        "前十",
        "后五",
        "最高",
        "最低",
        "平均",
        "均值",
        "总和",
        "总计",
        "合计",
        "日均",
        "月均",
        "季均",
        "年均",
        "周均",
        # General terms.
        "分析",
        "怎么样",
        "情况",
        "数据",
        "指标",
        "维度",
        "看看",
        "请问",
        "告诉",
        "为什么",
        "原因",
        "是多少",
        "多少",
        "是啥",
        "有什么",
        "各个",
        "各",
        "目前",
        "当前",
        "整体",
        "详细",
    }
)

# Fillers / courtesy / particles: Datarc marking these unknown does not affect query meaning,
# because semantic core (measure/dim/time) is elsewhere and recognized → query results remain trustworthy.
_NOISE_WORDS = frozenset(
    {
        # Courtesy / requests.
        "请问",
        "请",
        "帮我",
        "麻烦",
        "告诉我",
        "给我",
        "能否",
        "可以",
        "可否",
        "麻烦你",
        "请你",
        # Particles / conjunctions / exclamations.
        "一下",
        "下",
        "呀",
        "呢",
        "啊",
        "吗",
        "啦",
        "嗯",
        "哦",
        "哈",
        "呗",
        "嘛",
        "哟",
        "噢",
        "然后",
        "接着",
        "所以",
        "不过",
        "但是",
        "另外",
        "还有",
        "以及",
        "的话",
        "吧",
        "喔",
        "哇",
        # Redundant modifiers.
        "这个",
        "那个",
        "这些",
        "那些",
        "这样",
        "那样",
        "如此",
        "大概",
        "大致",
        "基本",
        "基本上",
        "应该",
        "大约",
        "差不多",
        "究竟",
        "到底",
        # Common particle fragments split by Datarc (look like noise).
        "之",
        "的",
        "了",
        "着",
        "过",
        "地",
        "得",
        "是",
        "有",
    }
)


# "Noise character set" expanded from _NOISE_WORDS / _STOP_WORDS identifies concatenations.
# For example, Datarc may leave "帮我看看一下" unsplit and unknown; all {帮/我/看/一/下}
# derive from noise words → treat as noise.
_NOISE_CHARS: frozenset[str] = frozenset(c for w in (_NOISE_WORDS | _STOP_WORDS) for c in w)


def is_noise_word(word: str) -> bool:
    """判断 datarc 标 unknown 的 token 是不是"无关紧要的填充词"——是的话 query
    仍有效，不触发作废；否则视为语义核心词缺失.

    判定（从松到严）：
    1. 精确命中 `_NOISE_WORDS` / `_STOP_WORDS` → 噪声
    2. 单字 → 噪声（"的"/"了"，"京"/"沪" 这种简称罕见被标 unknown）
    3. 纯 ASCII → 噪声（单位 / 型号；业务名 "GMV" datarc 一般识别为 alias）
    4. **每个字都在 _NOISE_CHARS 集合** → 噪声
       （覆盖 "帮我看看一下" "请问一下" "能不能告诉" 等被 datarc 整块切出的拼接串）
    5. 其它 2+ 字中文 → **核心词**（实体/维度值/业务名，保守 strict）
    """
    w = (word or "").strip()
    if not w:
        return True
    if w in _NOISE_WORDS or w in _STOP_WORDS:
        return True
    if len(w) == 1:
        return True
    if w.isascii():
        return True
    # Long string: every character in noise set → concatenated noise.
    return bool(all(c in _NOISE_CHARS for c in w))


def extract_candidate_entities(text: str) -> list[str]:
    """从 query text 里抽候选实体 token. 去重保留原顺序."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _CN_TOKEN_RE.findall(text):
        tok = m.strip()
        if not tok or tok in _STOP_WORDS or tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out


# Strip product vocabulary from natural-language data queries.
# UI terms for workspace can accidentally bind to unrelated business dimensions.
# Limit cleanup to this vocabulary family; broader terms may be real field names.
# The caller must disclose changes through _preflight_hint so the model can choose
# another wording if the source actually contains a matching field.
_PRODUCT_VOCAB = ("当前工作区", "本工作区", "该工作区", "此工作区", "工作区")


def strip_product_vocabulary(text: str) -> tuple[str, list[str]]:
    """剥掉发给 BI 的自然语言里属于**我们产品界面**的词。

    返回 ``(清洗后的文本, 剥掉的词)``；没命中就原样返回、列表为空（零行为变化）。
    """
    if not text:
        return text, []
    out = text
    hit: list[str] = []
    for w in _PRODUCT_VOCAB:  # Longest first; avoid "工作区" consuming part of "本工作区".
        if w in out:
            hit.append(w)
            out = out.replace(w, "")
    if not hit:
        return text, []
    # Stripping may leave harmless connective wording such as "覆盖全部可用月份"; collapse whitespace
    # so extra spaces do not become downstream token separators (punctuation doubled as data; see sprint notes).
    out = re.sub(r"\s{2,}", " ", out).strip()
    return (out or text), hit
