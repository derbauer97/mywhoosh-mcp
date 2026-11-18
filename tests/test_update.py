from mywhoosh_mcp.server import _content
from mywhoosh_mcp.workout import Block, build_workout


def _w(power: float, **kw) -> dict:
    return build_workout("W47 Do Rad 90 Z2", [Block(type="steady", duration_s=600, power=power)],
                         erg_mode=False, workout_id=1, **kw)


def test_content_detects_edits():
    assert _content(_w(70)) == _content(_w(70, ftp_watts=230))  # kJ only, not an edit
    assert _content(_w(70)) != _content(_w(75))
    assert _content(_w(70)) != _content(_w(70, description="Watt nur Kontrollgröße"))
    assert _content(_w(70)) != _content(_w(70) | {"ERGMode": "E_ON"})
