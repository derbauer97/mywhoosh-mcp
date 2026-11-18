"""Convert a simple block description into MyWhoosh custom-workout JSON.

Block -> step mapping mirrors the official web editor (workout.mywhoosh.com).
Power values are given in % FTP and stored as FTP fractions (90 -> 0.9).
"""

from __future__ import annotations

import time
import unicodedata
from typing import Literal

from pydantic import BaseModel, Field, model_validator

BlockType = Literal["warmup", "cooldown", "ramp", "steady", "interval", "freeride"]

# The MyWhoosh backend silently drops every non-ASCII character (verified 2026-11:
# raw UTF-8 and \u-escapes alike), so transliterate before upload.
_TRANSLIT = str.maketrans({
    "ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss",
    "–": "-", "—": "-", "‑": "-", "′": "'", "″": '"', "‘": "'", "’": "'", "‚": "'",
    "“": '"', "”": '"', "„": '"', "…": "...", "×": "x", "·": "-", "→": "->", "←": "<-",
    "≈": "~", "±": "+/-", "°": " Grad", "\u00a0": " ", "\u202f": " ",
})


def ascii_text(s: str) -> str:
    s = s.translate(_TRANSLIT)
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if ord(c) < 128 and not unicodedata.combining(c))


class Message(BaseModel):
    at_s: int = Field(ge=1, description="Seconds after block start")
    text: str


class Block(BaseModel):
    type: BlockType = Field(description=(
        "warmup/cooldown/ramp: linear ramp start_power->end_power; steady: constant power; "
        "interval: repeat x (duration@power, rest_duration@rest_power); freeride: no target"
    ))
    duration_s: int = Field(gt=0, description="Block duration in seconds (interval: ON phase)")
    power: float | None = Field(None, ge=0, le=400, description="% FTP for steady/interval ON")
    start_power: float | None = Field(None, ge=0, le=400, description="% FTP at ramp start")
    end_power: float | None = Field(None, ge=0, le=400, description="% FTP at ramp end")
    cadence: int = Field(0, ge=0, le=200, description="Target rpm, 0 = none")
    repeat: int | None = Field(None, ge=1, le=100, description="interval only: repetitions")
    rest_duration_s: int | None = Field(None, gt=0, description="interval only: rest seconds")
    rest_power: float | None = Field(None, ge=0, le=400, description="interval only: rest % FTP")
    rest_cadence: int | None = Field(None, ge=0, le=200, description="interval only: rest rpm")
    flat_road: bool = Field(False, description="freeride only: flatten the road")
    messages: list[Message] = Field(default_factory=list, description="On-screen texts in this block")

    @model_validator(mode="after")
    def _check(self) -> "Block":
        t = self.type
        if t == "steady" and self.power is None:
            raise ValueError("steady needs power")
        if t in ("warmup", "cooldown", "ramp") and (self.start_power is None or self.end_power is None):
            raise ValueError(f"{t} needs start_power and end_power")
        if t == "interval" and None in (self.power, self.repeat, self.rest_duration_s, self.rest_power):
            raise ValueError("interval needs power, repeat, rest_duration_s, rest_power")
        return self


def _frac(pct: float | None) -> float:
    return round((pct or 0) / 100, 8)


def _step(sid: int, step_type: str, time_s: int, *, power=0.0, start=0.0, end=0.0, rpm=0,
          interval_id=0, msgs=None, flat_road=0) -> dict:
    return {
        "IntervalId": interval_id, "StepType": step_type, "Id": sid,
        "WorkoutMessage": msgs or [], "Rpm": rpm, "Power": power, "Pace": 0,
        "StartPower": start, "EndPower": end, "Time": time_s,
        "IsManualGrade": False, "ManualGradeValue": 0, "ShowAveragePower": True, "FlatRoad": flat_road,
    }


def _msgs(messages: list[Message], lo: int, hi: int) -> list[dict]:
    """Messages whose block-relative offset falls into [lo, hi), re-based to the step."""
    out = [m for m in messages if lo <= m.at_s < hi]
    return [{"Id": i + 1, "Time": m.at_s - lo, "Message": ascii_text(m.text)} for i, m in enumerate(out)] if out else []


def build_steps(blocks: list[Block]) -> list[dict]:
    steps: list[dict] = []
    interval_id = 0
    kind = {"warmup": "E_WarmUp", "ramp": "E_WarmUp", "cooldown": "E_CoolDown"}
    for b in blocks:
        sid = len(steps) + 1
        msgs = _msgs(b.messages, 0, b.duration_s + 1)
        if b.type in kind:
            steps.append(_step(sid, kind[b.type], b.duration_s, start=_frac(b.start_power),
                               end=_frac(b.end_power), rpm=b.cadence, msgs=msgs))
        elif b.type == "steady":
            steps.append(_step(sid, "E_Normal", b.duration_s, power=_frac(b.power), rpm=b.cadence, msgs=msgs))
        elif b.type == "freeride":
            steps.append(_step(sid, "E_FreeRide", b.duration_s, rpm=b.cadence, msgs=msgs,
                               flat_road=int(b.flat_road)))
        elif b.type == "interval":
            interval_id += 1
            offset = 0
            for _ in range(b.repeat):
                for on in (True, False):
                    dur = b.duration_s if on else b.rest_duration_s
                    steps.append(_step(
                        len(steps) + 1, "E_Normal", dur,
                        power=_frac(b.power if on else b.rest_power),
                        rpm=b.cadence if on else (b.rest_cadence if b.rest_cadence is not None else b.cadence),
                        interval_id=interval_id, msgs=_msgs(b.messages, offset, offset + dur),
                    ))
                    offset += dur
    return steps


