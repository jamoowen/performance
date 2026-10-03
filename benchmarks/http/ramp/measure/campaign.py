"""Run the SQLite framework ramp through the narrow Flux GitOps surface.

The command is dry-run by default.  ``--execute`` is deliberately required
before it writes Git, requests reconciliation, starts k6, or contacts the node.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import resource
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

RUNTIMES = ("go", "node", "bun", "rust", "python", "elixir")
VARIANTS = (
    ("go", "nethttp"),
    ("node", "express"),
    ("bun", "native"),
    ("rust", "axum"),
    ("python", "fastapi"),
    ("elixir", "plug"),
    ("go", "chi"),
    ("node", "fastify"),
    ("bun", "hono"),
    ("rust", "actix"),
    ("elixir", "phoenix"),
    ("go", "fiber"),
    ("node", "nest"),
    ("bun", "elysia"),
    ("rust", "rocket"),
)
IMAGE = re.compile(r"^ghcr\.io/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
SAFE_HOST = re.compile(r"^[A-Za-z0-9._@-]+$")
SAFE_IP = re.compile(
    r"^(?:25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})(?:\.(?:25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})){3}$"
)
SAFE_NAMESPACE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
APP_PATH = Path("apps/performance-http-ramp/deployment.yaml")
OLD_PATHS = tuple(
    Path(f"apps/performance-http/{runtime}-deployment.yaml") for runtime in ("go", "bun", "rust")
)
ALLOWED_PATHS = frozenset((APP_PATH, *OLD_PATHS))
PRODUCTION_SCHEDULE = [
    {"targetRps": 300, "transitionSeconds": 0, "stableSeconds": 160, "settlingSeconds": 20},
    *[
        {"targetRps": rate, "transitionSeconds": 20, "stableSeconds": 160}
        for rate in (600, 900, 1200, 1500)
    ],
]
MIN_AVAILABLE_MEMORY_BYTES = 4 * 1024**3
MIN_FREE_DISK_BYTES = 20 * 1024**3
MIN_NOFILE = 8192
LOAD_SCRIPT = Path(__file__).resolve().parents[1] / "load.js"


@dataclass(frozen=True)
class Variant:
    order: int
    runtime: str
    framework: str


def load_hash() -> str:
    return hashlib.sha256(LOAD_SCRIPT.read_bytes()).hexdigest()


def schedule_hash(schedule: list[dict[str, int]]) -> str:
    return hashlib.sha256(
        json.dumps(schedule, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def attempt_identity(
    variant: Variant,
    image: str,
    source_revision: str,
    harness_source_revision: str,
    schedule: list[dict[str, int]],
) -> str:
    value = {
        "variant": asdict(variant),
        "image": image,
        "sourceRevision": source_revision,
        "loadHash": load_hash(),
        "scheduleHash": schedule_hash(schedule),
        "harnessSourceRevision": harness_source_revision,
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _yaml():
    import yaml

    return yaml


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster-repo", type=Path, required=True)
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--node-ip", required=True)
    parser.add_argument("--image-map", type=Path)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--harness-source-revision")
    parser.add_argument("--flux-revision")
    parser.add_argument("--namespace", default="my-api")
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--k6", default="k6")
    parser.add_argument("--schedule-json", type=Path)
    parser.add_argument("--attempt-id")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--local-only", action="store_true")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if args.ssh_host.startswith("-") or not SAFE_HOST.fullmatch(args.ssh_host):
        parser.error("--ssh-host has unsafe characters")
    if not SAFE_IP.fullmatch(args.node_ip):
        parser.error("--node-ip must be an IPv4 address")
    if not SAFE_NAMESPACE.fullmatch(args.namespace):
        parser.error("--namespace must be a DNS label")
    if args.execute and args.image_map is None:
        parser.error("--execute requires --image-map")
    if args.resume and not args.execute:
        parser.error("--resume requires --execute")
    if args.harness_source_revision is None:
        args.harness_source_revision = args.source_revision
    if args.execute and (
        not GIT_SHA.fullmatch(args.source_revision)
        or not GIT_SHA.fullmatch(args.harness_source_revision)
    ):
        parser.error("--execute requires immutable 40-character source revisions")
    if args.execute and args.attempt_id:
        parser.error("--attempt-id cannot be fixed for a full campaign")
    return args


def schedule_for(args: argparse.Namespace) -> list[dict[str, int]]:
    if args.schedule_json is None:
        return PRODUCTION_SCHEDULE
    try:
        value = json.loads(args.schedule_json.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("--schedule-json must contain a JSON stage array") from error
    if not isinstance(value, list) or not value:
        raise RuntimeError("--schedule-json must contain a non-empty stage array")
    required = {"targetRps", "transitionSeconds", "stableSeconds"}
    allowed = required | {"settlingSeconds"}
    if any(
        not isinstance(stage, dict) or not required <= stage.keys() or set(stage) - allowed
        for stage in value
    ):
        raise RuntimeError(
            "--schedule-json stages must contain targetRps, transitionSeconds, stableSeconds"
        )
    for stage in value:
        for name in stage:
            number = stage[name]
            if isinstance(number, bool) or not isinstance(number, int) or number < 0:
                raise RuntimeError("--schedule-json values must be non-negative integers")
        if stage["targetRps"] == 0 or stage["stableSeconds"] == 0:
            raise RuntimeError("--schedule-json targetRps and stableSeconds must be positive")
    if any(
        current["targetRps"] >= following["targetRps"]
        for current, following in zip(value, value[1:], strict=False)
    ):
        raise RuntimeError("--schedule-json targetRps levels must be strictly ascending")
    if (
        sum(
            stage["transitionSeconds"] + stage["stableSeconds"] + stage.get("settlingSeconds", 0)
            for stage in value
        )
        > 1800
    ):
        raise RuntimeError("--schedule-json duration must not exceed 1800 seconds")
    return value


def image_map(path: Path) -> dict[str, str]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("--image-map must be a JSON object") from error
    if (
        set(value) != set(RUNTIMES)
        or any(
            not isinstance(value[key], str)
            or not IMAGE.fullmatch(value[key])
            or not value[key].startswith(f"ghcr.io/jamoowen/performance-http-ramp-{key}@sha256:")
            or value[key].endswith("0" * 64)
            for key in RUNTIMES
        )
        or len(set(value.values())) != len(RUNTIMES)
    ):
        raise RuntimeError(
            "--image-map must contain one exact GHCR sha256 digest for every runtime"
        )
    return value


def _psutil():
    import psutil

    return psutil


def generator_preflight(results_dir: Path) -> dict[str, int]:
    """Validate the 3200-VU generator without changing the host configuration."""
    results_dir.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(results_dir)
    available_memory = _psutil().virtual_memory().available
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    child_limit = child_nofile_limit(hard)
    if disk.free < MIN_FREE_DISK_BYTES:
        raise RuntimeError("generator preflight requires at least 20 GiB free disk")
    if available_memory < MIN_AVAILABLE_MEMORY_BYTES:
        raise RuntimeError(
            "generator preflight requires at least 4 GiB available memory for 3200 VUs"
        )
    if child_limit < MIN_NOFILE:
        raise RuntimeError("generator preflight requires RLIMIT_NOFILE hard limit of at least 8192")
    return {
        "diskFreeBytes": disk.free,
        "availableMemoryBytes": available_memory,
        "nofileSoftBefore": soft,
        "nofileHard": hard,
        "nofileChild": child_limit,
        "preallocatedVus": 3200,
    }


def recorder_preexec() -> None:
    """Raise only the recorder child descriptor limit, never the host limit."""
    _soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (child_nofile_limit(hard), hard))


def child_nofile_limit(hard: int) -> int:
    if hard == resource.RLIM_INFINITY or hard < 0:
        return MIN_NOFILE
    return min(MIN_NOFILE, hard)


def plan(args: argparse.Namespace, images: dict[str, str] | None = None) -> dict[str, Any]:
    schedule = schedule_for(args)
    variants = [
        Variant(index + 1, runtime, framework)
        for index, (runtime, framework) in enumerate(VARIANTS)
    ]
    return {
        "mode": "execute" if args.execute else "dry-run",
        "durationSeconds": sum(
            stage["transitionSeconds"] + stage["stableSeconds"] + stage.get("settlingSeconds", 0)
            for stage in schedule
        ),
        "warmupSeconds": 60,
        "schedule": schedule,
        "loadHash": load_hash(),
        "scheduleHash": schedule_hash(schedule),
        "runs": [
            {
                **asdict(variant),
                "image": images[variant.runtime] if images else None,
                "loadHash": load_hash(),
                "scheduleHash": schedule_hash(schedule),
                "sourceRevision": args.source_revision,
                "harnessSourceRevision": args.harness_source_revision,
                "identity": attempt_identity(
                    variant,
                    images[variant.runtime],
                    args.source_revision,
                    args.harness_source_revision,
                    schedule,
                )
                if images
                else None,
            }
            for variant in variants
        ],
    }


def _git(repo: Path, *command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *command], check=check, text=True, capture_output=True
    )


def safe_cluster_repo(repo: Path) -> Path:
    resolved = repo.resolve()
    if (
        not resolved.is_absolute()
        or "ephemeral" not in resolved.parts
        or not (resolved / ".git").is_dir()
    ):
        raise RuntimeError("--cluster-repo must be an isolated ephemeral Git clone")
    if _git(resolved, "status", "--porcelain").stdout.strip():
        raise RuntimeError("cluster clone must be clean before a campaign")
    if not (resolved / APP_PATH).is_file():
        raise RuntimeError("cluster clone lacks the SQLite ramp deployment")
    return resolved


def snapshot(repo: Path) -> dict[Path, bytes]:
    paths = (APP_PATH, *OLD_PATHS)
    missing = [str(path) for path in paths if not (repo / path).is_file()]
    if missing:
        raise RuntimeError("missing baseline benchmark deployment(s): " + ", ".join(missing))
    return {path: (repo / path).read_bytes() for path in paths}


def persist_baseline(args: argparse.Namespace, baseline: dict[Path, bytes]) -> Path:
    path = args.results_dir / "campaign-baseline.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {
        str(name): base64.b64encode(contents).decode("ascii") for name, contents in baseline.items()
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"files": value}, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)
    return path


def load_persisted_baseline(args: argparse.Namespace) -> dict[Path, bytes]:
    path = args.results_dir / "campaign-baseline.json"
    try:
        encoded = json.loads(path.read_text())["files"]
        baseline = {
            Path(name): base64.b64decode(contents, validate=True)
            for name, contents in encoded.items()
        }
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("--resume requires a valid persisted campaign baseline") from error
    if set(baseline) != set(ALLOWED_PATHS):
        raise RuntimeError("persisted campaign baseline contains unexpected paths")
    return baseline


def restore_bytes(repo: Path, baseline: dict[Path, bytes]) -> None:
    for path, contents in baseline.items():
        (repo / path).write_bytes(contents)
    _git(repo, "diff", "--check")


def _env(container: dict[str, Any], values: dict[str, str]) -> None:
    existing = {
        item.get("name"): item for item in container.setdefault("env", []) if isinstance(item, dict)
    }
    for name, value in values.items():
        item = existing.get(name)
        if item is None:
            container["env"].append({"name": name, "value": value})
        else:
            item.clear()
            item.update({"name": name, "value": value})


def set_variant(repo: Path, variant: Variant, image: str, attempt_id: str) -> None:
    reject_remote_benchmark_conflict(repo)
    yaml = _yaml()
    for path in OLD_PATHS:
        document = yaml.safe_load((repo / path).read_text())
        document["spec"]["replicas"] = 0
        (repo / path).write_text(yaml.safe_dump(document, sort_keys=False))
    ramp = yaml.safe_load((repo / APP_PATH).read_text())
    ramp["spec"]["replicas"] = 1
    template = ramp["spec"]["template"]
    template.setdefault("metadata", {}).setdefault("annotations", {})[
        "benchmark.jamoowen.dev/attempt-id"
    ] = attempt_id
    container = template["spec"]["containers"][0]
    container["image"] = image
    _env(
        container,
        {
            "FRAMEWORK": variant.framework,
            "PORT": "8080",
            "SEED_COUNT": "5000",
            "SQLITE_PATH": "/data/benchmark.sqlite",
            "GOMAXPROCS": "1",
            "NODE_ENV": "production",
            "ERL_FLAGS": "+S 1:1 +SDcpu 1 +SDio 1",
            "RELEASE_DISTRIBUTION": "none",
            "RELEASE_TMP": "/tmp/ramp",
        },
    )
    _git(repo, "diff", "--check")
    changed = {Path(path) for path in _git(repo, "diff", "--name-only").stdout.splitlines()}
    if not changed <= ALLOWED_PATHS:
        raise RuntimeError("campaign changed a path outside the benchmark deployment allowlist")


def _commit_push(repo: Path, message: str) -> str:
    changed = [path for path in _git(repo, "diff", "--name-only").stdout.splitlines() if path]
    if not changed or not {Path(path) for path in changed} <= ALLOWED_PATHS:
        raise RuntimeError("refusing a GitOps commit outside the benchmark deployment allowlist")
    subprocess.run(
        ["kubectl", "kustomize", "clusters/optiplex"],
        cwd=repo,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    _git(repo, "add", "--", *changed)
    _git(repo, "commit", "-m", message)
    first = _git(repo, "push", "origin", "HEAD:main", check=False)
    if first.returncode:
        _git(repo, "fetch", "origin", "main")
        changed = _remote_benchmark_changes(repo)
        if changed:
            raise RuntimeError(
                "remote benchmark manifest changed concurrently; refusing to rebase over it"
            )
        try:
            _git(repo, "rebase", "origin/main")
        except subprocess.CalledProcessError as error:
            _git(repo, "rebase", "--abort", check=False)
            raise RuntimeError(
                "external update conflicts with a benchmark manifest; recovery required"
            ) from error
        if _git(repo, "push", "origin", "HEAD:main", check=False).returncode:
            raise RuntimeError("could not push benchmark commit after one rebase")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def reject_remote_benchmark_conflict(repo: Path) -> None:
    _git(repo, "fetch", "origin", "main")
    changed = _remote_benchmark_changes(repo)
    if changed:
        raise RuntimeError(
            "remote benchmark manifest changed concurrently; refusing to overwrite it"
        )


def _remote_benchmark_changes(repo: Path) -> list[str]:
    """Return allowlisted changes made by remote since the branches diverged."""
    merge_base = _git(repo, "merge-base", "HEAD", "origin/main").stdout.strip()
    if not merge_base:
        raise RuntimeError("could not determine the remote branch merge base")
    return _git(
        repo,
        "diff",
        "--name-only",
        f"{merge_base}..origin/main",
        "--",
        *map(str, ALLOWED_PATHS),
    ).stdout.splitlines()


def _ssh_kubectl(args: argparse.Namespace, *command: str) -> list[str]:
    if args.ssh_host.startswith("-") or not SAFE_HOST.fullmatch(args.ssh_host):
        raise RuntimeError("--ssh-host has unsafe characters")
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        args.ssh_host,
        shlex.join(["kubectl", "--request-timeout=10s", *command]),
    ]


def _remote_json(args: argparse.Namespace, *command: str) -> dict[str, Any]:
    result = subprocess.run(
        _ssh_kubectl(args, *command),
        check=True,
        text=True,
        capture_output=True,
        timeout=20,
    )
    return json.loads(result.stdout)


def _request_reconcile(args: argparse.Namespace) -> None:
    timestamp = str(int(time.time()))
    for flux_resource in ("gitrepository/flux-system", "kustomization/flux-system"):
        subprocess.run(
            _ssh_kubectl(
                args,
                "-n",
                "flux-system",
                "annotate",
                flux_resource,
                f"reconcile.fluxcd.io/requestedAt={timestamp}",
                "--overwrite",
            ),
            check=True,
            text=True,
            timeout=20,
        )


def revision_applied(args: argparse.Namespace, wanted: str, applied: str) -> bool:
    if wanted in applied:
        return True
    match = re.search(r"[0-9a-f]{40}", applied)
    if not match:
        return False
    _git(args.cluster_repo, "fetch", "origin", "main")
    return (
        _git(
            args.cluster_repo, "merge-base", "--is-ancestor", wanted, match.group(), check=False
        ).returncode
        == 0
    )


def _expected_environment(variant: Variant) -> dict[str, str]:
    return {
        "FRAMEWORK": variant.framework,
        "PORT": "8080",
        "SEED_COUNT": "5000",
        "SQLITE_PATH": "/data/benchmark.sqlite",
        "GOMAXPROCS": "1",
        "NODE_ENV": "production",
        "ERL_FLAGS": "+S 1:1 +SDcpu 1 +SDio 1",
        "RELEASE_DISTRIBUTION": "none",
        "RELEASE_TMP": "/tmp/ramp",
    }


def _container_matches_contract(container: dict[str, Any], variant: Variant, image: str) -> bool:
    environment = {item.get("name"): str(item.get("value")) for item in container.get("env", [])}
    resources = container.get("resources", {})
    return (
        container.get("image") == image
        and all(
            environment.get(key) == value for key, value in _expected_environment(variant).items()
        )
        and all(
            resources.get(kind, {}).get("cpu") == "1"
            and resources.get(kind, {}).get("memory") == "512Mi"
            for kind in ("requests", "limits")
        )
    )


def _ready(pod: dict[str, Any], variant: Variant, attempt_id: str, image: str) -> bool:
    statuses = pod.get("status", {}).get("containerStatuses", [])
    return (
        len(statuses) == 1
        and statuses[0].get("ready") is True
        and statuses[0].get("restartCount") == 0
        and bool(statuses[0].get("containerID"))
        and statuses[0].get("image") == image
        and len(pod.get("spec", {}).get("containers", [])) == 1
        and _container_matches_contract(pod["spec"]["containers"][0], variant, image)
        and pod.get("status", {}).get("phase") == "Running"
        and any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in pod.get("status", {}).get("conditions", [])
        )
        and pod.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/name") == "http-ramp"
        and bool(pod.get("metadata", {}).get("uid"))
        and pod.get("metadata", {}).get("annotations", {}).get("benchmark.jamoowen.dev/attempt-id")
        == attempt_id
        and not pod.get("metadata", {}).get("deletionTimestamp")
    )


def _pod_runtime_ready(pod: dict[str, Any], app_name: str) -> bool:
    statuses = pod.get("status", {}).get("containerStatuses", [])
    return (
        len(statuses) == 1
        and statuses[0].get("ready") is True
        and pod.get("status", {}).get("phase") == "Running"
        and any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in pod.get("status", {}).get("conditions", [])
        )
        and not pod.get("metadata", {}).get("deletionTimestamp")
        and pod.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/name") == app_name
    )


def strict_readiness(
    deployment: dict[str, Any],
    pods: list[dict[str, Any]],
    old_deployments: list[dict[str, Any]],
    variant: Variant,
    image: str,
    attempt_id: str,
) -> tuple[bool, str]:
    spec = deployment.get("spec", {})
    observed = deployment.get("status", {}).get("observedGeneration", 0)
    if (
        spec.get("replicas") != 1
        or observed < deployment.get("metadata", {}).get("generation", 1)
        or not any(
            condition.get("type") == "Available" and condition.get("status") == "True"
            for condition in deployment.get("status", {}).get("conditions", [])
        )
    ):
        return False, "deployment_not_observed"
    template = spec.get("template", {})
    annotations = template.get("metadata", {}).get("annotations", {})
    container = next(iter(template.get("spec", {}).get("containers", [])), {})
    if annotations.get(
        "benchmark.jamoowen.dev/attempt-id"
    ) != attempt_id or not _container_matches_contract(container, variant, image):
        return False, "deployment_contract_mismatch"
    if any(item.get("spec", {}).get("replicas") != 0 for item in old_deployments):
        return False, "old_benchmark_deployment_still_desired"
    if len(pods) != 1 or not _ready(pods[0], variant, attempt_id, image):
        return False, "expected_exactly_one_ready_nonterminating_benchmark_pod"
    return True, "ready"


def wait_for_flux(
    args: argparse.Namespace,
    revision: str,
    variant: Variant,
    image: str,
    attempt_id: str,
    timeout: int = 300,
) -> dict[str, Any]:
    _request_reconcile(args)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        flux = _remote_json(
            args, "-n", "flux-system", "get", "kustomization", "flux-system", "-o", "json"
        )
        applied = flux.get("status", {}).get("lastAppliedRevision", "")
        deployment = _remote_json(
            args, "-n", args.namespace, "get", "deployment", "http-ramp", "-o", "json"
        )
        old_deployments = [
            _remote_json(
                args, "-n", args.namespace, "get", "deployment", f"http-{runtime}", "-o", "json"
            )
            for runtime in ("go", "bun", "rust")
        ]
        pod_data = _remote_json(args, "-n", args.namespace, "get", "pods", "-o", "json")
        benchmark_pods = [
            pod
            for pod in pod_data.get("items", [])
            if pod.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/name")
            in {"http-ramp", "http-go", "http-bun", "http-rust"}
        ]
        ready, reason = strict_readiness(
            deployment, benchmark_pods, old_deployments, variant, image, attempt_id
        )
        if revision_applied(args, revision, applied) and ready:
            return {"deployment": deployment, "pods": benchmark_pods, "fluxRevision": applied}
        time.sleep(3)
    raise RuntimeError("Flux rollout did not meet the strict benchmark readiness contract")


def _deployment_baseline_matches(live: dict[str, Any], expected: dict[str, Any]) -> bool:
    live_spec = live.get("spec", {})
    expected_spec = expected.get("spec", {})
    live_container = next(
        iter(live_spec.get("template", {}).get("spec", {}).get("containers", [])), {}
    )
    expected_container = next(
        iter(expected_spec.get("template", {}).get("spec", {}).get("containers", [])), {}
    )
    live_env = {item.get("name"): item.get("value") for item in live_container.get("env", [])}
    expected_env = {
        item.get("name"): item.get("value") for item in expected_container.get("env", [])
    }
    replicas = expected_spec.get("replicas")
    return (
        live_spec.get("replicas") == expected_spec.get("replicas")
        and live.get("status", {}).get("observedGeneration", 0)
        >= live.get("metadata", {}).get("generation", 1)
        and live_container.get("image") == expected_container.get("image")
        and live_env == expected_env
        and (
            not replicas
            or any(
                condition.get("type") == "Available" and condition.get("status") == "True"
                for condition in live.get("status", {}).get("conditions", [])
            )
        )
    )


def wait_for_baseline(
    args: argparse.Namespace, revision: str, baseline: dict[Path, bytes], timeout: int = 300
) -> None:
    yaml = _yaml()
    expected = {path: yaml.safe_load(contents) for path, contents in baseline.items()}
    _request_reconcile(args)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        flux = _remote_json(
            args, "-n", "flux-system", "get", "kustomization", "flux-system", "-o", "json"
        )
        ramp = _remote_json(
            args, "-n", args.namespace, "get", "deployment", "http-ramp", "-o", "json"
        )
        old = {
            path: _remote_json(
                args, "-n", args.namespace, "get", "deployment", f"http-{runtime}", "-o", "json"
            )
            for path, runtime in zip(OLD_PATHS, ("go", "bun", "rust"), strict=True)
        }
        pods = _remote_json(args, "-n", args.namespace, "get", "pods", "-o", "json").get(
            "items", []
        )
        ramp_pods = [
            pod
            for pod in pods
            if pod.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/name")
            == "http-ramp"
        ]
        old_pods_match = True
        for path, runtime in zip(OLD_PATHS, ("go", "bun", "rust"), strict=True):
            expected_container = next(
                iter(
                    expected[path]
                    .get("spec", {})
                    .get("template", {})
                    .get("spec", {})
                    .get("containers", [])
                ),
                {},
            )
            runtime_pods = [
                pod
                for pod in pods
                if pod.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/name")
                == f"http-{runtime}"
            ]
            replicas = expected[path].get("spec", {}).get("replicas", 0)
            old_pods_match = (
                old_pods_match
                and len(runtime_pods) == replicas
                and all(
                    _pod_runtime_ready(pod, f"http-{runtime}")
                    and pod.get("status", {}).get("containerStatuses", [{}])[0].get("image")
                    == expected_container.get("image")
                    for pod in runtime_pods
                )
            )
        if (
            revision_applied(args, revision, flux.get("status", {}).get("lastAppliedRevision", ""))
            and _deployment_baseline_matches(ramp, expected[APP_PATH])
            and all(_deployment_baseline_matches(old[path], expected[path]) for path in OLD_PATHS)
            and not ramp_pods
            and old_pods_match
        ):
            return
        time.sleep(3)
    raise RuntimeError("baseline restoration did not reconcile through Flux")


def recorder_command(
    args: argparse.Namespace,
    variant: Variant,
    image: str,
    attempt_id: str,
    schedule: list[dict[str, int]],
    flux_revision: str,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "benchmarks.http.ramp.measure.record",
        "--runtime",
        variant.runtime,
        "--framework",
        variant.framework,
        "--image",
        image,
        "--source-revision",
        args.source_revision,
        "--harness-source-revision",
        args.harness_source_revision,
        "--flux-revision",
        flux_revision,
        "--attempt-id",
        attempt_id,
        "--base-url",
        f"http://{args.node_ip}:30083",
        "--namespace",
        args.namespace,
        "--results-dir",
        str(args.results_dir),
        "--k6",
        args.k6,
        "--warmup-rps",
        "100",
        "--warmup-seconds",
        "60",
        "--preallocated-vus",
        "3200",
        "--max-vus",
        "3200",
        "--schedule-json",
        json.dumps(schedule, separators=(",", ":")),
    ]
    if args.local_only:
        command.append("--local-only")
    else:
        command.extend(("--ssh-host", args.ssh_host))
    return command


def journal_file(args: argparse.Namespace) -> Path:
    return args.results_dir / "campaign-journal.json"


def _load_journal(args: argparse.Namespace) -> dict[str, Any]:
    path = journal_file(args)
    return json.loads(path.read_text()) if path.exists() else {"runs": []}


def _write_journal(args: argparse.Namespace, value: dict[str, Any]) -> None:
    path = journal_file(args)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def _recovery(args: argparse.Namespace, baseline: dict[Path, bytes], error: BaseException) -> None:
    path = args.results_dir / "RECOVERY.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "error": str(error),
                "baselineFile": str(args.results_dir / "campaign-baseline.json"),
                "restorePaths": [str(path) for path in baseline],
                "required": "restore exact baseline bytes and wait for Flux reconciliation",
            },
            indent=2,
        )
        + "\n"
    )


def validate_result(args: argparse.Namespace, row: dict[str, Any], attempt_id: str) -> Path:
    path = args.results_dir / attempt_id / "result.json"
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(
            "recorder did not write a readable result.json for this attempt"
        ) from error
    metadata = value.get("metadata", {})
    expected = {
        "attemptId": attempt_id,
        "runtime": row["runtime"],
        "framework": row["framework"],
        "image": row["image"],
        "sourceRevision": args.source_revision,
        "harnessSourceRevision": args.harness_source_revision,
        "loadHash": row["loadHash"],
        "scheduleHash": row["scheduleHash"],
    }
    if any(metadata.get(key) != value for key, value in expected.items()):
        raise RuntimeError("recorder result metadata does not match the campaign identity")
    counts = value.get("counts", {})
    if value.get("validity", {}).get("status") != "valid":
        raise RuntimeError("recorder result is not a valid measurement")
    if (
        not isinstance(counts.get("requests"), int)
        or counts["requests"] <= 0
        or not isinstance(counts.get("stockSuccesses"), int)
    ):
        raise RuntimeError("recorder result lacks valid request and stock-success counts")
    return path


def restore_remote(args: argparse.Namespace, repo: Path, baseline: dict[Path, bytes]) -> str | None:
    reject_remote_benchmark_conflict(repo)
    restore_bytes(repo, baseline)
    if not _git(repo, "diff", "--name-only").stdout.strip():
        wait_for_baseline(args, _git(repo, "rev-parse", "HEAD").stdout.strip(), baseline)
        return None
    revision = _commit_push(repo, "benchmark: restore SQLite ramp baseline")
    wait_for_baseline(args, revision, baseline)
    return revision


def run(args: argparse.Namespace) -> int:
    images = image_map(args.image_map) if args.execute else None
    rendered = plan(args, images)
    if not args.execute:
        print(json.dumps(rendered, indent=2, sort_keys=True))
        return 0
    preflight = generator_preflight(args.results_dir)
    repo = safe_cluster_repo(args.cluster_repo)
    if not args.resume and (
        journal_file(args).exists() or (args.results_dir / "campaign-baseline.json").exists()
    ):
        raise RuntimeError(
            "existing campaign results require --resume; refusing to overwrite baseline"
        )
    baseline = load_persisted_baseline(args) if args.resume else snapshot(repo)
    if not args.resume:
        persist_baseline(args, baseline)
    schedule = rendered["schedule"]
    journal = _load_journal(args)
    failure: BaseException | None = None
    try:
        for row in rendered["runs"]:
            variant = Variant(row["order"], row["runtime"], row["framework"])
            identity = row["identity"]
            previous = next(
                (item for item in reversed(journal["runs"]) if item["identity"] == identity), None
            )
            conflicting = next(
                (
                    item
                    for item in journal["runs"]
                    if item["runtime"] == variant.runtime
                    and item["framework"] == variant.framework
                    and item["identity"] != identity
                ),
                None,
            )
            if conflicting:
                raise RuntimeError(
                    "resume identity does not match the prior image, source revision, or load hash"
                )
            if previous and not args.resume:
                raise RuntimeError(
                    "existing attempted identity requires --resume; it will not be silently rerun"
                )
            if previous and previous.get("status") == "complete":
                validate_result(args, row, previous["attemptId"])
                continue
            attempt_id = args.attempt_id or str(uuid.uuid4())
            entry = {
                **row,
                "attemptId": attempt_id,
                "generatorPreflight": preflight,
                "status": "activating",
            }
            journal["runs"].append(entry)
            _write_journal(args, journal)
            set_variant(repo, variant, row["image"], attempt_id)
            revision = _commit_push(
                repo, f"benchmark: sqlite ramp {variant.runtime}/{variant.framework}"
            )
            rollout = wait_for_flux(args, revision, variant, row["image"], attempt_id)
            entry["status"] = "recording"
            entry["fluxRevision"] = rollout["fluxRevision"]
            _write_journal(args, journal)
            result = subprocess.run(
                recorder_command(
                    args, variant, row["image"], attempt_id, schedule, rollout["fluxRevision"]
                ),
                check=False,
                preexec_fn=recorder_preexec if os.name == "posix" else None,
            )
            entry["recorderExitCode"] = result.returncode
            if result.returncode:
                entry["status"] = "invalid"
                _write_journal(args, journal)
                raise RuntimeError("recorder reported infrastructure or protocol failure")
            entry["resultPath"] = str(validate_result(args, row, attempt_id))
            entry["status"] = "complete"
            _write_journal(args, journal)
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            restore_revision = restore_remote(args, repo, baseline)
            if journal["runs"]:
                journal["runs"][-1]["restoreRevision"] = restore_revision
                _write_journal(args, journal)
        except BaseException as restore_error:
            _recovery(args, baseline, restore_error)
            if failure is None:
                raise
        if failure is not None:
            _recovery(args, baseline, failure)
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(arguments(argv))


if __name__ == "__main__":
    raise SystemExit(main())
