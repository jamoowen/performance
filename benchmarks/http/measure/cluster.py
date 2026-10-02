"""Read-only Kubernetes resource collector for the HTTP benchmark."""

import json
import math
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

DEPLOYMENTS = {"http-go", "http-bun", "http-rust"}
DNS_NAME = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")


def parse_quantity(value):
    """Convert Kubernetes CPU or byte quantities to a finite float."""
    match = re.fullmatch(
        r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)(n|u|m|Ki|Mi|Gi|Ti|K|M|G)?",
        str(value).strip(),
    )
    if not match:
        raise ValueError(f"invalid quantity: {value!r}")
    factors = {
        None: 1.0,
        "n": 1e-9,
        "u": 1e-6,
        "m": 1e-3,
        "Ki": 1024.0,
        "Mi": 1024.0**2,
        "Gi": 1024.0**3,
        "Ti": 1024.0**4,
        "K": 1000.0,
        "M": 1000.0**2,
        "G": 1000.0**3,
    }
    result = float(match.group(1)) * factors[match.group(2)]
    if not math.isfinite(result):
        raise ValueError(f"non-finite quantity: {value!r}")
    return result


def _labels(raw):
    labels, position = {}, 0
    while position < len(raw):
        key = re.match(r"[a-zA-Z_][a-zA-Z0-9_]*", raw[position:])
        if not key:
            return None
        name = key.group(0)
        position += len(name)
        if position >= len(raw) or raw[position : position + 2] != '="':
            return None
        position += 2
        value = []
        while position < len(raw) and raw[position] != '"':
            if raw[position] == "\\" and position + 1 < len(raw):
                position += 1
                value.append({"n": "\n", "\\": "\\", '"': '"'}.get(raw[position], raw[position]))
            else:
                value.append(raw[position])
            position += 1
        if position >= len(raw):
            return None
        labels[name] = "".join(value)
        position += 1
        if position == len(raw):
            break
        if raw[position] != ",":
            return None
        position += 1
    return labels


def exact_container_metric(line, metric, namespace, pod, container):
    """Return one matching Prometheus sample, or None for unrelated/malformed data."""
    match = re.fullmatch(
        r"([A-Za-z_:][A-Za-z0-9_:]*)(?:\{(.*)\})?\s+([^\s]+)(?:\s+(\d+))?\s*", line
    )
    if not match or match.group(1) != metric:
        return None
    labels = _labels(match.group(2) or "")
    if labels is None or (labels.get("namespace"), labels.get("pod"), labels.get("container")) != (
        namespace,
        pod,
        container,
    ):
        return None
    if (
        labels.get("container") in {"", "POD"}
        or labels.get("image") == ""
        or ("cpu" in labels and labels["cpu"] != "total")
    ):
        return None
    if match.group(4) is None:
        return None
    try:
        value = float(match.group(3))
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    return {"value": value, "timestamp_ms": int(match.group(4) or 0), "id": labels.get("id")}


def _validate(namespace, deployment, interval, mode):
    if not DNS_NAME.fullmatch(namespace):
        raise ValueError("invalid namespace")
    if deployment not in DEPLOYMENTS:
        raise ValueError("invalid deployment")
    seconds = float(interval)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("interval must be positive and finite")
    if mode not in {"metadata", "once", "stream"}:
        raise ValueError("invalid mode")
    return seconds


def ssh_command(host, namespace, deployment, interval, mode):
    _validate(namespace, deployment, interval, mode)
    if not host or host.startswith("-") or any(character.isspace() for character in host):
        raise ValueError("invalid SSH host")
    remote = shlex.join(["python3", "-u", "-", namespace, deployment, str(interval), mode])
    return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, remote]


def collector_source():
    """Read source lazily so it works after being piped to SSH stdin."""
    return Path(__file__).read_text(encoding="utf-8")


def kubectl(arguments):
    return subprocess.check_output(
        ["kubectl", "--request-timeout=10s", *arguments], text=True, timeout=15
    )


def _json(arguments):
    return json.loads(kubectl(arguments))


