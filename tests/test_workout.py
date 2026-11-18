import json

import pytest
from pydantic import ValidationError

from mywhoosh_mcp.workout import Block, build_workout, summarize


def blocks():
    return [
        Block(type="warmup", duration_s=600, start_power=50, end_power=75),
        Block(type="interval", duration_s=240, power=105, repeat=3, rest_duration_s=180, rest_power=55,
              cadence=95, messages=[{"at_s": 10, "text": "push"}, {"at_s": 250, "text": "rest"}]),
        Block(type="steady", duration_s=600, power=70),
        Block(type="freeride", duration_s=120),
        Block(type="cooldown", duration_s=300, start_power=65, end_power=45),
    ]


def test_structure_and_erg():
    w = build_workout("t", blocks(), erg_mode=True, workout_id=123)
    steps = w["WorkoutStepsArray"]
    assert w["ERGMode"] == "E_ON" and w["Id"] == 123
    assert w["StepCount"] == len(steps) == 1 + 6 + 1 + 1 + 1
    assert w["Time"] == 600 + 3 * 420 + 600 + 120 + 300
    assert [s["Id"] for s in steps] == list(range(1, len(steps) + 1))
    assert set(w["WorkoutSteps"]) == {str(i) for i in range(1, len(steps) + 1)}
    assert w["IsIntervals"] is True
    assert build_workout("t", blocks(), erg_mode=False)["ERGMode"] == "E_OFF"


def test_step_mapping():
    s = build_workout("t", blocks(), erg_mode=False)["WorkoutStepsArray"]
    assert s[0]["StepType"] == "E_WarmUp" and (s[0]["StartPower"], s[0]["EndPower"]) == (0.5, 0.75)
    on, off = s[1], s[2]
    assert (on["Power"], on["Time"], on["Rpm"], on["IntervalId"]) == (1.05, 240, 95, 1)
    assert (off["Power"], off["Time"], off["Rpm"], off["IntervalId"]) == (0.55, 180, 95, 1)
    assert on["WorkoutMessage"] == [{"Id": 1, "Time": 10, "Message": "push"}]
    assert off["WorkoutMessage"] == [{"Id": 1, "Time": 10, "Message": "rest"}]
    assert s[7]["StepType"] == "E_Normal" and s[7]["Power"] == 0.7
    assert s[8]["StepType"] == "E_FreeRide"
    assert s[9]["StepType"] == "E_CoolDown"


def test_metrics_steady_100pct():
    w = build_workout("t", [Block(type="steady", duration_s=3600, power=100)], erg_mode=True, ftp_watts=250)
    assert w["IF"] == 1.0 and w["TSS"] == 100.0 and w["KJ"] == 900.0


@pytest.mark.parametrize("kw", [
    {"type": "steady", "duration_s": 60},
    {"type": "ramp", "duration_s": 60, "start_power": 50},
    {"type": "interval", "duration_s": 60, "power": 100},
])
def test_validation(kw):
    with pytest.raises(ValidationError):
        Block(**kw)


def test_summarize_roundtrip():
    sm = summarize(build_workout("x", blocks(), erg_mode=True, workout_id=1))
    assert sm["erg_mode"] is True and sm["duration_min"] == 48.0
    assert sm["steps"][0] == "600s 50%->75%"
    assert sm["steps"][1] == "240s 105% @95rpm [interval 1]"


def test_ascii_transliteration():
    w = build_workout("Schwelle – 3×8′", [Block(type="steady", duration_s=60, power=90,
                      messages=[{"at_s": 5, "text": "Kontrollgröße: Fuß ruhig, Café"}])],
                      erg_mode=True, description="Watt nur Kontrollgröße, HF ≈ 140")
    assert w["Name"] == "Schwelle - 3x8'"
    assert w["Description"] == "Watt nur Kontrollgroesse, HF ~ 140"
    assert w["WorkoutStepsArray"][0]["WorkoutMessage"][0]["Message"] == "Kontrollgroesse: Fuss ruhig, Cafe"
    assert all(ord(c) < 128 for c in json.dumps(w, ensure_ascii=False))
