"""CLI 主入口。"""

from __future__ import annotations

import typer
from rich.console import Console

app = typer.Typer(
    name="bowhead",
    help="Bowhead 命令行工具",
    no_args_is_help=True,
)

console = Console()


# ─── migrate subcommands ───────────────────────────────────────────
migrate_app = typer.Typer(help="Alembic 数据库迁移")
app.add_typer(migrate_app, name="migrate")


@migrate_app.command("upgrade")
def migrate_upgrade(
    revision: str = typer.Argument("head"),
    db_url: str | None = typer.Option(None, "--db", help="默认取 BOWHEAD_DB_URL"),
    target: str | None = typer.Option(None, "--target", help="仅迁移一个 SQLite 目标"),
) -> None:
    """执行 1.0.0 基线及后续 Alembic revision。"""
    if revision != "head":
        raise typer.BadParameter("currently only 'head' is supported")
    from bowhead_cli.migrate import upgrade

    try:
        completed = upgrade(db_url, target)
    except ValueError as exc:
        console.print(f"[red]Migration failed:[/] {exc}")
        raise typer.Exit(1) from exc
    console.print(f"[green]Migrated:[/] {', '.join(completed)}")


# ─── admin subcommands ─────────────────────────────────────────────
admin_app = typer.Typer(help="管理员诊断工具")
app.add_typer(admin_app, name="admin")


artifacts_app = typer.Typer(help="产物存储运维")
app.add_typer(artifacts_app, name="artifacts")

kb_app = typer.Typer(help="知识库诊断")
app.add_typer(kb_app, name="kb")

llm_request_app = typer.Typer(help="Diagnostic record of actual model sends (operator only)")
app.add_typer(llm_request_app, name="llm-request")


@llm_request_app.command("list")
def llm_request_list(
    turn_id: str = typer.Option(..., "--turn", help="Turn id"),
    db_url: str | None = typer.Option(None, "--db", help="SQLite file or URL; defaults to BOWHEAD_DB_URL"),
) -> None:
    """List a Turn's recorded sends without decrypting them."""
    import json

    import anyio

    from bowhead_cli.llm_request import list_records
    from bowhead_cli.subagent_audit import resolve_sqlite_path

    records = anyio.run(list_records, resolve_sqlite_path(db_url), turn_id)
    typer.echo(json.dumps(records, ensure_ascii=False, indent=2))


@llm_request_app.command("show")
def llm_request_show(
    ref: str = typer.Argument(..., help="Record id (lrq_…) or billing usage event id"),
    operator: str = typer.Option(..., "--operator", help="Who is reading and why; written to the audit log"),
    db_url: str | None = typer.Option(None, "--db", help="SQLite file or URL; defaults to BOWHEAD_DB_URL"),
) -> None:
    """Decrypt one recorded send, verify its digest and print it with its change records. Audited."""
    import functools
    import json
    import os

    import anyio
    from bowhead_core.chat_reference_storage import ReferenceCipher

    from bowhead_cli.llm_request import show_record
    from bowhead_cli.subagent_audit import resolve_sqlite_path

    if not operator.strip():
        raise typer.BadParameter("--operator must name who is reading")
    cipher = ReferenceCipher(os.environ.get("BOWHEAD_FERNET_KEY", ""))
    result = anyio.run(functools.partial(show_record, resolve_sqlite_path(db_url), ref, operator=operator.strip(), cipher=cipher))
    if result is None:
        typer.echo(f"No request record matches {ref}", err=True)
        raise typer.Exit(1)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if result["verified"] is False:
        typer.echo("The rebuilt body does not match the recorded digest", err=True)
        raise typer.Exit(2)
    if result["verified"] is None:
        typer.echo(f"Reconstructed except {len(result['omitted_chunks'])} omitted chunk(s); digest not checked", err=True)


@kb_app.command("inventory")
def kb_inventory(
    db_url: str = typer.Option(..., "--db", help="显式指定主 SQLite 文件或连接串；同目录须有 kb_files.db"),
    team_id: str | None = typer.Option(None, "--team-id", help="仅盘点指定团队；省略为本实例所有团队"),
    after: str = typer.Option("", "--after", help="上一页 next_after；按文件 ID 继续实时盘点"),
    limit: int = typer.Option(200, "--limit", min=1, max=1000, help="本页最多检查的文件数"),
) -> None:
    """只读输出 JSON：原件探测、解析统计与未验证绑定；不创建数据库、不修改文件或索引。"""
    import json
    import os
    from pathlib import Path

    import anyio
    from bowhead_core.artifact_store import LocalBackend, build_artifact_backend_from_env

    from bowhead_cli.kb_inventory import inventory_page
    from bowhead_cli.subagent_audit import resolve_sqlite_path

    class ReadOnlyLocalBackend(LocalBackend):
        def __post_init__(self) -> None:
            # A missing mount must stay missing; normal LocalBackend creates its root.
            self.root_dir = Path(self.root_dir)

    path = resolve_sqlite_path(db_url)
    if not path.is_file() or not (path.parent / "kb_files.db").is_file():
        raise typer.BadParameter("Both the main database and kb_files.db must already exist")
    kind = os.environ.get("BOWHEAD_ARTIFACT_BACKEND", "local").strip().lower()
    if kind == "local":
        backend = ReadOnlyLocalBackend(root_dir=path.parent.parent / "artifacts")
    elif kind == "s3":
        if not all(os.environ.get(name) for name in ("BOWHEAD_S3_ENDPOINT", "BOWHEAD_S3_BUCKET", "BOWHEAD_S3_ACCESS_KEY", "BOWHEAD_S3_SECRET_KEY")):
            raise typer.BadParameter("S3 configuration is incomplete; inventory cannot fall back to local storage")
        backend = build_artifact_backend_from_env(path)
    else:
        raise typer.BadParameter("Unsupported artifact backend")

    async def run() -> dict:
        return await inventory_page(path, backend, team_id=team_id, after=after, limit=limit)

    try:
        payload = anyio.run(run)
    except Exception:  # noqa: BLE001 - backend errors can contain credentials/paths; emit no raw exception
        console.print("[red]Inventory failed. Check database compatibility and storage configuration.[/]")
        raise typer.Exit(1) from None
    console.print_json(json.dumps(payload, ensure_ascii=False, sort_keys=True))