def _pod_summary(pod, deployment):
    status = next(
        (
            item
            for item in pod.get("status", {}).get("containerStatuses", [])
            if item.get("name") == deployment
        ),
        None,
    )
    if (
        not status
        or not status.get("ready")
        or not status.get("containerID")
        or "running" not in status.get("state", {})
    ):
        raise RuntimeError("selected pod is not ready")
    requested = next(
        (
            item.get("image")
            for item in pod.get("spec", {}).get("containers", [])
            if item.get("name") == deployment
        ),
        None,
    )
    return {
        "name": pod["metadata"]["name"],
        "uid": pod["metadata"]["uid"],
        "container_id": status["containerID"],
        "restart_count": status.get("restartCount", 0),
        "image": status.get("image"),
        "image_id": status.get("imageID"),
        "requested_image": requested,
        "started_at": status["state"]["running"].get("startedAt"),
        "last_termination_reason": status.get("lastState", {}).get("terminated", {}).get("reason"),
    }


def _workload(deployment):
    spec = deployment.get("spec", {})
    if spec.get("replicas", 1) != 1:
        raise RuntimeError("selected deployment must have one replica")
    name = deployment["metadata"]["name"]
    container = next(
        (
            item
            for item in spec.get("template", {}).get("spec", {}).get("containers", [])
            if item.get("name") == name
        ),
        None,
    )
    if container is None:
        raise RuntimeError("deployment container is missing")
    environment = {
        item.get("name"): item.get("value") for item in container.get("env", []) if "value" in item
    }
    return {
        "desired_replicas": 1,
        "resources": {
            key: container.get("resources", {}).get(key, {}) for key in ("requests", "limits")
        },
        "configuration": {
            "SEED_COUNT": environment.get("SEED_COUNT"),
            "MAX_OPEN_CONNS": environment.get("MAX_OPEN_CONNS", "1"),
            # Capture only benchmark/runtime knobs with literal values; never inspect Secrets.
            **{
                name: environment.get(name, "")
                for name in (
                    "BACKEND",
                    "ROUTER",
                    "WORKERS",
                    "DIAGNOSTICS",
                    "GOMAXPROCS",
                    "GOMEMLIMIT",
                    "GODEBUG",
                )
            },
        },
    }


def metadata(namespace, deployment):
    selected = _json(["get", "deployment", deployment, "-n", namespace, "-o", "json"])
    workload = _workload(selected)
    deployments = _json(["get", "deployments", "-n", namespace, "-o", "json"]).get("items", [])
    by_name = {item.get("metadata", {}).get("name"): item for item in deployments}
    for other_deployment in sorted(DEPLOYMENTS - {deployment}):
        other = by_name.get(other_deployment)
        # Rust is absent until its manifest is added. A missing disabled deployment
        # must not prevent recording an existing Go or Bun baseline.
        if other is None:
            continue
        if other.get("spec", {}).get("replicas", 1) != 0:
            raise RuntimeError("other benchmark deployment must have zero replicas")
    pods = _json(["get", "pods", "-n", namespace, "-o", "json"]).get("items", [])
    active = [
        pod
        for pod in pods
        if pod.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/name") in DEPLOYMENTS
        and (
            pod.get("metadata", {}).get("deletionTimestamp")
            or pod.get("status", {}).get("phase") in {"Running", "Pending"}
        )
    ]
    chosen = [
        pod
        for pod in active
        if pod.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/name") == deployment
        and not pod.get("metadata", {}).get("deletionTimestamp")
        and pod.get("status", {}).get("phase") == "Running"
    ]
    if len(active) != 1 or len(chosen) != 1:
        raise RuntimeError("expected exactly one active benchmark pod")
    pod = chosen[0]
    node_name = pod.get("spec", {}).get("nodeName")
    if not node_name:
        raise RuntimeError("selected pod has no node")
    node = _json(["get", "node", node_name, "-o", "json"])
    conditions = {
        item.get("type"): item.get("status")
        for item in node.get("status", {}).get("conditions", [])
    }
    return {
        "pod": _pod_summary(pod, deployment),
        "node": {
            "name": node["metadata"]["name"],
            "uid": node["metadata"]["uid"],
            "kubelet_version": node.get("status", {}).get("nodeInfo", {}).get("kubeletVersion"),
        },
        "workload": workload,
        "pressure": {
            key: conditions.get(key) for key in ("MemoryPressure", "DiskPressure", "PIDPressure")
        },
    }


