"""sandbox 失败必须带上「运维能照做的原因」和「模型能自纠的信息」.

2026-08-18 某客户实测的两个形状：
① 一上午 7 次失败全是 docker 层 exit 125 秒退，但 prod 把 stdout/stderr 清空后只剩
   「暂不支持代码执行 (sandbox 未就绪)」一句——daemon 挂了 / 镜像缺失 / runtime 没注册
   三种完全不同的运维动作合并成同一句，现场只能靠 exit code 和时间分布反推。
② compute agent 没声明 inputs 就照着 bi_query 的文件名猜路径，裸 FileNotFoundError 不带
   任何可行动信息 → 换个杜撰目录再猜一次。对照组：另一轮它看到可用清单后一次就改对了。

两组断言都成对写：该带的要带，不该动的一个字都不许加。
"""

from __future__ import annotations

import importlib.util
import sys
import urllib.error
from pathlib import Path
from types import ModuleType

import pytest

_RUNNER_PATH = Path(__file__).resolve().parents[1] / "catalog" / "python_sandbox" / "scripts" / "runner.py"


def _runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_diag_runner_under_test", _RUNNER_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_diag_runner_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    "failure,cause,state,retry_safe",
    [
        (urllib.error.URLError(ConnectionRefusedError("refused")), "executor_unreachable", "not_started", True),
        (TimeoutError("slow response"), "executor_response_timeout", "unknown", False),
        (urllib.error.HTTPError("http://executor/run", 503, "unavailable", {}, None), "executor_http_error", "unknown", False),
        (ConnectionResetError("reset after send"), "executor_transport_error", "unknown", False),
    ],
)
def test_executor_transport_failure_does_not_confuse_slow_execution_with_an_unreachable_service(
    tmp_path, monkeypatch, failure, cause, state, retry_safe
):
    runner = _runner()
    monkeypatch.setenv("BOWHEAD_ENV", "prod")

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(runner.urllib.request, "urlopen", fail)
    result = runner._run_via_executor("http://executor", "secret", workdir=tmp_path, timeout=120, requirements=[])
    assert (result["cause"], result["execution_state"], result["retry_safe"]) == (cause, state, retry_safe)
    assert result["ok"] is False and result["stderr"] == ""
    assert "secret" not in result["error"]


# ─── ① Not-ready cause enum ───────────────────────────────────────────


def test_not_ready_cause_is_classified() -> None:
    r = _runner()
    cases = {
        "docker: error response: unknown runtime specified: runsc": "runtime_unregistered",
        "docker: error response from daemon: invalid runtime name": "runtime_unregistered",
        "unable to find image 'bowhead-sandbox:1.0.2' locally, docker pull failed": "image_missing",
        "cannot connect to the docker daemon at unix:///var/run/docker.sock": "daemon_unreachable",
        "docker: error response from daemon: something else": "daemon_unreachable",
    }
    for stderr, expected in cases.items():
        assert r._sandbox_not_ready_cause(stderr.lower()) == expected, stderr


def test_friendly_error_carries_cause_even_when_prod_scrubs_stdio(monkeypatch) -> None:
    """★ prod 抹掉 stdout/stderr 是对的（可能含主机路径），但**原因枚举必须留下**。"""
    r = _runner()
    monkeypatch.setenv("BOWHEAD_ENV", "prod")
    out = r._friendly_runtime_error(
        stdout="",
        stderr="docker: Error response from daemon: unknown runtime specified: runsc",
        dur=11,
        exit_code=125,
    )
    assert out is not None
    assert out["stderr"] == ""  # Details remain redacted.
    assert out["cause"] == "runtime_unregistered"  # Operations can tell whether to install gVisor or change runc.
    assert out["exit_code"] == 125


def test_non_docker_failure_is_left_alone(monkeypatch) -> None:
    """负对照：用户代码自己的报错不许被改写成「sandbox 未就绪」。"""
    r = _runner()
    monkeypatch.setenv("BOWHEAD_ENV", "prod")
    assert r._friendly_runtime_error(stdout="", stderr="ZeroDivisionError: division by zero", dur=5, exit_code=1) is None


