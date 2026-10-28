"""Team-scoped daily operational counts; missing historical observations are zero."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite


def _utc_bounds(day: date, zone: ZoneInfo) -> tuple[str, str]:
    start = datetime.combine(day, time.min, zone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(UTC)
    return start.isoformat(), end.isoformat()


async def get_team_operations_report(
    db_path: str | Path,
    *,
    team_id: str,
    days: int,
    now: datetime | None = None,
) -> dict:
    if days not in {1, 7, 30}:
        raise ValueError("days must be 1, 7, or 30")
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT report_timezone FROM teams WHERE id=?", (team_id,)) as cur:
            team = await cur.fetchone()
        if team is None:
            raise ValueError("team not found")
        timezone = str(team["report_timezone"] or "Asia/Shanghai")
        try:
            zone = ZoneInfo(timezone)
        except KeyError:
            timezone = "Asia/Shanghai"
            zone = ZoneInfo(timezone)
        today = (now or datetime.now(UTC)).astimezone(zone).date()
        rows: list[dict] = []
        for offset in range(days, 0, -1):
            day = today - timedelta(days=offset)
            start, end = _utc_bounds(day, zone)
            async with db.execute(
                "SELECT COUNT(DISTINCT a.user_id) AS n FROM user_audit_log a "
                "WHERE a.team_id=? AND a.user_id IS NOT NULL "
                "AND (a.event_type='team_login_success' OR ("
                "a.event_type IN ('login_success','oauth_login','oauth_invite_activation','invite_activation_success') "
                "AND NOT EXISTS (SELECT 1 FROM user_audit_log n WHERE n.user_id=a.user_id "
                "AND n.event_type='team_login_success' "
                "AND ABS(strftime('%s',n.created_at)-strftime('%s',a.created_at))<=60))) "
                "AND datetime(a.created_at)>=datetime(?) AND datetime(a.created_at)<datetime(?)",
                (team_id, start, end),
            ) as cur:
                logins = int((await cur.fetchone())["n"])
            async with db.execute(
                "SELECT COUNT(*) AS n FROM sessions s JOIN workspaces w ON w.id=s.workspace_id "
                "WHERE w.team_id=? AND s.origin='normal' "
                "AND datetime(s.started_at)>=datetime(?) AND datetime(s.started_at)<datetime(?)",
                (team_id, start, end),
            ) as cur:
                sessions = int((await cur.fetchone())["n"])
            async with db.execute(
                "SELECT COALESCE(SUM(CASE WHEN reason='turn' THEN tokens_input ELSE 0 END),0) AS input_tokens, "
                "COALESCE(SUM(CASE WHEN reason='turn' THEN tokens_output ELSE 0 END),0) AS output_tokens, "
                "COALESCE(SUM(CASE WHEN reason!='turn' THEN tokens_input+tokens_output ELSE 0 END),0) AS background_tokens "
                "FROM llm_usage_events WHERE team_id=? "
                "AND datetime(created_at)>=datetime(?) AND datetime(created_at)<datetime(?)",
                (team_id, start, end),
            ) as cur:
                usage = await cur.fetchone()
            async with db.execute(
                "SELECT COALESCE(SUM(o.skill_loads),0) AS skill_loads, "
                "COALESCE(SUM(o.tool_loads),0) AS tool_loads, "
                "COALESCE(SUM(o.tool_calls),0) AS tool_calls "
                "FROM turn_operation_usage o JOIN turns t ON t.id=o.turn_id "
                "JOIN sessions s ON s.id=t.session_id JOIN workspaces w ON w.id=s.workspace_id "
                "WHERE w.team_id=? AND s.origin='normal' "
                "AND datetime(o.recorded_at)>=datetime(?) AND datetime(o.recorded_at)<datetime(?)",
                (team_id, start, end),
            ) as cur:
                operations = await cur.fetchone()
            rows.append(
                {
                    "day": day.isoformat(),
                    "login_users": logins,
                    "sessions": sessions,
                    "input_tokens": int(usage["input_tokens"]),
                    "output_tokens": int(usage["output_tokens"]),
                    "background_tokens": int(usage["background_tokens"]),
                    "skill_loads": int(operations["skill_loads"]),
                    "tool_loads": int(operations["tool_loads"]),
                    "tool_calls": int(operations["tool_calls"]),
                }
            )
    return {"timezone": timezone, "days": rows}


async def set_team_report_timezone(db_path: str | Path, *, team_id: str, timezone: str) -> None:
    ZoneInfo(timezone)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE teams SET report_timezone=? WHERE id=?", (timezone, team_id))
        await db.commit()
