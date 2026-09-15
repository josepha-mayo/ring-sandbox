"""Scripted event sequences you can replay against the emulator.

A scenario is a list of steps with relative offsets. ``run`` posts each step to
``/_sandbox/events`` so history is written and webhooks are delivered exactly as they
would be in production.

Event *timestamps* always come from a virtual clock that starts ``length + 60s`` in the past,
so a 90-minute scenario produces events spanning 90 minutes that all sit before "now" (Ring
media endpoints reject future timestamps). ``speed`` only controls pacing: ``speed=60`` sleeps
1 real second per virtual minute; ``backdate=True`` (or ``speed=0``) sends everything at once.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx


@dataclass
class Step:
    offset_s: float
    type: str = "motion_detected"
    sub_type: str | None = None
    device: str | None = None  # device name or id; None = first camera
    duration_ms: int = 20_000


@dataclass
class Scenario:
    name: str
    description: str
    steps: list[Step] = field(default_factory=list)

    @property
    def length_s(self) -> float:
        return max((s.offset_s for s in self.steps), default=0.0)


BUILTIN: dict[str, Scenario] = {
    "delivery": Scenario(
        "delivery",
        "Courier walks up, leaves a package, drives off.",
        [
            Step(0, "motion_detected", "vehicle"),
            Step(8, "motion_detected", "human"),
            Step(14, "button_press"),
            Step(22, "motion_detected", "package"),
            Step(30, "motion_detected", "vehicle"),
        ],
    ),
    "home_aide_visit": Scenario(
        "home_aide_visit",
        "Caregiver arrives, rings, door opens/closes, stays ~90 min, leaves.",
        [
            Step(0, "motion_detected", "human"),
            Step(6, "button_press"),
            Step(20, "contact_sensor_faulted", device="Front Door Sensor"),
            Step(35, "contact_sensor_cleared", device="Front Door Sensor"),
            Step(90 * 60, "contact_sensor_faulted", device="Front Door Sensor"),
            Step(90 * 60 + 12, "contact_sensor_cleared", device="Front Door Sensor"),
            Step(90 * 60 + 15, "motion_detected", "human"),
        ],
    ),
    "short_visit": Scenario(
        "short_visit",
        "Worker arrives and leaves after 12 minutes (billing dispute case).",
        [
            Step(0, "motion_detected", "human"),
            Step(5, "button_press"),
            Step(18, "contact_sensor_faulted", device="Front Door Sensor"),
            Step(30, "contact_sensor_cleared", device="Front Door Sensor"),
            Step(12 * 60, "contact_sensor_faulted", device="Front Door Sensor"),
            Step(12 * 60 + 10, "contact_sensor_cleared", device="Front Door Sensor"),
            Step(12 * 60 + 14, "motion_detected", "human"),
        ],
    ),
    "no_show": Scenario(
        "no_show",
        "Only a vehicle passes; nobody comes to the door.",
        [Step(0, "motion_detected", "vehicle")],
    ),
    "device_flap": Scenario(
        "device_flap",
        "Doorbell drops offline and recovers.",
        [Step(0, "device_offline"), Step(45, "device_online")],
    ),
}


def load_yaml(path: str) -> Scenario:
    import yaml  # optional dependency (server extra)

    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    steps = [Step(**s) for s in raw.get("steps", [])]
    return Scenario(raw["name"], raw.get("description", ""), steps)


def _resolve_device(state: dict[str, Any], ref: str | None) -> str | None:
    if ref is None:
        return None
    for d in state["devices"]:
        if ref in (d["id"], d["name"]):
            return d["id"]
    raise KeyError(f"no device named/id {ref!r} in sandbox world")


def run(
    scenario: Scenario,
    base_url: str = "http://127.0.0.1:8787",
    *,
    speed: float = 1.0,
    backdate: bool = False,
    start: datetime | None = None,
    deliver: bool = True,
) -> list[dict[str, Any]]:
    """Play ``scenario`` against a running emulator; returns the injected webhook payloads."""
    out: list[dict[str, Any]] = []
    start = start or datetime.now(tz=UTC) - timedelta(seconds=scenario.length_s + 60)
    pace = not backdate and speed > 0
    with httpx.Client(base_url=base_url, timeout=10.0) as http:
        state = http.get("/_sandbox/state").json()
        t0 = time.monotonic()
        for step in sorted(scenario.steps, key=lambda s: s.offset_s):
            if pace:
                wait = step.offset_s / speed - (time.monotonic() - t0)
                if wait > 0:
                    time.sleep(wait)
            body: dict[str, Any] = {
                "device_id": _resolve_device(state, step.device),
                "type": step.type,
                "sub_type": step.sub_type,
                "at": (start + timedelta(seconds=step.offset_s)).isoformat(),
                "duration_ms": step.duration_ms,
                "deliver": deliver,
            }
            resp = http.post("/_sandbox/events", json=body)
            resp.raise_for_status()
            out.append(resp.json()["webhook"])
    return out
