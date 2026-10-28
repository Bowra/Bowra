"""Secret redaction for untrusted user text and pasted material（配置副驾 §2.1 / 批 1-c 加固）.

一次扫描同时产出「脱敏文本」与「带标签的命中」——两者必须同源（批 1-c review P0-1：
原实现是「熵驱动脱敏 + label 只喂旁路」两套机制各干一半，漏抓整类真实密钥）。

规则优先级（收集 span → 冲突取高优先级 → 右往左替换）：
  R1 PEM 私钥块
  R2 URL userinfo 口令（只替换口令组，用户名保留）
  R3 JSON 对 "key": "value"，key 命中密钥词表
  R4 行式 KEY[:=]value，key 命中密钥词表 —— **不看长度不看熵**（DB_PASSWORD=hunter2 必抓）
  R5 行式 KEY[:=]value，key 未命中但 value 过 token 测试 —— **只替换 value，键名保留**
  R6 bearer <token>
  R7 裸 token（兜底）

token 测试三条同时成立：正则匹配（`=` 只作 base64 padding、点分整体化）、香农熵 ≥3.5、
最长连续 alnum run ≥20（杀掉下划线分词的表名/路径——实测业务文本 FP 从 14/14 降到 3/14）。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

REDACTED = "[REDACTED]"
MAX_SECRET_HITS = 20

_ENTROPY_THRESHOLD = 3.5
_MIN_ALNUM_RUN = 20

# Key-name criterion: **match broadly by default** (misses violate iron rule 4; false positives affect only prefill quality), exempt only empirically
# supported business shapes. R2 found unbounded substrings erased 22 business keys including primary_key/max_tokens/author_name;
# R3 found my exact-last-word overcorrection missed 25 genuine vendor keys such as PGPASSWORD/accessKeySecret/
# SECRET_KEY_BASE/key1. Current rule = substring matching + three narrow exception classes.
_SECRET_SUBSTRINGS = ("secret", "token", "key", "password", "passwd", "pwd", "passphrase", "credential", "auth", "dsn")
# Credential abbreviations lacking those substrings match **whole words only** (leaving `patch`/`bypass`/`risk` unaffected).
_SECRET_EXACT_WORDS = frozenset({"pat", "pass", "sk"})
# Ordinary English lookalikes containing secret substrings do not signal secrets.
_LOOKALIKE_WORDS = frozenset(
    {
        "keyword",
        "keywords",
        "keyspace",
        "keyboard",
        "monkey",
        "donkey",
        "turkey",
        "hockey",
        "jockey",
        "whiskey",
        "author",
        "authors",
        "authored",
        "authority",
        "authorities",
        "authentic",
        "authentication",
        "authenticate",
        "tokenize",
        "tokenizer",
        "tokenized",
        "connection",
        "connections",
        "connector",
        "connectors",
    }
)
# Strip purely noisy suffixes before classification; `AWS_ACCESS_KEY_ID`/`SECRET_KEY_BASE`/`credentials_json` are all secrets.
_SKIP_SUFFIX = frozenset(
    {
        "id",
        "base",
        "json",
        "yaml",
        "yml",
        "pem",
        "der",
        "file",
        "path",
        "str",
        "string",
        "value",
        "val",
        "new",
        "old",
        "prod",
        "dev",
        "test",
        "staging",
        "backup",
        "bak",
        "encrypted",
        "enc",
        "b64",
        "raw",
    }
)
_VERSION_SUFFIX_RE = re.compile(r"\A(v?\d+)\Z")
# These qualifiers before `key` imply database/structure (primary_key…); count qualifiers before `token(s)` imply usage metrics (max_tokens…).
_STRUCTURE_QUALIFIERS = frozenset(
    {
        "primary",
        "partition",
        "sort",
        "foreign",
        "unique",
        "natural",
        "composite",
        "index",
        "row",
        "shard",
        "cache",
        "dedup",
        "idempotency",
        "join",
        "map",
        "order",
        "hash",
        "surrogate",
        "group",
        "business",
        "case",
        "cluster",
        "range",
    }
)
_COUNT_QUALIFIERS = frozenset({"max", "min", "total", "avg", "prompt", "completion", "input", "output", "num", "sum"})
# Split camelCase **before lowercasing**; lowercasing first makes that branch unreachable (R3 observed accessKeySecret missed).
_WORD_SPLIT_RE = re.compile(r"[_.\-]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


def _secretish(word: str) -> bool:
    """词本身是否携带密钥信号：含密钥子串且不是形近词。"""
    if word in _LOOKALIKE_WORDS:
        return False
    return word in _SECRET_EXACT_WORDS or any(part in word for part in _SECRET_SUBSTRINGS)


def _is_secret_key(key: str) -> bool:
    """键名是否表明其值为密钥。

    从宽命中 + 两类窄豁免：形近词（keyword/author/connection/tokenizer）与结构/计数修饰
    （primary_key/max_tokens）。business 尾缀（token_count/auth_mode/secret_name）不需要单列——
    「末词必须 secretish」已经覆盖（突变实测：删掉专门的尾缀表，全部用例仍绿）。
    裸 `key` 单独一词不算——它在 SQL 与中文行文里太常见（`WHERE key='gmv'`），
    真凭据值仍由 R5/R7 的 token 测试兜底。
    """
    words = [word.lower() for word in _WORD_SPLIT_RE.split(key) if word]
    while len(words) > 1 and (words[-1] in _SKIP_SUFFIX or _VERSION_SUFFIX_RE.match(words[-1])):
        words.pop()
    if not words:
        return False
    last = words[-1]
    if not _secretish(last):
        return False
    if len(words) == 1 and last == "key":
        return False
    if len(words) > 1:
        previous = words[-2]
        if last in ("key", "keys") and previous in _STRUCTURE_QUALIFIERS:
            return False
        if previous in _COUNT_QUALIFIERS:
            return False
    return True


_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)
# Password = group(2); retain username and scheme.
_URL_USERINFO_RE = re.compile(r"(?<=://)([^:/@\s]+):([^@/\s]+)(?=@)")
_JSON_PAIR_RE = re.compile(r"\"([A-Za-z_][A-Za-z0-9_.\-]*)\"\s*:\s*\"([^\"\\]{1,500})\"")
# KEY=value / KEY: value (value ends at whitespace or quote; strip optional quotes).
# Do not anchor at line start: newly protected `user_text` is freeform; users write sentences and Markdown lists, not
# .env lines: `连接串是 DB_PASSWORD=hunter2`, `- password: hunter2`, and the latter half of `A=x B=y`
# were entirely missed with line-start anchoring (implementation review). Leading boundary excludes `/` to avoid path `dt=…`.
_KV_LINE_RE = re.compile(
    r"(?:^|(?<=[\s,;&?|`(\[{<\"'*+·、，；（【《]))([A-Za-z_][A-Za-z0-9_.\-]*)[ \t]*[:=][ \t]*(?!//)(\"|')?([^\s,;&\"']{1,500})(?:\2)?",
    re.MULTILINE,
)
_BEARER_RE = re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._~+/-]{8,})")
# `Authorization: Bearer xxx` KV value stops at the space, capturing only scheme; extend to the actual credential.
_SCHEME_WORD_RE = re.compile(r"(?i)\A(bearer|basic|token|apikey)\Z")
_SCHEME_TAIL_RE = re.compile(r"[ \t]+([^\s\"']{1,500})")
# `=` is only base64 trailing padding; dotted tokens remain whole (JWT is one hit, not fragmented).
_TOKEN_RE = re.compile(r"[A-Za-z0-9_+/-]{32,}={0,2}(?:\.[A-Za-z0-9_+/-]+={0,2})*")
_ALNUM_RUN_RE = re.compile(r"[A-Za-z0-9]+")


@dataclass(frozen=True, slots=True)
class SecretHit:
    """一个 hit = 一个可直接粘进输入框的完整凭据（批 1-c review P2-5）。"""

    value: str
    label: str | None
    kind: str  # "kv" | "json" | "bearer" | "url_userinfo" | "private_key" | "token"


@dataclass(frozen=True, slots=True)
class _Span:
    start: int
    end: int
    label: str | None
    kind: str
    priority: int


def _shannon_entropy(token: str) -> float:
    counts = {character: token.count(character) for character in set(token)}
    return -sum((count / len(token)) * math.log2(count / len(token)) for count in counts.values())


def _looks_like_token(token: str) -> bool:
    """熵 + 最小连续 alnum run 双门——单看熵会把 SQL 表名/路径/UUID 全误伤。"""
    if not any(character.isalpha() for character in token):
        return False
    if not (any(character.isdigit() for character in token) or any(character in "_+/=-" for character in token)):
        return False
    if _shannon_entropy(token) < _ENTROPY_THRESHOLD:
        return False
    runs = _ALNUM_RUN_RE.findall(token)
    return bool(runs) and max(len(run) for run in runs) >= _MIN_ALNUM_RUN


def _token_span(text: str, start: int, end: int) -> bool:
    """value span 是否**整体**过 token 测试（R5 用）。

    必须整体匹配 _TOKEN_RE（含 32 字符地板）——只查熵/run 会让 `organization=org-…`(28)
    这类非密钥标识符被误伤（实测）。键名命中密钥词表的走 R4，不经过这里。
    """
    value = text[start:end]
    match = _TOKEN_RE.fullmatch(value)
    return match is not None and _looks_like_token(value)


def _collect(text: str) -> list[_Span]:
    spans: list[_Span] = []

    for match in _PRIVATE_KEY_RE.finditer(text):
        spans.append(_Span(match.start(), match.end(), None, "private_key", 0))

    for match in _URL_USERINFO_RE.finditer(text):
        spans.append(_Span(match.start(2), match.end(2), None, "url_userinfo", 1))

    for match in _JSON_PAIR_RE.finditer(text):
        key = match.group(1)
        if _is_secret_key(key) and not _CJK_RE.search(match.group(2)):
            spans.append(_Span(match.start(2), match.end(2), key, "json", 2))
        elif _token_span(text, match.start(2), match.end(2)):
            spans.append(_Span(match.start(2), match.end(2), key, "json", 4))

    for match in _KV_LINE_RE.finditer(text):
        key = match.group(1)
        value_start, value_end = match.start(3), match.end(3)
        if _is_secret_key(key) and not _CJK_RE.search(text[value_start:value_end]):
            # Matching key captures value regardless of length or entropy (DB_PASSWORD=hunter2 / AKIA... / cli_xxx).
            # Values containing CJK characters cannot be credentials; avoid false positives in prose such as `分组 key: 渠道维度`.
            if _SCHEME_WORD_RE.match(text[value_start:value_end]):
                # One hit = one complete directly pasteable credential; a scheme-only hit is unusable.
                tail = _SCHEME_TAIL_RE.match(text, value_end)
                if tail is not None:
                    value_end = tail.end(1)
            spans.append(_Span(value_start, value_end, key, "kv", 3))
        elif _token_span(text, value_start, value_end):
            # Key unmatched: replace only value, **preserve key verbatim** (model needs it for field mapping).
            spans.append(_Span(value_start, value_end, key, "kv", 5))

    for match in _BEARER_RE.finditer(text):
        spans.append(_Span(match.start(2), match.end(2), "authorization", "bearer", 6))

    for match in _TOKEN_RE.finditer(text):
        if _looks_like_token(match.group(0)):
            spans.append(_Span(match.start(), match.end(), None, "token", 7))

    return spans


def _resolve(spans: list[_Span]) -> list[_Span]:
    """冲突取高优先级（priority 小者胜）；同优先级取靠前、更长者。"""
    ordered = sorted(spans, key=lambda s: (s.priority, s.start, -(s.end - s.start)))
    kept: list[_Span] = []
    for span in ordered:
        if any(span.start < other.end and other.start < span.end for other in kept):
            continue
        kept.append(span)
    return sorted(kept, key=lambda s: s.start)


def extract_secrets(text: str) -> tuple[str, list[SecretHit]]:
    """Return ``(redacted_text, hits)``——同源保证：每个 hit 的 value 都不在 redacted 里。"""
    if not text:
        return text, []
    kept = _resolve(_collect(text))
    if not kept:
        return text, []

    hits: list[SecretHit] = []
    seen: set[str] = set()
    for span in kept:
        value = text[span.start : span.end]
        if value in seen:
            continue
        seen.add(value)
        hits.append(SecretHit(value=value, label=span.label, kind=span.kind))
        if len(hits) >= MAX_SECRET_HITS:
            break

    redacted = text
    for span in sorted(kept, key=lambda s: s.start, reverse=True):
        redacted = redacted[: span.start] + REDACTED + redacted[span.end :]
    return redacted, hits


def redact_secrets(text: str) -> tuple[str, bool]:
    """旧签名保留（既有调用零改）：``(redacted_text, hit)``。"""
    redacted, hits = extract_secrets(text)
    return redacted, bool(hits)


def wrap_untrusted(text: str, *, tag: str) -> str:
    """把不可信文本包进 ``<tag>…</tag>`` 并折叠换行，防止内容破栏。

    批 2-a tech design §3.5。放在这里而不是新建模块：它与 :func:`extract_secrets`
    是同一件事的两半（一个挡注入、一个挡密钥），几乎总是成对使用。

    只做包裹与折栏，**不做 lint**——``lint_user_append`` 是 manifest ``system.append``
    指令槽的 reject 闸（命中即 raise），套到一次性的自然语言数据上会误伤，且撞
    「生成失败不挡手填」这条铁律。

    闭合串本身也被中和：文本里出现 ``</tag>`` 时转义成实体，使其无法提前闭合
    （不用零宽字符——那是不可见的，下一个读代码的人会以为是笔误）。
    """
    if not tag or not tag.replace("_", "").isalnum():
        raise ValueError(f"tag must be alphanumeric/underscore, got {tag!r}")
    # Fold only newlines/tabs (prevent wrapper escape), **not all whitespace**: flattening all paragraph structure of 1500-character
    # document bodies harms classification quality, invisible to gate tests (implementation review P2).
    body = re.sub(r"[\r\n\t\f\v]+", " ", text or "").strip()
    # Neutralize content's closing sequence: case-insensitive with internal whitespace tolerated (`</ TAG >` also closes the wrapper).
    body = re.sub(rf"<\s*/\s*{re.escape(tag)}\s*>", f"&lt;/{tag}&gt;", body, flags=re.IGNORECASE)
    return f"<{tag}>\n{body}\n</{tag}>"
