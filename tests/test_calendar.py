from datetime import date, datetime, timezone

from mywhoosh_mcp.server import _day_bounds, _task_summary, _tasks


def test_day_bounds_berlin_dst():
    lo, hi = _day_bounds(date(2026, 11, 19))  # CET, UTC+1
    assert lo == int(datetime(2026, 11, 18, 23, 0, tzinfo=timezone.utc).timestamp())
    assert hi - lo == 86400 - 1
    lo2, hi2 = _day_bounds(date(2026, 3, 29))  # DST switch: 23h day
    assert hi2 - lo2 == 23 * 3600 - 1


def test_tasks_envelope_and_summary():
    ep = int(datetime(2026, 11, 18, 17, 0, tzinfo=timezone.utc).timestamp())  # 18:00 CET
    t = {"TaskId": "abc", "TaskType": "E_Simple_Workout", "TaskTypeId": "1794986468", "TaskName": "x",
         "TaskStartedTimeEpoc": ep, "TaskEndEpochTime": ep + 2580, "TaskState": "E_NotStarted"}
    for resp in ({"data": [t]}, {"data": {"tasks": [t]}}, [t], {"Tasks": [{"Day": [t]}]}):
        assert _tasks(resp) == [t]
    s = _task_summary(t)
    assert s == {"task_id": "abc", "type": "E_Simple_Workout", "workout_id": "1794986468", "name": "x",
                 "date": "2026-11-18", "start": "18:00", "duration_min": 43, "state": "E_NotStarted"}
