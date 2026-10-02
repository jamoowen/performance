"""Plan and run bounded, Flux-managed HTTP benchmark follow-up campaigns.

The default mode is deliberately dry-run: it prints the exact finite matrix and
does not touch a cluster repository, Kubernetes, SSH, or k6.  Executing a plan
requires explicit image digests and operates only on apps/performance-http.
"""

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

RUNTIMES = ("go", "bun", "rust")
RATES = (300, 600, 900)
IMAGE = re.compile(r"^ghcr\.io/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$")
DNS = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
IPV4 = re.compile(
    r"^(?:25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})(?:\.(?:25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})){3}$"
)


@dataclass(frozen=True)
class Entry:
    experiment: str
    implementation: str
    variant: str
    repetition: int
    rate: int
    workload: str
    backend: str
    router: str
    workers: int
    cpu: int

    @property
    def fingerprint(self):
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster-repo", type=Path, required=True)
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--node-ip", required=True)
    parser.add_argument("--namespace", default="my-api")
    parser.add_argument("--results-dir", type=Path, default=Path("results/http/followups"))
    parser.add_argument(
        "--stage",
        choices=("scheduling", "sqlite", "memory", "frameworks", "scaling", "all"),
        required=True,
    )
    parser.add_argument("--go-image")
    parser.add_argument("--bun-image")
    parser.add_argument("--rust-image")
    parser.add_argument("--go-procs", type=int, choices=(1, 2), default=2)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9._-]+(?:@[A-Za-z0-9._-]+)?", args.ssh_host):
        parser.error("--ssh-host has unsafe characters")
    if not IPV4.fullmatch(args.node_ip):
        parser.error("--node-ip must be an IPv4 address")
    if not DNS.fullmatch(args.namespace):
        parser.error("--namespace must be a DNS label")
    for runtime in RUNTIMES:
        value = getattr(args, f"{runtime}_image")
        if value is not None and not IMAGE.fullmatch(value):
            parser.error(f"--{runtime}-image must be an exact GHCR sha256 digest")
    if not args.dry_run:
        required = {entry.implementation for entry in build_plan(args.stage, args.go_procs)}
        missing = [runtime for runtime in sorted(required) if not getattr(args, f"{runtime}_image")]
        if missing:
            parser.error("missing required image digest(s): " + ", ".join(missing))
    return args


def build_plan(stage, go_procs=2):
    """Return a deterministic, finite run matrix without external effects."""
    selected = (
        ("scheduling", "sqlite", "memory", "frameworks", "scaling") if stage == "all" else (stage,)
    )
    entries = []
    if "scheduling" in selected:
        # Alternation avoids giving one setting all of the early or late runs.
        for repetition, procs in enumerate((2, 1, 2, 1, 2, 1), 1):
            entries.append(
                Entry(
                    "scheduling",
                    "go",
                    f"GOMAXPROCS={procs}",
                    (repetition + 1) // 2,
                    600,
                    "mixed",
                    "sqlite",
                    "stdlib",
                    procs,
                    1,
                )
            )
    for stage_name, backend in (("sqlite", "sqlite"), ("memory", "memory")):
        if stage_name not in selected:
            continue
        for rate in RATES:
            repeats = 3 if rate == 600 else 1
            for repetition in range(1, repeats + 1):
                # Rotate the first runtime at each rate/repetition.
                offset = (RATES.index(rate) + repetition - 1) % len(RUNTIMES)
                for runtime in RUNTIMES[offset:] + RUNTIMES[:offset]:
                    router = "axum" if runtime == "rust" else "stdlib"
                    label = "rust Axum" if runtime == "rust" else f"{runtime} stdlib"
                    entries.append(
                        Entry(
                            stage_name,
                            runtime,
                            f"{label} {backend}",
                            repetition,
                            rate,
                            "mixed",
                            backend,
                            router,
                            go_procs if runtime == "go" else 1,
                            1,
                        )
                    )
    if "frameworks" in selected:
        for repetition in range(1, 4):
            entries.extend(
                [
                    Entry(
                        "frameworks",
                        "go",
                        "Go chi memory",
                        repetition,
                        600,
                        "mixed",
                        "memory",
                        "chi",
                        go_procs,
                        1,
                    ),
                    Entry(
                        "frameworks",
                        "bun",
                        "Bun Elysia memory",
                        repetition,
                        600,
                        "mixed",
                        "memory",
                        "elysia",
                        1,
                        1,
                    ),
                ]
            )
    if "scaling" in selected:
        for rate in (600, 3000):
            for runtime in RUNTIMES:
                for cpu in (1, 2):
                    router = "axum" if runtime == "rust" else "stdlib"
                    label = "rust Axum" if runtime == "rust" else runtime
                    entries.append(
                        Entry(
                            "scaling",
                            runtime,
                            f"{label} {cpu} CPU",
                            1,
                            rate,
                            "list",
                            "memory",
                            router,
                            cpu,
                            cpu,
                        )
                    )
    return tuple(entries)


