"""Read-only, restart-aware pod telemetry streamed over SSH.

This module deliberately keeps the pod cgroup separate from the container cgroup.
The former survives a container restart long enough to preserve OOM counters; the
latter is only used while a particular container identity exists.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import signal
import subprocess
import time

STOP_REQUESTED = False
SAFE_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
SAFE_UID = re.compile(r"^[0-9a-f-]{36}$")
SAFE_ID = re.compile(r"^[0-9a-f]{64}$")


def lines(path):
    with open(path, encoding="utf-8") as source:
        return {
            parts[0]: int(parts[1])
            for line in source
            if len(parts := line.split()) > 1 and parts[1].lstrip("-").isdigit()
        }


def text(path):
    with open(path, encoding="utf-8") as source:
        return source.read().strip()


def optional_int(path):
    try:
        return int(text(path))
    except FileNotFoundError:
        return None


def node_cpu():
    fields = (
        "user",
        "nice",
        "system",
        "idle",
        "iowait",
        "irq",
        "softirq",
        "steal",
        "guest",
        "guest_nice",
    )
    with open("/proc/stat", encoding="utf-8") as source:
        for line in source:
            parts = line.split()
            if parts and parts[0] == "cpu":
                return {field: int(value) for field, value in zip(fields, parts[1:], strict=False)}
    raise RuntimeError("node_cpu_missing")


def safe(value, pattern, label):
    if not pattern.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def cgroup_for(container_id):
    safe(container_id, SAFE_ID, "container ID")
    matches = glob.glob(
        f"/sys/fs/cgroup/kubepods.slice/**/cri-containerd-{container_id}.scope", recursive=True
    )
    if len(matches) != 1:
        raise RuntimeError(f"container_cgroup_match_count:{len(matches)}")
    return matches[0]


def pod_cgroup_for(container_root, uid):
    """Find the pod ancestor for *this* container and prove it names the UID."""
    suffix = f"pod{uid.replace('-', '_')}.slice"
    current = os.path.dirname(container_root)
    while current.startswith("/sys/fs/cgroup/"):
        if os.path.basename(current).endswith(suffix):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    raise RuntimeError("pod_cgroup_uid_mismatch")


def pod_identity(namespace, pod, container):
    raw = subprocess.check_output(
        ["kubectl", "get", "pod", pod, "-n", namespace, "-o", "json"], text=True, timeout=5
    )
    value = json.loads(raw)
    statuses = {item["name"]: item for item in value.get("status", {}).get("containerStatuses", [])}
    status = statuses.get(container, {})
    state = status.get("state", {})
    last_state = status.get("lastState", {})
    terminated = state.get("terminated", {}) or last_state.get("terminated", {})
    return {
        "uid": value.get("metadata", {}).get("uid"),
        "deleting": bool(value.get("metadata", {}).get("deletionTimestamp")),
        "ready": bool(status.get("ready")),
        "container_id": status.get("containerID", "").removeprefix("containerd://"),
        "restarts": status.get("restartCount", 0),
        "state": next(iter(state), None),
        "lastState": next(iter(last_state), None),
        "reason": terminated.get("reason"),
        "exitCode": terminated.get("exitCode"),
        "finishedAt": terminated.get("finishedAt"),
    }


def sample(root, scope):
    memory = lines(f"{root}/memory.stat")
    return {
        "kind": "sample" if scope == "pod" else "container_sample",
        "scope": scope,
        "realtime_ns": time.time_ns(),
        "monotonic_ns": time.monotonic_ns(),
        "cpu_stat": lines(f"{root}/cpu.stat"),
        "memory_current": int(text(f"{root}/memory.current")),
        "memory_peak": optional_int(f"{root}/memory.peak"),
        "inactive_file": memory.get("inactive_file", 0),
        "memory_events": lines(f"{root}/memory.events"),
        "cpu_pressure": text(f"{root}/cpu.pressure"),
        "memory_pressure": text(f"{root}/memory.pressure"),
        "node_cpu": node_cpu(),
    }


def emit(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)


def stamped(event_type, **details):
    return {
        "kind": "event",
        "type": event_type,
        "realtime_ns": time.time_ns(),
        "monotonic_ns": time.monotonic_ns(),
        **{key: value for key, value in details.items() if value is not None},
    }


def request_stop(_signal, _frame):
    global STOP_REQUESTED
    STOP_REQUESTED = True


def validate_args(args):
    safe(args.namespace, SAFE_NAME, "namespace")
    safe(args.pod, SAFE_NAME, "pod")
    safe(args.container, SAFE_NAME, "container")
    safe(args.uid, SAFE_UID, "uid")
    safe(args.container_id, SAFE_ID, "container ID")
    if args.duration <= 0 or args.interval < 1:
        raise ValueError("invalid duration or interval")


def initial_identity_error(identity, args):
    if identity["uid"] != args.uid or identity["deleting"]:
        return "initial_pod_identity_mismatch"
    if identity["container_id"] != args.container_id:
        return "initial_container_identity_mismatch"
    if not identity["ready"]:
        return "initial_container_not_ready"
    if identity["restarts"] != 0:
        return "initial_container_already_restarted"
    if identity["lastState"] == "terminated" or identity["reason"] or identity["finishedAt"]:
        return "initial_container_termination_present"
    return None


def lifecycle_event(previous, current):
    """Return a deduplicated Kubernetes lifecycle observation.

    Readiness is deliberately absent from this decision: a probe flap is not a
    workload restart. ``finishedAt`` plus the restart count identifies one
    terminated container even when Kubernetes reports it across several polls.
    """
    restarted = (
        current["container_id"] != previous["container_id"]
        or current["restarts"] > previous["restarts"]
    )
    termination_key = (current.get("restarts"), current.get("finishedAt"))
    previous_termination_key = (previous.get("restarts"), previous.get("finishedAt"))
    if current.get("reason") == "OOMKilled" and termination_key != previous_termination_key:
        return "oom", termination_key
    if restarted:
        return "restart", termination_key
    return None


def error_record(code):
    return {
        "kind": "error",
        "type": "error",
        "code": code,
        "realtime_ns": time.time_ns(),
        "monotonic_ns": time.monotonic_ns(),
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("namespace")
    parser.add_argument("pod")
    parser.add_argument("container")
    parser.add_argument("uid")
    parser.add_argument("container_id")
    parser.add_argument("duration", type=float)
    parser.add_argument("interval", type=float)
    args = parser.parse_args(argv)
    validate_args(args)
    signal.signal(signal.SIGTERM, request_stop)

    try:
        initial_root = cgroup_for(args.container_id)
        parent_root = pod_cgroup_for(initial_root, args.uid)
        identity = pod_identity(args.namespace, args.pod, args.container)
        initial_error = initial_identity_error(identity, args)
        if initial_error:
            raise RuntimeError(initial_error)
    except Exception as error:
        emit(error_record(str(error)))
        raise

    emit(
        {
            "kind": "metadata",
            "pid": os.getpid(),
            "scope": "pod",
            "podCgroup": parent_root,
            "initialContainerCgroup": initial_root,
            "identity": identity,
        }
    )
    known_container = args.container_id
    current_root = initial_root
    last_identity = identity
    last_oom = None
    next_identity = 0.0
    start = time.monotonic()
    try:
        while not STOP_REQUESTED and time.monotonic() - start < args.duration:
            now = time.monotonic()
            if now >= next_identity:
                identity = pod_identity(args.namespace, args.pod, args.container)
                if identity["uid"] != args.uid or identity["deleting"]:
                    emit(stamped("pod_replaced", reason="uid_changed_or_deleted"))
                    raise RuntimeError("pod_identity_changed")
                emit(
                    {
                        "kind": "identity",
                        "identity": identity,
                        "realtime_ns": time.time_ns(),
                        "monotonic_ns": time.monotonic_ns(),
                    }
                )
                lifecycle = lifecycle_event(last_identity, identity)
                if lifecycle:
                    event_type, _termination_key = lifecycle
                    emit(
                        stamped(
                            event_type,
                            reason=identity.get("reason"),
                            exitCode=identity.get("exitCode"),
                            restartCount=identity.get("restarts"),
                            containerId=identity.get("container_id"),
                            finishedAt=identity.get("finishedAt"),
                            source="kubernetes",
                            deathKey=f"kubernetes:{identity.get('restarts')}:{identity.get('finishedAt')}",
                        )
                    )
                if identity.get("container_id") and identity["container_id"] != known_container:
                    known_container = identity["container_id"]
                    try:
                        current_root = cgroup_for(known_container)
                    except RuntimeError:
                        current_root = None
                        emit(stamped("container_missing", containerId=known_container))
                last_identity = identity
                next_identity = now + 2.0

            parent = sample(parent_root, "pod")
            oom = parent["memory_events"].get("oom_kill", 0)
            if (last_oom is None and oom) or (last_oom is not None and oom > last_oom):
                emit(
                    stamped(
                        "oom",
                        reason="memory.events",
                        oomKills=oom if last_oom is None else oom - last_oom,
                        source="cgroup_memory_events",
                        observationOnly=True,
                    )
                )
            last_oom = oom
            emit(parent)
            if current_root:
                try:
                    container = sample(current_root, "container")
                    container["containerId"] = known_container
                    emit(container)
                except FileNotFoundError:
                    current_root = None
                    emit(stamped("container_missing", containerId=known_container))
            elif last_identity.get("ready"):
                try:
                    current_root = cgroup_for(known_container)
                except RuntimeError:
                    emit(stamped("collector_gap", reason="container_cgroup_unavailable"))
            time.sleep(args.interval)
        final = pod_identity(args.namespace, args.pod, args.container)
        if final["uid"] != args.uid or final["deleting"]:
            emit(stamped("pod_replaced", reason="uid_changed_or_deleted"))
            raise RuntimeError("pod_identity_changed")
        lifecycle = lifecycle_event(last_identity, final)
        if lifecycle:
            event_type, _termination_key = lifecycle
            emit(
                stamped(
                    event_type,
                    reason=final.get("reason"),
                    exitCode=final.get("exitCode"),
                    restartCount=final.get("restarts"),
                    containerId=final.get("container_id"),
                    finishedAt=final.get("finishedAt"),
                    source="kubernetes",
                    deathKey=f"kubernetes:{final.get('restarts')}:{final.get('finishedAt')}",
                )
            )
        emit(
            {
                "kind": "end",
                "identity": final,
                "stopped": STOP_REQUESTED,
                "realtime_ns": time.time_ns(),
                "monotonic_ns": time.monotonic_ns(),
            }
        )
    except Exception as error:
        emit(error_record(str(error)))
        raise


if __name__ == "__main__":
    main()
