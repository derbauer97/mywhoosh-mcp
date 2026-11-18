"""MyWhoosh MCP server (stdio): create and list custom cycling workouts."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

from mcp.server.mcpserver import MCPServer

from .client import MyWhooshClient, MyWhooshError
from .workout import Block, build_workout, summarize

# httpx INFO logs would print pre-signed S3 URLs to stderr.
logging.getLogger("httpx").setLevel(logging.WARNING)

server = MCPServer(
    name="mywhoosh",
    version="0.2.0",
    instructions=(
        "Create, list, delete and schedule MyWhoosh custom cycling workouts. Power is in % FTP. "
        "Workouts appear in the MyWhoosh app under Custom Workouts after a resync/restart. "
        "Calls may be issued in parallel; the server serializes writes itself."
    ),
)
_client = MyWhooshClient()

# The backend processes uploads/deletes asynchronously and rewrites the whole per-user
# workout archive; overlapping writes lose each other (verified 2026-11). All writes go
# through this lock and each one waits until the change is visible before releasing it.
_write_lock = asyncio.Lock()
_issued_ids: set[int] = set()
CONFIRM_TIMEOUT_S = 45.0
CONFIRM_INTERVAL_S = 2.0


async def _wait_for(predicate) -> list[dict] | None:
    deadline = time.monotonic() + CONFIRM_TIMEOUT_S
    while True:
        ws = await _client.list_workouts()
        if predicate(ws):
            return ws
        if time.monotonic() > deadline:
            return None
        await asyncio.sleep(CONFIRM_INTERVAL_S)


@server.tool()
async def mywhoosh_create_workout(
    name: str,
    blocks: list[Block],
    erg_mode: bool = False,
    description: str = "",
    ftp_watts: int | None = None,
) -> dict:
    """Create a custom cycling workout in MyWhoosh.

    blocks are executed in order. Power in % FTP (e.g. 90 = 90% FTP).
    erg_mode=True: trainer holds the target power (ERG). False: rider controls resistance.
    ftp_watts is optional and only used to compute kJ.
    Text (name, description, messages) is transliterated to ASCII (ö->oe, ß->ss, – -> -),
    because MyWhoosh drops all non-ASCII characters.

    Example blocks:
      {"type":"warmup","duration_s":600,"start_power":50,"end_power":75}
      {"type":"interval","duration_s":240,"power":105,"repeat":4,"rest_duration_s":180,"rest_power":55}
      {"type":"steady","duration_s":1200,"power":70,"cadence":90}
      {"type":"cooldown","duration_s":300,"start_power":65,"end_power":45}
    """
    if not blocks:
        raise MyWhooshError("blocks must not be empty")
    w = build_workout(name, blocks, description=description, erg_mode=erg_mode, ftp_watts=ftp_watts)
    async with _write_lock:
        # Ids are unix seconds (as the app does); never reuse one, an equal Id overwrites.
        taken = {x.get("Id") for x in await _client.list_workouts()} | _issued_ids
        while w["Id"] in taken:
            w["Id"] += 1
        _issued_ids.add(w["Id"])
        await _client.upload_workouts([w])
        confirmed = await _wait_for(lambda ws: any(x.get("Id") == w["Id"] for x in ws)) is not None
    out = {"confirmed": confirmed, "workout": summarize(w)}
    if not confirmed:
        out["warning"] = f"Upload accepted but not visible after {CONFIRM_TIMEOUT_S:.0f}s; check mywhoosh_list_workouts later."
    return out


def _content(w: dict) -> dict:
    """The parts an edit can change; used to see whether the stored workout shows the edit."""
    s = summarize(w, include_steps=True)
    return {k: s[k] for k in ("name", "description", "erg_mode", "duration_min", "steps")}


@server.tool()
async def mywhoosh_update_workout(
    workout_id: int,
    name: str,
    blocks: list[Block],
    erg_mode: bool = False,
    description: str = "",
    ftp_watts: int | None = None,
) -> dict:
    """Replace an existing custom workout in place (same id), e.g. to fix a typo or change targets.

    Same arguments as mywhoosh_create_workout plus workout_id; the whole workout is replaced.
    The id stays, so calendar entries keep pointing at it. Calendar entries keep their own copy
    of name and TSS, which is not updated: re-schedule if those must change.
    """
    if not blocks:
        raise MyWhooshError("blocks must not be empty")
    w = build_workout(name, blocks, description=description, erg_mode=erg_mode,
                      ftp_watts=ftp_watts, workout_id=workout_id)
    async with _write_lock:
        old = next((x for x in await _client.list_workouts() if x.get("Id") == workout_id), None)
        if old is None:
            raise MyWhooshError(f"No custom workout with id {workout_id}")
        for keep in ("IsFavorite", "CompletedCount"):
            if keep in old:
                w[keep] = old[keep]
        # An upload with an existing Id overwrites that workout instead of adding one.
        await _client.upload_workouts([w])
        want = _content(w)
        ws = await _wait_for(lambda ws: [_content(x) for x in ws if x.get("Id") == workout_id] == [want])
    out = {"confirmed": ws is not None, "workout": summarize(w),
           "previous": summarize(old, include_steps=False)}
    if ws is None:
        out["warning"] = (f"Upload accepted, but after {CONFIRM_TIMEOUT_S:.0f}s there is not exactly one "
                          f"workout with id {workout_id} showing the edit; check mywhoosh_list_workouts.")
    return out


@server.tool()
async def mywhoosh_delete_workout(workout_id: int, force: bool = False) -> dict:
    """Delete a custom cycling workout by id (see mywhoosh_list_workouts).

    If the workout is used by a training plan, nothing is deleted unless force=True.
    """
    async with _write_lock:
        ws = await _client.list_workouts()
        target = next((x for x in ws if x.get("Id") == workout_id), None)
        if target is None:
            raise MyWhooshError(f"No custom workout with id {workout_id}")
        usage = await _client.delete_usage(workout_id)
        if usage and not force:
            return {"deleted": False, "reason": "workout is referenced; pass force=True to delete anyway",
                    "used_by": usage}
        await _client.delete_workout(workout_id)
        confirmed = await _wait_for(lambda ws: all(x.get("Id") != workout_id for x in ws)) is not None
    return {"deleted": True, "confirmed": confirmed, "name": target.get("Name")}


@server.tool()
async def mywhoosh_list_workouts(include_steps: bool = False, name_contains: str | None = None) -> dict:
    """List custom cycling workouts stored in MyWhoosh (newest first).

    include_steps adds a compact per-step breakdown. name_contains filters case-insensitively.
    """
    ws = await _client.list_workouts()
    if name_contains:
        ws = [w for w in ws if name_contains.lower() in (w.get("Name") or "").lower()]
    ws.sort(key=lambda w: w.get("Id") or 0, reverse=True)
    return {
        "count": len(ws),
        **(await _client.credits()),
        "workouts": [summarize(w, include_steps) | {"created": _ts(w.get("Id"))} for w in ws],
    }


def _ts(wid: int | None) -> str | None:
    # Ids are unix seconds for workouts created by the app / this server.
    if isinstance(wid, int) and 1_000_000_000 < wid < 10_000_000_000:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(wid))
    return None


# --- calendar -------------------------------------------------------------------------

_TZ = ZoneInfo(os.environ.get("MYWHOOSH_TZ", "Europe/Berlin"))


def _day_bounds(day: date) -> tuple[int, int]:
    start = datetime.combine(day, dtime.min, _TZ)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp()) - 1


def _parse_date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError as e:
        raise MyWhooshError(f"Invalid date {s!r}, expected YYYY-MM-DD") from e


def _tasks(resp) -> list[dict]:
    """Find the task dicts in whatever envelope the calendar API returns."""
    if isinstance(resp, list):
        if all(isinstance(x, dict) for x in resp) and any("TaskId" in x for x in resp):
            return resp
        return [t for x in resp for t in _tasks(x)]
    if isinstance(resp, dict):
        if "TaskId" in resp:
            return [resp]
        return [t for v in resp.values() for t in _tasks(v)]
    return []


def _task_summary(t: dict) -> dict:
    start = t.get("TaskStartedTimeEpoc") or 0
    end = t.get("TaskEndEpochTime") or start
    local = datetime.fromtimestamp(start, _TZ) if start else None
    return {
        "task_id": t.get("TaskId"),
        "type": t.get("TaskType"),
        "workout_id": t.get("TaskTypeId"),
        "name": t.get("TaskName"),
        "date": local.strftime("%Y-%m-%d") if local else None,
        "start": local.strftime("%H:%M") if local else None,
        "duration_min": round((end - start) / 60) if end and start else None,
        "state": t.get("TaskState"),
    }


async def _tasks_between(first: date, last: date) -> list[dict]:
    lo, _ = _day_bounds(first)
    _, hi = _day_bounds(last)
    ts = [_task_summary(t) for t in _tasks(await _client.list_tasks(lo, hi))]
    return sorted(ts, key=lambda t: (t["date"] or "", t["start"] or ""))


@server.tool()
async def mywhoosh_schedule_workout(workout_id: int, date: str, start_time: str = "18:00") -> dict:
    """Put a custom workout (id from mywhoosh_list_workouts/mywhoosh_create_workout) on the MyWhoosh calendar.

    date: YYYY-MM-DD, start_time: HH:MM, both local time (Europe/Berlin).
    """
    day = _parse_date(date)
    try:
        hh, mm = (int(x) for x in start_time.split(":"))
        start = datetime.combine(day, dtime(hh, mm), _TZ)
    except ValueError as e:
        raise MyWhooshError(f"Invalid start_time {start_time!r}, expected HH:MM") from e
    w = next((x for x in await _client.list_workouts() if x.get("Id") == workout_id), None)
    if w is None:
        raise MyWhooshError(f"No custom workout with id {workout_id}")
    start_epoch = int(start.timestamp())
    await _client.create_task(
        workout_id=workout_id, name=w.get("Name") or "", description=w.get("Description") or "",
        tss=w.get("TSS") or 0, start_epoch=start_epoch, end_epoch=start_epoch + int(w.get("Time") or 0),
    )
    same_day = await _tasks_between(day, day)
    match = [t for t in same_day if str(t["workout_id"]) == str(workout_id)]
    return {"scheduled": bool(match), "task": match[-1] if match else None,
            **({} if match else {"warning": "Accepted, but not found in the calendar for that day",
                                 "calendar_that_day": same_day})}


@server.tool()
async def mywhoosh_list_scheduled_workouts(start_date: str, end_date: str) -> dict:
    """List MyWhoosh calendar entries between two dates (YYYY-MM-DD, inclusive, local time)."""
    first, last = _parse_date(start_date), _parse_date(end_date)
    if last < first:
        raise MyWhooshError("end_date is before start_date")
    ts = await _tasks_between(first, last)
    return {"count": len(ts), "tasks": ts}


@server.tool()
async def mywhoosh_unschedule_workout(task_id: str) -> dict:
    """Remove a calendar entry by task_id (from mywhoosh_list_scheduled_workouts). The workout itself stays."""
    res = await _client.delete_task(task_id)
    return {"unscheduled": True, "response": res}


# --- profile / FTP ----------------------------------------------------------------------


@server.tool()
async def mywhoosh_get_profile() -> dict:
    """FTP, weight, height and best power values stored in the MyWhoosh profile.

    Workout targets in MyWhoosh are % of this FTP, so check it before planning watt targets.
    """
    pd = await _client.player_data()
    p = (pd.get("PlayerDataStruct") or {}).get("PlayerPersonalStruct") or {}
    prof = (pd.get("PlayerDataStruct") or {}).get("PlayerProfileStruct") or {}
    return {
        "ftp_watts": p.get("FtpPlayer"),
        "weight_kg": prof.get("Weight"),
        "verified_weight_kg": p.get("VerifiedWeight"),
        "height_cm": prof.get("HeightCm"),
        "best_power": {k.removeprefix("BestPower"): v for k, v in p.items()
                       if k.startswith("BestPower") and v},
        "max_power": p.get("MaxPower"),
    }


@server.tool()
async def mywhoosh_set_ftp(ftp_watts: int) -> dict:
    """Set the cycling FTP in the MyWhoosh profile (watts, 50-600).

    Rewrites the profile with only FtpPlayer changed and verifies that nothing else changed.
    """
    if not 50 <= ftp_watts <= 600:
        raise MyWhooshError("ftp_watts must be between 50 and 600")
    async with _write_lock:
        res = await _client.set_ftp(ftp_watts)
    expected = ["PlayerDataStruct.PlayerPersonalStruct.FtpPlayer"]
    ok = res["after"] == float(ftp_watts) and res["changed_fields"] in ([], expected)
    out = {"confirmed": ok, "before": res["before"], "after": res["after"]}
    if not ok:
        out["warning"] = f"Unexpected result, changed fields: {res['changed_fields']}"
    return out


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
