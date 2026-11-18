"""Minimal MyWhoosh API client: login + custom workout upload/list/delete.

Endpoints were verified against the live API (2026-11) and the official
web editor at workout.mywhoosh.com.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import subprocess
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError

LOGIN_URL = "https://services.mywhoosh.com/http-service/api/login"
COACHING = "https://coaching.mywhoosh.com/api/v2"
CALENDAR = "https://service14.mywhoosh.com/v1"
KEYCHAIN_SERVICE = "mywhoosh-mcp"
TIMEOUT = 30.0
# MyWhoosh allows only one active session: while the app is logged in, a new API login
# is rejected. The access token is therefore cached so restarts don't need a fresh login.
TOKEN_FILE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "mywhoosh-mcp" / "token.json"

# Path segment / SportsModeType per sport (as sent by the app).
SPORT_TYPE = {"cycling": 0}


class MyWhooshError(ToolError):
    """Raised deliberately; the message is shown to the calling agent (never contains secrets)."""


def _credentials() -> tuple[str, str]:
    """Env vars first, then macOS Keychain (service 'mywhoosh-mcp')."""
    user, pw = os.environ.get("MYWHOOSH_USERNAME"), os.environ.get("MYWHOOSH_PASSWORD")
    if user and pw:
        return user, pw
    try:
        meta = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE],
            capture_output=True, text=True, check=True,
        ).stdout
        pw = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise MyWhooshError(
            f"No credentials: set MYWHOOSH_USERNAME/MYWHOOSH_PASSWORD or add a Keychain item "
            f"with service '{KEYCHAIN_SERVICE}'"
        ) from e
    for line in meta.splitlines():
        if '"acct"' in line:
            user = line.split("=", 1)[1].strip().strip('"')
            break
    if not user or not pw:
        raise MyWhooshError("Keychain item found but account or password is empty")
    return user, pw


def _jwt_exp(token: str) -> float:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))
    except Exception:
        return 0.0


def _load_cached_token() -> str | None:
    try:
        tok = json.loads(TOKEN_FILE.read_text())["access_token"]
    except (OSError, ValueError, KeyError):
        return None
    return tok if _jwt_exp(tok) > time.time() + 60 else None


def _store_token(token: str | None) -> None:
    if token is None:
        TOKEN_FILE.unlink(missing_ok=True)
        return
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"access_token": token}, f)


def _device_id() -> str:
    """Stable per machine, so all our processes count as one device for MyWhoosh."""
    path = TOKEN_FILE.parent / "device-id"
    try:
        return path.read_text().strip()
    except OSError:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        dev = str(uuid.uuid4())
        path.write_text(dev)
        return dev


class MyWhooshClient:
    def __init__(self) -> None:
        self._token: str | None = _load_cached_token()
        self._http = httpx.AsyncClient(timeout=TIMEOUT)

    async def _login(self) -> None:
        user, pw = _credentials()
        r = await self._http.post(LOGIN_URL, json={
            "Username": user,
            "Password": pw,
            "Platform": "Android",
            "Action": 1001,
            "CorrelationId": str(uuid.uuid4()),
            "DeviceId": _device_id(),
            "Authorization": "",
        })
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or not data.get("Success"):
            msg = data.get("Message", r.text[:200])
            if "already logged in" in msg.lower():
                raise MyWhooshError(
                    "MyWhoosh login rejected: the account is logged in elsewhere (usually the MyWhoosh "
                    "app). Close/log out of the app, then retry."
                )
            raise MyWhooshError(f"Login failed ({r.status_code}): {msg}")
        self._token = data["AccessToken"]
        _store_token(self._token)

    async def _request(self, method: str, path: str, base: str = COACHING, **kw: Any) -> httpx.Response:
        for attempt in range(2):
            if not self._token:
                await self._login()
            r = await self._http.request(
                method, f"{base}{path}", headers={"Authorization": f"Bearer {self._token}"}, **kw
            )
            if r.status_code == 401 and attempt == 0:
                self._token = None  # token expired/invalidated -> re-login once
                _store_token(None)
                continue
            return r
        return r

    async def upload_workouts(self, workouts: list[dict], sport: str = "cycling") -> dict:
        # json.dumps default ensure_ascii; text fields are ASCII anyway (backend drops non-ASCII).
        r = await self._request("POST", "/client/custom-workout-upload", json={
            "SportsModeType": SPORT_TYPE[sport],
            "WorkoutsData": workouts,
        })
        if r.status_code not in (200, 201):
            raise MyWhooshError(f"Upload failed ({r.status_code}): {r.text[:300]}")
        body = r.json()
        if not body.get("status"):
            if "credit" in str(body.get("message", "")).lower():
                raise MyWhooshError(
                    "MyWhoosh: no free custom-workout slots (credits) left. Delete old custom "
                    "workouts with mywhoosh_delete_workout to free slots, then retry."
                )
            raise MyWhooshError(f"Upload rejected: {body}")
        return body

    async def list_workouts(self, sport: str = "cycling") -> list[dict]:
        # Right after a write the API may point at an archive that is not on S3 yet (404).
        for attempt in range(6):
            r = await self._request("GET", f"/client/custom-workout-upload/{SPORT_TYPE[sport]}")
            if r.status_code == 400 and "No workouts found" in r.text:
                return []
            if r.status_code != 200:
                raise MyWhooshError(f"List failed ({r.status_code}): {r.text[:300]}")
            url = (r.json().get("data") or {}).get("workoutZipUrl")
            if not url:
                return []
            # Pre-signed S3 URL; must not carry our bearer token.
            async with httpx.AsyncClient(timeout=TIMEOUT) as s3:
                z = await s3.get(url)
            if z.status_code in (403, 404) and attempt < 5:
                await asyncio.sleep(2)
                continue
            if z.status_code != 200:
                raise MyWhooshError(f"Workout archive download failed ({z.status_code})")
            break
        workouts = []
        with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
            for name in zf.namelist():
                if name.endswith(".json"):
                    workouts.append(json.loads(zf.read(name)))
        return workouts

    async def credits(self) -> dict:
        r = await self._request("GET", "/client/my-credits")
        if r.status_code != 200:
            return {}
        d = (r.json() or {}).get("data") or {}
        return {"free_slots": d.get("CurrentCredits"), "subscription": d.get("IsSubscriptionExists")}
        with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
            for name in zf.namelist():
                if name.endswith(".json"):
                    workouts.append(json.loads(zf.read(name)))
        return workouts

    async def delete_usage(self, workout_id: int, sport: str = "cycling") -> list:
        """Training plans etc. that reference the workout (same pre-check as the web editor)."""
        r = await self._request("POST", f"/custom-workout/check-before-delete/{SPORT_TYPE[sport]}/{workout_id}")
        try:
            body = r.json()
        except ValueError:
            return []
        data = body.get("data") if isinstance(body, dict) else None
        return data if body.get("error") is True and isinstance(data, list) else []

    async def delete_workout(self, workout_id: int, sport: str = "cycling") -> dict:
        r = await self._request("DELETE", f"/client/custom-workout-upload/{SPORT_TYPE[sport]}/{workout_id}")
        if r.status_code not in (200, 201, 204):
            raise MyWhooshError(f"Delete failed ({r.status_code}): {r.text[:300]}")
        try:
            return r.json()
        except ValueError:
            return {"status": True}

    # --- calendar (tasks) -------------------------------------------------------------

    @staticmethod
    def _json(r: httpx.Response, what: str) -> Any:
        if r.status_code not in (200, 201, 204):
            raise MyWhooshError(f"{what} failed ({r.status_code}): {r.text[:300]}")
        try:
            return r.json()
        except ValueError:
            return r.text

    async def create_task(self, *, workout_id: int, name: str, description: str, tss: float,
                          start_epoch: int, end_epoch: int) -> Any:
        r = await self._request("POST", "/task/create", base=CALENDAR, json={
            "TaskId": "",
            "TaskType": "E_Simple_Workout",
            "TaskStartedTimeEpoc": start_epoch,
            "TaskTypeId": str(workout_id),
            "CurDayId": "",
            "TaskState": "E_NotStarted",
            "DayNo": 0,
            "TaskEndEpochTime": end_epoch,
            "MapId": 0,
            "TaskName": name,
            "TaskDescription": description,
            "TotalKilometers": 0,
            "TotalElevation": 0,
            "TSS": tss,
            "SportMode": "E_Cycling",
        })
        return self._json(r, "Schedule")

    async def list_tasks(self, start_epoch: int, end_epoch: int) -> Any:
        r = await self._request("GET", "/task/date-range-task-list", base=CALENDAR,
                                params={"startDate": start_epoch, "endDate": end_epoch})
        return self._json(r, "Calendar list")

    async def delete_task(self, task_id: str) -> Any:
        r = await self._request("DELETE", f"/task/{task_id}", base=CALENDAR)
        return self._json(r, "Unschedule")
