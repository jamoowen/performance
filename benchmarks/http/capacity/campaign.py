"""Run the opt-in bounded SQLite capacity search through the Flux allowlist."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from benchmarks.http.ramp.measure import campaign as ramp

from . import record
from .protocol import MEASURED_VUS, SAFETY_CEILING_RPS, initial_steps, protocol_hash

VARIANTS = (
    ("go", "nethttp"),
    ("node", "express"),
    ("bun", "native"),
    ("rust", "axum"),
    ("python", "fastapi"),
    ("go", "chi"),
    ("node", "fastify"),
    ("bun", "hono"),
    ("rust", "actix"),
    ("elixir", "phoenix"),
    ("go", "fiber"),
    ("node", "nest"),
    ("bun", "elysia"),
)


@dataclass(frozen=True)
class Variant:
    order: int
    runtime: str
    framework: str


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster-repo", type=Path, required=True)
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--node-ip", required=True)
    parser.add_argument("--image-map", type=Path)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--harness-source-revision")
    parser.add_argument("--namespace", default="my-api")
    parser.add_argument("--results-dir", type=Path, default=Path("results/http/sqlite-capacity"))
    parser.add_argument("--k6", default="k6")
    parser.add_argument("--safety-ceiling", type=int, default=SAFETY_CEILING_RPS)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if args.safety_ceiling < 1500:
        parser.error("--safety-ceiling must be at least 1500 RPS")
    # Reuse the mature grammar/immutable SHA validation without carrying its schedule.
    ramp.arguments(
        [
            "--cluster-repo",
            str(args.cluster_repo),
            "--ssh-host",
            args.ssh_host,
            "--node-ip",
            args.node_ip,
            "--source-revision",
            args.source_revision,
            "--results-dir",
            str(args.results_dir),
            *(["--image-map", str(args.image_map)] if args.image_map else []),
            *(["--execute"] if args.execute else []),
            *(["--resume"] if args.resume else []),
        ]
    )
    if args.harness_source_revision is None:
        args.harness_source_revision = args.source_revision
    if args.execute and not ramp.GIT_SHA.fullmatch(args.harness_source_revision):
        parser.error("--execute requires an immutable 40-character harness source revision")
    if args.resume and not args.execute:
        parser.error("--resume requires --execute")
    return args


def identity(variant: Variant, image: str, args: argparse.Namespace) -> str:
    import hashlib

    value = {
        "variant": asdict(variant),
        "image": image,
        "sourceRevision": args.source_revision,
        "harnessSourceRevision": args.harness_source_revision,
        "loadHash": record.load_hash(),
        "protocolHash": protocol_hash(args.safety_ceiling),
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def plan(args: argparse.Namespace, images: dict[str, str] | None = None) -> dict[str, Any]:
    variants = [Variant(index + 1, *pair) for index, pair in enumerate(VARIANTS)]
    return {
        "mode": "execute" if args.execute else "dry-run",
        "warmup": {"rps": 100, "seconds": 30, "vus": 256},
        "measuredVus": MEASURED_VUS,
        "initialSteps": [step.as_k6_stage() for step in initial_steps()],
        "safetyCeilingRps": args.safety_ceiling,
        "loadHash": record.load_hash(),
        "protocolHash": protocol_hash(args.safety_ceiling),
        "runs": [
            {
                **asdict(item),
                "image": images[item.runtime] if images else None,
                "sourceRevision": args.source_revision,
                "harnessSourceRevision": args.harness_source_revision,
                "loadHash": record.load_hash(),
                "protocolHash": protocol_hash(args.safety_ceiling),
                "identity": identity(item, images[item.runtime], args) if images else None,
            }
            for item in variants
        ],
    }


def _journal_path(args: argparse.Namespace) -> Path:
    return args.results_dir / "campaign-journal.json"


def _journal(args: argparse.Namespace) -> dict[str, Any]:
    path = _journal_path(args)
    return json.loads(path.read_text()) if path.exists() else {"runs": []}


def _record_command(
    args: argparse.Namespace, variant: Variant, image: str, attempt_id: str, flux_revision: str
) -> list[str]:
    # record is a separate executable process so SIGINT always reaches its finally cleanup.
    return [
        sys.executable,
        "-m",
        "benchmarks.http.capacity.record",
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
        "--ssh-host",
        args.ssh_host,
        "--k6",
        args.k6,
        "--safety-ceiling",
        str(args.safety_ceiling),
    ]


def _restore(args: argparse.Namespace, repo: Path, baseline: dict[Path, bytes]) -> None:
    ramp.restore_remote(args, repo, baseline)


def _validate_completed(args: argparse.Namespace, row: dict[str, Any]) -> None:
    marker = args.results_dir / row["attemptId"] / "owned-process-cleanup-failure.json"
    if marker.exists():
        raise RuntimeError("owned_process_cleanup_failure")
    try:
        value = json.loads(Path(row["resultPath"]).read_text())
    except (KeyError, OSError, json.JSONDecodeError) as error:
        raise RuntimeError("completed capacity attempt has no readable result") from error
    metadata = value.get("metadata", {})
    expected = {
        "attemptId": row["attemptId"],
        "runtime": row["runtime"],
        "framework": row["framework"],
        "image": row["image"],
        "sourceRevision": args.source_revision,
        "harnessSourceRevision": args.harness_source_revision,
        "loadHash": row["loadHash"],
        "protocolHash": row["protocolHash"],
    }
    if any(metadata.get(key) != wanted for key, wanted in expected.items()):
        raise RuntimeError("completed capacity result metadata does not match campaign identity")


def execute(args: argparse.Namespace) -> dict[str, Any]:
    images = ramp.image_map(args.image_map)
    args.results_dir.mkdir(parents=True, exist_ok=True)
    # Prove the local generator can safely start warmup before any GitOps mutation.
    record.preflight(args.results_dir, 256, campaign_start=True)
    repo = ramp.safe_cluster_repo(args.cluster_repo)
    if not args.resume and (
        _journal_path(args).exists() or (args.results_dir / "campaign-baseline.json").exists()
    ):
        raise RuntimeError(
            "existing capacity campaign requires --resume; refusing to overwrite baseline"
        )
    journal = _journal(args)
    expected_rows = {(row["runtime"], row["framework"]): row for row in plan(args, images)["runs"]}
    for previous in journal["runs"]:
        expected = expected_rows.get((previous.get("runtime"), previous.get("framework")))
        if expected is None or previous.get("identity") != expected["identity"]:
            raise RuntimeError(
                "resume identity does not match prior capacity image/source/load/protocol"
            )
        marker = (
            args.results_dir / previous.get("attemptId", "") / "owned-process-cleanup-failure.json"
        )
        if marker.exists():
            raise RuntimeError("owned_process_cleanup_failure")
        if previous.get("status") == "complete":
            _validate_completed(args, previous)
    baseline = ramp.load_persisted_baseline(args) if args.resume else ramp.snapshot(repo)
    if not args.resume:
        ramp.persist_baseline(args, baseline)
    try:
        for order, (runtime, framework) in enumerate(VARIANTS, 1):
            variant = Variant(order, runtime, framework)
            image = images[runtime]
            expected = identity(variant, image, args)
            conflicting = next(
                (
                    row
                    for row in journal["runs"]
                    if row.get("runtime") == runtime
                    and row.get("framework") == framework
                    and row.get("identity") != expected
                ),
                None,
            )
            if conflicting:
                raise RuntimeError(
                    "resume identity does not match prior capacity image/source/load/protocol"
                )
            prior = next(
                (
                    row
                    for row in journal["runs"]
                    if row.get("identity") == expected and row.get("status") == "complete"
                ),
                None,
            )
            if prior:
                _validate_completed(args, prior)
                continue
            attempt_id = str(uuid.uuid4())
            row = {
                "attemptId": attempt_id,
                "runtime": runtime,
                "framework": framework,
                "image": image,
                "identity": expected,
                "status": "in_progress",
                "resultPath": str(args.results_dir / attempt_id / "result.json"),
                "sourceRevision": args.source_revision,
                "harnessSourceRevision": args.harness_source_revision,
                "loadHash": record.load_hash(),
                "protocolHash": protocol_hash(args.safety_ceiling),
            }
            journal["runs"].append(row)
            _write(_journal_path(args), journal)
            ramp.set_variant(repo, ramp.Variant(order, runtime, framework), image, attempt_id)
            revision = ramp._commit_push(repo, f"run SQLite capacity search {runtime}/{framework}")
            ramp.wait_for_flux(
                args, revision, ramp.Variant(order, runtime, framework), image, attempt_id
            )
            recorder_returncode = subprocess.run(
                _record_command(args, variant, image, attempt_id, revision), check=False
            ).returncode
            row["recorderReturncode"] = recorder_returncode
            marker = args.results_dir / attempt_id / "owned-process-cleanup-failure.json"
            if marker.exists():
                row["status"] = "failed"
                _write(_journal_path(args), journal)
                raise RuntimeError("owned_process_cleanup_failure")
            # An explicitly invalid result is complete evidence, whereas a
            # missing result leaves the attempt failed and resumable.
            row["status"] = "complete" if Path(row["resultPath"]).is_file() else "failed"
            if row["status"] == "complete":
                _validate_completed(args, row)
            _write(_journal_path(args), journal)
            # Return to the exact baseline between adapters; a failure remains durable
            # while the next independent workload can still run.
            _restore(args, repo, baseline)
    except BaseException as error:
        ramp._recovery(args, baseline, error)
        try:
            _restore(args, repo, baseline)
        except Exception:
            pass
        raise
    finally:
        _restore(args, repo, baseline)
    return journal


def main(argv: list[str] | None = None) -> int:
    args = arguments(argv)
    images = ramp.image_map(args.image_map) if args.image_map else None
    if not args.execute:
        print(json.dumps(plan(args, images), indent=2))
        return 0
    execute(args)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