# ─── ② Self-correction hints for FileNotFoundError ─────────────────────


def test_missing_file_hint_lists_input_keys_not_paths() -> None:
    """★ 提示里给 key 和取法，**不给真实路径**——贴出 /workdir/a.csv 等于教它硬编码，
    正是本次要治的毛病。"""
    r = _runner()
    result = {
        "ok": False,
        "stderr": "FileNotFoundError: [Errno 2] No such file or directory: 'INPUTS/sales/x.csv'",
        "error": "exit_code=1",
    }
    r._hint_missing_file(result, {"df26": {"path": "/workdir/inputs/df26.csv"}, "df25": {"path": "/workdir/inputs/df25.csv"}})
    assert "df26(df26.csv)" in result["stderr"] and "df25(df25.csv)" in result["stderr"]
    assert "inputs/" in result["stderr"]
    # Negative control: omit absolute paths so the model cannot copy them as hardcoded paths.
    assert "/workdir/inputs/df26.csv" not in result["stderr"]


def test_missing_file_hint_says_no_inputs_were_declared() -> None:
    """★ 实测最常见的那一种：一个 input 都没传，模型却以为 INPUTS 是个目录。"""
    r = _runner()
    result = {
        "ok": False,
        "stderr": "FileNotFoundError: [Errno 2] No such file or directory: '2026年销量_225e27a.csv'",
        "error": "exit_code=1",
    }
    r._hint_missing_file(result, {})
    assert "一个 input 都没声明" in result["stderr"]
    assert "inputs/<key>.csv" in result["stderr"]
    assert "artifact_id" in result["stderr"]  # Explain how to pass it.
    assert "_ref_artifact_id" in result["stderr"]  # And where to get this ID.


def test_missing_file_hint_names_the_literal_inputs_string_shape() -> None:
    """★ compute-agent-optimize 批1：116 30 天里 FNF 的 25% 是把 INPUTS['x']['path'] 加引号
    写成字符串字面量——通用提示教的取法跟它写的只差一对引号，要单独点名。"""
    r = _runner()
    result = {
        "ok": False,
        "stderr": "FileNotFoundError: [Errno 2] No such file or directory: \"INPUTS['rows']['path']\"",
        "error": "exit_code=1",
    }
    r._hint_missing_file(result, {"rows": {"path": "/workdir/inputs/rows.csv"}})
    assert "字符串字面量" in result["stderr"]
    assert "不加引号" in result["stderr"]
    # Do not append the generic list for this shape; it distracts toward changing keys instead of removing quotes.
    assert "本次可用" not in result["stderr"]


def test_literal_inputs_string_with_no_inputs_declared_gets_both_hints() -> None:
    """review 批1-R7：字面量写法 + 一个 input 都没声明——只说「去引号」下一步照样 KeyError，
    必须把「先声明 inputs」一并给出。"""
    r = _runner()
    result = {
        "ok": False,
        "stderr": "FileNotFoundError: [Errno 2] No such file or directory: \"INPUTS['rows']['path']\"",
        "error": "exit_code=1",
    }
    r._hint_missing_file(result, {})
    assert "字符串字面量" in result["stderr"]
    assert "一个 input 都没声明" in result["stderr"]


def test_no_hint_when_the_failure_is_unrelated() -> None:
    """负对照：不是找不到文件就别贴清单，否则每个报错后面都挂一坨噪音。"""
    r = _runner()
    result = {"ok": False, "stderr": "KeyError: '销售金额'", "error": "exit_code=1"}
    before = dict(result)
    r._hint_missing_file(result, {"df26": {"path": "/workdir/a.csv"}})
    assert result == before


# ─── ③ Artifact fetch failures must point to base-path misconfiguration ───


