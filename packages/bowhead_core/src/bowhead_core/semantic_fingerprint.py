"""确定性语义签名（词面指纹）——反馈候选与 verified examples 共用的切词/签名 helper。

设计权威：`docs/sprints/agent-feedback-loop-2026-09-08/tech-design.md` §2.1。

- **不是 embedding**：签名要做 UNIQUE 索引与 cooldown 键，必须可精确相等、确定性。
- 归一化＝NFKC → casefold（全角/半角、兼容分解统一——同 `memory_write_policy._normalize`
  的教训：不做 NFKC，全角变体就能绕过去重与冷却）。
- 切词沿用记忆批 1 的两条硬教训：**CJK 单字切（含假名）、拉丁/数字按连续段成词**；
  取排序去重 token 集，canonical JSON 过 HMAC-SHA256。
- ⚠ 改本文件的切词或 canonical 口径＝改指纹＝存量 suppressions 与唯一键**集体失效**
  （同 `memory_candidates._candidate_content_fingerprint` 的血泪注释）。
  只能新增 `*_v2`，不得修改 v1 的行为。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import unicodedata

_PURPOSE_SEMANTIC_SIG_V1 = b"bowhead/feedback-candidate/semantic-sig/v1"


def derive_semantic_secret(seed: str | bytes) -> bytes:
    """从实例根密钥（调用方传 `BOWHEAD_FERNET_KEY`）按固定用途派生签名密钥。

    与 `MemoryCandidateStore.__post_init__` 同款派生形制；不新增环境变量。
    """
    seed_bytes = seed.encode("utf-8") if isinstance(seed, str) else seed
    return hmac.new(seed_bytes, _PURPOSE_SEMANTIC_SIG_V1, hashlib.sha256).digest()


def _is_cjk(ch: str) -> bool:
    # Write code points as escapes, not literal characters (lesson documented in memory_write_policy).
    # CJK Unified Ideographs + Extension A + Japanese kana (hiragana/katakana, matching the memory precedent).
    return "\u4e00" <= ch <= "\u9fff" or "\u3400" <= ch <= "\u4dbf" or "\u3040" <= ch <= "\u30ff"


def tokenize_v1(text: str) -> tuple[str, ...]:
    """NFKC+casefold 归一后：CJK/假名单字、拉丁/数字连续段成词，其余当分隔符。

    返回**排序去重**后的 token 元组（词面集合语义：语序无关、重复无关）。
    """
    normalized = unicodedata.normalize("NFKC", text).casefold()
    tokens: set[str] = set()
    word: list[str] = []
    for ch in normalized:
        if _is_cjk(ch):
            if word:
                tokens.add("".join(word))
                word.clear()
            tokens.add(ch)
        elif ch.isalnum():
            word.append(ch)
        else:
            if word:
                tokens.add("".join(word))
                word.clear()
    if word:
        tokens.add("".join(word))
    return tuple(sorted(tokens))


def semantic_signature_v1(
    *,
    secret: bytes,
    target_kind: str,
    target_ref: str,
    text: str,
) -> str:
    """(target_kind, target_ref, 词面 token 集) 的 HMAC 十六进制签名。

    target 前缀限定碰撞面：同一句诉求指向不同资产时是不同签名。
    （kind/ref 收进 HMAC 内；需要按 target 归属排查冷却时，读
    `feedback_candidate_suppressions` 的 target_kind/target_ref 明文列。）
    """
    canonical = json.dumps(
        {
            "kind": target_kind,
            "ref": target_ref,
            "tokens": tokenize_v1(text),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hmac.new(secret, canonical.encode("utf-8"), hashlib.sha256).hexdigest()
