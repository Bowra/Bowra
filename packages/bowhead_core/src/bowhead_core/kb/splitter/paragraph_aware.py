"""Paragraph-aware splitter — RecursiveCharacterTextSplitter wrap.

Tech design v3.6 §3.2 + PRD v0.7 §3.3.6 句号 hard cap (法律条文场景).

P1-G: V1 用 cl100k_base (OpenAI GPT-4 tokenizer), 跟 qwen 实际 tokenizer 误差 ±20%.
chunk_size=800 (cl100k) 在 qwen 实际可能 600-1000 token, V1 接受这个误差.
V2 升 qwen native tokenizer (`Qwen/Qwen2.5-Tokenizer` huggingface 或 dashscope tokenizer API).
"""

from __future__ import annotations

from langchain_text_splitters import RecursiveCharacterTextSplitter

# Sprint ω Phase 1: tiktoken cl100k_base usage unified to bowhead_core.token_estimator.
# (was: local `_TOKEN_ENCODER = tiktoken.get_encoding("cl100k_base")`)
from bowhead_core.token_estimator import (
    estimate_tokens as count_tokens,
)
from bowhead_core.token_estimator import (
    truncate_to_tokens,
)

# Re-export to preserve the existing public API surface (count_tokens / truncate_to_tokens
# are imported by name elsewhere in the KB splitter pipeline).
__all__ = [
    "PARAGRAPH_SEPARATORS",
    "count_tokens",
    "truncate_to_tokens",
]


# PRD v0.7 §3.3.6 + tech design §3.2: hard cap at sentence punctuation, no character splitting (legal clauses)
PARAGRAPH_SEPARATORS: list[str] = [
    "\n\n",  # Paragraph
    "\n",  # Line
    "。",
    "！",
    "？",  # Chinese sentence punctuation
    ".",
    "!",
    "?",  # English sentence punctuation
    # NO spaces / character-level fallback—hard cap at sentence punctuation
]

# α-5: Sentence-aware splitter with extended punctuation fallback.
# Adds clause separators (；！?, Chinese comma ，；：) as lower-priority separators so very long
# paragraphs without sentence punctuation still split at clause boundaries instead of being
# returned whole and exceeding chunk_size.
SENTENCE_AWARE_SEPARATORS: list[str] = [
    "\n\n",  # paragraph
    "\n",  # line
    "。",
    "！",
    "？",
    "!",
    "?",
    ".",  # full sentence end
    "；",
    ";",  # semicolon / parallel-clause
    "，",  # Chinese comma (clause)
    ",",  # English comma (only as last resort)
    "：",
    ":",  # colon (list lead-in)
    # NO spaces / char-level fallback—preserves "no mid-character split" rule
]


# Anthropic Contextual Retrieval standard 800 tokens (aligned with PRD v0.7 §3.3.5)
DEFAULT_CHUNK_SIZE = 800
DEFAULT_CHUNK_OVERLAP = 80  # 10% overlap


def make_paragraph_splitter(
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> RecursiveCharacterTextSplitter:
    """Paragraph-aware splitter, 中文友好.

    句号 hard cap: 不切字符级别 (法律条文不能切到字).
    长 chunk (单 paragraph > chunk_size token) 走 RecursiveCharacterTextSplitter
    的 fallback 排序, 但因 separators 列表无字符级 fallback, 长 paragraph 整段保留 (超 cap).
    """
    return RecursiveCharacterTextSplitter(
        separators=PARAGRAPH_SEPARATORS,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        length_function=count_tokens,
        add_start_index=True,
        keep_separator=True,
    )


def make_sentence_aware_splitter(
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> RecursiveCharacterTextSplitter:
    """α-5: Sentence-aware splitter with clause-level fallback.

    比 make_paragraph_splitter 多了 ；, 中文逗号 ， 等 fallback separators. 用于:
    - 长段落 (> chunk_size) 无句号时, 按 clause 切, 不再整段超 cap
    - 中英混排, 兼容 ; , : 各类清单 / 表述

    仍然不切字符 (不破坏 legal text 完整性).
    """
    return RecursiveCharacterTextSplitter(
        separators=SENTENCE_AWARE_SEPARATORS,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        length_function=count_tokens,
        add_start_index=True,
        keep_separator=True,
    )