def test_artifact_404_points_at_the_gateway_base_path() -> None:
    """★ 实测：同一个 artifact id 少了部署前缀 404、带上前缀 200。裸的「HTTP Error 404」
    看不出这一层，ops 会去查 artifact 存不存在（存在），白绕一圈。"""
    import urllib.error

    r = _runner()

    class _Opener:
        def open(self, *_a, **_kw):
            raise urllib.error.HTTPError("http://h/api/v1/files/art_x", 404, "Not Found", {}, None)

    orig = r.urllib.request.build_opener
    r.urllib.request.build_opener = lambda *_a, **_kw: _Opener()
    try:
        try:
            r._fetch_artifact("art_x", "https://host.example")
        except RuntimeError as e:
            msg = str(e)
        else:
            raise AssertionError("expected RuntimeError")
    finally:
        r.urllib.request.build_opener = orig

    assert "404" in msg
    assert "BOWHEAD_GATEWAY_URL" in msg
    assert "base path" in msg
    assert "https://host.example" in msg  # Include the actual base so operations can immediately spot the missing part.


def test_non_404_http_error_is_not_reinterpreted() -> None:
    """负对照：401/500 不是前缀问题，别硬套这套说辞。"""
    import urllib.error

    r = _runner()

    class _Opener:
        def open(self, *_a, **_kw):
            raise urllib.error.HTTPError("http://h/api/v1/files/art_x", 401, "Unauthorized", {}, None)

    orig = r.urllib.request.build_opener
    r.urllib.request.build_opener = lambda *_a, **_kw: _Opener()
    try:
        try:
            r._fetch_artifact("art_x", "https://host.example")
        except urllib.error.HTTPError as e:
            assert e.code == 401
        else:
            raise AssertionError("expected HTTPError to propagate")
    finally:
        r.urllib.request.build_opener = orig


# ═════════════════════════════════════════════════════════════════════
# sandbox-input-contract L1 — self-explanatory INPUTS
#
# Compare both supported code shapes with omitted inputs:
#   Hardcoded 'rows.csv' → FileNotFoundError → `_hint_missing_file` provides the full hint
#   Documented INPUTS['rows'] → KeyError → error contains only `exit_code=1`
# Both failures must explain the missing declaration so callers can correct it.
# ═════════════════════════════════════════════════════════════════════


def _run_scaffold(tmp_path, code: str, payload: dict) -> str:
    """把 scaffold + 用户代码当成真的 user.py **跑一遍**，返 stderr。

    ⚠ 真跑而不是只断言它「长什么样」——scaffold 是拼进 user.py 的**代码**，
    写坏了字符串断言照样绿、真机每次调用全挂（`_SCAFFOLD` 历史上就打死过
    subprocess 后端，见本文件姊妹测试 test_sandbox_scaffold_path.py）。
    """
    import json as _json
    import subprocess
    import sys

    r = _runner()
    (tmp_path / "input.json").write_text(_json.dumps(payload), encoding="utf-8")
    (tmp_path / "user.py").write_text(r._SCAFFOLD + "\n" + code, encoding="utf-8")
    proc = subprocess.run([sys.executable, str(tmp_path / "user.py")], capture_output=True, text=True, timeout=60)
    return proc.stderr


def test_empty_inputs_keyerror_carries_the_call_shape(tmp_path) -> None:
    """★ L1 的命门：没声明 inputs 时，KeyError 自己就得说清怎么声明。

    突变：把 scaffold 里的 `_BhInputs` 换回普通 dict → 本条必红。
    """
    shape = 'python_exec(code="<python>", inputs={"rows": {"artifact_id": "art_..."}})'
    err = _run_scaffold(tmp_path, "INPUTS['rows']\n", {"inputs": {}, "_call_shape": shape})
    assert "KeyError" in err
    assert "没有声明任何输入" in err
    assert "python_exec(code=" in err, "按入口渲的骨架没进异常"
    assert "INPUTS" in err and "path" in err, "取法那一半丢了"