def recorder_command(args, entry):
    """Build the one recorder invocation. No shell interpolation is used."""
    port = {"go": 30080, "bun": 30081, "rust": 30082}[entry.implementation]
    return [
        "python3",
        "-m",
        "measure.run",
        entry.implementation,
        "--base-url",
        f"http://{args.node_ip}:{port}",
        "--ssh-host",
        args.ssh_host,
        "--namespace",
        args.namespace,
        "--profile",
        "steady",
        "--workload",
        entry.workload,
        "--rate",
        str(entry.rate),
        "--duration",
        "2m",
        "--warmup-duration",
        "60s",
        "--seed-count",
        "5000",
        "--preallocated-vus",
        "1000",
        "--max-vus",
        "2000",
        "--p95-ms",
        "1000",
        "--max-error-rate",
        "0.01",
        "--sample-interval",
        "5",
        "--results-dir",
        str(args.results_dir / entry.experiment),
        "--experiment",
        entry.experiment,
        "--variant",
        entry.variant,
        "--repetition",
        str(entry.repetition),
        "--campaign-fingerprint",
        entry.fingerprint,
    ]


def render_plan(args):
    return {
        "stage": args.stage,
        "run_count": len(build_plan(args.stage, args.go_procs)),
        "estimated_load_time_minutes": len(build_plan(args.stage, args.go_procs)) * 3,
        "settings": {"warmup": "60s", "duration": "2m", "diagnostics": False},
        "runs": [
            {
                **asdict(entry),
                "fingerprint": entry.fingerprint,
                "command": recorder_command(args, entry),
            }
            for entry in build_plan(args.stage, args.go_procs)
        ],
    }


def _git(repo, *command):
    return subprocess.run(
        ["git", "-C", str(repo), *command], check=True, text=True, capture_output=True
    )


def _safe_cluster_repo(repo):
    repo = repo.resolve()
    if not repo.is_absolute() or "ephemeral" not in repo.parts or not (repo / ".git").exists():
        raise RuntimeError("--cluster-repo must be an isolated ephemeral Git clone")
    if _git(repo, "status", "--porcelain").stdout.strip():
        raise RuntimeError("cluster clone must be clean before a campaign")
    app = repo / "apps" / "performance-http"
    if not app.is_dir():
        raise RuntimeError("cluster clone has no apps/performance-http directory")
    return repo, app


def _literal_env(container, values):
    env = container.setdefault("env", [])
    by_name = {item.get("name"): item for item in env if isinstance(item, dict)}
    for name, value in values.items():
        if name in by_name:
            by_name[name].clear()
            by_name[name].update({"name": name, "value": str(value)})
        else:
            env.append({"name": name, "value": str(value)})


