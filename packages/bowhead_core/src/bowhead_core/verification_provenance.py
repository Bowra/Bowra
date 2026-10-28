"""δ-#6 β verification hooks — pre / post LLM.

Two narrow helpers that splice into the existing β verification path
(see verification/engine.py for the rule-engine side; this module is the
δ-specific thin layer that doesn't need to be a rule):

- ``pre_llm_inject_facts``: per-turn fact retrieval block to splice into
  system prompt before LLM call. Thin wrapper over
  ``provenance_prompts.build_active_facts_block``.

- ``post_llm_verify_provenance``: parse all ``[bi:..]/[fact:..]/...`` tags
  in LLM output, return a ``ProvenanceVerification`` summary —
    * fact_warnings: ``[llm-std:]`` claim where a registered fact for the
      same concept exists (LLM ignored the override → flag for surface).
    * untagged_claims_count: lines with numbers / facts but no source tag.
    * is_user_corrected_match: tags where ``[fact:k]`` matches a fact with
      ``is_user_corrected=True`` — these are silent-skip from further verify.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bowhead_core.provenance import (
    extract_fact_keys,
    has_any_source,
    parse_tag,
)
from bowhead_core.provenance_flags import is_feature_enabled
from bowhead_core.provenance_prompts import build_active_facts_block

if TYPE_CHECKING:
    from bowhead_core.registered_fact_store import RegisteredFactStore

logger = logging.getLogger(__name__)


# Sentence-like fragments containing numbers / time / percentages / entities are untagged claims without a bound tag.
# Heuristic: Arabic digits + units (CNY/亿/万/%/Q[1-4]/年/月), or plain numbers paired with unit words.
_DATA_HINT_RE = re.compile(
    r"\d{2,}|\d+\.\d+|\d+%|Q[1-4]|\d{4}\s*年|[一二三四五六七八九十]+亿|[一二三四五六七八九十]+万",
    re.UNICODE,
)


@dataclass
class ProvenanceVerification:
    """Summary of post-LLM provenance check. ``passed=True`` when no issues."""

    passed: bool
    fact_warnings: list[str] = field(default_factory=list)  # fact keys LLM ignored
    untagged_claim_count: int = 0
    silent_skip_count: int = 0  # fact with is_user_corrected matched

    def as_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "fact_warnings": self.fact_warnings,
            "untagged_claim_count": self.untagged_claim_count,
            "silent_skip_count": self.silent_skip_count,
        }


async def pre_llm_inject_facts(
    *,
    workspace_id: str,
    user_text: str,
    fact_store: RegisteredFactStore,
    project_id: str | None = None,
    user_id: str | None = None,
) -> str:
    """Splice fact block into system prompt. Empty string when sub-flag off.

    Caller (turn_runner δ-#7) appends to compute system prompt before invoke.
    """
    return await build_active_facts_block(
        workspace_id=workspace_id,
        user_text=user_text,
        fact_store=fact_store,
        project_id=project_id,
        user_id=user_id,
    )


def _split_lines(text: str) -> list[str]:
    """Sentence-ish split for untagged-claim detection — newlines + 句号."""
    return [seg.strip() for line in text.splitlines() for seg in re.split(r"[。；;]", line) if seg.strip()]


async def post_llm_verify_provenance(
    text: str,
    *,
    workspace_id: str,
    fact_store: RegisteredFactStore,
    project_id: str | None = None,
    user_id: str | None = None,
) -> ProvenanceVerification:
    """Parse output, cross-check against RegisteredFacts.

    Returns ``ProvenanceVerification`` — ``passed`` is True when:
    - no untagged data claims (sentences with numbers but no [..:..] tag)
    - no [llm-std:] claims about a concept where a registered fact exists
      (LLM ignoring user override is a P0 anti-hallu signal)

    Silent skip cases (``[fact:k]`` matching a user-corrected fact) are
    counted but don't affect ``passed`` — they're the happy path.
    """
    if not is_feature_enabled("FACT_LOOKUP"):
        return ProvenanceVerification(passed=True)

    fact_keys = extract_fact_keys(text)
    silent_skip_count = 0
    if fact_keys:
        for key in fact_keys:
            try:
                f = await fact_store.get(
                    workspace_id=workspace_id,
                    fact_key=key,
                    project_id=project_id,
                    user_id=user_id,
                )
            except Exception as e:
                logger.warning("delta.post_verify_fact_get_failed key=%s err=%s", key, e)
                continue
            if f and f.is_user_corrected:
                silent_skip_count += 1

    # δ-#8.5 SRE P0-4: hoist fact list ONCE out of the per-tag loop. Old code
    # ran list_active per llm-std tag (N+1 SQLite queries). Single fetch shared
    # across all llm-std checks in this verify pass.
    fact_warnings: list[str] = []
    parsed_tags = list(parse_tag(text))
    has_llm_std = any(src.kind == "llm-std" for tag in parsed_tags for src in tag.sources)
    if has_llm_std:
        try:
            facts_nearby = await fact_store.list_active(
                workspace_id=workspace_id,
                project_id=project_id,
                user_id=user_id,
                limit=50,
            )
        except Exception as e:
            logger.warning("delta.post_verify_list_failed err=%s", e)
            facts_nearby = []
        if facts_nearby:
            lo = text.lower()
            for f in facts_nearby:
                # Heuristic: fact_key such as ``ebitda_definition``; split on ``_``, then any
                # token of ≥3 characters present in text counts as mentioning the concept. The full key matches too.
                tokens = [t for t in f.fact_key.split("_") if len(t) >= 3]
                tokens.append(f.fact_key)
                if any(t in lo for t in tokens) and f.fact_key not in fact_warnings:
                    fact_warnings.append(f.fact_key)

    # Untagged data claims — sentences with data hints but no nearby tag.
    untagged = 0
    if not has_any_source(text) and _DATA_HINT_RE.search(text):
        # Whole-text untagged: count lines with data hints.
        for sentence in _split_lines(text):
            if _DATA_HINT_RE.search(sentence) and not has_any_source(sentence):
                untagged += 1
    else:
        # Has some tags — only flag sentences without local tag.
        for sentence in _split_lines(text):
            if _DATA_HINT_RE.search(sentence) and not has_any_source(sentence):
                untagged += 1

    passed = not fact_warnings and untagged == 0
    return ProvenanceVerification(
        passed=passed,
        fact_warnings=fact_warnings,
        untagged_claim_count=untagged,
        silent_skip_count=silent_skip_count,
    )