def test_key_mismatch_lists_the_available_keys(tmp_path) -> None:
    """★ `_hint_missing_file` **永远看不见**的那一类：声明了，但代码里键名不符。

    它只在 FileNotFoundError 时触发，而这一档抛的是 KeyError。
    """
    err = _run_scaffold(
        tmp_path,
        "INPUTS['rows']\n",
        {"inputs": {"d16": {"path": "/x/d16.csv"}, "d21": {"path": "/x/d21.csv"}}},
    )
    assert "KeyError" in err
    assert "d16" in err and "d21" in err, "没列出真实可用的 key"
    assert "自己起的名字" in err


def test_hit_key_behaves_exactly_like_a_plain_dict(tmp_path) -> None:
    """★ 负对照：命中时一个字节都不许变（自解释只发生在 miss 那条路上）。"""
    err = _run_scaffold(
        tmp_path,
        "assert INPUTS['rows']['path'] == '/x/rows.csv'\nassert list(INPUTS) == ['rows']\nassert 'nope' not in INPUTS\n",
        {"inputs": {"rows": {"path": "/x/rows.csv"}}},
    )
    assert err.strip() == "", f"命中路径不该有任何输出：{err}"


def test_missing_call_shape_falls_back_to_the_entry_agnostic_wording(tmp_path) -> None:
    """拿不到骨架（老 host / 其它调用方）→ 用两条路都成立的保底说法，不猜层级。"""
    err = _run_scaffold(tmp_path, "INPUTS['rows']\n", {"inputs": {}})
    assert "与 code 同级" in err
    assert "顶层" not in err


# ═════════════════════════════════════════════════════════════════════
# Never describe declared-but-unfetched inputs as undeclared.
#
# A failed input fetch must not emit these contradictory diagnoses together:
#   Original message: KeyError: 'rows' —— 本次调用没有声明任何输入…
#   [runner] input errors: input 'rows' fetch failed: HTTP Error 404
# The first message is false: the model declared inputs, but the platform failed to fetch them. If all fetches fail, INPUTS is empty too;
# the old `if not self` checked emptiness first and blamed the caller for a platform failure, prompting pointless code rewrites and retries.
# ═════════════════════════════════════════════════════════════════════


def test_declared_but_unfetched_does_not_blame_the_caller(tmp_path) -> None:
    """★ 命门：声明了却全取不到时，**不许**出现「没有声明任何输入」。

    突变：把 scaffold 里 `if _bh_unresolved:` 整段删掉 → 本条必红。
    """
    err = _run_scaffold(
        tmp_path,
        "INPUTS['rows']\n",
        {"inputs": {}, "_unresolved_inputs": ["rows"], "_call_shape": "python_exec(...)"},
    )
    assert "KeyError" in err
    assert "没有声明任何输入" not in err, "把平台取不到说成了调用方没声明"
    assert "rows" in err and "没能取到" in err
    assert "input errors" in err, "没把人指向真正写着原因的那一行"


def test_partial_fetch_failure_names_both_sides(tmp_path) -> None:
    """部分失败：既要点名取不到的，也要列出仍然可用的。"""
    err = _run_scaffold(
        tmp_path,
        "INPUTS['rows']\n",
        {"inputs": {"ok1": {"path": "/x/ok1.csv"}}, "_unresolved_inputs": ["rows"]},
    )
    assert "rows" in err and "没能取到" in err
    assert "ok1" in err, "没列出仍然可用的 key"


def test_no_unresolved_keeps_the_original_two_branches(tmp_path) -> None:
    """★ 负对照：没有取失败时，原来两条话术一个字都不许变。"""
    shape = 'python_exec(code="<python>", inputs={"rows": {"artifact_id": "art_..."}})'
    empty = _run_scaffold(tmp_path, "INPUTS['rows']\n", {"inputs": {}, "_call_shape": shape})
    assert "没有声明任何输入" in empty and "没能取到" not in empty
    mism = _run_scaffold(tmp_path, "INPUTS['rows']\n", {"inputs": {"d16": {"path": "/x/d16.csv"}}})
    assert "自己起的名字" in mism and "没能取到" not in mism