def _power_series(steps: list[dict]) -> list[float]:
    """Per-second target power as FTP fraction (freeride counted as 0.7, like the web editor)."""
    series: list[float] = []
    for s in steps:
        t = s["Time"]
        if s["StepType"] == "E_FreeRide":
            series += [0.7] * t
        elif s["StepType"] in ("E_WarmUp", "E_CoolDown"):
            series += [s["StartPower"] + (s["EndPower"] - s["StartPower"]) * i / max(t - 1, 1) for i in range(t)]
        else:
            series += [s["Power"]] * t
    return series


def metrics(steps: list[dict], ftp_watts: int | None) -> dict:
    p = _power_series(steps)
    if not p:
        return {"IF": 0, "TSS": 0, "KJ": 0}
    window = 30
    if len(p) >= window:
        csum = [0.0]
        for v in p:
            csum.append(csum[-1] + v)
        rolling = [(csum[i + window] - csum[i]) / window for i in range(len(p) - window + 1)]
    else:
        rolling = [sum(p) / len(p)]
    if_ = (sum(r ** 4 for r in rolling) / len(rolling)) ** 0.25
    tss = len(p) / 3600 * if_ ** 2 * 100
    kj = sum(p) * ftp_watts / 1000 if ftp_watts else 0
    return {"IF": round(if_, 3), "TSS": round(tss, 1), "KJ": round(kj, 1)}


def build_workout(name: str, blocks: list[Block], *, description: str = "", erg_mode: bool,
                  ftp_watts: int | None = None, workout_id: int | None = None) -> dict:
    steps = build_steps(blocks)
    m = metrics(steps, ftp_watts)
    name = ascii_text(name).strip() or "Workout"
    return {
        "Id": workout_id or int(time.time()),
        "Name": name,
        "Description": ascii_text(description).strip() or name,  # API rejects empty Description
        "Mode": "E_Ride",
        "ERGMode": "E_ON" if erg_mode else "E_OFF",
        "IsRecovery": False,
        "IsIntervals": any(b.type == "interval" for b in blocks),
        "FTPMode": "E_NoFTP",
        "IsTT": False, "IsTSS": False, "IsIF": False,
        "FTPMultiplier": 1,
        "StressPoint": 0,
        "Time": sum(s["Time"] for s in steps),
        "CustomTagDescription": "",
        "CategoryId": 1,
        "SubcategoryId": 0,
        "Type": "E_Custom",
        "DisplayType": "E_byWatts",
        "StepCount": len(steps),
        "IsFavorite": False,
        "CompletedCount": 0,
        "WorkoutStepsArray": steps,
        "WorkoutSteps": {str(s["Id"]): s for s in steps},
        "WorkoutstepsTMap": [],
        "WokoutAssociationId": 0,
        "AuthorName": "coach",
        "TSS": m["TSS"], "IF": m["IF"], "KJ": m["KJ"],
        "IsVODAvailable": False,
        "Environment": "E_Outside",
        "ScheduledDate": "",
    }


def _pct(x: float) -> str:
    return f"{round(x * 100)}%"


def summarize(w: dict, include_steps: bool = True) -> dict:
    steps = w.get("WorkoutStepsArray") or list((w.get("WorkoutSteps") or {}).values())
    out = {
        "id": w.get("Id"),
        "name": w.get("Name"),
        "description": w.get("Description"),
        "erg_mode": w.get("ERGMode") == "E_ON",
        "duration_min": round((w.get("Time") or 0) / 60, 1),
        "step_count": w.get("StepCount", len(steps)),
        "tss": w.get("TSS"),
        "if": w.get("IF"),
    }
    if include_steps:
        lines = []
        for s in sorted(steps, key=lambda s: s.get("Id", 0)):
            t, st = s.get("Time", 0), s.get("StepType")
            if st in ("E_WarmUp", "E_CoolDown"):
                tgt = f"{_pct(s.get('StartPower', 0))}->{_pct(s.get('EndPower', 0))}"
            elif st == "E_FreeRide":
                tgt = "freeride"
            else:
                tgt = _pct(s.get("Power", 0))
            extra = f" @{s['Rpm']}rpm" if s.get("Rpm") else ""
            iv = f" [interval {s['IntervalId']}]" if s.get("IntervalId") else ""
            lines.append(f"{t}s {tgt}{extra}{iv}")
        out["steps"] = lines
    return out
