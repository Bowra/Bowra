"""StructuredExtractor — PDF/DOCX text → fields_json (合同金额/起止日/付款条件 ...).

kb-knowledge-tables sprint Day 2. tech design §2 (Extractor).

输入: 一份文档的 plain text + 文档表的 ExtractField list.
输出: {field_name: {value, source_snippet, source_page, confidence, ...}}.

Pipeline:
1. chunk text (长 PDF > MAX_CHUNK_CHARS 切片) → list[chunk]
2. 每 chunk 走 LLM (json_object response_format) → 部分 fields
3. cross-verify=true 字段 N=2 self-consistency (同 chunk 跑 2 次, 多数投票) — RASC ACL 2025
4. merge per-chunk → highest-confidence-per-field (LlamaExtract pattern)
5. citation grounding: source_snippet fuzzy match against source text, 不 match → set ""
6. type coerce: currency/number 转数字, date 留 ISO string

Spike 验过:
- Spike 2: source_snippet fuzzy match 100% (100% 真合同 4 字段都在 PDF 子串)
- Spike 3: json_object stability 95%+ (5/5 PASS in spike)
- Spike 4: list/dict normalize 三 shape 全 OK

P0 deps: router (any provider 走 OpenAI 兼容).
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bowhead_core.rate_card import ModelCreditRates, ModelTokenUsage, ModelUsageLine

if TYPE_CHECKING:
    from bowhead_core.kb.doc_type_store import ExtractField

logger = logging.getLogger(__name__)


# Slice long PDFs. qwen-plus 32K context, reserve 8K for prompt + output.
MAX_CHUNK_CHARS = 24_000
# Citation snippet: first N chars for fuzzy matching (LLM occasionally returns an overly long whole paragraph).
MAX_SNIPPET_MATCH_CHARS = 100
# Self-consistency N (high-sensitivity fields). N=2 balances cost (2x) and error reduction (~30%).
SELF_CONSISTENCY_N = 2


# ─── Prompt templates ─────────


_EXTRACT_SYS_PROMPT = (
    "你是合同/单据/文档结构化字段抽取 agent. 从下面文本抽 用户指定的字段.\n\n"
    "每字段返:\n"
    "{\n"
    '  "value": <抽出的值, 找不到 null>,\n'
    '  "source_snippet": "<verbatim 引用原文中说该字段的原句, ≤100 chars, 必须是原文复制不能改写>",\n'
    '  "source_page": <页码 int 估算, 不知 null>,\n'
    '  "confidence": <0-1 float, 自评抽取信心>\n'
    "}\n\n"
    "规则:\n"
    "1. 不要编造 — 找不到就 null + confidence 0\n"
    "2. source_snippet 必须是文档原文 verbatim 复制, 不能改写不能省略\n"
    "3. type=currency: value 用纯数字 (RMB 5,000,000 → 5000000)\n"
    "4. type=date: value 用 ISO format YYYY-MM-DD\n"
    "5. type=number: value 用纯数字\n"
    "6. type=string: value 简洁直接, 不带解释\n"
    "7. 输出 JSON 顶层 {field_name: {value,...}}, 字段名跟用户给的一致\n"
    "8. 仅输出 JSON, 不要解释\n"
)


def _build_user_prompt(text: str, fields: list[ExtractField]) -> str:
    fields_spec = []
    for f in fields:
        spec: dict = {"name": f.name, "type": f.type}
        if f.description:
            spec["description"] = f.description
        if f.extraction_hint:
            spec["hint"] = f.extraction_hint
        if f.required:
            spec["required"] = True
        fields_spec.append(spec)
    return f"文档文本:\n{text}\n\n要抽取的字段:\n{json.dumps(fields_spec, ensure_ascii=False)}"


# ─── normalize_llm_output (per Spike 4) ─────────


def _list_to_dict_by_schema(
    llm_output: list,
    field_names: list[str],
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for i, item in enumerate(llm_output):
        if i < len(field_names):
            out[field_names[i]] = item
    return out


def _normalize_llm_output(raw: Any, field_names: list[str]) -> dict[str, dict]:
    """LLM 输出 → 统一为 {field_name: field_data}. 支持 3 种 shape (per Spike 4).

    1. list (Spike 2 实测): [{value,...}, {value,...}] → 按 schema 顺序映射
    2. nested: {"fields": [...]} → 按 schema 顺序
    3. dict by name: {field_name: {value,...}} → 直接返回 (理想 shape)
    """
    if isinstance(raw, list):
        return _list_to_dict_by_schema(raw, field_names)
    if isinstance(raw, dict):
        if "fields" in raw and isinstance(raw["fields"], list):
            return _list_to_dict_by_schema(raw["fields"], field_names)
        # Already keyed by name (ideal shape).
        out: dict[str, dict] = {}
        for k, v in raw.items():
            if isinstance(v, dict):
                out[str(k)] = v
        return out
    return {}


# ─── citation grounding ─────────


def _normalize_for_match(s: str) -> str:
    return re.sub(r"\s+", "", s)


def backfill_chunk_ids(fields: dict, chunks: list[tuple[str, str]]) -> int:
    """Provenance path B: set ``field['chunk_id']`` by locating the origin chunk.

    For each field with a ``source_snippet``, whitespace-insensitive substring-match
    the snippet (first ``MAX_SNIPPET_MATCH_CHARS``) against each chunk's content;
    the first containing chunk's id is recorded. Best-effort — empty snippet / no
    match leaves chunk_id absent (→ None downstream). Mutates ``fields`` in place;
    returns the number of fields bound (for observability). Reuses the same
    normalization as ``_ground_citation`` so behavior matches citation grounding.
    """
    if not chunks or not isinstance(fields, dict):
        return 0
    norm_chunks = [(cid, _normalize_for_match(content or "")) for cid, content in chunks]
    bound = 0
    for fdata in fields.values():
        if not isinstance(fdata, dict):
            continue
        snippet = fdata.get("source_snippet")
        if not snippet or not isinstance(snippet, str):
            continue
        key = _normalize_for_match(snippet[:MAX_SNIPPET_MATCH_CHARS])
        if not key:
            continue
        for cid, norm in norm_chunks:
            if cid and key in norm:
                fdata["chunk_id"] = cid
                bound += 1
                break
    return bound


def _ground_citation(
    field_data: dict,
    source_text_normalized: str,
) -> dict:
    """source_snippet fuzzy match (whitespace-insensitive) against source text.

    匹配: snippet 前 100 chars 必须是 source 子串. 不匹配 → 清空 snippet
    + lower confidence (LLM 编造 source 视为 fact 不可信).
    """
    snippet = field_data.get("source_snippet", "")
    if not snippet or not isinstance(snippet, str):
        return field_data
    snippet_norm = _normalize_for_match(snippet[:MAX_SNIPPET_MATCH_CHARS])
    if snippet_norm and snippet_norm in source_text_normalized:
        field_data["citation_verified"] = True
        return field_data
    # No match → untrusted snippet. Keep value, mark unverified.
    field_data = dict(field_data)
    field_data["citation_verified"] = False
    field_data["source_snippet_unverified"] = snippet
    field_data["source_snippet"] = ""
    cur_conf = field_data.get("confidence", 0)
    try:
        field_data["confidence"] = max(0.0, float(cur_conf) - 0.2)
    except (TypeError, ValueError):
        field_data["confidence"] = 0.5
    return field_data


# ─── type coercion ─────────


_NUM_RE = re.compile(r"-?[\d,]+(?:\.\d+)?")


def _coerce_value(value: Any, type_: str) -> Any:
    """coerce LLM raw value 跟字段 type 对齐.

    currency/number: 提数字 (剥 ¥/RMB/逗号 / 元/万元)
    date: 留原始 string (假设 LLM 跟 ISO YYYY-MM-DD, 简单留住)
    string: str(value)
    """
    if value is None:
        return None
    if type_ in ("currency", "number"):
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            # 万元 / 万: × 10000
            multiplier = 1
            v = value
            if "万元" in v or v.endswith("万"):
                multiplier = 10000
            v_clean = re.sub(r"[¥￥,，\s元万]", "", v)
            m = _NUM_RE.search(v_clean)
            if m:
                num_str = m.group(0).replace(",", "")
                try:
                    n = float(num_str)
                    if n.is_integer():
                        return int(n) * multiplier
                    return n * multiplier
                except ValueError:
                    return value
        return value
    if type_ == "date":
        return str(value) if value is not None else None
    return str(value) if value is not None else None


# ─── chunk text ─────────


def _chunk_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """简单切片 (per_chunk extract 用). 长 PDF 走 LlamaExtract pattern.

    优先按 \\n\\n 段落切, 段落超大才硬切.
    """
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for para in text.split("\n\n"):
        p = para + "\n\n"
        if cur_len + len(p) > max_chars and cur:
            chunks.append("".join(cur))
            cur = [p]
            cur_len = len(p)
        else:
            cur.append(p)
            cur_len += len(p)
    if cur:
        chunks.append("".join(cur))
    # Hard-split any chunk that's still too big (single paragraph > max).
    final: list[str] = []
    for c in chunks:
        if len(c) <= max_chars:
            final.append(c)
        else:
            for i in range(0, len(c), max_chars):
                final.append(c[i : i + max_chars])
    # Drop whitespace-only fragments (artifact of trailing \n\n appender).
    return [c for c in final if c.strip()]


# ─── merge per-chunk results ─────────


def _merge_chunks(per_chunk_fields: list[dict[str, dict]]) -> dict[str, dict]:
    """Per-field highest-confidence merge across chunks (LlamaExtract pattern).

    长 PDF chunk 1 抽 "合同金额"=null conf 0, chunk 2 抽 "合同金额"=500w conf 0.9
    → merged 取 chunk 2 那条.
    """
    merged: dict[str, dict] = {}
    for chunk_fields in per_chunk_fields:
        for fname, fdata in chunk_fields.items():
            if not isinstance(fdata, dict):
                continue
            cur = merged.get(fname)
            new_conf = fdata.get("confidence") or 0
            cur_conf = (cur or {}).get("confidence") or 0
            try:
                new_conf_f = float(new_conf)
                cur_conf_f = float(cur_conf)
            except (TypeError, ValueError):
                new_conf_f, cur_conf_f = 0.0, 0.0
            new_value = fdata.get("value")
            # Overwrite only when new result has value and higher confidence.
            if cur is None or (new_value is not None and new_conf_f > cur_conf_f):
                merged[fname] = fdata
    return merged


# ─── self-consistency vote (cross_verify=true fields) ─────────


def _vote_value(samples: list[Any]) -> tuple[Any, float]:
    """Majority vote among samples. Tie → 取 first non-null. 返 (value, agreement_rate)."""
    non_null = [s for s in samples if s is not None]
    if not non_null:
        return None, 0.0
    counter: dict[Any, int] = {}
    for s in non_null:
        # Convert dict/list to json key to make hashable
        key = s if isinstance(s, (str, int, float, bool)) else json.dumps(s, ensure_ascii=False, sort_keys=True)
        counter[key] = counter.get(key, 0) + 1
    best_key, best_count = max(counter.items(), key=lambda kv: kv[1])
    agree = best_count / len(samples)
    # Restore best_key to original value
    for s in non_null:
        s_key = s if isinstance(s, (str, int, float, bool)) else json.dumps(s, ensure_ascii=False, sort_keys=True)
        if s_key == best_key:
            return s, agree
    return None, 0.0


def _self_consistency_merge(
    samples: list[dict[str, dict]],
    cross_verify_field_names: set[str],
) -> dict[str, dict]:
    """N samples → 单 dict. cross_verify 字段走多数投票, 其他取 sample[0]."""
    if not samples:
        return {}
    base = dict(samples[0])  # Copy first by default
    if not cross_verify_field_names:
        return base
    for fname in cross_verify_field_names:
        values = [s.get(fname, {}).get("value") for s in samples if isinstance(s.get(fname), dict)]
        voted, agree = _vote_value(values)
        if fname not in base or not isinstance(base[fname], dict):
            base[fname] = {}
        else:
            base[fname] = dict(base[fname])
        base[fname]["value"] = voted
        base[fname]["self_consistency_agree"] = agree
        # agree ≤half (N=2 all disagree →0.5; N=3 three-way →0.33) → mark review + reduce conf
        if agree <= 0.5:
            cur_conf = base[fname].get("confidence", 0)
            try:
                base[fname]["confidence"] = max(0.0, float(cur_conf) - 0.3)
            except (TypeError, ValueError):
                base[fname]["confidence"] = 0.3
            base[fname]["needs_review"] = True
    return base


# ─── result DTO ─────────


@dataclass
class ExtractionResult:
    """Extractor 单文件输出."""

    fields: dict[str, dict[str, Any]]
    extraction_duration_ms: int
    extractor_model: str
    chunks_processed: int
    self_consistency_runs: int = 0  # Count of fields using N>1 sampling
    has_low_confidence_fields: bool = False  # Any field confidence <0.5
    raw_llm_outputs: list[str] = field(default_factory=list)  # debug
    # kb-matrix M2 §8①: accumulate per-extract usage + expose effective provider/model
    # from last successful invoke (billing/health short-circuit/cache-key anchor). Old worker path ignores defaults.
    usage_input_tokens: int = 0
    usage_output_tokens: int = 0
    effective_provider: str | None = None
    effective_model: str | None = None
    model_usage: tuple[ModelUsageLine, ...] = ()


# ─── main extractor ─────────


class StructuredExtractor:
    """Extract structured fields from text. DI: caller 注 router (LLM provider)."""

    def __init__(self, router: object) -> None:
        # router: bowhead_router.Router instance (has .invoke(role, LLMRequest)).
        self._router = router

    async def extract(
        self,
        *,
        text: str,
        fields: list[ExtractField],
        extractor_model: str = "qwen-plus",
        cross_verify_enabled: bool = True,
        max_tokens: int = 1500,
        user_pref: Any = None,
        strict_invoke: bool = False,
    ) -> ExtractionResult:
        """Extract fields_json from `text` per `fields` schema.

        Pipeline: chunk → per-chunk LLM → (optional N=2 for cross_verify) → merge → ground citations.

        kb-matrix M2 §8②: ``user_pref`` 透传 router.invoke（健康短路 seam）；
        ``strict_invoke=True`` 时 invoke 异常不再吞成 "{}" 而是抛出（矩阵引擎
        要区分 llm_invoke_failed）。双参数默认值 = 旧行为逐字节不变（worker 不传）。
        """
        if not fields:
            return ExtractionResult(
                fields={},
                extraction_duration_ms=0,
                extractor_model=extractor_model,
                chunks_processed=0,
            )

        t0 = time.time()
        chunks = _chunk_text(text)
        field_names = [f.name for f in fields]
        cross_verify_names = {f.name for f in fields if f.cross_verify} if cross_verify_enabled else set()

        per_chunk_results: list[dict[str, dict]] = []
        sc_runs = 0
        raw_outputs: list[str] = []
        usage_input = 0
        usage_output = 0
        model_usage: list[ModelUsageLine] = []
        effective_provider: str | None = None
        effective_model: str | None = None

        for chunk in chunks:
            n_samples = SELF_CONSISTENCY_N if cross_verify_names else 1
            samples: list[dict[str, dict]] = []
            for sample_i in range(n_samples):
                # Slightly raise temperature for multiple samples to create diversity (RASC pattern).
                temperature = 0.0 if sample_i == 0 else 0.3
                raw, resp = await self._invoke_llm(
                    chunk=chunk,
                    fields=fields,
                    model=extractor_model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    user_pref=user_pref,
                    strict=strict_invoke,
                )
                raw_outputs.append(raw)
                if resp is not None:
                    # Defensive access: existing fake test responses are MagicMock(content=...);
                    # autogenerated attributes are nonnumeric—skip anything other than int/str, preserving old behavior.
                    usage = getattr(resp, "usage", None)
                    u_in = getattr(usage, "input_tokens", None)
                    u_out = getattr(usage, "output_tokens", None)
                    if isinstance(u_in, int):
                        usage_input += u_in
                    if isinstance(u_out, int):
                        usage_output += u_out
                    provider = getattr(resp, "provider", None)
                    model_used = getattr(resp, "model", None)
                    if isinstance(provider, str) and provider:
                        effective_provider = provider
                    if isinstance(model_used, str) and model_used:
                        effective_model = model_used
                    cached = getattr(usage, "cached_tokens", None)
                    if (
                        isinstance(u_in, int)
                        and not isinstance(u_in, bool)
                        and isinstance(u_out, int)
                        and not isinstance(u_out, bool)
                        and isinstance(cached, int)
                        and not isinstance(cached, bool)
                        and isinstance(provider, str)
                        and provider
                        and isinstance(model_used, str)
                        and model_used
                    ):
                        rates = getattr(self._router, "model_credit_rates", {}).get((provider, model_used))
                        family = getattr(self._router, "model_provider_types", {}).get(provider)
                        model_usage.append(
                            ModelUsageLine(
                                provider,
                                model_used,
                                ModelTokenUsage.from_provider_counts(
                                    u_in,
                                    cached,
                                    u_out,
                                    cached_in_input=family != "anthropic_compatible",
                                    rates=ModelCreditRates.from_config(rates),
                                ),
                            )
                        )
                normalized = self._parse_llm_output(raw, field_names)
                samples.append(normalized)
            if n_samples > 1:
                sc_runs += len(cross_verify_names)
                merged_sample = _self_consistency_merge(samples, cross_verify_names)
            else:
                merged_sample = samples[0] if samples else {}
            per_chunk_results.append(merged_sample)

        # Merge across chunks (per-field highest confidence).
        merged = _merge_chunks(per_chunk_results)

        # Citation grounding (single normalize-once, fuzzy substring).
        source_norm = _normalize_for_match(text)
        for fname, fdata in list(merged.items()):
            if isinstance(fdata, dict):
                merged[fname] = _ground_citation(fdata, source_norm)

        # Type coercion.
        for f in fields:
            fdata = merged.get(f.name)
            if isinstance(fdata, dict) and "value" in fdata:
                fdata["value"] = _coerce_value(fdata["value"], f.type)

        # Has low confidence? (>=1 field < 0.5)
        has_low_conf = any(isinstance(d, dict) and float(d.get("confidence", 0) or 0) < 0.5 for d in merged.values())

        duration_ms = int((time.time() - t0) * 1000)
        return ExtractionResult(
            fields=merged,
            extraction_duration_ms=duration_ms,
            extractor_model=extractor_model,
            chunks_processed=len(chunks),
            self_consistency_runs=sc_runs,
            has_low_confidence_fields=has_low_conf,
            raw_llm_outputs=raw_outputs,
            usage_input_tokens=usage_input,
            usage_output_tokens=usage_output,
            effective_provider=effective_provider,
            effective_model=effective_model,
            model_usage=tuple(model_usage),
        )

    async def _invoke_llm(
        self,
        *,
        chunk: str,
        fields: list[ExtractField],
        model: str,
        max_tokens: int,
        temperature: float,
        user_pref: Any = None,
        strict: bool = False,
    ) -> tuple[str, Any]:
        """返 (content, resp | None)——resp 供 extract 累加 usage / 记 effective
        provider·model（M2 §8①）。私有方法，唯一调用点在 extract 内部同步改。"""
        from bowhead_router import LLMRequest, Message

        user_prompt = _build_user_prompt(chunk, fields)
        req = LLMRequest(
            model=model,
            messages=[Message(role="user", content=user_prompt)],
            system=_EXTRACT_SYS_PROMPT,
            max_output_tokens=max_tokens,
            temperature=temperature,
            response_format={"type": "json_object"},
        )
        # user_pref=None omits kwarg—old worker path call shape remains byte-identical
        # (existing fake routers have signatures without **kw).
        extra_kwargs: dict[str, Any] = {}
        if user_pref is not None:
            extra_kwargs["user_pref"] = user_pref
        try:
            resp = await self._router.invoke("lead", req, **extra_kwargs)
        except Exception as e:
            if strict:
                raise
            logger.warning("StructuredExtractor: LLM invoke failed: %s", e)
            return "{}", None
        return (resp.content or "").strip(), resp

    @staticmethod
    def _parse_llm_output(raw: str, field_names: list[str]) -> dict[str, dict]:
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Try fenced extraction
            m = re.search(r"\{.*\}|\[.*\]", raw, re.DOTALL)
            if not m:
                logger.warning("StructuredExtractor: JSON parse failed, raw=%s", raw[:200])
                return {}
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                logger.warning("StructuredExtractor: fenced JSON parse failed, raw=%s", raw[:200])
                return {}
        return _normalize_llm_output(data, field_names)
