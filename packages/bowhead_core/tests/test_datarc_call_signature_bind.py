"""批1/c9 (workspace-space-split) — datarc 调用点真签名绑定守卫.

背景（impl review CONFIRMED P0）：c1 把 DatarcClient 方法首参改名
`workspace_id → datarc_workspace_id` 后，tool_registry 有 5 个调用点漏改
（bi_attribute×2 / bi_get_card / bi_get_cards / bi_explain），真调用必
TypeError；但 6855 绿测试全用 AsyncMock/duck-type stub，bind 永不发生，
假绿放行。本测试用 AST 扫出所有 `*.datarc*.<method>(...)` 调用点，逐个
对 DatarcClient **真签名** 做 inspect.bind——kwarg 改名/漏参/多参一律红。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from bowhead_bi_bridge import DatarcClient

_SRC = Path(__file__).resolve().parents[1] / "src" / "bowhead_core"


def _receiver_chain(node: ast.expr) -> str:
    """展平 `ctx.datarc` / `self._datarc` / `datarc_client` 这类 receiver 为点串."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _iter_datarc_calls() -> list[tuple[str, int, str, ast.Call]]:
    """(rel_path, lineno, method_name, call) for every DatarcClient-method call
    whose receiver 名字里带 datarc（ctx.datarc / datarc_client / self._datarc…）."""
    hits: list[tuple[str, int, str, ast.Call]] = []
    for py in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            method = node.func.attr
            receiver = _receiver_chain(node.func.value)
            if "datarc" not in receiver.lower():
                continue
            real = getattr(DatarcClient, method, None)
            if real is None or not callable(real):
                continue
            hits.append((str(py.relative_to(_SRC)), node.lineno, method, node))
    return hits


def test_datarc_client_call_sites_bind_real_signatures() -> None:
    calls = _iter_datarc_calls()
    # Minimum-count guard: fail here if broken AST matching finds nothing; never silently turn this into an empty test.
    assert len(calls) >= 8, f"只扫到 {len(calls)} 个 datarc 调用点——匹配逻辑疑似失效"

    failures: list[str] = []
    for rel, lineno, method, call in calls:
        sig = inspect.signature(getattr(DatarcClient, method))
        has_star_spread = any(isinstance(a, ast.Starred) for a in call.args)
        kw_names = [k.arg for k in call.keywords if k.arg is not None]
        has_kw_spread = any(k.arg is None for k in call.keywords)
        pos = [object()] * (1 + len([a for a in call.args if not isinstance(a, ast.Starred)]))
        kwargs = dict.fromkeys(kw_names)
        try:
            if has_star_spread or has_kw_spread:
                sig.bind_partial(*pos, **kwargs)  # With spreads, check only known keyword argument names.
            else:
                sig.bind(*pos, **kwargs)
        except TypeError as e:
            failures.append(f"{rel}:{lineno} → DatarcClient.{method}(kwargs={kw_names}): {e}")
    assert not failures, "datarc 调用点与 DatarcClient 真签名不匹配（c1 改名漏网或漏参）:\n" + "\n".join(failures)
