"""The bounded, adaptive capacity-search protocol.

This module deliberately contains no deployment or k6 side effects, so the
rate and overload rules can be reviewed and tested independently.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Any

WARMUP_RPS = 100
WARMUP_SECONDS = 30
WARMUP_VUS = 256
MEASURED_VUS = 1024
COLLECTOR_DURATION_SECONDS = 3600
TRANSITION_SECONDS = 15
STABLE_SECONDS = 75
SAFETY_CEILING_RPS = 20_000
INITIAL_TARGETS = (300, 600, 900, 1200, 1500)


@dataclass(frozen=True)
class Step:
    target_rps: int
    transition_seconds: int = TRANSITION_SECONDS
    stable_seconds: int = STABLE_SECONDS
    settling_seconds: int = 0

    @property
    def vus(self) -> int:
        return MEASURED_VUS

    def as_k6_stage(self) -> dict[str, int]:
        return {
            "targetRps": self.target_rps,
            "transitionSeconds": self.transition_seconds,
            "stableSeconds": self.stable_seconds,
            "settlingSeconds": self.settling_seconds,
        }


def next_target(previous: int, ceiling: int = SAFETY_CEILING_RPS) -> int | None:
    """Return the next 25% step, rounded up, without exceeding ``ceiling``."""
    if previous <= 0 or ceiling <= 0 or previous >= ceiling:
        return None
    candidate = math.ceil(previous * 1.25)
    return min(candidate, ceiling)


def initial_steps() -> list[Step]:
    return [
        Step(rate, 0, STABLE_SECONDS, TRANSITION_SECONDS)
        if index == 0
        else Step(rate, TRANSITION_SECONDS)
        for index, rate in enumerate(INITIAL_TARGETS)
    ]


def step_hash(step: Step) -> str:
    return hashlib.sha256(
        json.dumps(asdict(step), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def protocol_hash(ceiling: int = SAFETY_CEILING_RPS) -> str:
    value: dict[str, Any] = {
        "warmup": {"rps": WARMUP_RPS, "seconds": WARMUP_SECONDS, "vus": WARMUP_VUS},
        "measuredVus": MEASURED_VUS,
        "collectorDurationSeconds": COLLECTOR_DURATION_SECONDS,
        "initialTargets": list(INITIAL_TARGETS),
        "transitionSeconds": TRANSITION_SECONDS,
        "stableSeconds": STABLE_SECONDS,
        "ceilingRps": ceiling,
        "httpTimeoutSeconds": 2,
        "gracefulStopSeconds": 3,
        "overload": {
            "stableWindowFailureFraction": 0.01,
            "consecutiveFullFiveSecondBuckets": 4,
            "dropExpectedArrivalsFraction": 0.01,
        },
        "latencySloP95Ms": 250,
        "generator": {
            "minimumCampaignDiskGiB": 20,
            "minimumStepDiskGiB": 2,
            "minimumStepMemoryBytes": 1024**3,
            "memoryPerVuMiB": 0.45,
            "minimumNofile": 8192,
            "fdBudget": "max(8192,ceil((2*vus+512)/0.8))",
            "fdStopFraction": 0.9,
            "threadStop": 2500,
            "lowMemoryStopBytes": 512 * 1024 * 1024,
            "hostCpuStopPercent": 95,
            "sustainedSeconds": 5,
        },
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def overload_from_history(history: list[dict[str, Any]], target_rps: int) -> dict[str, Any]:
    """Require four consecutive 5s stable buckets with >=1% errors/drops."""
    run = 0
    reasons: set[str] = set()
    expected = target_rps * 5
    previous_seconds: float | None = None
    for bucket in sorted(history, key=lambda value: value.get("seconds", 0)):
        if bucket.get("phase") != "stable":
            run = 0
            reasons.clear()
            previous_seconds = None
            continue
        seconds = bucket.get("seconds")
        if (
            bucket.get("bucketSeconds") != 5
            or not isinstance(seconds, (int, float))
            or (previous_seconds is not None and seconds - previous_seconds != 5)
        ):
            run = 0
            reasons.clear()
        previous_seconds = seconds if isinstance(seconds, (int, float)) else None
        completed = bucket.get("completed", 0) or 0
        failed = bucket.get("httpFailures", 0) + bucket.get("validationFailures", 0)
        drops = bucket.get("dropped", 0) or 0
        bucket_reasons = set()
        if completed and failed / completed >= 0.01:
            bucket_reasons.add("errors")
        if expected and drops / expected >= 0.01:
            bucket_reasons.add("drops")
        if bucket_reasons:
            run += 1
            reasons.update(bucket_reasons)
            if run >= 4:
                return {"status": True, "reasons": sorted(reasons), "buckets": run}
        else:
            run = 0
            reasons.clear()
    return {"status": False, "reasons": [], "buckets": run}


def overload_from_window(window: dict[str, Any]) -> bool:
    """A full stable window must itself reach the one-percent boundary."""
    completed = window.get("completed", 0) or 0
    failures = (window.get("httpFailures", 0) or 0) + (window.get("validationFailures", 0) or 0)
    expected = window.get("expectedArrivals", 0) or 0
    return bool(completed and failures / completed >= 0.01) or bool(
        expected and (window.get("dropped", 0) or 0) / expected >= 0.01
    )


def latency_failed(window: dict[str, Any]) -> bool:
    value = window.get("client", {}).get("p95Ms")
    return isinstance(value, (int, float)) and value > 250
