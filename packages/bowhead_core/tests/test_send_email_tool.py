"""Contract tests for the team SMTP-backed send_email Lead tool."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from bowhead_core.agents._lead_context import set_current_subagent_invocations
from bowhead_core.email_config import SmtpConfig
from bowhead_core.email_send_confirmation import EmailSendConfirmations
from bowhead_core.language_context import LanguageContext
from bowhead_core.tool_registry import ToolRegistry
from bowhead_core.types import ToolInvocation

from bowhead_core import send_email_tool as setool

# Category-data excerpt from production 1.3.36 on 116: amounts are in yuan, shares in %.
_CATEGORY_CSV = (
    "品类,销售额,占比,排名\n"
    "护肤美妆,954123456.12,31.84,1\n"
    "服饰鞋包,611873205.62,20.42,2\n"
    "家居日用,401795126.43,13.41,3\n"
    "食品饮料,267983514.09,8.95,4\n"
    "钟表/眼镜,145897063.28,4.87,5\n"
    "数码家电,131487552.19,4.39,6\n"
    "母婴玩具,120118734.61,4.01,7\n"
    "男装及其他,98790416.35,3.30,8\n"
)
_WRONG_BODY = (
    "各位好，本月品类销售概况如下：\n"
    "护肤美妆用品约 9.51 亿元，位列第一；第 5 名 男装及其他。\n"
    "头部四品类合计占比 74.64%。完整数据图表已附于画布报告中。"
)
_RIGHT_BODY = "各位好，本月品类销售概况如下：\n护肤美妆用品约 9.54 亿元，位列第一；第 5 名 钟表/眼镜，占比 4.87%。\n头部四品类合计占比 74.62%。"


def _run(coro):
    return asyncio.run(coro)


def _ctx(**over) -> SimpleNamespace:
    base = {
        "team_id": "team_1",
        "user_id": "usr_owner",
        "turn_id": "t_1",
        "session_id": "s_1",
        "user_text": "把这个品类分析发邮件给 recipient@example.com",
        "config_lite_supported": True,
        "artifact_store": None,
        "language_context": LanguageContext(output_language="zh-CN"),
    }
    base.update(over)
    return SimpleNamespace(**base)


def _args(**over: str) -> dict[str, str]:
    args = {"to": "recipient@example.com", "subject": "项目更新", "body": "本周进展如下。"}
    args.update(over)
    return args


class _Store:
    def __init__(self, config: dict[str, object]) -> None:
        self.config = config

    async def get_team_email_config_decrypted(self, team_id: str) -> dict[str, object]:
        assert team_id == "team_1"
        return self.config


class _Confirmations:
    """Scripted owner decision; records every snapshot shown to the owner."""

    def __init__(self, decision: str) -> None:
        self.decision = decision
        self.requests: list[dict[str, object]] = []

    async def request(self, **kwargs: object) -> str:
        self.requests.append(kwargs)
        return self.decision


class _Artifacts:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files

    async def list_result_artifacts_for_turn(self, turn_id: str):
        assert turn_id == "t_1"
        return [
            SimpleNamespace(id=f"art_{i}", filename=name, kind="tool_data", trust="trusted", size=len(data))
            for i, (name, data) in enumerate(self.files.items())
        ]

    async def open_read(self, art) -> bytes:
        return self.files[art.filename]


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    setool.set_send_email_store_provider(lambda: _Store({"host": "smtp.example.com", "port": 587, "from_addr": "bot@example.com"}))
    sent: dict[str, object] = {}

    async def _deliver(to: str, subject: str, body: str, *, config: SmtpConfig):
        sent.update(to=to, subject=subject, body=body, config=config)
        return True, None

    monkeypatch.setattr(setool, "deliver_email", _deliver)
    return sent


@pytest.fixture
def owner_decides():
    def _bind(decision: str) -> _Confirmations:
        confirmations = _Confirmations(decision)
        setool.set_send_email_confirmations(confirmations)  # type: ignore[arg-type]
        return confirmations

    yield _bind
    setool.set_send_email_confirmations(EmailSendConfirmations())


async def _call(args: dict[str, str], ctx: SimpleNamespace, invocations: list[ToolInvocation] | None = None):
    set_current_subagent_invocations(list(invocations or []))
    return await setool._handler(args, ctx)


def _peer_with(*nested: ToolInvocation, answer: str = "") -> ToolInvocation:
    return ToolInvocation(tool_name="subagent_spawn", arguments={}, result={"final_answer": answer}, nested=list(nested))


def test_registration_is_lead_only() -> None:
    registry = ToolRegistry()
    assert setool.register_send_email_tool(registry) is True
    spec = registry.get(setool.TOOL_NAME)
    assert spec.allowed_for_roles == frozenset({"lead"})
    assert set(spec.schema.input_schema["required"]) == {"to", "subject", "body"}


def test_reports_team_email_service_not_configured() -> None:
    setool.set_send_email_store_provider(lambda: _Store({}))

    result = _run(_call(_args(), _ctx()))

    assert result == {
        "ok": False,
        "error": "email_service_not_configured",
        "message": "当前团队尚未配置邮件服务，暂时无法发送邮件。请前往团队设置的“邮件服务”完成配置。",
    }


def test_sends_confirmed_snapshot_with_current_team_configuration(configured, owner_decides) -> None:
    confirmations = owner_decides("approved")

    result = _run(_call(_args(), _ctx()))

    assert result == {"ok": True, "message": "邮件已发送。", "sent": {"to": "recipient@example.com", "subject": "项目更新"}}
    assert confirmations.requests[0]["owner_user_id"] == "usr_owner"
    assert (confirmations.requests[0]["to"], confirmations.requests[0]["subject"], confirmations.requests[0]["body"]) == (
        "recipient@example.com",
        "项目更新",
        "本周进展如下。",
    )
    assert sent_matches(configured, _args())
    assert isinstance(configured["config"], SmtpConfig) and configured["config"].source == "team"


def sent_matches(sent: dict[str, object], args: dict[str, str]) -> bool:
    return (sent.get("to"), sent.get("subject"), sent.get("body")) == (args["to"], args["subject"], args["body"])


@pytest.mark.parametrize(
    ("decision", "error"),
    [("rejected", "email_send_rejected"), ("expired", "email_confirmation_expired"), ("unavailable", "email_confirmation_unavailable")],
)
def test_unconfirmed_email_is_not_delivered_and_says_so(configured, owner_decides, decision: str, error: str) -> None:
    owner_decides(decision)

    result = _run(_call(_args(), _ctx()))

    assert result["ok"] is False and result["error"] == error
    assert "邮件未发送" in result["message"] and "不要告诉用户邮件已发送" in result["message"]
    assert configured == {}


def test_non_interactive_channel_never_delivers(configured, owner_decides) -> None:
    confirmations = owner_decides("approved")

    result = _run(_call(_args(), _ctx(config_lite_supported=False)))

    assert result["error"] == "email_confirmation_unavailable"
    assert confirmations.requests == [] and configured == {}


@pytest.mark.parametrize("config_lite_supported", [True, False])
def test_custom_agent_run_sends_under_the_owner_grant_without_asking(configured, owner_decides, config_lite_supported: bool) -> None:
    confirmations = owner_decides("rejected")

    result = _run(_call(_args(), _ctx(role="analyst", config_lite_supported=config_lite_supported)))

    assert result["ok"] is True and result["sent"] == {"to": "recipient@example.com", "subject": "项目更新"}
    assert confirmations.requests == [] and sent_matches(configured, _args())


def test_custom_agent_run_still_checks_numbers_against_turn_data(configured, owner_decides) -> None:
    owner_decides("approved")

    result = _run(_call(_args(body="护肤美妆用品约 9.51 亿元。"), _ctx(role="analyst")))

    assert result["error"] == "email_numbers_unverified" and configured == {}


def test_lead_run_keeps_asking_the_requester(configured, owner_decides) -> None:
    confirmations = owner_decides("rejected")

    result = _run(_call(_args(), _ctx(role="lead")))

    assert result["error"] == "email_send_rejected" and len(confirmations.requests) == 1 and configured == {}


def test_production_miscopy_is_blocked_before_confirmation(configured, owner_decides) -> None:
    confirmations = owner_decides("approved")
    ctx = _ctx(artifact_store=_Artifacts({"品类销售.csv": _CATEGORY_CSV.encode()}))
    fetch = ToolInvocation(tool_name="bi_fetch_rows", arguments={}, result={"artifact_id": "art_0", "row_count": 8})

    result = _run(_call(_args(subject="本月品类销售概况", body=_WRONG_BODY), ctx, [_peer_with(fetch)]))

    assert result["ok"] is False and result["error"] == "email_numbers_unverified"
    assert result["unverified_numbers"] == ["9.51亿", "74.64%"]
    assert "9.51亿" in result["message"] and "邮件未发送" in result["message"]
    assert confirmations.requests == [] and configured == {}


def test_numbers_from_turn_data_and_computed_totals_pass(configured, owner_decides) -> None:
    confirmations = owner_decides("approved")
    ctx = _ctx(artifact_store=_Artifacts({"品类销售.csv": _CATEGORY_CSV.encode()}))
    fetch = ToolInvocation(tool_name="bi_fetch_rows", arguments={}, result={"artifact_id": "art_0"})
    total = ToolInvocation(tool_name="python_sandbox", arguments={}, result={"stdout": "头部四品类合计占比 74.62"})
    args = _args(subject="本月品类销售概况", body=_RIGHT_BODY)

    result = _run(_call(args, ctx, [_peer_with(fetch, total)]))

    assert result["ok"] is True and result["message"] == "邮件已发送。"
    assert confirmations.requests[0]["body"] == _RIGHT_BODY
    assert sent_matches(configured, args)


def test_model_authored_results_are_not_a_number_source(configured, owner_decides) -> None:
    confirmations = owner_decides("approved")
    # The same wrong number first appeared in the peer answer, canvas paragraph, and subagent_return, all authored by the model.
    peer = _peer_with(
        ToolInvocation(tool_name="subagent_return", arguments={}, result={"payload": {"exec_summary": "护肤美妆 9.51 亿元"}}),
        ToolInvocation(tool_name="canvas_add_insight", arguments={}, result={"markdown": "护肤美妆 9.51 亿元"}),
        answer="护肤美妆用品约 9.51 亿元",
    )

    result = _run(_call(_args(body="护肤美妆用品约 9.51 亿元。"), _ctx(), [peer]))

    assert result["error"] == "email_numbers_unverified" and result["unverified_numbers"] == ["9.51亿"]
    assert confirmations.requests == [] and configured == {}


def test_numbers_the_user_dictated_are_a_source(configured, owner_decides) -> None:
    owner_decides("approved")
    ctx = _ctx(user_text="发邮件给 recipient@example.com，告诉他下季度预算 120 万元，占比 12.5%")

    result = _run(_call(_args(body="下季度预算 120 万元，占比 12.5%。"), ctx))

    assert result["ok"] is True


def test_confirmation_queue_is_owner_scoped_and_single_use() -> None:
    async def scenario() -> tuple[str, list[dict[str, object]], bool, bool, bool]:
        queue = EmailSendConfirmations(timeout_seconds=5)
        waiting = asyncio.create_task(
            queue.request(owner_user_id="usr_owner", turn_id="t_1", session_id="s_1", to="a@example.com", subject="主题", body="正文")
        )
        while not queue.pending("usr_owner"):
            await asyncio.sleep(0)
        shown = queue.pending("usr_owner")
        foreign = queue.decide("usr_other", str(shown[0]["id"]), True)
        mine = queue.decide("usr_owner", str(shown[0]["id"]), False)
        again = queue.decide("usr_owner", str(shown[0]["id"]), True)
        return await waiting, shown, foreign, mine, again

    decision, shown, foreign, mine, again = _run(scenario())

    assert shown[0]["body"] == "正文" and shown[0]["to"] == "a@example.com"
    assert (foreign, mine, again) == (False, True, False)
    assert decision == "rejected"


def test_confirmation_expires_without_a_decision() -> None:
    queue = EmailSendConfirmations(timeout_seconds=0.05)

    decision = _run(queue.request(owner_user_id="usr_owner", turn_id="t_1", session_id="s_1", to="a@example.com", subject="s", body="b"))

    assert decision == "expired" and queue.pending("usr_owner") == []


def test_unsent_notice_follows_the_last_send_email_outcome() -> None:
    def inv(result: dict, to: str = "recipient@example.com") -> ToolInvocation:
        return ToolInvocation(tool_name=setool.TOOL_NAME, arguments={"to": to}, result=result)

    failed = {"ok": False, "error": "email_confirmation_expired"}
    sent = {"ok": True, "message": "邮件已发送。"}
    assert setool.unsent_email_notice([]) == ""
    assert setool.unsent_email_notice([inv(failed), inv(sent)]) == ""
    notice = setool.unsent_email_notice([inv(sent), inv(failed)])
    assert notice.startswith("**The email to recipient@example.com was not sent**")
    assert "not answered in time" in notice
    assert "邮件未发送" in setool.unsent_email_notice([inv(failed)], "zh-CN")
    # Unknown errors use fallback copy; invalid addresses are not echoed.
    unknown = setool.unsent_email_notice([inv({"ok": False, "error": "boom"}, to="bad\naddr")])
    assert unknown.startswith("**The email was not sent**") and "something went wrong" in unknown
    en = setool.unsent_email_notice([inv({"ok": False, "error": "email_numbers_unverified"})], "en-US")
    assert en.startswith("**The email to recipient@example.com was not sent**")
    assert "re-fetch the data" in en
    for lang, prefix in (("de-DE", "**Die E-Mail"), ("fr-FR", "**L’e-mail")):
        other = setool.unsent_email_notice([inv({"ok": False, "error": "email_numbers_unverified"})], lang)
        assert other.startswith(prefix)
        assert "recipient@example.com" in other and "{to}" not in other


@pytest.mark.parametrize(("lang", "phrase"), [("de-DE", "Berechnungstool"), ("fr-FR", "outil de calcul")])
def test_unverified_email_execution_localizes_guidance_without_changing_numbers(configured, owner_decides, lang: str, phrase: str) -> None:
    confirmations = owner_decides("approved")
    ctx = _ctx(language_context=LanguageContext(ui_language="en-US", output_language=lang))
    result = _run(_call(_args(subject="Budget", body="The rate is 99.33%."), ctx))
    assert result["error"] == "email_numbers_unverified"
    assert result["unverified_numbers"] == ["99.33%"]
    assert "99.33%" in result["message"] and phrase in result["message"]
    assert "ok=true" in result["message"] and "{to}" not in result["message"]
    assert confirmations.requests == [] and configured == {}


@pytest.mark.parametrize(("lang", "message"), [("de-DE", "Die E-Mail wurde gesendet."), ("fr-FR", "L’e-mail a été envoyé.")])
def test_success_receipt_uses_output_language_and_preserves_email_content(configured, owner_decides, lang: str, message: str) -> None:
    confirmations = owner_decides("approved")
    args = _args(subject="Update", body="Work is complete.")
    result = _run(_call(args, _ctx(language_context=LanguageContext(output_language=lang))))
    assert result == {"ok": True, "message": message, "sent": {"to": args["to"], "subject": "Update"}}
    assert confirmations.requests[0]["body"] == args["body"]
    assert sent_matches(configured, args)