def ensure_rust_manifests(app, image):
    """Create the approved Rust Deployment/NodePort from the Go benchmark shape."""
    import yaml

    deployment_path, service_path = app / "rust-deployment.yaml", app / "rust-service.yaml"
    if deployment_path.exists() and service_path.exists():
        return
    go_deployment = yaml.safe_load((app / "go-deployment.yaml").read_text())
    go_service = yaml.safe_load((app / "go-service.yaml").read_text())
    rust = copy.deepcopy(go_deployment)
    rust["metadata"]["name"] = "http-rust"
    rust["metadata"].setdefault("labels", {})["app.kubernetes.io/name"] = "http-rust"
    rust["spec"]["replicas"] = 0
    rust["spec"]["selector"]["matchLabels"]["app.kubernetes.io/name"] = "http-rust"
    template = rust["spec"]["template"]
    template["metadata"]["labels"]["app.kubernetes.io/name"] = "http-rust"
    container = template["spec"]["containers"][0]
    container["name"] = "http-rust"
    container["image"] = image
    _literal_env(container, {"BACKEND": "sqlite", "ROUTER": "axum", "WORKERS": 1, "DIAGNOSTICS": 0})
    deployment_path.write_text(yaml.safe_dump(rust, sort_keys=False))
    service = copy.deepcopy(go_service)
    service["metadata"]["name"] = "http-rust"
    service["metadata"].setdefault("labels", {})["app.kubernetes.io/name"] = "http-rust"
    service["spec"]["selector"]["app.kubernetes.io/name"] = "http-rust"
    service["spec"]["ports"][0]["nodePort"] = 30082
    service_path.write_text(yaml.safe_dump(service, sort_keys=False))
    kustomization = app / "kustomization.yaml"
    data = yaml.safe_load(kustomization.read_text())
    resources = data.setdefault("resources", [])
    for name in ("rust-deployment.yaml", "rust-service.yaml"):
        if name not in resources:
            resources.append(name)
    kustomization.write_text(yaml.safe_dump(data, sort_keys=False))


def activate_manifest(repo, entry, image, attempt_id=None):
    """Change only the selected benchmark manifests and create a fresh pod template.

    This intentionally never calls kubectl apply. Flux consumes the resulting Git
    commit; callers are responsible for waiting for Flux and rollout readiness.
    """
    import yaml

    repo, app = _safe_cluster_repo(repo)
    paths = {runtime: app / f"{runtime}-deployment.yaml" for runtime in RUNTIMES}
    if not paths[entry.implementation].is_file():
        raise RuntimeError(f"missing deployment manifest for {entry.implementation}")
    originals = {path: path.read_bytes() for path in paths.values() if path.is_file()}
    try:
        for runtime, path in paths.items():
            if not path.is_file():
                continue
            document = yaml.safe_load(path.read_text())
            if not isinstance(document, dict) or document.get("kind") != "Deployment":
                raise RuntimeError(f"invalid deployment manifest: {path}")
            spec = document.setdefault("spec", {})
            spec["replicas"] = 1 if runtime == entry.implementation else 0
            if runtime != entry.implementation:
                path.write_text(yaml.safe_dump(document, sort_keys=False))
                continue
            template = spec.setdefault("template", {})
            metadata = template.setdefault("metadata", {})
            metadata.setdefault("annotations", {})["benchmark.jamoowen.dev/run-id"] = (
                attempt_id or entry.fingerprint
            )
            containers = template.setdefault("spec", {}).get("containers", [])
            container = next(
                (item for item in containers if item.get("name") == f"http-{runtime}"), None
            )
            if container is None:
                raise RuntimeError(f"selected container missing from {path}")
            container["image"] = image
            _literal_env(
                container,
                {
                    "SEED_COUNT": 5000,
                    "BACKEND": entry.backend,
                    "ROUTER": entry.router,
                    "WORKERS": entry.workers,
                    "DIAGNOSTICS": 0,
                    **({"GOMAXPROCS": entry.workers} if runtime == "go" else {}),
                },
            )
            resources = container.setdefault("resources", {})
            for key in ("requests", "limits"):
                resources.setdefault(key, {})["cpu"] = str(entry.cpu)
                resources[key]["memory"] = "512Mi"
            path.write_text(yaml.safe_dump(document, sort_keys=False))
        _git(repo, "diff", "--check")
        changed = _git(repo, "diff", "--name-only").stdout.splitlines()
        allowed = {str(path.relative_to(repo)) for path in originals}
        if any(path not in allowed for path in changed):
            raise RuntimeError("activation changed a path outside apps/performance-http")
        return originals
    except Exception:
        for path, contents in originals.items():
            path.write_bytes(contents)
        raise