def _metric(metrics, metric, namespace, pod, container, identifier):
    for line in metrics.splitlines():
        found = exact_container_metric(line, metric, namespace, pod, container)
        if found and found.get("id") == identifier:
            return found
    return None


def _runtime_id(container_id):
    runtime, separator, identifier = container_id.partition("://")
    if not runtime or not separator or not identifier:
        raise RuntimeError("selected container has an invalid runtime ID")
    return identifier


def _matches_runtime_id(cgroup_id, runtime_id):
    return isinstance(cgroup_id, str) and runtime_id in cgroup_id.split("/")[-1]


def sample(namespace, identity, deployment):
    node = identity["node"]["name"]
    node_usage = _json(["get", "--raw", f"/apis/metrics.k8s.io/v1beta1/nodes/{node}"])
    cadvisor = kubectl(["get", "--raw", f"/api/v1/nodes/{node}/proxy/metrics/cadvisor"])
    pod = identity["pod"]
    runtime_id = _runtime_id(pod["container_id"])
    cpu = None
    for line in cadvisor.splitlines():
        candidate = exact_container_metric(
            line, "container_cpu_usage_seconds_total", namespace, pod["name"], deployment
        )
        if candidate and _matches_runtime_id(candidate.get("id"), runtime_id):
            cpu = candidate
            break
    if not cpu or not cpu.get("id"):
        raise RuntimeError("essential CPU metric is missing")
    identifier = cpu["id"]
    values = {
        "cpu_seconds": cpu,
        "memory_working_set_bytes": _metric(
            cadvisor,
            "container_memory_working_set_bytes",
            namespace,
            pod["name"],
            deployment,
            identifier,
        ),
    }
    if not values["memory_working_set_bytes"]:
        raise RuntimeError("essential memory metric is missing")
    for key, metric in {
        "cfs_periods": "container_cpu_cfs_periods_total",
        "cfs_throttled_periods": "container_cpu_cfs_throttled_periods_total",
        "cfs_throttled_seconds": "container_cpu_cfs_throttled_seconds_total",
        "memory_rss_bytes": "container_memory_rss",
    }.items():
        values[key] = _metric(cadvisor, metric, namespace, pod["name"], deployment, identifier)
    usage = node_usage.get("usage", {})
    event = {
        "type": "sample",
        "observed_at": time.time(),
        "pod_uid": pod["uid"],
        "container_id": pod["container_id"],
        "cgroup_id": identifier,
        "node_usage": {
            "cpu_cores": parse_quantity(usage["cpu"]),
            "memory_bytes": parse_quantity(usage["memory"]),
        },
        "node_timestamp": node_usage.get("timestamp"),
        "node_window": node_usage.get("window"),
        "warnings": [
            key
            for key, value in values.items()
            if value is None and key not in {"cpu_seconds", "memory_working_set_bytes"}
        ],
    }
    timestamps = {
        "cpu_seconds": "cpu_timestamp_ms",
        "cfs_periods": "cfs_periods_timestamp_ms",
        "cfs_throttled_periods": "cfs_throttled_periods_timestamp_ms",
        "cfs_throttled_seconds": "cfs_throttled_seconds_timestamp_ms",
        "memory_working_set_bytes": "memory_working_set_timestamp_ms",
        "memory_rss_bytes": "memory_rss_timestamp_ms",
    }
    for key, value in values.items():
        event[key] = value["value"] if value else None
        event[timestamps[key]] = value["timestamp_ms"] if value else None
    return event


def _emit(event):
    print(json.dumps(event, separators=(",", ":")), flush=True)


def main():
    try:
        if len(sys.argv) != 5:
            raise ValueError("usage: cluster.py namespace deployment interval mode")
        namespace, deployment, interval, mode = sys.argv[1:]
        seconds = _validate(namespace, deployment, interval, mode)
        identity = metadata(namespace, deployment)
        _emit({"type": "metadata", "metadata": identity})
        if mode == "metadata":
            return
        _emit(sample(namespace, identity, deployment))
        if mode == "once":
            return
        while True:
            time.sleep(seconds)
            _emit(sample(namespace, identity, deployment))
    except BrokenPipeError:
        return
    except Exception as error:
        try:
            _emit({"type": "error", "error": str(error)})
        except BrokenPipeError:
            pass
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