def test_runner_wires_unresolved_names_end_to_end(tmp_path, monkeypatch) -> None:
    """★★ 测接线不只测函数：真跑 `main()`，让 fetch 真失败，读 result.json。

    只测 scaffold 会漏掉「main 忘了把 `_unresolved_inputs` 写进 input.json」——
    那正是本修复唯一的新接线。突变：删掉 main 里写该键的三行 → 本条必红。
    """
    import json as _json
    import sys

    r = _runner()
    workdir = tmp_path / "wd"
    inp = tmp_path / "in.json"
    inp.write_text(
        _json.dumps({"code": "p = INPUTS['rows']['path']\n", "inputs": {"rows": {"artifact_id": "art_does_not_exist"}}}),
        encoding="utf-8",
    )
    # Point to an unreachable gateway → fetch fails → resolved lacks 'rows'.
    monkeypatch.setenv("BOWHEAD_GATEWAY_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("BOWHEAD_SANDBOX_BACKEND", "subprocess")
    monkeypatch.setattr(sys, "argv", ["runner.py", "--input", str(inp), "--workdir", str(workdir)])
    assert r.main() == 0

    container_input = _json.loads((workdir / "input.json").read_text(encoding="utf-8"))
    assert container_input["_unresolved_inputs"] == ["rows"], "声明名没随 input.json 进容器"
    assert "rows" not in (container_input.get("inputs") or {}), "取失败的 key 不该出现在 inputs 里"

    result = _json.loads((workdir / "result.json").read_text(encoding="utf-8"))
    assert result["ok"] is False
    assert "没能取到" in result["stderr"], "真跑一遍拿到的仍是旧话术"
    assert "没有声明任何输入" not in result["stderr"]


# ═════════════════════════════════════════════════════════════════════
# L3/L4 — include actual column names when one is missing; lowercase `inputs` alias
#
# Mutually exclusive classification of 142 sandbox failures on 116 over 30 days (harness-side priority): after the INPUTS fix
# (bare `KeyError: 'rows'` ended on 09-07; all messages were enriched from 09-10), the leading cause became
#   M2 KeyError: column/dict 30 cases 21.1% ← the missing key is a business column invented by the model
#   M4 NameError             5 cases  3.5% ← including lowercase `inputs[...]`
# Observed missing keys: '营业额' '品牌' '种类大类' '退款额_25' '6月环比%' '营业额 / 元'
# Both '营业额' and '营业额 / 元' appeared in the same data, so approximate matching must
# handle unit suffixes (difflib's default cutoff misses this pair with a score of only 0.55).
# 12 of 38 cases occurred on groupby/merge-derived frames, beyond host-side checks of input CSV headers;
# inspect actual runtime objects inside the container instead.
# ═════════════════════════════════════════════════════════════════════


def test_lowercase_inputs_alias_reads_the_same_dict(tmp_path) -> None:
    """L3：`inputs['k']` 与 `INPUTS['k']` 等价——小写正是工具参数名，模型照着写不该炸。

    突变：删掉 scaffold 里 `inputs = INPUTS` → 本条必红（NameError）。
    """
    err = _run_scaffold(
        tmp_path,
        "assert inputs['rows']['path'] == '/x/rows.csv'\nassert inputs is INPUTS\n",
        {"inputs": {"rows": {"path": "/x/rows.csv"}}},
    )
    assert err.strip() == "", f"别名路径不该有任何输出：{err}"


def test_user_code_may_shadow_the_lowercase_alias(tmp_path) -> None:
    """负对照：用户自己写 `inputs = ...` 必须照常覆盖（scaffold 在前，用户代码在后）。"""
    err = _run_scaffold(
        tmp_path,
        "inputs = {'mine': 1}\nassert inputs == {'mine': 1}\nassert INPUTS['rows']['path'] == '/x/rows.csv'\n",
        {"inputs": {"rows": {"path": "/x/rows.csv"}}},
    )
    assert err.strip() == "", f"别名不该挡住用户自己的赋值：{err}"


def test_missing_column_keyerror_lists_the_real_columns(tmp_path) -> None:
    """★ L4 命门：编出来的列名要撞上**真实列名清单**。

    突变：删掉 scaffold 里 `_bh_sys.excepthook = _bh_excepthook` → 本条必红。
    """
    code = "import pandas as pd\ndf = pd.DataFrame({'城市': ['A'], '营业额 / 元': [1], '客流量 / 人次': [2]})\ndf['营业额']\n"
    err = _run_scaffold(tmp_path, code, {"inputs": {}})
    assert "KeyError" in err
    assert "df 的真实列名" in err, "没点名是哪个 DataFrame"
    assert "城市" in err and "客流量 / 人次" in err, "没列出真实列名"
    assert "最接近" in err and "营业额 / 元" in err, "带单位后缀这一档没被近似匹配捞到"


def test_derived_frame_columns_are_reported_too(tmp_path) -> None:
    """★ 派生帧（groupby/agg 之后）也要覆盖——只读入参表头的做法在这里是瞎的。"""
    code = (
        "import pandas as pd\n"
        "raw = pd.DataFrame({'品类': ['x', 'x'], '额': [1, 2]})\n"
        "g = raw.groupby('品类', as_index=False)['额'].sum()\n"
        "g['销售额']\n"
    )
    err = _run_scaffold(tmp_path, code, {"inputs": {}})
    assert "KeyError" in err
    assert "g 的真实列名" in err, "派生帧没被捞到"
    assert "品类" in err and "额" in err


def test_inputs_keyerror_is_not_diluted_by_column_hints(tmp_path) -> None:
    """★ 负对照：`_BhInputs.__missing__` 那三档自解释文案已经够了，不许再叠一层。

    突变：删掉 excepthook 里 `co_name == '__missing__'` 的早退 → 本条必红。
    """
    code = "import pandas as pd\ndf = pd.DataFrame({'a': [1]})\nINPUTS['rows']\n"
    err = _run_scaffold(tmp_path, code, {"inputs": {"d16": {"path": "/x/d16.csv"}}})
    assert "自己起的名字" in err and "d16" in err
    assert "真实列名" not in err, "把 INPUTS 的自解释文案冲淡了"


def test_non_keyerror_is_left_alone(tmp_path) -> None:
    """负对照：别的异常一个字都不许加。"""
    code = "import pandas as pd\ndf = pd.DataFrame({'a': [1]})\nraise ValueError('boom')\n"
    err = _run_scaffold(tmp_path, code, {"inputs": {}})
    assert "ValueError: boom" in err
    assert "真实列名" not in err


def test_diagnostics_never_swallow_the_original_traceback(tmp_path) -> None:
    """★★ 诊断增强坏掉也不许盖掉用户的真异常——原始 traceback 必须先打出来。

    这里让 pandas 在 excepthook 里必然抛（把 DataFrame 换成一个 columns 会炸的赝品），
    验证 stderr 里仍然有完整的原始 KeyError。
    """
    code = (
        "import sys, types\n"
        "import pandas as pd\n"
        "class Evil(pd.DataFrame):\n"
        "    @property\n"
        "    def columns(self):\n"
        "        raise RuntimeError('boom in diagnostics')\n"
        "df = Evil({'a': [1]})\n"
        "dict()['期望的列']\n"
    )
    err = _run_scaffold(tmp_path, code, {"inputs": {}})
    assert "KeyError" in err and "期望的列" in err, "原始异常被诊断代码盖掉了"
    assert "boom in diagnostics" not in err, "诊断自己的异常泄漏到了用户面前"