def restore_manifest(repo, originals):
    for path, contents in originals.items():
        path.write_bytes(contents)
    _git(repo, "diff", "--check")


def journal_path(args, stage):
    return args.results_dir / stage / "campaign-journal.json"


def write_journal(args, stage, entries):
    path = journal_path(args, stage)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"stage": stage, "runs": entries}, indent=2, sort_keys=True) + "\n")
    return path


def _commit_activation(repo, entry):
    subprocess.run(
        ["/usr/local/bin/kubectl", "kustomize", "clusters/optiplex"],
        cwd=repo,
        stdout=subprocess.DEVNULL,
        check=True,
    )
    status = _git(repo, "status", "--porcelain").stdout.splitlines()
    paths = [line[3:] for line in status if len(line) > 3]
    if any(not path.startswith("apps/performance-http/") for path in paths):
        raise RuntimeError("refusing to commit a path outside apps/performance-http")
    if not paths:
        raise RuntimeError("refusing to create an empty benchmark commit")
    _git(repo, "add", "--", *paths)
    _git(repo, "commit", "-m", f"benchmark: {entry.experiment} {entry.fingerprint}")
    _push_narrow_commit(repo)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _push_narrow_commit(repo):
    """Push once, then rebase our isolated benchmark commit if Training advanced main."""
    first = subprocess.run(
        ["git", "-C", str(repo), "push", "origin", "HEAD:main"], text=True, capture_output=True
    )
    if first.returncode == 0:
        return
    _git(repo, "fetch", "origin", "main")
    try:
        _git(repo, "rebase", "origin/main")
    except subprocess.CalledProcessError as error:
        # Do not force-push or discard an external commit on a manifest conflict.
        subprocess.run(["git", "-C", str(repo), "rebase", "--abort"], check=False)
        raise RuntimeError("benchmark commit conflicts with a newer remote commit") from error
    retry = subprocess.run(
        ["git", "-C", str(repo), "push", "origin", "HEAD:main"], text=True, capture_output=True
    )
    if retry.returncode:
        raise RuntimeError("benchmark commit could not be pushed after rebase")


def _top_pods(host):
    completed = subprocess.run(
        ["ssh", host, "kubectl", "top", "pods", "-A"], text=True, capture_output=True, timeout=25
    )
    return completed.stdout if completed.returncode == 0 else None


def _remote_json(args, *command):
    result = subprocess.run(
        ["ssh", args.ssh_host, "kubectl", "--request-timeout=10s", *command],
        check=True,
        text=True,
        capture_output=True,
        timeout=20,
    )
    return json.loads(result.stdout)


def _request_reconcile(args):
    stamp = str(int(time.time()))
    for resource in ("gitrepository/flux-system", "kustomization/flux-system"):
        subprocess.run(
            [
                "ssh",
                args.ssh_host,
                "kubectl",
                "-n",
                "flux-system",
                "annotate",
                resource,
                f"reconcile.fluxcd.io/requestedAt={stamp}",
                "--overwrite",
            ],
            check=True,
            text=True,
            timeout=20,
        )


def _deployment_matches(deployment, entry, image, namespace, attempt_id):
    spec = deployment.get("spec", {})
    template = spec.get("template", {})
    annotation = (
        template.get("metadata", {}).get("annotations", {}).get("benchmark.jamoowen.dev/run-id")
    )
    containers = template.get("spec", {}).get("containers", [])
    container = next(
        (item for item in containers if item.get("name") == f"http-{entry.implementation}"), {}
    )
    env = {item.get("name"): str(item.get("value")) for item in container.get("env", [])}
    resources = container.get("resources", {})
    return (
        spec.get("replicas") == 1
        and deployment.get("metadata", {}).get("namespace") == namespace
        and deployment.get("status", {}).get("observedGeneration", 0)
        >= deployment.get("metadata", {}).get("generation", 1)
        and annotation == attempt_id
        and container.get("image") == image
        and all(
            env.get(key) == str(value)
            for key, value in {
                "SEED_COUNT": 5000,
                "BACKEND": entry.backend,
                "ROUTER": entry.router,
                "WORKERS": entry.workers,
                "DIAGNOSTICS": 0,
            }.items()
        )
        and (entry.implementation != "go" or env.get("GOMAXPROCS") == str(entry.workers))
        and (entry.implementation != "go" or env.get("MAX_OPEN_CONNS") == "1")
        and all(
            resources.get(kind, {}).get("cpu") == str(entry.cpu) for kind in ("requests", "limits")
        )
        and all(resources.get(kind, {}).get("memory") == "512Mi" for kind in ("requests", "limits"))
    )


