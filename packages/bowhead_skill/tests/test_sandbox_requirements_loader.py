"""pptx-from-template M2 (wheelhouse) — loader parses `sandbox_requirements`.

Mirrors the existing `allowed-tools` list-or-comma parse: a SKILL.md frontmatter
`sandbox_requirements:` may be a YAML list OR a comma-string, and both land in
`SkillSpec.sandbox_requirements: list[str]`. Absent → empty (backward compatible).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bowhead_skill import load_skill

if TYPE_CHECKING:
    from pathlib import Path


def _write(root: Path, name: str, fm_extra: str) -> Path:
    d = root / name
    d.mkdir()
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: deps test\n{fm_extra}---\n\n# body\n",
        encoding="utf-8",
    )
    return d


def test_sandbox_requirements_list(tmp_path: Path) -> None:
    d = _write(
        tmp_path,
        "forecasting",
        "sandbox_requirements:\n  - prophet\n  - cmdstanpy==1.2.0\n",
    )
    s = load_skill(d)
    assert s.sandbox_requirements == ["prophet", "cmdstanpy==1.2.0"]


def test_sandbox_requirements_comma_string(tmp_path: Path) -> None:
    d = _write(tmp_path, "fc", "sandbox_requirements: prophet, cmdstanpy==1.2.0\n")
    s = load_skill(d)
    assert s.sandbox_requirements == ["prophet", "cmdstanpy==1.2.0"]


def test_sandbox_requirements_absent_is_empty(tmp_path: Path) -> None:
    d = _write(tmp_path, "plain", "")
    s = load_skill(d)
    assert s.sandbox_requirements == []


def test_sandbox_requirements_hyphen_key_alias(tmp_path: Path) -> None:
    # `sandbox-requirements:` (hyphen) is accepted like `allowed-tools`.
    d = _write(tmp_path, "fc2", "sandbox-requirements:\n  - prophet\n")
    s = load_skill(d)
    assert s.sandbox_requirements == ["prophet"]


def test_sandbox_requirements_strips_blanks(tmp_path: Path) -> None:
    d = _write(tmp_path, "fc3", "sandbox_requirements: prophet, , cmdstanpy ,\n")
    s = load_skill(d)
    assert s.sandbox_requirements == ["prophet", "cmdstanpy"]
