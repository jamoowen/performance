"""Read-only program streamed to a Kubernetes node over SSH."""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import signal
import subprocess
import time

SAFE_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
SAFE_UID = re.compile(r"^[0-9a-f-]{36}$")
SAFE_ID = re.compile(r"^[0-9a-f]{64}$")
STOP_REQUESTED = False


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
        raise RuntimeError(f"cgroup_match_count:{len(matches)}")
    return matches[0]


def lines(path):
    with open(path, encoding="utf-8") as source:
        return {
            parts[0]: int(parts[1])
            for line in source
            if len(parts := line.split()) > 1 and parts[1].lstrip("-").isdigit()
        }


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


def text(path):
    with open(path, encoding="utf-8") as source:
        return source.read().strip()


def pod_identity(namespace, pod, container):
    raw = subprocess.check_output(
        ["kubectl", "get", "pod", pod, "-n", namespace, "-o", "json"], text=True
    )
    value = json.loads(raw)
    statuses = {item["name"]: item for item in value.get("status", {}).get("containerStatuses", [])}
    status = statuses.get(container, {})
    return {
        "uid": value.get("metadata", {}).get("uid"),
        "deleting": bool(value.get("metadata", {}).get("deletionTimestamp")),
        "ready": status.get("ready"),
        "container_id": status.get("containerID", "").removeprefix("containerd://"),
        "restarts": status.get("restartCount"),
    }


def sample(root):
    memory = lines(f"{root}/memory.stat")
    return {
        "kind": "sample",
        "realtime_ns": time.time_ns(),
        "monotonic_ns": time.monotonic_ns(),
        "cpu_stat": lines(f"{root}/cpu.stat"),
        "cpu_max": text(f"{root}/cpu.max"),
        "memory_max": text(f"{root}/memory.max"),
        "memory_current": int(text(f"{root}/memory.current")),
        "inactive_file": memory.get("inactive_file", 0),
        "memory_events": lines(f"{root}/memory.events"),
        "cpu_pressure": text(f"{root}/cpu.pressure"),
        "memory_pressure": text(f"{root}/memory.pressure"),
        "node_cpu": node_cpu(),
        "loadavg": text("/proc/loadavg"),
        "meminfo": lines("/proc/meminfo"),
        "node_cpu_pressure": text("/proc/pressure/cpu"),
        "cpu_frequency_khz": text("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
        if glob.glob("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
        else None,
    }


def emit(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)


def request_stop(_signal, _frame):
    global STOP_REQUESTED
    STOP_REQUESTED = True


def identity_matches(identity, args, initial_restarts):
    return (
        identity["uid"] == args.uid
        and identity["container_id"] == args.container_id
        and identity["restarts"] == initial_restarts
        and not identity["deleting"]
        and bool(identity["ready"])
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("namespace")
    parser.add_argument("pod")
    parser.add_argument("container")
    parser.add_argument("uid")
    parser.add_argument("container_id")
    parser.add_argument("duration", type=float)
    parser.add_argument("interval", type=float)
    args = parser.parse_args()
    safe(args.namespace, SAFE_NAME, "namespace")
    safe(args.pod, SAFE_NAME, "pod")
    safe(args.container, SAFE_NAME, "container")
    safe(args.uid, SAFE_UID, "uid")
    safe(args.container_id, SAFE_ID, "container ID")
    if args.duration <= 0 or args.interval < 1:
        raise ValueError("invalid duration or interval")
    signal.signal(signal.SIGTERM, request_stop)
    root = cgroup_for(args.container_id)
    initial_identity = pod_identity(args.namespace, args.pod, args.container)
    if not identity_matches(initial_identity, args, initial_identity["restarts"]):
        raise RuntimeError("identity_drift")
    start = time.monotonic()
    last_identity = 0
    emit(
        {
            "kind": "metadata",
            "pid": os.getpid(),
            "cgroup": root,
            "cpu_max": text(f"{root}/cpu.max"),
            "memory_max": text(f"{root}/memory.max"),
            "identity": initial_identity,
        }
    )
    try:
        while not STOP_REQUESTED and time.monotonic() - start < args.duration:
            now = time.monotonic()
            if now - last_identity >= 5:
                identity = pod_identity(args.namespace, args.pod, args.container)
                if not identity_matches(identity, args, initial_identity["restarts"]):
                    raise RuntimeError("identity_drift")
                emit({"kind": "identity", "identity": identity})
                last_identity = now
            emit(sample(root))
            time.sleep(args.interval)
        identity = pod_identity(args.namespace, args.pod, args.container)
        if not identity_matches(identity, args, initial_identity["restarts"]):
            raise RuntimeError("identity_drift")
        emit({"kind": "end", "identity": identity, "stopped": STOP_REQUESTED})
    except Exception as error:
        emit({"kind": "error", "code": str(error)})
        raise


if __name__ == "__main__":
    main()