def _wait_for_rollout(args, entry, commit, image, attempt_id):
    deployment = f"http-{entry.implementation}"
    _request_reconcile(args)
    deadline = time.monotonic() + 240
    last = "waiting for Flux"
    while time.monotonic() < deadline:
        try:
            flux = _remote_json(
                args, "-n", "flux-system", "get", "kustomization", "flux-system", "-o", "json"
            )
            revision = flux.get("status", {}).get("lastAppliedRevision", "")
            selected = _remote_json(
                args, "-n", args.namespace, "get", "deployment", deployment, "-o", "json"
            )
            deployments = _remote_json(
                args, "-n", args.namespace, "get", "deployments", "-o", "json"
            ).get("items", [])
            pods = _remote_json(args, "-n", args.namespace, "get", "pods", "-o", "json").get(
                "items", []
            )
            others_down = all(
                item.get("spec", {}).get("replicas", 1) == 0
                for item in deployments
                if item.get("metadata", {}).get("name")
                in {f"http-{runtime}" for runtime in RUNTIMES}
                and item.get("metadata", {}).get("name") != deployment
            )
            active = [
                pod
                for pod in pods
                if pod.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/name")
                in {f"http-{runtime}" for runtime in RUNTIMES}
                and (
                    pod.get("metadata", {}).get("deletionTimestamp")
                    or pod.get("status", {}).get("phase") in {"Pending", "Running"}
                )
            ]
            ready = selected.get("status", {}).get("readyReplicas") == 1
            selected_pods = [
                pod
                for pod in active
                if pod.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/name")
                == deployment
            ]
            pod = selected_pods[0] if len(selected_pods) == 1 else {}
            pod_container = next(
                (
                    item
                    for item in pod.get("spec", {}).get("containers", [])
                    if item.get("name") == deployment
                ),
                {},
            )
            pod_status = next(
                (
                    item
                    for item in pod.get("status", {}).get("containerStatuses", [])
                    if item.get("name") == deployment
                ),
                {},
            )
            pod_ready = (
                not pod.get("metadata", {}).get("deletionTimestamp")
                and pod.get("status", {}).get("phase") == "Running"
                and pod.get("metadata", {})
                .get("annotations", {})
                .get("benchmark.jamoowen.dev/run-id")
                == attempt_id
                and pod_container.get("image") == image
                and pod_status.get("ready") is True
                and any(
                    condition.get("type") == "Ready" and condition.get("status") == "True"
                    for condition in pod.get("status", {}).get("conditions", [])
                )
            )
            if (
                commit in revision
                and _deployment_matches(selected, entry, image, args.namespace, attempt_id)
                and others_down
                and ready
                and len(active) == len(selected_pods) == 1
                and pod_ready
            ):
                return
            last = f"revision={revision!r}, ready={ready}, others_down={others_down}"
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            json.JSONDecodeError,
        ) as error:
            last = str(error)
        time.sleep(2)
    raise RuntimeError(f"Flux rollout did not reach requested revision {commit}: {last}")


def _wait_for_baseline(args, commit, runtime):
    _request_reconcile(args)
    deadline = time.monotonic() + 240
    last = "waiting for Flux"
    while time.monotonic() < deadline:
        try:
            flux = _remote_json(
                args, "-n", "flux-system", "get", "kustomization", "flux-system", "-o", "json"
            )
            deployments = _remote_json(
                args, "-n", args.namespace, "get", "deployments", "-o", "json"
            ).get("items", [])
            by_name = {item.get("metadata", {}).get("name"): item for item in deployments}
            selected = by_name.get(f"http-{runtime}", {})
            others_down = all(
                item.get("spec", {}).get("replicas", 1) == 0
                for name, item in by_name.items()
                if name in {f"http-{name}" for name in RUNTIMES} and name != f"http-{runtime}"
            )
            if (
                commit in flux.get("status", {}).get("lastAppliedRevision", "")
                and selected.get("spec", {}).get("replicas") == 1
                and selected.get("status", {}).get("readyReplicas") == 1
                and others_down
            ):
                return
            last = "baseline pod is not ready at requested revision"
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            json.JSONDecodeError,
        ) as error:
            last = str(error)
        time.sleep(2)
    raise RuntimeError(f"Flux did not restore baseline {runtime} at {commit}: {last}")


