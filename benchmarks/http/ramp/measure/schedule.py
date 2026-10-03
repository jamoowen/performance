from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Stage:
    target_rps: int
    transition_seconds: int
    stable_seconds: int
    settling_seconds: int = 0

    @property
    def expected_arrivals(self) -> int:
        return self.target_rps * self.stable_seconds


def production_schedule() -> list[Stage]:
    return [Stage(300, 0, 160, 20), *[Stage(rps, 20, 160) for rps in (600, 900, 1200, 1500)]]


def schedule_hash(stages: list[Stage]) -> str:
    payload = json.dumps([asdict(stage) for stage in stages], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def classify(seconds: float, stages: list[Stage]) -> tuple[int, str]:
    elapsed = 0.0
    for stage in stages:
        elapsed += stage.transition_seconds
        if seconds < elapsed:
            return stage.target_rps, "transition"
        elapsed += stage.settling_seconds
        if seconds < elapsed:
            return stage.target_rps, "settling"
        elapsed += stage.stable_seconds
        if seconds < elapsed:
            return stage.target_rps, "stable"
    return 0, "drain"
