"""Provenance Taxonomy — Sprint δ.

Each LLM claim is tagged with source provenance. 6 source kinds (per tech-design §1.2):

| Tag           | Meaning                              | Example                    |
|---------------|--------------------------------------|----------------------------|
| [bi:art_id]   | BI tool real return                  | [bi:art_001]               |
| [kb:doc#chunk]| KB document chunk                    | [kb:rules.md]              |
| [user:turn]   | User directive in this/prior turn    | [user:t_xxx]               |
| [fact:key]    | RegisteredFact (cross-session)       | [fact:ebitda_definition]   |
| [llm-std:]    | LLM training (encyclopedic standard) | [llm-std:]                 |
| [llm-rsn:..]  | LLM reasoning chain over other tags  | [llm-rsn:bi:art|fact:cogs] |

Per-claim BNF:
  <source-tag>     ::= "[" <source-spec> ("|" <source-spec>)* "]"
  <source-spec>    ::= <kind> ":" <ref>
  <kind>           ::= "bi" | "kb" | "user" | "fact" | "llm-std" | "llm-rsn"
  <ref>            ::= [^]|:]* (use encode_ref for : | ] in raw refs)

`ref` containing `:`, `|`, `]` MUST percent-encode (round 2 BE-P0-2 fix):
  ":" → "%3A", "|" → "%7C", "]" → "%5D"

γ migration: existing [num:V:U:src:S] regex remains; parsed and remapped to [bi:S]
in new BNF for compatibility (see migrate_legacy_num_tag).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

SourceKind = Literal["bi", "kb", "user", "fact", "llm-std", "llm-rsn"]
_VALID_KINDS = ("bi", "kb", "user", "fact", "llm-std", "llm-rsn")


@dataclass
class Source:
    """Single source spec — one (kind, ref) pair within a tag."""

    kind: SourceKind
    ref: str  # decoded (no percent-encoding)


@dataclass
class ProvenanceTag:
    """Source provenance for a claim. May contain multiple sources (chain)."""

    sources: list[Source]


# BNF regex: matches `[<kind>:<ref>(|<kind>:<ref>)*]`
# ref is `[^]|:]*` (excludes raw `:` `|` `]`) plus `%XX` percent-encoded.
_KIND_PATTERN = "(?:" + "|".join(_VALID_KINDS) + ")"
_REF_PATTERN = r"(?:[^\]|:]|%[0-9A-Fa-f]{2})*"
PROVENANCE_RE = re.compile(r"\[(" + _KIND_PATTERN + r":" + _REF_PATTERN + r"(?:\|" + _KIND_PATTERN + r":" + _REF_PATTERN + r")*)\]")


def encode_ref(raw: str) -> str:
    """Percent-encode reserved chars in ref (BNF safe)."""
    return raw.replace("%", "%25").replace(":", "%3A").replace("|", "%7C").replace("]", "%5D")


def decode_ref(encoded: str) -> str:
    """Inverse of encode_ref. Order matters: decode % last."""
    return encoded.replace("%5D", "]").replace("%7C", "|").replace("%3A", ":").replace("%25", "%")


def parse_tag(text: str) -> list[ProvenanceTag]:
    """Find all provenance tags in `text`. Multi-tag text returns one ProvenanceTag per `[..]`.

    δ-#8.5 Backend P0-3: if **any** spec inside a chain has an invalid kind,
    drop the **whole tag** — not just the malformed spec. The previous
    skip-malformed-spec policy let ``[bi:art|kbdoc]`` (no colon in second spec)
    silently parse as just ``[bi:art]``, which then makes
    ``is_data_claim_valid`` return True for what is actually a confused tag.
    Anti-hallucination correctness > permissive parsing.
    """
    out: list[ProvenanceTag] = []
    for m in PROVENANCE_RE.finditer(text):
        inner = m.group(1)
        sources: list[Source] = []
        chain_ok = True
        for spec in inner.split("|"):
            if ":" not in spec:
                chain_ok = False
                break
            kind, _, ref = spec.partition(":")
            if kind not in _VALID_KINDS:
                chain_ok = False
                break
            sources.append(Source(kind=kind, ref=decode_ref(ref)))  # type: ignore[arg-type]
        if chain_ok and sources:
            out.append(ProvenanceTag(sources=sources))
    return out


def render_tag(tag: ProvenanceTag) -> str:
    """Serialize a ProvenanceTag back to canonical `[..]` form (encodes refs)."""
    parts = [f"{s.kind}:{encode_ref(s.ref)}" for s in tag.sources]
    return "[" + "|".join(parts) + "]"


def is_data_claim_valid(tag: ProvenanceTag) -> bool:
    """Per tech-design §1.2: data claim (numbers / entities / time)
    must have at least one bi: / user: / fact: source. llm-std/llm-rsn alone INVALID
    for data claims (anti-hallucination, G2.5)."""
    return any(s.kind in ("bi", "user", "fact") for s in tag.sources)


def strip_provenance_tags(text: str) -> str:
    """Strip machine provenance markers from human-readable text.

    Preserve live [data:...] Artifact-chip markup. Machine provenance uses [bi:...];
    historical data markup retains its existing meaning.
    """
    if not isinstance(text, str) or not text:
        return text
    # Remove **leading spaces** too: tags usually follow a conclusion, as in "…结论。 [llm-std:X]";
    # removing only the tag leaves double spaces. Do not compress whitespace afterward: it damages Markdown indentation/alignment.
    return _STRIP_RE.sub("", text).rstrip()


_TAG = r"(?:" + PROVENANCE_RE.pattern + r")"
# Two branches; their order is essential for correctness:
#  ① Parentheses contain **only a tag**: remove them too (116 observed "概念（[llm-std:]）");
#     removing only the tag leaves `（）`. Try this branch first.
#  ② Bare tag plus leading spaces.
# ⚠ Do not clean up empty parentheses afterward: that also consumes code references such as `foo()` (observed false removal).
_STRIP_RE = re.compile(r"[ \t]*[（(][ \t]*" + _TAG + r"[ \t]*[)）]|[ \t]*" + _TAG)


def has_any_source(text: str) -> bool:
    """True if text contains at least one provenance tag (δ BNF).

    citation-audit M3-5：原来还 or 一个 legacy `[num:…:src:S]` 判断（γ 代残留），
    M1 停写 + M2 洗净后恒 false → 与 `migrate_legacy_num_tag`（γ→δ remap，已零
    消费方）一并删除。
    """
    return bool(PROVENANCE_RE.search(text))


def extract_fact_keys(text: str) -> list[str]:
    """Return all fact_key refs in `[fact:..]` tags within text. Used by β verify."""
    out: list[str] = []
    for tag in parse_tag(text):
        for s in tag.sources:
            if s.kind == "fact":
                out.append(s.ref)
    return out