def _result_for_fingerprint(directory, fingerprint, before=()):
    before = {Path(path) for path in before}
    for path in directory.rglob("result.json"):
        if path in before:
            continue
        try:
            result = json.loads(path.read_text())
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if result.get("metadata", {}).get("campaign_fingerprint") == fingerprint:
            return path, result
    return None, None


def _completed_fingerprints(args, stage, entries):
    expected = {entry.fingerprint: entry for entry in entries}
    source = Path(__file__).resolve().parents[3]
    source_commit = _git(source, "rev-parse", "HEAD").stdout.strip()
    load_hash = hashlib.sha256((source / "benchmarks/http/load.js").read_bytes()).hexdigest()
    try:
        k6_version = subprocess.check_output(["k6", "version"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return set()
    completed = set()
    for path in (args.results_dir / stage).rglob("result.json"):
        try:
            result = json.loads(path.read_text())
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        metadata, cluster = (
            result.get("metadata", {}),
            result.get("metadata", {}).get("cluster", {}),
        )
        fingerprint = metadata.get("campaign_fingerprint")
        entry = expected.get(fingerprint)
        configuration = cluster.get("workload", {}).get("configuration", {})
        resources = cluster.get("workload", {}).get("resources", {})
        pod = cluster.get("pod", {})
        image_id = str(pod.get("image_id") or "")
        if image_id.startswith("docker-pullable://"):
            image_id = image_id.removeprefix("docker-pullable://")
        image = pod.get("requested_image") or (
            image_id if "@sha256:" in image_id else pod.get("image")
        )
        settings = metadata.get("settings", {})
        resource = result.get("resource", {})
        essential = {
            "insufficient distinct CPU samples",
            "CPU counter reset",
            "CPU counter identity changed",
            "essential metric unavailable: memory_working_set",
        }
        if (
            entry
            and result.get("status") in {"complete", "invalid"}
            and result.get("error") is None
            and not result.get("collector_errors")
            and result.get("k6_exit_code") in {0, 99}
            and image == getattr(args, f"{entry.implementation}_image")
            and metadata.get("source_git_commit") == source_commit
            and not metadata.get("source_git_dirty")
            and metadata.get("load_script_sha256") == load_hash
            and metadata.get("k6_version") == k6_version
            and settings.get("rate") == entry.rate
            and settings.get("workload") == entry.workload
            and settings.get("profile") == "steady"
            and settings.get("duration") == "2m"
            and settings.get("warmup_duration") == "60s"
            and settings.get("seed_count") == 5000
            and settings.get("preallocated_vus") == 1000
            and settings.get("max_vus") == 2000
            and settings.get("p95_ms") == 1000
            and settings.get("max_error_rate") == 0.01
            and settings.get("sample_interval") == 5
            and settings.get("diagnostics") is False
            and str(configuration.get("BACKEND")) == entry.backend
            and str(configuration.get("ROUTER")) == entry.router
            and str(configuration.get("WORKERS")) == str(entry.workers)
            and str(configuration.get("DIAGNOSTICS")) in {"0", "false", "False", ""}
            and (
                entry.implementation != "go"
                or str(configuration.get("GOMAXPROCS")) == str(entry.workers)
            )
            and not essential.intersection(resource.get("warnings", []))
            and resource.get("coverage", {}).get("cpu_span_seconds", 0) > 0
            and all(
                resources.get(kind, {}).get("cpu") == str(entry.cpu)
                for kind in ("requests", "limits")
            )
            and all(
                resources.get(kind, {}).get("memory") == "512Mi" for kind in ("requests", "limits")
            )
        ):
            completed.add(fingerprint)
    return completed


def _operational_result(result, code):
    resource = result.get("resource", {}) if isinstance(result, dict) else {}
    essential = {
        "insufficient distinct CPU samples",
        "CPU counter reset",
        "CPU counter identity changed",
        "essential metric unavailable: memory_working_set",
    }
    return (
        code in (0, 99)
        and isinstance(result, dict)
        and result.get("error") is None
        and not result.get("collector_errors")
        and not essential.intersection(resource.get("warnings", []))
    )


def execute(args):
    """Run one finite stage sequentially, leaving Flux as the sole applier.

    Threshold code 99 is a recorded experiment outcome and continues. Any other
    recorder failure stops the stage after the initial baseline configuration is
    restored. The journal deliberately records failures instead of retrying.
    """
    if args.dry_run:
        raise ValueError("execute does not accept --dry-run")
    repo, app = _safe_cluster_repo(args.cluster_repo)
    selected = build_plan(args.stage, args.go_procs)
    if any(entry.implementation == "rust" for entry in selected):
        ensure_rust_manifests(app, args.rust_image)
        _git(repo, "diff", "--check")
        if _git(repo, "status", "--porcelain").stdout.strip():
            _commit_activation(
                repo, Entry("prepare", "rust", "manifest", 1, 0, "", "sqlite", "axum", 1, 1)
            )
    initial = {path: path.read_bytes() for path in app.glob("*-deployment.yaml")}
    baseline = next(
        (
            path.stem.removesuffix("-deployment").replace("http-", "")
            for path, contents in initial.items()
            if b"replicas: 1" in contents
        ),
        None,
    )
    journal = []
    completed = _completed_fingerprints(args, args.stage, selected)
    try:
        for entry in selected:
            if entry.fingerprint in completed:
                journal.append({"fingerprint": entry.fingerprint, "outcome": "resumed"})
                continue
            # Do not overwrite an external commit. A clean fast-forward preserves it.
            _git(repo, "fetch", "origin", "main")
            _git(repo, "merge", "--ff-only", "origin/main")
            attempt_id = f"{entry.fingerprint}-{uuid.uuid4().hex[:12]}"
            activate_manifest(
                repo, entry, getattr(args, f"{entry.implementation}_image"), attempt_id
            )
            commit = _commit_activation(repo, entry)
            before = _top_pods(args.ssh_host)
            _wait_for_rollout(
                args, entry, commit, getattr(args, f"{entry.implementation}_image"), attempt_id
            )
            command = recorder_command(args, entry)
            environment = os.environ.copy()
            environment["PYTHONPATH"] = "benchmarks/http"
            result_directory = args.results_dir / entry.experiment
            prior_results = (
                set(result_directory.rglob("result.json")) if result_directory.exists() else set()
            )
            code = subprocess.run(command, check=False, env=environment).returncode
            path, result = _result_for_fingerprint(
                result_directory, entry.fingerprint, prior_results
            )
            outcome = {
                "fingerprint": entry.fingerprint,
                "cluster_commit": commit,
                "attempt_id": attempt_id,
                "recorder_exit_code": code,
                "result_path": str(path) if path else None,
                "before_top_pods": before,
                "after_top_pods": _top_pods(args.ssh_host),
            }
            journal.append(outcome)
            write_journal(args, args.stage, journal)
            if not _operational_result(result, code):
                raise RuntimeError(f"operational recorder failure for {entry.fingerprint}")
    finally:
        # Restore only once, after the whole stage (or its safe interruption).
        restore_manifest(repo, initial)
        if _git(repo, "status", "--porcelain").stdout.strip():
            restore_commit = _commit_activation(
                repo, Entry("restore", baseline or "bun", "baseline", 1, 0, "", "", "", 0, 1)
            )
            if baseline:
                _wait_for_baseline(args, restore_commit, baseline)
        write_journal(args, args.stage, journal)
    return journal


def main(argv=None):
    args = arguments(argv)
    plan = render_plan(args)
    print(json.dumps(plan, indent=2, sort_keys=True))
    if args.dry_run:
        return 0
    execute(args)
    return 0


if __name__ == "__main__":
    main()