@artifacts_app.command("reconcile")
def artifacts_reconcile(
    db_url: str | None = typer.Option(None, "--db", help="SQLite 文件或连接串；默认取 BOWHEAD_DB_URL"),
    apply: bool = typer.Option(False, "--apply", help="落标记；默认只预演"),
    force_ratio: bool = typer.Option(
        False,
        "--force-ratio",
        help="新判为丢失的比例超过闸值时仍然落标记（确认是历史存量而不是存储故障后再用）",
    ),
    restore_all: bool = typer.Option(False, "--restore-all", help="回退：把全部 missing 恢复为 ready"),
    json_output: bool = typer.Option(False, "--json", help="输出稳定 JSON"),
) -> None:
    """原文件丢失对账：把原文件已不在存储里的产物标为已失效。须在应用工作目录下运行（local 相对路径按其解析）。"""
    import asyncio
    import json

    from bowhead_core.artifact_reconcile import reconcile_missing_artifacts
    from bowhead_core.artifact_store import ArtifactStore, build_artifact_backend_from_env

    from bowhead_cli.subagent_audit import resolve_sqlite_path

    db_path = resolve_sqlite_path(db_url)
    store = ArtifactStore(db_path=str(db_path), backend=build_artifact_backend_from_env(db_path))

    async def _restore_all() -> int:
        missing: list[str] = []
        after = ""
        while rows := await store.list_reconcile_candidates(after_id=after, limit=1000):
            missing += [aid for aid, _, status in rows if status == store.MISSING_STATUS]
            after = rows[-1][0]
        return await store.set_blob_missing(missing, missing=False)

    if restore_all:
        restored = asyncio.run(_restore_all())
        console.print(f"[green]restored[/] {restored}")
        return
    result = asyncio.run(reconcile_missing_artifacts(store, apply=apply, force_ratio=force_ratio))
    payload = {**result.summary(), "sample_absent": result.sample_absent}
    if json_output:
        console.print_json(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    console.print(payload)
    if result.skipped_reason == "ratio_gate":
        console.print("[yellow]比例闸触发，未标记。确认是历史存量而非存储故障后加 --force-ratio 重跑。[/]")
    elif result.skipped_reason == "storage_unhealthy":
        console.print("[red]最近的产物也读不到，存储可能未挂载或配置错误；未做任何改动。[/]")


@admin_app.command("backfill-agent-assignments")
def admin_backfill_agent_assignments(
    db_url: str | None = typer.Option(
        None,
        "--db",
        help="SQLite 文件或连接串；默认取 BOWHEAD_DB_URL",
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="实际写入并在对账成功后开启 assignment cutover；默认只预演",
    ),
    source_build: str | None = typer.Option(
        None,
        "--source-build",
        help="可选的发行构建标识，只用于迁移运行记录",
    ),
    batch_size: int = typer.Option(
        100,
        "--batch-size",
        min=1,
        max=1000,
        help="每个事务处理的成员数；失败后从持久游标恢复",
    ),
    json_output: bool = typer.Option(False, "--json", help="输出稳定 JSON"),
) -> None:
    """回填升级前可见 Agent；cutover 前可恢复，默认 dry-run。"""
    import asyncio
    import json
    import sqlite3

    from bowhead_core.agent_assignment_store import AgentAssignmentStore

    from bowhead_cli.subagent_audit import resolve_sqlite_path

    try:
        result = asyncio.run(
            AgentAssignmentStore(resolve_sqlite_path(db_url)).backfill_existing_access(
                apply=apply,
                source_build=source_build,
                batch_size=batch_size,
            ),
        )
    except (FileNotFoundError, RuntimeError, sqlite3.Error, ValueError) as exc:
        console.print(f"[red]Agent assignment backfill failed:[/] {exc}")
        raise typer.Exit(1) from exc

    payload = {
        "dry_run": not apply,
        "expected": result.expected,
        "inserted": result.inserted,
        "conflict": result.conflict,
        "digest": result.digest,
        "cutover_completed_at": result.cutover_completed_at,
    }
    if json_output:
        console.print_json(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    state = "cutover complete" if result.cutover_completed_at else "dry-run only"
    console.print(
        f"[green]Agent assignment backfill:[/] {state}; expected={result.expected} inserted={result.inserted} conflict={result.conflict}",
    )


@admin_app.command("scan-agent-listings")
def admin_scan_agent_listings(
    db_url: str | None = typer.Option(
        None,
        "--db",
        help="SQLite 文件或连接串；默认取 BOWHEAD_DB_URL",
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="持久化 exact-hash 报告；默认只预演且不写数据库",
    ),
    batch_size: int = typer.Option(
        100,
        "--batch-size",
        min=1,
        max=1000,
        help="本批最多扫描的 Agent 数量",
    ),
    cursor: str | None = typer.Option(
        None,
        "--cursor",
        help="上一批返回的不透明 next_cursor",
    ),
    json_output: bool = typer.Option(False, "--json", help="输出稳定 JSON"),
) -> None:
    """分批扫描存量 Agent listing；幂等、可从 next_cursor 恢复。"""
    import asyncio
    import json
    import sqlite3

    from bowhead_core.agent_listing_safety import AgentListingSafetyStore

    from bowhead_cli.subagent_audit import resolve_sqlite_path

    try:
        db_path = resolve_sqlite_path(db_url)
        if not db_path.is_file():
            raise FileNotFoundError(f"database not found: {db_path}")
        result = asyncio.run(
            AgentListingSafetyStore(db_path).scan_inventory(
                apply=apply,
                batch_size=batch_size,
                cursor=cursor,
            ),
        )
    except (FileNotFoundError, RuntimeError, sqlite3.Error, ValueError) as exc:
        console.print(f"[red]Agent listing scan failed:[/] {exc}")
        raise typer.Exit(1) from exc

    payload = {
        "dry_run": not apply,
        "scanned": result.scanned,
        "blocked": result.blocked,
        "persisted_rows": result.persisted,
        "complete": result.complete,
        "next_cursor": result.next_cursor,
    }
    if json_output:
        console.print_json(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    state = "complete" if result.complete else "more batches required"
    console.print(
        f"[green]Agent listing scan:[/] {state}; scanned={result.scanned} blocked={result.blocked} persisted_rows={result.persisted}",
    )
    if result.next_cursor:
        console.print(f"next_cursor={result.next_cursor}")


@admin_app.command("import-agent-release-baselines")
def admin_import_agent_release_baselines(
    db_url: str | None = typer.Option(
        None,
        "--db",
        help="SQLite 文件或连接串；默认取 BOWHEAD_DB_URL",
    ),
    stage: bool = typer.Option(False, "--stage", help="冻结并持久化 shadow inventory；不切换 live pointer"),
    apply: bool = typer.Option(False, "--apply", help="按已确认 digest 导入 immutable baseline release"),
    abandon: bool = typer.Option(False, "--abandon", help="废弃尚未开始导入的 staging/staged run"),
    inspect: bool = typer.Option(False, "--inspect", help="列出 staged run 的安全 reason code，不返回 Agent 内容"),
    run_id: str | None = typer.Option(None, "--run-id", help="恢复、导入或废弃的 baseline run ID"),
    expected_digest: str | None = typer.Option(None, "--expected-digest", help="stage 输出的完整 sha256 digest"),
    confirm_writer_stopped: bool = typer.Option(
        False,
        "--confirm-writer-stopped",
        help="确认所有 legacy Agent 写入者已停止；仅 --apply 接受",
    ),
    source_build: str | None = typer.Option(None, "--source-build", help="可选的发行构建标识"),
    batch_size: int = typer.Option(100, "--batch-size", min=1, max=1000, help="每个事务最多处理的 Agent 数量"),
    limit: int = typer.Option(100, "--limit", min=1, max=1000, help="--inspect 最多返回的记录数"),
    json_output: bool = typer.Option(False, "--json", help="输出稳定 JSON"),
) -> None:
    """预演、分批暂存或显式导入 legacy Agent release baseline。"""
    import asyncio
    import json
    import sqlite3

    from bowhead_core.agent_release_baseline import AgentReleaseBaselineStore

    from bowhead_cli.subagent_audit import resolve_sqlite_path

    operations = sum((stage, apply, abandon, inspect))
    if operations > 1:
        raise typer.BadParameter("--stage, --apply, --abandon and --inspect are mutually exclusive")
    if (apply or abandon or inspect) and not run_id:
        raise typer.BadParameter("--run-id is required for --apply, --abandon and --inspect")
    if apply and not expected_digest:
        raise typer.BadParameter("--expected-digest is required for --apply")
    if confirm_writer_stopped and not apply:
        raise typer.BadParameter("--confirm-writer-stopped is only valid with --apply")

    try:
        store = AgentReleaseBaselineStore(resolve_sqlite_path(db_url))
        if inspect:
            issues = asyncio.run(store.list_issues(run_id=str(run_id), limit=limit))
            payload = {
                "run_id": run_id,
                "issues": [
                    {
                        "team_id": item.team_id,
                        "agent_id": item.agent_id,
                        "state": item.state,
                        "reason_codes": list(item.reason_codes),
                        "imported": item.imported,
                    }
                    for item in issues
                ],
            }
            if json_output:
                console.print_json(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            else:
                console.print(f"[green]Agent release baseline issues:[/] run_id={run_id} count={len(issues)}")
                for item in issues:
                    console.print(
                        f"{item.team_id}/{item.agent_id} state={item.state} reasons={','.join(item.reason_codes)}",
                    )
            return
        if apply:
            result = asyncio.run(
                store.apply(
                    run_id=str(run_id),
                    expected_digest=str(expected_digest),
                    writer_stopped=confirm_writer_stopped,
                    batch_size=batch_size,
                ),
            )
        elif abandon:
            result = asyncio.run(store.abandon(run_id=str(run_id)))
        elif stage:
            result = asyncio.run(
                store.stage(
                    run_id=run_id,
                    source_build=source_build,
                    batch_size=batch_size,
                ),
            )
        else:
            result = asyncio.run(store.preview())
    except (FileNotFoundError, RuntimeError, sqlite3.Error, ValueError) as exc:
        console.print(f"[red]Agent release baseline failed:[/] {exc}")
        raise typer.Exit(1) from exc

    payload = {
        "run_id": result.run_id,
        "state": result.state,
        "expected": result.expected,
        "staged": result.staged,
        "ready": result.ready,
        "degraded": result.degraded,
        "blocked": result.blocked,
        "imported": result.imported,
        "digest": result.digest,
    }
    if json_output:
        console.print_json(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    console.print(
        "[green]Agent release baseline:[/] "
        f"state={result.state} expected={result.expected} staged={result.staged} "
        f"degraded={result.degraded} blocked={result.blocked} imported={result.imported}",
    )
    if result.run_id:
        console.print(f"run_id={result.run_id}")
    console.print(f"digest={result.digest}")


@admin_app.command("ensure-agent-release-baseline")
def admin_ensure_agent_release_baseline(
    db_url: str | None = typer.Option(None, "--db", help="SQLite 文件或连接串；默认取 BOWHEAD_DB_URL"),
    source_build: str | None = typer.Option(None, "--source-build", help="可选的发行构建标识"),
    batch_size: int = typer.Option(100, "--batch-size", min=1, max=1000, help="每个事务处理的 Agent 数量"),
    json_output: bool = typer.Option(False, "--json", help="输出稳定 JSON"),
) -> None:
    """幂等完成 Agent release baseline cutover（启动期自动调用）。

    未迁移是受支持的可用状态，所以「跳过」不是错误：只有真正的故障才非零退出。
    否则一台有历史数据问题的机器会因为这一步而起不来。
    """
    import asyncio
    import json
    import sqlite3

    from bowhead_core.agent_release_baseline import AgentReleaseBaselineStore

    from bowhead_cli.subagent_audit import resolve_sqlite_path

    try:
        store = AgentReleaseBaselineStore(resolve_sqlite_path(db_url))
        result = asyncio.run(
            store.ensure_release_baseline(source_build=source_build, batch_size=batch_size),
        )
    except (FileNotFoundError, RuntimeError, sqlite3.Error, ValueError) as exc:
        console.print(f"[red]Agent release baseline failed:[/] {exc}")
        raise typer.Exit(1) from exc

    payload = {
        "state": result.state,
        "expected": result.expected,
        "imported": result.imported,
        "degraded": result.degraded,
        "blocked": result.blocked,
        "run_id": result.run_id,
        "detail": result.digest,
    }
    if json_output:
        console.print_json(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    elif result.state == "skipped":
        console.print(f"[yellow]Agent release baseline skipped:[/] {result.digest}")
    else:
        console.print(f"[green]Agent release baseline {result.state}:[/] imported={result.imported}")


@admin_app.command("ensure-agent-marketplace-cutover")
def admin_ensure_agent_marketplace_cutover(
    db_url: str | None = typer.Option(
        None,
        "--db",
        help="SQLite 文件或连接串；默认取 BOWHEAD_DB_URL",
    ),
    source_build: str | None = typer.Option(
        None,
        "--source-build",
        help="可选的发行构建标识",
    ),
    batch_size: int = typer.Option(
        100,
        "--batch-size",
        min=1,
        max=1000,
        help="每个事务处理的 assignment 数量",
    ),
    verify_only: bool = typer.Option(
        False,
        "--verify-only",
        help="只验证 schema、兼容元数据和 cutover marker，不执行回填",
    ),
    json_output: bool = typer.Option(False, "--json", help="输出稳定 JSON"),
) -> None:
    """幂等完成或验证 Agent Marketplace 一次性 cutover。"""
    import asyncio
    import json
    import sqlite3

    from bowhead_core.agent_assignment_store import AgentAssignmentStore

    from bowhead_cli.subagent_audit import resolve_sqlite_path

    try:
        store = AgentAssignmentStore(resolve_sqlite_path(db_url))
        if verify_only:
            result = asyncio.run(store.verify_marketplace_cutover())
        else:
            result = asyncio.run(
                store.ensure_marketplace_cutover(
                    source_build=source_build,
                    batch_size=batch_size,
                ),
            )
    except (FileNotFoundError, RuntimeError, sqlite3.Error, ValueError) as exc:
        console.print(f"[red]Agent Marketplace cutover failed:[/] {exc}")
        raise typer.Exit(1) from exc

    payload = {
        "ready": True,
        "expected": result.expected,
        "inserted": result.inserted,
        "conflict": result.conflict,
        "digest": result.digest,
        "cutover_completed_at": result.cutover_completed_at,
        "verify_only": verify_only,
    }
    if json_output:
        console.print_json(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        console.print(
            f"[green]Agent Marketplace cutover ready:[/] expected={result.expected} inserted={result.inserted}",
        )


@admin_app.command("audit-team-model-boundary")
def admin_audit_team_model_boundary(
    db_url: str | None = typer.Option(
        None,
        "--db",
        help="SQLite 文件或连接串；默认取 BOWHEAD_DB_URL",
    ),
    team_id: str | None = typer.Option(
        None,
        "--team-id",
        help="仅审计指定 active team；默认审计全部 active team",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="输出稳定、脱敏的 JSON 结果",
    ),
) -> None:
    """只读审计团队 catalog / Router 可解析性，不调用 Provider。"""
    import json
    import sqlite3

    from bowhead_cli.subagent_audit import resolve_sqlite_path
    from bowhead_cli.team_model_boundary_audit import audit_team_model_boundary

    try:
        report = audit_team_model_boundary(
            resolve_sqlite_path(db_url),
            team_id=team_id,
        )
    except (FileNotFoundError, sqlite3.Error, ValueError) as exc:
        console.print(f"[red]Team model boundary audit failed:[/] {exc}")
        raise typer.Exit(1) from exc

    payload = report.to_dict()
    if json_output:
        console.print_json(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return

    from rich.table import Table

    console.print(
        f"[bold]Team model boundary audit[/] teams={report.teams_checked} generated_at={report.generated_at}",
    )
    table = Table(show_header=True)
    for column in (
        "team",
        "version",
        "result",
        "reason",
        "catalog",
        "router",
        "providers",
        "models",
    ):
        table.add_column(column)
    for row in report.teams:
        table.add_row(
            row.team_id,
            str(row.config_version),
            row.result,
            row.reason,
            "ok" if row.catalog_resolvable else "unavailable",
            "ok" if row.router_resolvable else "unavailable",
            str(row.provider_count),
            str(row.model_count),
        )
    console.print(table)


@admin_app.command("audit-subagents")
def admin_audit_subagents(
    team_id: str = typer.Option(..., "--team-id", help="仅审计指定 team"),
    db_url: str | None = typer.Option(
        None,
        "--db",
        help="SQLite 文件或连接串；默认取 BOWHEAD_DB_URL",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="输出稳定、脱敏的 JSON 聚合结果",
    ),
) -> None:
    """只读审计 Subagent 迁移影响，不输出定义内容、名称或授权对象。"""
    import json
    import sqlite3

    from bowhead_cli.subagent_audit import audit_subagents, resolve_sqlite_path

    try:
        report = audit_subagents(resolve_sqlite_path(db_url), team_id=team_id)
    except (FileNotFoundError, sqlite3.Error, ValueError) as exc:
        console.print(f"[red]Subagent audit failed:[/] {exc}")
        raise typer.Exit(1) from exc

    payload = report.to_dict()
    if json_output:
        console.print_json(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return

    from rich.table import Table

    console.print(
        f"[bold]Subagent migration audit[/] team={report.team_id} schema=v{report.schema_version} generated_at={report.generated_at}",
    )
    totals = Table(title="Safe aggregates", show_header=True)
    totals.add_column("category")
    totals.add_column("value", justify="right")
    totals.add_row("subagents", str(report.totals["subagents"]))
    for category in ("status", "audience", "owner_state"):
        for label, value in report.totals[category].items():
            totals.add_row(f"{category}.{label}", str(value))
    console.print(totals)

    findings = Table(title="Migration findings", show_header=True)
    findings.add_column("finding")
    findings.add_column("affected", justify="right")
    for label, value in report.findings.items():
        findings.add_row(label, "not_available" if value is None else str(value))
    console.print(findings)

    unavailable = [name for name, available in report.schema_capabilities.items() if not available]
    if unavailable:
        console.print(
            f"[yellow]Schema capability unavailable:[/] {', '.join(unavailable)}; related metrics use not_available.",
        )


@admin_app.command("report-subagent-migration-impact")
def admin_report_subagent_migration_impact(
    team_id: str = typer.Option(..., "--team-id", help="仅预演指定 team"),
    db_url: str | None = typer.Option(
        None,
        "--db",
        help="SQLite 文件或连接串；默认取 BOWHEAD_DB_URL",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="输出稳定、脱敏的 JSON 聚合结果",
    ),
) -> None:
    """只读预演 Release B 修复；不修改 Agent、grant 或 validation。"""
    import json
    import sqlite3

    from bowhead_cli.subagent_audit import (
        report_subagent_migration_impact,
        resolve_sqlite_path,
    )

    try:
        report = report_subagent_migration_impact(
            resolve_sqlite_path(db_url),
            team_id=team_id,
        )
    except (FileNotFoundError, sqlite3.Error, ValueError) as exc:
        console.print(f"[red]Subagent migration impact failed:[/] {exc}")
        raise typer.Exit(1) from exc

    payload = report.to_dict()
    if json_output:
        console.print_json(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return

    from rich.table import Table

    console.print(
        "[bold]Subagent Release B migration impact[/] "
        f"team={report.team_id} schema=v{report.schema_version} "
        f"generated_at={report.generated_at} dry_run={report.dry_run}",
    )
    summary = Table(title="Automatic impact", show_header=True)
    summary.add_column("action")
    summary.add_column("affected", justify="right")
    summary.add_row("subagents", str(report.totals["subagents"]))
    summary.add_row(
        "access_or_lifecycle_change_subagents",
        _display_optional_count(report.totals["access_or_lifecycle_change_subagents"]),
    )
    for label, value in report.actions["owner"].items():
        summary.add_row(f"owner.{label}", _display_optional_count(value))
    for label, value in report.actions["audience"].items():
        if label == "dispositions":
            continue
        summary.add_row(f"audience.{label}", _display_optional_count(value))
    console.print(summary)

    dispositions = Table(title="Mutually exclusive audience disposition")
    dispositions.add_column("disposition")
    dispositions.add_column("subagents", justify="right")
    for label, value in report.actions["audience"]["dispositions"].items():
        dispositions.add_row(label, _display_optional_count(value))
    console.print(dispositions)

    follow_up = Table(title="Follow-up")
    follow_up.add_column("finding")
    follow_up.add_column("affected", justify="right")
    for label, value in report.follow_up.items():
        follow_up.add_row(label, _display_optional_count(value))
    console.print(follow_up)

    unavailable = report.completeness["unavailable_sections"]
    if unavailable:
        console.print(
            f"[yellow]Report section unavailable:[/] {', '.join(unavailable)}; related values use not_available.",
        )


@admin_app.command("check-subagent-deployment-admission")
def admin_check_subagent_deployment_admission(
    db_url: str | None = typer.Option(
        None,
        "--db",
        help="SQLite 文件或连接串；默认取 BOWHEAD_DB_URL",
    ),
    reader_version: str | None = typer.Option(
        None,
        "--reader-version",
        help="目标镜像 reader compatibility version；默认使用当前构建值",
    ),
    writer_version: str | None = typer.Option(
        None,
        "--writer-version",
        help="目标镜像 writer compatibility version；默认使用当前构建值",
    ),
    live_window_seconds: int = typer.Option(
        90,
        "--live-window-seconds",
        help="live heartbeat 窗口；生产合同默认 90 秒",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="输出稳定、脱敏的 JSON admission 结果",
    ),
) -> None:
    """只读检查目标镜像和已注册 live 实例的 Subagent 兼容性。"""
    import asyncio
    import json
    from dataclasses import asdict

    import aiosqlite
    from bowhead_core.agent_assignment_store import (
        AgentAssignmentStore,
        AgentAssignmentUnavailableError,
    )
    from bowhead_core.subagent_compatibility import (
        TeamSubagentCompatibilityDatabaseError,
        TeamSubagentCompatibilityMetadataError,
        TeamSubagentCompatibilityStore,
    )
    from bowhead_core.team_subagent_store import (
        AGENT_RELEASE_COMPATIBILITY_VERSION,
    )

    from bowhead_cli.subagent_audit import resolve_sqlite_path

    try:
        # This image must declare the same capability value that it uses to register live instances **at runtime**
        # (`bowhead_gateway.main` registers them with AGENT_RELEASE_COMPATIBILITY_VERSION).
        # This used to remain at RELEASE_A_COMPATIBILITY_VERSION="0001" while 0002/0003 were later added
        # to team_subagent_store without updating it: the same image declared 0003 at runtime
        # and 0001 during admission checks. Once `ensure-agent-release-baseline` ran, it ratcheted the
        # database's minimum_reader/writer to 0003 (only upward), after which **no** image could pass
        # this fail-closed gate, blocking both upgrades and rollbacks (observed on 116: even the running version
        # was rejected). The constant follows the code; genuinely older images still declare their own older value.
        candidate_reader_version = AGENT_RELEASE_COMPATIBILITY_VERSION if reader_version is None else reader_version
        candidate_writer_version = AGENT_RELEASE_COMPATIBILITY_VERSION if writer_version is None else writer_version
        database_path = resolve_sqlite_path(db_url)
        report = asyncio.run(
            TeamSubagentCompatibilityStore(
                database_path,
            ).deployment_admission(
                reader_version=candidate_reader_version,
                writer_version=candidate_writer_version,
                live_window_seconds=live_window_seconds,
            ),
        )
        marketplace_report = asyncio.run(
            AgentAssignmentStore(database_path).deployment_admission(),
        )
    except TeamSubagentCompatibilityDatabaseError as exc:
        # Distinct from a schema verdict: nothing was learned about the database,
        # so the message must not read like a migration is outstanding.
        console.print(
            f"[red]Subagent deployment admission could not read the database:[/] {exc}",
        )
        console.print(
            "[yellow]提示：[/]WAL 数据库需要在库文件旁创建 -shm；若数据目录以只读方式挂载，"
            "即使 mode=ro 也无法打开。请以可写方式挂载数据目录，只读语义由 mode=ro 与 "
            "PRAGMA query_only=ON 保证。",
        )
        raise typer.Exit(1) from exc
    except (
        FileNotFoundError,
        AgentAssignmentUnavailableError,
        TeamSubagentCompatibilityMetadataError,
        aiosqlite.Error,
        ValueError,
    ) as exc:
        console.print(f"[red]Subagent deployment admission failed:[/] {exc}")
        raise typer.Exit(1) from exc

    payload = report.to_dict()
    marketplace_payload = asdict(marketplace_report)
    payload.update(
        {f"agent_marketplace_{key}": value for key, value in marketplace_payload.items()},
    )
    payload["ready"] = bool(report.ready and marketplace_report.ready)
    if json_output:
        console.print_json(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )
    else:
        from rich.table import Table

        console.print(
            f"[bold]Subagent deployment admission[/] checked_at={report.checked_at} ready={payload['ready']}",
        )
        summary = Table(title="Compatibility", show_header=True)
        summary.add_column("dimension")
        summary.add_column("value")
        for key in (
            "candidate_reader_version",
            "candidate_writer_version",
            "migration_pending",
            "minimum_reader_version",
            "minimum_writer_version",
            "candidate_compatible",
            "live_instances",
            "compatible_live_instances",
            "incompatible_live_instances",
            "malformed_instances",
            "stale_instances",
            "scan_truncated",
            "agent_marketplace_candidate_reader_version",
            "agent_marketplace_candidate_writer_version",
            "agent_marketplace_migration_pending",
            "agent_marketplace_minimum_reader_version",
            "agent_marketplace_minimum_writer_version",
            "agent_marketplace_candidate_compatible",
        ):
            summary.add_row(key, str(payload[key]))
        console.print(summary)
        if report.migration_pending:
            console.print(
                "[yellow]Subagent compatibility metadata will be created by Alembic before Gateway starts; this read-only preflight is deferred.[/]",
            )

    if not payload["ready"]:
        raise typer.Exit(2)


def _display_optional_count(value: object) -> str:
    return "not_available" if value is None else str(value)


@admin_app.command("benchmark-subagent-catalog")
def admin_benchmark_subagent_catalog(
    agent_count: int = typer.Option(
        5000,
        "--agents",
        help="合成 Subagent 数量：1、50、500 或 5000",
    ),
    member_count: int = typer.Option(
        1000,
        "--members",
        help="合成团队成员数：1、100 或 1000",
    ),
    iterations: int = typer.Option(
        20,
        "--iterations",
        help="每个查询的新连接与暖连接采样次数（5–200）",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="输出机器可读 JSON",
    ),
) -> None:
    """在隔离临时库上运行 Subagent 目录读路径容量基准。"""
    import json
    import sqlite3

    from bowhead_cli.subagent_benchmark import run_subagent_catalog_benchmark

    try:
        report = run_subagent_catalog_benchmark(
            agent_count=agent_count,
            member_count=member_count,
            iterations=iterations,
        )
    except (sqlite3.Error, ValueError) as exc:
        console.print(f"[red]Subagent catalog benchmark failed:[/] {exc}")
        raise typer.Exit(1) from exc

    payload = report.to_dict()
    if json_output:
        console.print_json(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        from rich.table import Table

        console.print(
            f"[bold]Subagent catalog benchmark[/] scope={report.scope} generated_at={report.generated_at}",
        )
        console.print(
            "[yellow]Synthetic, isolated result only; it does not establish a production SLO.[/]",
        )

        dataset = Table(title="Dataset", show_header=True)
        dataset.add_column("dimension")
        dataset.add_column("value", justify="right")
        dataset.add_row("agents", str(report.dataset["agents"]))
        dataset.add_row("members", str(report.dataset["members"]))
        dataset.add_row("groups", str(report.dataset["groups"]))
        dataset.add_row("grants", str(report.dataset["grants"]))
        dataset.add_row(
            "audience",
            ", ".join(f"{name}={count}" for name, count in report.dataset["audience"].items()),
        )
        dataset.add_row(
            "custom roster counts",
            ", ".join(f"{size}={count}" for size, count in report.dataset["custom_rosters"]["actual_counts"].items()) or "none",
        )
        console.print(dataset)

        workloads = Table(title="Catalog reads", show_header=True)
        workloads.add_column("workload")
        workloads.add_column("rows", justify="right")
        workloads.add_column("new p50/p95 ms", justify="right")
        workloads.add_column("warm p50/p95 ms", justify="right")
        workloads.add_column("plan")
        for result in report.workloads:
            new_connection = result["new_connection_ms"]
            warm_connection = result["warm_connection_ms"]
            workloads.add_row(
                result["name"],
                str(result["row_count"]),
                f"{new_connection['p50']:.4f}/{new_connection['p95']:.4f}",
                f"{warm_connection['p50']:.4f}/{warm_connection['p95']:.4f}",
                "indexed" if result["plan_ok"] else ", ".join(result["plan_issues"]),
            )
        console.print(workloads)

        checks = Table(title="Contract checks", show_header=True)
        checks.add_column("check")
        checks.add_column("result")
        for name, passed in report.checks.items():
            checks.add_row(name, "[green]pass[/]" if passed else "[yellow]false[/]")
        console.print(checks)
        console.print(
            f"SQLite {report.environment['sqlite']} · "
            f"{report.environment['platform']} {report.environment['architecture']} · "
            f"{report.environment['cache_note']}",
        )

    required_checks = (
        "all_query_plans_indexed",
        "first_two_pages_unique",
        "page_50_requirement_met",
        "fixture_counts_match",
    )
    if not all(report.checks[name] for name in required_checks):
        raise typer.Exit(2)


@admin_app.command("spike-subagent-access")
def admin_spike_subagent_access(
    json_output: bool = typer.Option(
        False,
        "--json",
        help="输出机器可读 JSON",
    ),
) -> None:
    """在隔离临时库上验证规划中的 Subagent canonical ACL 语义。"""
    import json
    import sqlite3

    from bowhead_cli.subagent_access_spike import run_subagent_access_spike

    try:
        report = run_subagent_access_spike()
    except sqlite3.Error as exc:
        console.print(f"[red]Subagent access spike failed:[/] {exc}")
        raise typer.Exit(1) from exc

    payload = report.to_dict()
    if json_output:
        console.print_json(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        from rich.table import Table

        console.print(f"[bold]Subagent access spike[/] scope={report.scope}")
        console.print(
            "[yellow]Synthetic Stage 0A evidence only; production ACL is not changed or activated.[/]",
        )

        scenarios = Table(title="Canonical resolution matrix", show_header=True)
        scenarios.add_column("scenario")
        scenarios.add_column("manage")
        scenarios.add_column("consume")
        scenarios.add_column("govern")
        scenarios.add_column("reason")
        scenarios.add_column("legacy drift")
        for scenario in report.scenarios:
            resolution = scenario["resolution"]
            if resolution is None:
                scenarios.add_row(
                    scenario["name"],
                    "-",
                    "-",
                    "-",
                    "not_found",
                    "no",
                )
                continue
            scenarios.add_row(
                scenario["name"],
                "yes" if resolution["can_manage"] else "no",
                "yes" if resolution["can_consume"] else "no",
                "yes" if resolution["can_govern"] else "no",
                resolution["access_reason"] or resolution["denial_reason"] or "none",
                "[yellow]yes[/]" if scenario["legacy_drift"] else "no",
            )
        console.print(scenarios)

        checks = Table(title="Spike checks", show_header=True)
        checks.add_column("check")
        checks.add_column("result")
        for name, passed in report.checks.items():
            checks.add_row(name, "[green]pass[/]" if passed else "[red]fail[/]")
        console.print(checks)
        console.print(
            f"SQLite {report.environment['sqlite']} · {len(report.query_plan)} plan steps · current is_visible shortcut measured but not modified",
        )

    if not all(report.checks.values()):
        raise typer.Exit(2)


@admin_app.command("spike-subagent-triggers")
def admin_spike_subagent_triggers(
    json_output: bool = typer.Option(
        False,
        "--json",
        help="输出机器可读 JSON",
    ),
) -> None:
    """在隔离临时库上验证 Subagent Release A/B 触发器与回滚门。"""
    import json
    import sqlite3

    from bowhead_cli.subagent_trigger_spike import run_subagent_trigger_spike

    try:
        report = run_subagent_trigger_spike()
    except (sqlite3.Error, ValueError) as exc:
        console.print(f"[red]Subagent trigger spike failed:[/] {exc}")
        raise typer.Exit(1) from exc

    payload = report.to_dict()
    if json_output:
        console.print_json(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        from rich.table import Table

        console.print(f"[bold]Subagent trigger spike[/] scope={report.scope}")
        console.print(
            "[yellow]Synthetic Stage 0A evidence only; no production schema or semantics are activated.[/]",
        )

        activation = Table(title="Release A/B activation", show_header=True)
        activation.add_column("check")
        activation.add_column("result")
        activation.add_row(
            "live old writer",
            ("blocked" if not report.activation["live_old_writer_attempt"]["accepted"] else "unexpectedly accepted"),
        )
        activation.add_row(
            "after old writer is stale",
            ("activated" if report.activation["drained_attempt"]["accepted"] else "blocked"),
        )
        activation.add_row(
            "metadata atomic",
            "yes" if report.activation["metadata_activated_atomically"] else "no",
        )
        console.print(activation)

        scenarios = Table(title="Raw SQL compatibility matrix", show_header=True)
        scenarios.add_column("phase")
        scenarios.add_column("scenario")
        scenarios.add_column("decision")
        scenarios.add_column("error")
        scenarios.add_column("state")
        for phase, phase_scenarios in (
            ("A", report.release_a_scenarios),
            ("B", report.release_b_scenarios),
        ):
            for scenario in phase_scenarios:
                scenarios.add_row(
                    phase,
                    scenario["name"],
                    "accepted" if scenario["accepted"] else "blocked",
                    scenario["error_code"] or "-",
                    "changed" if scenario["state_changed"] else "unchanged",
                )
        console.print(scenarios)

        checks = Table(title="Spike checks", show_header=True)
        checks.add_column("check")
        checks.add_column("result")
        for name, passed in report.checks.items():
            checks.add_row(name, "[green]pass[/]" if passed else "[red]fail[/]")
        console.print(checks)
        console.print(
            f"SQLite {report.environment['sqlite']} · rollback keeps minimum writer={report.rollback_fence['minimum_writer_version']} and active guards",
        )

    if not all(report.checks.values()):
        raise typer.Exit(2)


@admin_app.command("audit-subagent-write-ingress")
def admin_audit_subagent_write_ingress(
    json_output: bool = typer.Option(
        False,
        "--json",
        help="输出机器可读 JSON",
    ),
) -> None:
    """静态盘点 Subagent 定义、授权和状态写入口及 Release A 处置。"""
    import json

    from bowhead_cli.subagent_write_ingress_spike import (
        audit_subagent_write_ingress,
    )

    try:
        report = audit_subagent_write_ingress()
    except (OSError, SyntaxError, ValueError) as exc:
        console.print(f"[red]Subagent write-ingress audit failed:[/] {exc}")
        raise typer.Exit(1) from exc

    payload = report.to_dict()
    if json_output:
        console.print_json(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        from rich.table import Table

        console.print(f"[bold]Subagent write-ingress audit[/] scope={report.scope}")
        console.print(
            "[yellow]Read-only Stage 0A source inventory; production writers are not changed.[/]",
        )

        summary = Table(title="Inventory summary", show_header=True)
        summary.add_column("dimension")
        summary.add_column("value", justify="right")
        for key in (
            "source_files_scanned",
            "relevant_findings",
            "direct_sql_findings",
            "mutator_call_findings",
            "blocking_findings",
            "unclassified_findings",
        ):
            summary.add_row(key, str(report.summary[key]))
        summary.add_row(
            "release_a_ready",
            "yes" if report.release_a_readiness["ready"] else "no",
        )
        console.print(summary)

        findings = Table(title="Classified write ingress", show_header=True)
        findings.add_column("source")
        findings.add_column("symbol")
        findings.add_column("target")
        findings.add_column("Release A")
        findings.add_column("blocker")
        for finding in report.findings:
            findings.add_row(
                finding["path"],
                finding["symbol"],
                finding["target"],
                finding["release_a_disposition"],
                "yes" if finding["blocking"] else "no",
            )
        console.print(findings)

        checks = Table(title="Inventory checks", show_header=True)
        checks.add_column("check")
        checks.add_column("result")
        for name, passed in report.checks.items():
            checks.add_row(name, "[green]pass[/]" if passed else "[red]fail[/]")
        console.print(checks)
        console.print(
            f"fingerprint={report.summary['inventory_fingerprint']} · non-Subagent fixed-scope calls={report.non_subagent_mutator_calls}",
        )

    if not all(report.checks.values()):
        raise typer.Exit(2)


@admin_app.command("spike-subagent-dependencies")
def admin_spike_subagent_dependencies(
    json_output: bool = typer.Option(
        False,
        "--json",
        help="输出机器可读 JSON",
    ),
) -> None:
    """在隔离合成图上验证 Subagent 依赖精准失效与补漏。"""
    import json
    import sqlite3

    from bowhead_cli.subagent_dependency_spike import (
        run_subagent_dependency_spike,
    )

    try:
        report = run_subagent_dependency_spike()
    except (sqlite3.Error, ValueError) as exc:
        console.print(f"[red]Subagent dependency spike failed:[/] {exc}")
        raise typer.Exit(1) from exc

    payload = report.to_dict()
    if json_output:
        console.print_json(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        from rich.table import Table

        console.print(f"[bold]Subagent dependency spike[/] scope={report.scope}")
        console.print(
            "[yellow]Synthetic Stage 0A evidence only; production schema, catalogs, validation and runtime are unchanged.[/]",
        )

        dataset = Table(title="Synthetic dependency graph", show_header=True)
        dataset.add_column("dimension")
        dataset.add_column("value", justify="right")
        for key, value in report.dataset.items():
            dataset.add_row(key, str(value))
        console.print(dataset)

        targeted = Table(title="Invalidation scope", show_header=True)
        targeted.add_column("scenario")
        targeted.add_column("matched", justify="right")
        targeted.add_column("new stale", justify="right")
        targeted.add_column("queue", justify="right")
        targeted.add_column("deferred", justify="right")
        for label, result in (
            (
                "unreferenced tool change",
                report.targeted_invalidation["unreferenced_change"],
            ),
            (
                "referenced tool disabled",
                report.targeted_invalidation["referenced_disable"],
            ),
            (
                "native denylist change",
                report.global_rule_invalidation["native_deny"],
            ),
            ("missed hook repair", report.reconciler["repair"]),
            (
                "validator version",
                report.backpressure["global_invalidation"],
            ),
        ):
            targeted.add_row(
                label,
                str(result["matched_agents"]),
                str(result["newly_stale_agents"]),
                str(result["queue_depth"]),
                str(result["deferred_stale_agents"]),
            )
        console.print(targeted)

        checks = Table(title="Spike checks", show_header=True)
        checks.add_column("check")
        checks.add_column("result")
        for name, passed in report.checks.items():
            checks.add_row(
                name,
                "[green]pass[/]" if passed else "[red]fail[/]",
            )
        console.print(checks)
        console.print(
            f"SQLite {report.environment['sqlite']} · reverse lookup indexed · queue limit={report.dataset['queue_limit']}",
        )

    if not all(report.checks.values()):
        raise typer.Exit(2)


# ─── user/admin subcommands ────────────────────────────────────────
user_app = typer.Typer(help="用户 / 管理员")
app.add_typer(user_app, name="user")


@user_app.command("create")
def user_create(
    email: str = typer.Option(..., "--email", "-e", help="登录邮箱"),
    password: str = typer.Option(..., "--password", "-p", help="登录密码"),
    team: str | None = typer.Option(None, "--team", help="team 名（默认取邮箱域名）"),
    admin: bool = typer.Option(True, "--admin/--no-admin", help="设为 team 管理员（首个管理员用）"),
    tier: str = typer.Option("ultra", "--tier", help="用户 tier"),
) -> None:
    """建用户（默认新建 team 并设 team_admin）—— 首个管理员 bootstrap。

    prod 默认关自助注册，用本命令 seed 第一个管理员。读 BOWHEAD_DB_URL 定位
    SQLite；在 app 容器内跑：
        docker compose exec app bowhead user create -e you@co.com -p '****'
    """
    import asyncio
    import os
    from pathlib import Path

    from bowhead_core.user_store import UserStore

    default_db_path = Path(".data/sqlite/bowhead.db").resolve()
    db_url = os.environ.get("BOWHEAD_DB_URL", f"sqlite+aiosqlite:///{default_db_path}")
    db_path = db_url.split("///", 1)[-1] if db_url.startswith("sqlite") else str(default_db_path)

    async def _run() -> None:
        store = UserStore(db_path)
        await store.initialize()  # Idempotent: ensure the table exists.
        if await store.get_user_by_email(email) is not None:
            console.print(f"[red]✗ 邮箱已存在：{email}[/]")
            raise typer.Exit(1)
        created_team = await store.create_team(name=team or email.rsplit("@", maxsplit=1)[-1])
        user = await store.create_user(
            team_id=created_team.id,
            email=email,
            password=password,
            tier=tier,
            email_verified=True,
        )
        if admin:
            await store.promote_team_role(
                user_id=user.id,
                team_id=created_team.id,
                new_role="team_admin",
                promoted_by=user.id,
            )
        console.print(
            f"[green]✓ 用户已创建[/] email={email} user_id={user.id} team_id={created_team.id} role={'team_admin' if admin else 'member'} tier={tier}"
        )

    asyncio.run(_run())


# ─── workspace subcommands ────────────────────────────────────────
ws_app = typer.Typer(help="Workspace 管理")
app.add_typer(ws_app, name="space")  # Batch 3/c5: canonical.
app.add_typer(ws_app, name="workspace")  # Legacy alias (retain for one release).


@ws_app.command("bootstrap")
def ws_bootstrap(workspace_id: str) -> None:
    """新 workspace onboarding：创建 MEMORY.md + BOWHEAD.md 初版。"""
    console.print(f"[yellow]⚠ Sprint 0 stub — 会为 {workspace_id} 创建初始模板文件[/]")


# ─── trace subcommands ─────────────────────────────────────────────
trace_app = typer.Typer(help="Trace 调试")
app.add_typer(trace_app, name="trace")


@trace_app.command("dump")
def trace_dump(session_id: str) -> None:
    """导出指定 session 的完整 trace（含 agent_calls / llm_calls / tool_calls）。"""
    console.print(f"[yellow]⚠ Sprint 0 stub — 会 dump session {session_id} 的 trace[/]")


# ─── skill subcommands ─────────────────────────────────────────────
skill_app = typer.Typer(help="Skill 管理 — 与 Claude Code skills 兼容")
app.add_typer(skill_app, name="skill")


@skill_app.command("list")
def skill_list(
    scope: str = typer.Option(
        "all",
        "--scope",
        "-s",
        help="builtin | user | project | all",
    ),
) -> None:
    """列出可用 skills（name + description）."""
    from bowhead_skill import SkillCatalog, builtin_catalog_dir, project_catalog_dir, user_catalog_dir

    roots = []
    if scope in ("builtin", "all"):
        roots.append(("builtin", builtin_catalog_dir()))
    if scope in ("user", "all"):
        roots.append(("user", user_catalog_dir()))
    if scope in ("project", "all"):
        roots.append(("project", project_catalog_dir()))

    from rich.table import Table

    table = Table(title=f"Skills ({scope})")
    table.add_column("scope", style="cyan")
    table.add_column("name", style="bold")
    table.add_column("version")
    table.add_column("description")

    for label, root in roots:
        if not root.is_dir():
            continue
        cat = SkillCatalog(roots=[root])
        cat.refresh()
        for s in cat.list_specs():
            table.add_row(label, s.name, s.version or "-", s.description[:60])
    console.print(table)


@skill_app.command("install")
def skill_install(
    source: str = typer.Argument(..., help="Git URL / 本地路径 / anthropics/skills:<name>"),
    target_scope: str = typer.Option(
        "user",
        "--scope",
        "-s",
        help="user | project",
    ),
    name: str | None = typer.Option(
        None,
        "--name",
        help="覆盖目录名（default=source 末段）",
    ),
) -> None:
    """安装一个 skill 到 ~/.bowhead/skills/ 或 .bowhead/skills/.

    支持：
      bowhead skill install ./path/to/skill-dir              # 拷贝本地目录
      bowhead skill install https://github.com/org/skill.git # git clone
      bowhead skill install anthropics/skills:pdf            # 从官方 repo 取一个子目录
    """
    import shutil
    import subprocess
    from pathlib import Path

    from bowhead_skill import load_skill, project_catalog_dir, user_catalog_dir

    target_root = user_catalog_dir() if target_scope == "user" else project_catalog_dir()
    target_root.mkdir(parents=True, exist_ok=True)

    def _install_from_dir(src: Path, dst_name: str) -> Path:
        # Use spec.name as the target directory (symmetric with uninstall), unless --name overrides it.
        try:
            probe = load_skill(src)
            spec_name = probe.name
        except Exception:
            spec_name = src.name
        final_name = dst_name or spec_name
        dst = target_root / final_name
        if dst.exists():
            raise typer.BadParameter(f"{dst} already exists — remove first")
        shutil.copytree(src, dst)
        return dst

    src_path = Path(source)
    dst_path: Path | None = None

    # 1. Local directory.
    if src_path.is_dir():
        dst_path = _install_from_dir(src_path, name or "")

    # 2. anthropics/skills:<sub> — shortcut
    elif ":" in source and "github.com" not in source and source.startswith(("anthropics/", "anthropic/")):
        repo, sub = source.split(":", 1)
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "repo"
            url = f"https://github.com/{repo}.git"
            console.print(f"[cyan]Cloning {url}[/]")
            subprocess.run(
                ["git", "clone", "--depth", "1", url, str(tmp)],  # noqa: S607
                check=True,
            )
            candidate = tmp / "skills" / sub
            if not candidate.is_dir():
                raise typer.BadParameter(f"{candidate} not in cloned repo")
            dst_path = _install_from_dir(candidate, name or "")

    # 3. git URL
    elif source.startswith(("http://", "https://", "git@")):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "repo"
            console.print(f"[cyan]Cloning {source}[/]")
            subprocess.run(
                ["git", "clone", "--depth", "1", source, str(tmp)],  # noqa: S607
                check=True,
            )
            # If the repository root is itself a skill (contains SKILL.md), install it directly.
            if (tmp / "SKILL.md").is_file():
                dst_path = _install_from_dir(tmp, name or "")
            else:
                raise typer.BadParameter(
                    "git repo root lacks SKILL.md — use `source:subpath` shortcut or clone manually",
                )
    else:
        raise typer.BadParameter(f"unsupported source: {source}")

    assert dst_path is not None
    # Validate.
    try:
        spec = load_skill(dst_path)
    except Exception as e:
        shutil.rmtree(dst_path, ignore_errors=True)
        console.print(f"[red]Invalid SKILL.md at {dst_path}: {e}[/]")
        raise typer.Exit(1)
    console.print(f"[green]✓ installed[/] [bold]{spec.name}[/] v{spec.version or '-'} → {dst_path}")


# ─── dream subcommands ─────────────────────────────────────────────
dream_app = typer.Typer(help="Dreaming — 周期性汇总 + 报告（REM / Deep）")
app.add_typer(dream_app, name="dream")


@dream_app.command("digest")
def dream_digest(
    db_url: str = typer.Option(
        "sqlite+aiosqlite:///.data/sqlite/bowhead.db",
        "--db",
        help="SQLite 连接串；与 gateway.settings.db_url 保持一致",
    ),
    phase: str = typer.Option("all", "--phase", "-p", help="rem | deep | all"),
    window_days: int = typer.Option(14, "--rem-days"),
    window_hours: int = typer.Option(24, "--deep-hours"),
) -> None:
    """产出 REM + Deep 报告（只读数据库）."""
    import asyncio

    from bowhead_memory import DreamingEngine

    path = db_url.split("///", 1)[-1] if db_url.startswith("sqlite") else db_url

    async def run() -> None:
        eng = DreamingEngine(db_path=path)
        if phase in ("rem", "all"):
            rem = await eng.rem_digest(window_days=window_days)
            console.print(
                f"\n[bold cyan]REM[/] window={rem.window_days}d turns={rem.total_turns} patterns={len(rem.top_patterns)}",
            )
            from rich.table import Table

            t = Table(show_header=True)
            t.add_column("#")
            t.add_column("intent")
            t.add_column("tool_chain")
            t.add_column("sections")
            t.add_column("count", justify="right")
            for i, p in enumerate(rem.top_patterns, start=1):
                t.add_row(str(i), p["intent"], p["tool_chain"], p["sections"], str(p["count"]))
            console.print(t)

        if phase in ("deep", "all"):
            deep = await eng.deep_digest(window_hours=window_hours)
            console.print(
                f"\n[bold cyan]Deep[/] window={deep.window_hours}h turns={deep.total_turns} avg_dur={deep.avg_duration_ms or 0:.0f}ms",
            )
            if deep.tool_usage:
                from rich.table import Table

                t = Table(show_header=True, title="tool usage")
                t.add_column("tool")
                t.add_column("calls", justify="right")
                t.add_column("fail_rate", justify="right")
                for name, n in sorted(deep.tool_usage.items(), key=lambda x: -x[1]):
                    fr = deep.tool_failure_rate.get(name, 0.0)
                    t.add_row(name, str(n), f"{fr * 100:.1f}%")
                console.print(t)
            if deep.errors:
                console.print("\n[bold red]recent errors[/]:")
                for e in deep.errors:
                    console.print(f"  • {e}")

    asyncio.run(run())


@skill_app.command("uninstall")
def skill_uninstall(
    name: str,
    target_scope: str = typer.Option("user", "--scope", "-s"),
) -> None:
    """卸载 skill（从 user 或 project catalog 删目录）."""
    import shutil

    from bowhead_skill import project_catalog_dir, user_catalog_dir

    root = user_catalog_dir() if target_scope == "user" else project_catalog_dir()
    target = root / name
    if not target.is_dir():
        console.print(f"[yellow]not found: {target}[/]")
        raise typer.Exit(1)
    shutil.rmtree(target)
    console.print(f"[green]✓ removed[/] {target}")


# ─── credit subcommands ────────────────────────────────────────────
# credit-billing gap 15: previously, **only the admin HTTP API** could read/change credit quotas. During license incidents
# (typically: tier_credits omitted during issuance → quota=0 for everyone → 402 on every turn, while the admin configuration
# endpoint always returns 409 managed_by_license in local deployments), manually editing the database with SQL was the only recovery option. These commands
# connect directly to SQLite inside the container for recovery and report background usage or reconcile the two ledger layers with one command.
credit_app = typer.Typer(help="Credit 额度 —— 查询 / 用量 / 对账（运维逃生门）")
app.add_typer(credit_app, name="credit")

_DB_OPT = typer.Option(None, "--db", help="SQLite 连接串；默认取 BOWHEAD_DB_URL")


def _credit_db_path(db_url: str | None) -> str:
    """与 gateway 同一套 DB 定位规则（BOWHEAD_DB_URL 优先，回落 .data/sqlite）。"""
    import os
    from pathlib import Path as _Path

    default_db_path = _Path(".data/sqlite/bowhead.db").resolve()
    resolved = db_url or os.environ.get("BOWHEAD_DB_URL") or f"sqlite+aiosqlite:///{default_db_path}"
    return resolved.split("///", 1)[-1] if resolved.startswith("sqlite") else str(default_db_path)


@credit_app.command("show")
def credit_show(db_url: str | None = _DB_OPT) -> None:
    """只读：team 池 / 各用户钱包 / tier_credits / license 到期，一屏看全。

    诊断顺序提示（credit 402 曾被前端 generic 文案说成「授权未通过」）：先看这里的余额，
    再谈 license。
    """
    import sqlite3

    con = sqlite3.connect(_credit_db_path(db_url))
    con.row_factory = sqlite3.Row
    try:
        console.print("[bold]— teams —[/]")
        for row in con.execute(
            "SELECT id,name,team_credit_balance,team_credit_quota,team_credit_period_start,tier_credits_json,max_seats,status FROM teams"
        ):
            t = dict(row)
            quota = t["team_credit_quota"] or 0
            pct = f"{100 * t['team_credit_balance'] / quota:.0f}%" if quota else "n/a"
            console.print(f"  {t['id']} ({t['name']}) 池={t['team_credit_balance']}/{quota} ({pct}) seats={t['max_seats']} status={t['status']}")
            console.print(f"    tier_credits={t['tier_credits_json']} period_start={t['team_credit_period_start']}")
            if t["tier_credits_json"] in ("{}", "", None):
                console.print("    [red]⚠ tier_credits 为空 —— 新开通用户 quota 会是 0, 每轮对话都撞额度不足[/]")
        console.print("[bold]— users —[/]")
        for row in con.execute("SELECT email,tier,credit_balance,credit_quota,status FROM users ORDER BY email"):
            u = dict(row)
            quota = u["credit_quota"] or 0
            balance = u["credit_balance"] or 0
            pct = f"{100 * balance / quota:.0f}%" if quota else "n/a"
            flag = ""
            if quota and balance / quota < 0.2:
                flag = " [yellow]低余额[/]"
            if balance <= 0:
                flag = " [red]已耗尽 → 会 402[/]"
            console.print(f"  {u['email']:<36} {u['tier']:<6} {balance}/{quota} ({pct}) status={u['status']}{flag}")
        console.print("[bold]— license —[/]")
        rows = list(
            con.execute(
                "SELECT team_id,license_id,expires_at,grace_period_days,max_offline_lifetime_days,"
                "max_seats,last_good_health,deployment_mode FROM team_license"
            )
        )
        if not rows:
            console.print("  (空 —— saas 模式不读 license, 属正常; local 模式则说明尚未激活)")
        for row in rows:
            console.print(f"  {dict(row)}")
    finally:
        con.close()


@credit_app.command("usage")
def credit_usage(
    db_url: str | None = _DB_OPT,
    days: int = typer.Option(30, "--days", help="统计窗口（天）"),
) -> None:
    """只读：按 reason 汇总 llm_usage_events —— 回答「后台调用到底烧了多少」。

    `turn` = 用户请求驱动（已计费）；`dreaming` / `unattributed` 等 = 后台调用，
    **当前一律不计费**。这份占比是「该不该对后台用量计费」的决策依据。
    """
    import asyncio
    import sqlite3
    from datetime import UTC, datetime, timedelta

    from bowhead_core.user_cron import llm_usage_retention_days
    from bowhead_core.user_store import UserStore

    path = _credit_db_path(db_url)
    days = max(1, days)
    cutoff = datetime.now(UTC) - timedelta(days=days)
    store = UserStore(path)
    # Raw records are kept only within the retention window (90 days by default); query daily aggregates for longer windows —
    # otherwise --days 365 would **silently underreport**, returning only the most recent 90 days.
    retention = llm_usage_retention_days()
    use_rollup = retention > 0 and days > retention
    try:
        if use_rollup:
            rows = asyncio.run(store.llm_usage_daily_by_reason(since_day=cutoff.strftime("%Y-%m-%d")))
        else:
            rows = asyncio.run(store.llm_usage_by_reason(since=cutoff.isoformat()))
    except sqlite3.OperationalError as e:
        # Both tables were added in credit-billing gap 7 and are absent in older databases. Provide guidance instead of a traceback.
        if "no such table" not in str(e):
            raise
        console.print("[yellow]该库还没有 llm_usage_events / llm_usage_daily 表 —— 说明它由更早的版本创建。[/]")
        console.print("网关启动时会幂等建表；升级后跑一次 gateway 即可，或用 `bowhead db init`。")
        return
    source = f"日级汇总（原始明细只留 {retention} 天）" if use_rollup else "原始明细"
    console.print(f"[dim]数据源：{source}[/]")
    if not rows:
        console.print(f"[yellow]近 {days} 天没有 llm_usage_events 记录[/]")
        console.print("（计量在 0.86.x 才接线；更早的用量不会回填）")
        return
    total = sum((r["tokens_input"] or 0) + (r["tokens_output"] or 0) for r in rows)
    console.print(f"[bold]近 {days} 天 LLM 用量（按 reason）[/] 合计 {total} tokens")
    for r in rows:
        tokens = (r["tokens_input"] or 0) + (r["tokens_output"] or 0)
        share = f"{100 * tokens / total:.1f}%" if total else "-"
        billed = "已计费" if r["reason"] == "turn" else "[yellow]未计费[/]"
        console.print(f"  {r['reason']:<18} calls={r['calls']:<6} tokens={tokens:<12} ({share:>6})  {billed}")


@credit_app.command("reconcile")
def credit_reconcile(db_url: str | None = _DB_OPT) -> None:
    """只读对账：team 池消耗 vs 用户钱包消耗是否对得上 + 扣费失败事件。

    扣费是「先跑后扣、扣失败只 log 不回滚」（model §5 认可的取舍），失败只落
    `credit_exhaustion_events` 的 `credit_deduct_failed`，此前没有任何对账面。
    差额通常由 `admin_grant`（从 team 池划拨到用户钱包）解释；解释不满则要怀疑双层扣费漏账。
    """
    import sqlite3

    con = sqlite3.connect(_credit_db_path(db_url))
    con.row_factory = sqlite3.Row
    try:
        for team in con.execute("SELECT id,name,team_credit_balance,team_credit_quota FROM teams"):
            t = dict(team)
            pool_spent = (t["team_credit_quota"] or 0) - (t["team_credit_balance"] or 0)
            wallet_spent = 0
            for u in con.execute(
                "SELECT credit_balance,credit_quota FROM users WHERE team_id=? AND status='active'",
                (t["id"],),
            ):
                wallet_spent += (u["credit_quota"] or 0) - (u["credit_balance"] or 0)
            # Count only admin_grant as a pool-to-wallet transfer: period_reset also writes positive deltas, but resets
            # each layer separately to its quota rather than transferring credits; including it would produce a false discrepancy.
            grants = con.execute(
                "SELECT COALESCE(SUM(delta),0) FROM credit_ledger WHERE team_id=? AND delta > 0 AND reason LIKE 'admin_grant%'",
                (t["id"],),
            ).fetchone()[0]
            # A grant adds +G to pool spending and -G to wallet spending (the user's balance exceeds quota),
            # so it appears twice in the difference between the two measures; subtract 2G rather than G.
            #   pool_spent   = turn_spend + G
            #   wallet_spent = turn_spend - G
            #   → pool_spent - wallet_spent - 2G == 0 when the ledgers reconcile.
            gap = pool_spent - wallet_spent - 2 * grants
            console.print(f"[bold]{t['id']}[/] ({t['name']}) 池消耗={pool_spent} 钱包消耗合计={wallet_spent} grant 划拨={grants} → 未解释差额={gap}")
            if gap:
                console.print("  [yellow]⚠ 差额非 0：可能是周期重置跨期、手工改库或双层扣费漏账；查 credit_ledger 的非 turn 行定性[/]")
        console.print("[bold]— 扣费失败 / 硬拦事件（近 50 条）—[/]")
        rows = list(con.execute("SELECT code,user_id,team_id,created_at FROM credit_exhaustion_events ORDER BY created_at DESC LIMIT 50"))
        if not rows:
            console.print("  (无)")
        for row in rows:
            e = dict(row)
            mark = "[red]" if e["code"] == "credit_deduct_failed" else ""
            close = "[/]" if mark else ""
            console.print(f"  {mark}{e['code']}{close} user={e['user_id']} team={e['team_id']} at={e['created_at']}")
    finally:
        con.close()


@credit_app.command("topup")
def credit_topup(
    email: str = typer.Option(..., "--email", "-e", help="目标用户邮箱"),
    amount: int = typer.Option(..., "--amount", "-a", help="加多少 credit（同时抬 quota 上限）"),
    db_url: str | None = _DB_OPT,
    also_team_pool: bool = typer.Option(True, "--team-pool/--no-team-pool", help="同时补 team 池（预检两层都查，只补一层仍会 402）"),
    yes: bool = typer.Option(False, "--yes", help="跳过确认"),
) -> None:
    """[写操作] 直接给用户充额度 —— license 事故时的逃生门。

    正常运营请走「设置 → 额度」的 admin 界面/API（有审计与角色校验）。本命令**绕过**那些
    校验直改库，只在 admin 面用不了时使用（例如 local 部署 license 漏签 tier_credits，
    改配置接口恒返回 409 managed_by_license）。

    两层都补是因为发送前预检**同时**查用户钱包和 team 池，任一为 0 都会 402。
    """
    import sqlite3

    if amount <= 0:
        console.print("[red]✗ amount 必须 > 0[/]")
        raise typer.Exit(1)
    con = sqlite3.connect(_credit_db_path(db_url))
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT id,team_id,credit_balance,credit_quota FROM users WHERE email=?", (email,)).fetchone()
        if row is None:
            console.print(f"[red]✗ 找不到用户：{email}[/]")
            raise typer.Exit(1)
        u = dict(row)
        new_balance = (u["credit_balance"] or 0) + amount
        new_quota = max(u["credit_quota"] or 0, new_balance)
        pool_note = f"；team 池同步 +{amount}" if also_team_pool else ""
        console.print(f"将改：{email} 余额 {u['credit_balance']}→{new_balance}，quota {u['credit_quota']}→{new_quota}{pool_note}")
        console.print("[yellow]⚠ 月初 cron 会按 tier_credits 重算 quota —— 手工调的值不会长期存活；根治要重签 license 或改 team 的 tier_credits[/]")
        if not yes and not typer.confirm("确认执行？"):
            console.print("已取消")
            raise typer.Exit(1)
        con.execute("UPDATE users SET credit_balance=?, credit_quota=? WHERE id=?", (new_balance, new_quota, u["id"]))
        if also_team_pool and u["team_id"]:
            con.execute(
                "UPDATE teams SET team_credit_balance = team_credit_balance + ?, "
                "team_credit_quota = MAX(team_credit_quota, team_credit_balance + ?) WHERE id=?",
                (amount, amount, u["team_id"]),
            )
        con.commit()
        console.print(f"[green]✓ 已充值[/] {email} 余额={new_balance} quota={new_quota}")
    finally:
        con.close()


if __name__ == "__main__":
    app()
