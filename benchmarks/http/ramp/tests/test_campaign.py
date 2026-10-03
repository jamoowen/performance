from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from benchmarks.http.ramp.measure import campaign

IMAGE = "ghcr.io/jamoowen/performance-http-ramp-go@sha256:" + "a" * 64
CONFIG_IMAGE = "sha256:" + "b" * 64
RETAINED_ATTEMPT = "7e57f571-2a63-413f-b747-cb3f521d3f2c"


def deployment(replicas: int = 1, image: str = IMAGE, attempt: str = "attempt") -> dict:
    return {
        "metadata": {"generation": 2},
        "spec": {
            "replicas": replicas,
            "template": {
                "metadata": {"annotations": {"benchmark.jamoowen.dev/attempt-id": attempt}},
                "spec": {
                    "containers": [
                        {
                            "name": "http-ramp",
                            "image": image,
                            "resources": {
                                "requests": {"cpu": "1", "memory": "512Mi"},
                                "limits": {"cpu": "1", "memory": "512Mi"},
                            },
                            "env": [
                                {"name": "FRAMEWORK", "value": "nethttp"},
                                {"name": "PORT", "value": "8080"},
                                {"name": "SEED_COUNT", "value": "5000"},
                                {"name": "SQLITE_PATH", "value": "/data/benchmark.sqlite"},
                                {"name": "GOMAXPROCS", "value": "1"},
                                {"name": "NODE_ENV", "value": "production"},
                                {"name": "ERL_FLAGS", "value": "+S 1:1 +SDcpu 1 +SDio 1"},
                                {"name": "RELEASE_DISTRIBUTION", "value": "none"},
                                {"name": "RELEASE_TMP", "value": "/tmp/ramp"},
                            ],
                        }
                    ]
                },
            },
        },
        "status": {
            "observedGeneration": 2,
            "conditions": [{"type": "Available", "status": "True"}],
        },
    }


def pod(terminating: bool = False) -> dict:
    metadata = {
        "name": "http-ramp",
        "uid": "pod-uid",
        "labels": {"app.kubernetes.io/name": "http-ramp"},
        "annotations": {"benchmark.jamoowen.dev/attempt-id": "attempt"},
    }
    if terminating:
        metadata["deletionTimestamp"] = "2026-10-03T00:00:00Z"
    return {
        "metadata": metadata,
        "spec": {
            "containers": deployment()["spec"]["template"]["spec"]["containers"],
        },
        "status": {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": "True"}],
            "containerStatuses": [
                {
                    "name": "http-ramp",
                    "ready": True,
                    "restartCount": 0,
                    "containerID": "containerd://abc",
                    "image": CONFIG_IMAGE,
                    "imageID": IMAGE,
                }
            ],
        },
    }


class CampaignTests(unittest.TestCase):
    def _resume_args(self, results: Path, retained: list[str]) -> argparse.Namespace:
        return argparse.Namespace(
            execute=True,
            image_map=Path("images.json"),
            results_dir=results,
            resume=True,
            retain_invalid_attempt=retained,
            cluster_repo=Path("repo"),
            attempt_id=None,
            source_revision="source",
            harness_source_revision="harness",
        )

    def _row(self, order: int, identity: str, framework: str = "nethttp") -> dict:
        return {
            "order": order,
            "runtime": "go",
            "framework": framework,
            "identity": identity,
            "image": IMAGE,
            "loadHash": "load",
            "scheduleHash": "schedule",
        }

    def _write_result(
        self,
        results: Path,
        row: dict,
        attempt_id: str,
        status: str,
        reasons: list[str] | None = None,
    ) -> Path:
        path = results / attempt_id / "result.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "metadata": {
                        "attemptId": attempt_id,
                        "runtime": row["runtime"],
                        "framework": row["framework"],
                        "image": row["image"],
                        "sourceRevision": "source",
                        "harnessSourceRevision": "harness",
                        "loadHash": row["loadHash"],
                        "scheduleHash": row["scheduleHash"],
                    },
                    "counts": {"requests": 1, "stockSuccesses": 1},
                    "validity": {"status": status, "reasons": reasons or []},
                }
            )
        )
        return path

    def test_fixed_interleaved_order_and_identity(self):
        self.assertEqual(campaign.VARIANTS[0], ("go", "nethttp"))
        self.assertEqual(campaign.VARIANTS[-1], ("rust", "rocket"))
        self.assertEqual(len(campaign.VARIANTS), 15)
        variant = campaign.Variant(1, "go", "nethttp")
        first = campaign.attempt_identity(
            variant, IMAGE, "source", "harness", campaign.PRODUCTION_SCHEDULE
        )
        self.assertEqual(
            first,
            campaign.attempt_identity(
                variant, IMAGE, "source", "harness", campaign.PRODUCTION_SCHEDULE
            ),
        )
        self.assertNotEqual(
            first,
            campaign.attempt_identity(
                variant, IMAGE, "other", "harness", campaign.PRODUCTION_SCHEDULE
            ),
        )

    def test_digest_map_requires_all_six_exact_digests(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "images.json"
            path.write_text(json.dumps({"go": IMAGE}))
            with self.assertRaisesRegex(RuntimeError, "every runtime"):
                campaign.image_map(path)

    def test_generator_preflight_records_limits_and_rejects_low_memory(self):
        disk = SimpleNamespace(free=campaign.MIN_FREE_DISK_BYTES)
        memory = SimpleNamespace(available=campaign.MIN_AVAILABLE_MEMORY_BYTES)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(campaign.shutil, "disk_usage", return_value=disk),
            patch.object(
                campaign, "_psutil", return_value=SimpleNamespace(virtual_memory=lambda: memory)
            ),
            patch.object(campaign.resource, "getrlimit", return_value=(256, 65535)),
        ):
            result = campaign.generator_preflight(Path(directory))
        self.assertEqual(result["nofileChild"], 8192)
        self.assertEqual(result["preallocatedVus"], 3200)
        self.assertEqual(campaign.child_nofile_limit(campaign.resource.RLIM_INFINITY), 8192)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(campaign.shutil, "disk_usage", return_value=disk),
            patch.object(
                campaign,
                "_psutil",
                return_value=SimpleNamespace(virtual_memory=lambda: SimpleNamespace(available=1)),
            ),
            patch.object(campaign.resource, "getrlimit", return_value=(256, 65535)),
        ):
            with self.assertRaisesRegex(RuntimeError, "4 GiB"):
                campaign.generator_preflight(Path(directory))

    def test_strict_readiness_rejects_terminating_old_pod_and_contract_drift(self):
        variant = campaign.Variant(1, "go", "nethttp")
        self.assertEqual(
            campaign.strict_readiness(
                deployment(), [pod()], [{"spec": {"replicas": 0}}] * 3, variant, IMAGE, "attempt"
            ),
            (True, "ready"),
        )
        self.assertEqual(
            campaign.strict_readiness(
                deployment(),
                [pod(), pod(terminating=True)],
                [{"spec": {"replicas": 0}}] * 3,
                variant,
                IMAGE,
                "attempt",
            )[0],
            False,
        )
        stale = deployment(image="ghcr.io/jamoowen/performance-http-ramp-go@sha256:" + "b" * 64)
        self.assertEqual(
            campaign.strict_readiness(
                stale, [pod()], [{"spec": {"replicas": 0}}] * 3, variant, IMAGE, "attempt"
            )[1],
            "deployment_contract_mismatch",
        )
        stale_pod = pod()
        stale_pod["metadata"]["annotations"]["benchmark.jamoowen.dev/attempt-id"] = "old-attempt"
        self.assertFalse(
            campaign.strict_readiness(
                deployment(),
                [stale_pod],
                [{"spec": {"replicas": 0}}] * 3,
                variant,
                IMAGE,
                "attempt",
            )[0]
        )
        self.assertEqual(
            campaign.strict_readiness(
                deployment(), [pod()], [{"spec": {"replicas": 1}}] * 3, variant, IMAGE, "attempt"
            )[1],
            "old_benchmark_deployment_still_desired",
        )

    def test_baseline_wait_rejects_missing_required_bun_readiness(self):
        baseline = {
            campaign.APP_PATH: b"spec:\n  replicas: 0\n  template:\n    spec:\n      containers:\n        - image: ramp\n          env: []\n",
            campaign.OLD_PATHS[
                0
            ]: b"spec:\n  replicas: 0\n  template:\n    spec:\n      containers:\n        - image: go\n          env: []\n",
            campaign.OLD_PATHS[
                1
            ]: b"spec:\n  replicas: 1\n  template:\n    spec:\n      containers:\n        - image: bun\n          env: []\n",
            campaign.OLD_PATHS[
                2
            ]: b"spec:\n  replicas: 0\n  template:\n    spec:\n      containers:\n        - image: rust\n          env: []\n",
        }
        live = {
            name: {
                "spec": {
                    "replicas": replicas,
                    "template": {"spec": {"containers": [{"image": image, "env": []}]}},
                }
            }
            for name, replicas, image in (
                ("http-ramp", 0, "ramp"),
                ("http-go", 0, "go"),
                ("http-bun", 1, "bun"),
                ("http-rust", 0, "rust"),
            )
        }
        args = argparse.Namespace(ssh_host="host", namespace="my-api")

        def remote(_args, *command):
            if "kustomization" in command:
                return {"status": {"lastAppliedRevision": "revision"}}
            if "pods" in command:
                return {"items": []}
            return live[command[command.index("deployment") + 1]]

        with (
            patch.object(campaign, "_request_reconcile"),
            patch.object(campaign, "_remote_json", side_effect=remote),
            patch.object(campaign.time, "monotonic", side_effect=(0, 1)),
        ):
            with self.assertRaisesRegex(RuntimeError, "baseline restoration"):
                campaign.wait_for_baseline(args, "revision", baseline, timeout=0.5)

    def test_snapshot_restore_is_exact_and_path_guard_rejects_unrelated_file(self):
        with tempfile.TemporaryDirectory(prefix="ephemeral-") as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            for path in (campaign.APP_PATH, *campaign.OLD_PATHS):
                target = repo / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(
                    "apiVersion: apps/v1\n"
                    "kind: Deployment\n"
                    "spec:\n"
                    "  replicas: 0\n"
                    "  template:\n"
                    "    spec:\n"
                    "      containers:\n"
                    "        - name: http-ramp\n"
                    "          image: placeholder\n"
                )
            baseline = campaign.snapshot(repo)
            (repo / campaign.APP_PATH).write_text("changed\n")
            campaign.restore_bytes(repo, baseline)
            self.assertEqual((repo / campaign.APP_PATH).read_bytes(), baseline[campaign.APP_PATH])
            (repo / "unrelated.txt").write_text("outside allowlist\n")

            def fake_git(_repo, *command, **_kwargs):
                if command[:2] == ("merge-base", "HEAD"):
                    output = "base\n"
                elif command[:2] == ("diff", "--name-only") and "base..origin/main" in command:
                    output = ""
                else:
                    output = "unrelated.txt\n"
                return subprocess.CompletedProcess([], 0, output, "")

            with patch.object(campaign, "_git", side_effect=fake_git):
                with self.assertRaisesRegex(RuntimeError, "allowlist"):
                    campaign.set_variant(
                        repo, campaign.Variant(1, "go", "nethttp"), IMAGE, "attempt"
                    )

    def test_set_variant_persists_ramp_and_old_deployments_within_allowlist(self):
        yaml = campaign._yaml()
        with tempfile.TemporaryDirectory(prefix="ephemeral-") as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True
            )
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            for path in (campaign.APP_PATH, *campaign.OLD_PATHS):
                document = deployment(replicas=1)
                document["spec"]["template"]["spec"]["containers"][0]["name"] = path.stem
                target = repo / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(yaml.safe_dump(document, sort_keys=False))
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "baseline"], check=True)

            with patch.object(campaign, "reject_remote_benchmark_conflict"):
                campaign.set_variant(repo, campaign.Variant(1, "go", "chi"), IMAGE, "attempt-2")

            ramp = yaml.safe_load((repo / campaign.APP_PATH).read_text())
            container = ramp["spec"]["template"]["spec"]["containers"][0]
            self.assertEqual(ramp["spec"]["replicas"], 1)
            self.assertEqual(container["image"], IMAGE)
            self.assertEqual(
                ramp["spec"]["template"]["metadata"]["annotations"][
                    "benchmark.jamoowen.dev/attempt-id"
                ],
                "attempt-2",
            )
            self.assertEqual(
                container["resources"],
                {
                    "requests": {"cpu": "1", "memory": "512Mi"},
                    "limits": {"cpu": "1", "memory": "512Mi"},
                },
            )
            self.assertEqual(
                {item["name"]: item["value"] for item in container["env"]},
                {
                    "FRAMEWORK": "chi",
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
            self.assertTrue(
                all(
                    yaml.safe_load((repo / path).read_text())["spec"]["replicas"] == 0
                    for path in campaign.OLD_PATHS
                )
            )
            changed = {
                Path(path)
                for path in campaign._git(repo, "diff", "--name-only").stdout.splitlines()
            }
            self.assertEqual(changed, campaign.ALLOWED_PATHS)

    def test_remote_conflict_checks_only_remote_divergence_and_never_forces(self):
        calls = []

        def fake_git(_repo, *command, **_kwargs):
            calls.append(command)
            if command[:2] == ("diff", "--name-only") and len(command) == 2:
                return subprocess.CompletedProcess([], 0, str(campaign.APP_PATH) + "\n", "")
            if command[:2] == ("merge-base", "HEAD"):
                return subprocess.CompletedProcess([], 0, "base\n", "")
            if command[:2] == ("diff", "--name-only"):
                return subprocess.CompletedProcess([], 0, "", "")
            if command[0] == "push":
                pushes = sum(call[0] == "push" for call in calls)
                return subprocess.CompletedProcess([], 1 if pushes == 1 else 0, "", "")
            if command[:2] == ("rev-parse", "HEAD"):
                return subprocess.CompletedProcess([], 0, "new-head\n", "")
            return subprocess.CompletedProcess([], 0, "", "")

        with (
            patch.object(campaign, "_git", side_effect=fake_git),
            patch.object(
                campaign.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
            ),
        ):
            self.assertEqual(campaign._commit_push(Path("repo"), "message"), "new-head")
        self.assertIn(("rebase", "origin/main"), calls)
        self.assertFalse(any("--force" in call for call in calls))

        def remote_benchmark(_repo, *command, **_kwargs):
            if command[:2] == ("merge-base", "HEAD"):
                return subprocess.CompletedProcess([], 0, "base\n", "")
            if command[:2] == ("diff", "--name-only"):
                return subprocess.CompletedProcess([], 0, str(campaign.APP_PATH) + "\n", "")
            return subprocess.CompletedProcess([], 0, "", "")

        with patch.object(campaign, "_git", side_effect=remote_benchmark):
            with self.assertRaisesRegex(RuntimeError, "concurrently"):
                campaign.reject_remote_benchmark_conflict(Path("repo"))

    def test_pod_contract_and_baseline_ready_require_exact_runtime_state(self):
        ready = pod()
        self.assertTrue(campaign._pod_runtime_ready(ready, "http-ramp"))
        for mutate in (
            lambda item: item["status"].pop("conditions"),
            lambda item: item["status"]["conditions"].__setitem__(
                0, {"type": "Ready", "status": "False"}
            ),
            lambda item: item["metadata"].update({"deletionTimestamp": "now"}),
        ):
            item = pod()
            mutate(item)
            self.assertFalse(campaign._pod_runtime_ready(item, "http-ramp"))
        stale = pod()
        stale["spec"]["containers"][0]["image"] = "stale"
        self.assertFalse(
            campaign.strict_readiness(
                deployment(),
                [stale],
                [{"spec": {"replicas": 0}}] * 3,
                campaign.Variant(1, "go", "nethttp"),
                IMAGE,
                "attempt",
            )[0]
        )

    def test_pod_image_identity_requires_named_spec_and_resolved_image_id(self):
        self.assertTrue(campaign.pod_container_image_matches(pod(), "http-ramp", IMAGE))
        docker_pullable = pod()
        docker_pullable["status"]["containerStatuses"][0]["imageID"] = f"docker-pullable://{IMAGE}"
        self.assertTrue(campaign.pod_container_image_matches(docker_pullable, "http-ramp", IMAGE))

        for mutate in (
            lambda item: item["status"]["containerStatuses"][0].pop("imageID"),
            lambda item: item["status"]["containerStatuses"][0].update({"imageID": "wrong"}),
            lambda item: item["spec"]["containers"][0].update({"image": "wrong"}),
            lambda item: item["spec"]["containers"][0].update({"name": "wrong"}),
            lambda item: item["status"]["containerStatuses"][0].update({"name": "wrong"}),
        ):
            item = pod()
            mutate(item)
            self.assertFalse(campaign.pod_container_image_matches(item, "http-ramp", IMAGE))

    def test_restore_waits_when_baseline_bytes_are_already_current(self):
        args = argparse.Namespace(results_dir=Path("results"))
        baseline = {campaign.APP_PATH: b"baseline"}
        with (
            patch.object(campaign, "reject_remote_benchmark_conflict"),
            patch.object(campaign, "restore_bytes"),
            patch.object(
                campaign,
                "_git",
                side_effect=(
                    subprocess.CompletedProcess([], 0, "", ""),
                    subprocess.CompletedProcess([], 0, "head\n", ""),
                ),
            ),
            patch.object(campaign, "wait_for_baseline") as wait,
        ):
            self.assertIsNone(campaign.restore_remote(args, Path("repo"), baseline))
        wait.assert_called_once_with(args, "head", baseline)

    def test_run_restores_once_on_normal_and_error_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(
                execute=True,
                image_map=Path("images.json"),
                results_dir=Path(directory),
                resume=False,
                cluster_repo=Path("repo"),
                attempt_id=None,
            )
            with ExitStack() as stack:
                stack.enter_context(patch.object(campaign, "image_map", return_value={}))
                stack.enter_context(patch.object(campaign, "generator_preflight", return_value={}))
                stack.enter_context(
                    patch.object(campaign, "safe_cluster_repo", return_value=Path("repo"))
                )
                stack.enter_context(patch.object(campaign, "snapshot", return_value={}))
                stack.enter_context(patch.object(campaign, "persist_baseline"))
                stack.enter_context(
                    patch.object(campaign, "_load_journal", return_value={"runs": []})
                )
                stack.enter_context(
                    patch.object(campaign, "plan", return_value={"schedule": [], "runs": []})
                )
                restore = stack.enter_context(patch.object(campaign, "restore_remote"))
                self.assertEqual(campaign.run(args), 0)
                restore.assert_called_once()

            row = {
                "order": 1,
                "runtime": "go",
                "framework": "nethttp",
                "identity": "identity",
                "image": IMAGE,
                "loadHash": "load",
                "scheduleHash": "schedule",
            }
            with ExitStack() as stack:
                stack.enter_context(patch.object(campaign, "image_map", return_value={}))
                stack.enter_context(patch.object(campaign, "generator_preflight", return_value={}))
                stack.enter_context(
                    patch.object(campaign, "safe_cluster_repo", return_value=Path("repo"))
                )
                stack.enter_context(patch.object(campaign, "snapshot", return_value={}))
                stack.enter_context(patch.object(campaign, "persist_baseline"))
                stack.enter_context(
                    patch.object(campaign, "_load_journal", return_value={"runs": []})
                )
                stack.enter_context(
                    patch.object(campaign, "plan", return_value={"schedule": [], "runs": [row]})
                )
                stack.enter_context(
                    patch.object(
                        campaign, "set_variant", side_effect=RuntimeError("activation failed")
                    )
                )
                stack.enter_context(
                    patch.object(
                        campaign,
                        "_git",
                        return_value=subprocess.CompletedProcess([], 0, "controller\n", ""),
                    )
                )
                restore = stack.enter_context(patch.object(campaign, "restore_remote"))
                stack.enter_context(patch.object(campaign, "_recovery"))
                with self.assertRaisesRegex(RuntimeError, "activation failed"):
                    campaign.run(args)
                restore.assert_called_once()

    def test_fresh_campaign_does_not_overwrite_a_persisted_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            results = Path(directory)
            (results / "campaign-baseline.json").write_text("preserve me")
            args = argparse.Namespace(
                execute=True,
                image_map=Path("images.json"),
                results_dir=results,
                resume=False,
                cluster_repo=Path("repo"),
            )
            with (
                patch.object(campaign, "image_map", return_value={}),
                patch.object(campaign, "plan", return_value={"schedule": [], "runs": []}),
                patch.object(campaign, "generator_preflight", return_value={}),
                patch.object(campaign, "safe_cluster_repo", return_value=Path("repo")),
                patch.object(campaign, "snapshot") as snapshot,
            ):
                with self.assertRaisesRegex(RuntimeError, "refusing to overwrite baseline"):
                    campaign.run(args)
            snapshot.assert_not_called()
            self.assertEqual((results / "campaign-baseline.json").read_text(), "preserve me")

    def test_resume_preserves_exact_persisted_baseline_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            results = Path(directory)
            baseline = {path: f"original {path}\n".encode() for path in campaign.ALLOWED_PATHS}
            initial_args = argparse.Namespace(results_dir=results)
            baseline_file = campaign.persist_baseline(initial_args, baseline)
            before = baseline_file.read_bytes()
            args = argparse.Namespace(
                execute=True,
                image_map=Path("images.json"),
                results_dir=results,
                resume=True,
                cluster_repo=Path("repo"),
            )
            with (
                patch.object(campaign, "image_map", return_value={}),
                patch.object(campaign, "plan", return_value={"schedule": [], "runs": []}),
                patch.object(campaign, "generator_preflight", return_value={}),
                patch.object(campaign, "safe_cluster_repo", return_value=Path("repo")),
                patch.object(campaign, "snapshot") as snapshot,
                patch.object(campaign, "persist_baseline") as persist,
                patch.object(campaign, "restore_remote"),
            ):
                self.assertEqual(campaign.run(args), 0)
            snapshot.assert_not_called()
            persist.assert_not_called()
            self.assertEqual(baseline_file.read_bytes(), before)

    def test_retain_invalid_attempt_cli_requires_resume_and_unique_canonical_uuid(self):
        common = [
            "--cluster-repo",
            "ephemeral/cluster",
            "--ssh-host",
            "user@host",
            "--node-ip",
            "127.0.0.1",
            "--source-revision",
            "a" * 40,
            "--results-dir",
            "results",
            "--execute",
            "--image-map",
            "images.json",
            "--retain-invalid-attempt",
            RETAINED_ATTEMPT,
        ]
        with self.assertRaises(SystemExit):
            campaign.arguments(common)
        with self.assertRaises(SystemExit):
            campaign.arguments([*common, "--resume", "--retain-invalid-attempt", "not-a-uuid"])
        with self.assertRaises(SystemExit):
            campaign.arguments([*common, "--resume", "--retain-invalid-attempt", RETAINED_ATTEMPT])

    def test_retained_invalid_attempt_rejects_invalid_prior_state(self):
        row = self._row(1, "identity")
        with tempfile.TemporaryDirectory() as directory:
            results = Path(directory)
            args = self._resume_args(results, [RETAINED_ATTEMPT])
            invalid_entry = {**row, "attemptId": RETAINED_ATTEMPT, "status": "invalid"}
            cases = (
                ({"runs": []}, "does not identify"),
                ({"runs": [{**invalid_entry, "status": "complete"}]}, "must identify an invalid"),
                ({"runs": [{**invalid_entry, "status": "recording"}]}, "must identify an invalid"),
                (
                    {"runs": [{**invalid_entry, "identity": "different"}]},
                    "identity does not match",
                ),
                (
                    {
                        "runs": [
                            invalid_entry,
                            {
                                **invalid_entry,
                                "attemptId": "f57f571-2a63-413f-b747-cb3f521d3f2c",
                            },
                        ]
                    },
                    "latest prior",
                ),
                ({"runs": [invalid_entry]}, "readable result"),
            )
            for journal, message in cases:
                with self.subTest(message=message):
                    with self.assertRaisesRegex(RuntimeError, message):
                        campaign.retained_invalid_attempts(args, journal, [row])

            self._write_result(results, row, RETAINED_ATTEMPT, "valid")
            with self.assertRaisesRegex(RuntimeError, "invalid result"):
                campaign.retained_invalid_attempts(args, {"runs": [invalid_entry]}, [row])

            path = self._write_result(results, row, RETAINED_ATTEMPT, "invalid", ["oom"])
            value = json.loads(path.read_text())
            value["counts"]["requests"] = 0
            path.write_text(json.dumps(value))
            self.assertIn(
                "identity",
                campaign.retained_invalid_attempts(args, {"runs": [invalid_entry]}, [row]),
            )

            value["counts"] = {}
            path.write_text(json.dumps(value))
            self.assertIn(
                "identity",
                campaign.retained_invalid_attempts(args, {"runs": [invalid_entry]}, [row]),
            )

            for requests in (-1, True, "1", None):
                value["counts"] = {"requests": requests}
                path.write_text(json.dumps(value))
                with self.subTest(requests=requests):
                    with self.assertRaisesRegex(RuntimeError, "invalid result"):
                        campaign.retained_invalid_attempts(args, {"runs": [invalid_entry]}, [row])

            self._write_result(results, row, RETAINED_ATTEMPT, "invalid", [""])
            with self.assertRaisesRegex(RuntimeError, "invalid result"):
                campaign.retained_invalid_attempts(args, {"runs": [invalid_entry]}, [row])

            path = self._write_result(results, row, RETAINED_ATTEMPT, "invalid", ["oom"])
            value = json.loads(path.read_text())
            value["metadata"]["image"] = "wrong"
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(RuntimeError, "metadata"):
                campaign.retained_invalid_attempts(args, {"runs": [invalid_entry]}, [row])

            path = self._write_result(results, row, RETAINED_ATTEMPT, "valid")
            value = json.loads(path.read_text())
            value["counts"]["requests"] = 0
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(RuntimeError, "valid request"):
                campaign.validate_result(args, row, RETAINED_ATTEMPT)

    def test_run_retains_invalid_skips_it_and_restores_after_next_variant(self):
        invalid = self._row(1, "invalid")
        complete = self._row(2, "complete", "chi")
        next_row = self._row(3, "next", "fiber")
        complete_attempt = "e57f571-2a63-413f-b747-cb3f521d3f2c"
        next_attempt = "f57f571-2a63-413f-b747-cb3f521d3f2c"
        journal = {
            "runs": [
                {**invalid, "attemptId": RETAINED_ATTEMPT, "status": "invalid"},
                {**complete, "attemptId": complete_attempt, "status": "complete"},
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            results = Path(directory)
            args = self._resume_args(results, [RETAINED_ATTEMPT])
            invalid_path = self._write_result(
                results, invalid, RETAINED_ATTEMPT, "invalid", ["kernel_oom"]
            )
            before = invalid_path.read_bytes()
            self._write_result(results, complete, complete_attempt, "valid")
            with ExitStack() as stack:
                stack.enter_context(patch.object(campaign, "image_map", return_value={}))
                stack.enter_context(patch.object(campaign, "generator_preflight", return_value={}))
                stack.enter_context(
                    patch.object(campaign, "safe_cluster_repo", return_value=Path("repo"))
                )
                stack.enter_context(
                    patch.object(campaign, "load_persisted_baseline", return_value={})
                )
                stack.enter_context(
                    patch.object(
                        campaign,
                        "plan",
                        return_value={"schedule": [], "runs": [invalid, complete, next_row]},
                    )
                )
                stack.enter_context(patch.object(campaign, "_load_journal", return_value=journal))
                source_git = stack.enter_context(
                    patch.object(
                        campaign,
                        "_git",
                        side_effect=lambda repo, *_command: subprocess.CompletedProcess(
                            [],
                            0,
                            "performance-source\n"
                            if repo == Path(campaign.__file__).resolve().parents[4]
                            else "cluster-source\n",
                            "",
                        ),
                    )
                )
                stack.enter_context(patch.object(campaign.uuid, "uuid4", return_value=next_attempt))
                set_variant = stack.enter_context(
                    patch.object(
                        campaign, "set_variant", side_effect=RuntimeError("stop after next")
                    )
                )
                validate_complete = stack.enter_context(
                    patch.object(campaign, "validate_result", wraps=campaign.validate_result)
                )
                restore = stack.enter_context(patch.object(campaign, "restore_remote"))
                stack.enter_context(patch.object(campaign, "_recovery"))
                with self.assertRaisesRegex(RuntimeError, "stop after next"):
                    campaign.run(args)

            self.assertEqual(invalid_path.read_bytes(), before)
            self.assertEqual(journal["runs"][0]["status"], "invalid")
            self.assertTrue(journal["runs"][0]["retainedInvalid"])
            self.assertEqual(journal["runs"][0]["resultPath"], str(invalid_path))
            validate_complete.assert_called_once_with(args, complete, complete_attempt)
            self.assertEqual(journal["runs"][2]["controllerSourceRevision"], "performance-source")
            source_git.assert_called_once_with(
                Path(campaign.__file__).resolve().parents[4], "rev-parse", "HEAD"
            )
            set_variant.assert_called_once_with(
                Path("repo"), campaign.Variant(3, "go", "fiber"), IMAGE, next_attempt
            )
            restore.assert_called_once()

    def test_resume_retries_retained_invalid_without_explicit_uuid(self):
        row = self._row(1, "invalid")
        journal = {
            "runs": [
                {
                    **row,
                    "attemptId": RETAINED_ATTEMPT,
                    "status": "invalid",
                    "retainedInvalid": True,
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            args = self._resume_args(Path(directory), [])
            with ExitStack() as stack:
                stack.enter_context(patch.object(campaign, "image_map", return_value={}))
                stack.enter_context(patch.object(campaign, "generator_preflight", return_value={}))
                stack.enter_context(
                    patch.object(campaign, "safe_cluster_repo", return_value=Path("repo"))
                )
                stack.enter_context(
                    patch.object(campaign, "load_persisted_baseline", return_value={})
                )
                stack.enter_context(
                    patch.object(campaign, "plan", return_value={"schedule": [], "runs": [row]})
                )
                stack.enter_context(patch.object(campaign, "_load_journal", return_value=journal))
                stack.enter_context(
                    patch.object(
                        campaign,
                        "_git",
                        return_value=subprocess.CompletedProcess([], 0, "controller\n", ""),
                    )
                )
                set_variant = stack.enter_context(
                    patch.object(campaign, "set_variant", side_effect=RuntimeError("retrying"))
                )
                stack.enter_context(patch.object(campaign, "restore_remote"))
                stack.enter_context(patch.object(campaign, "_recovery"))
                with self.assertRaisesRegex(RuntimeError, "retrying"):
                    campaign.run(args)
            set_variant.assert_called_once()

    def test_execute_rejects_fixed_attempt_and_schedule_ambiguity(self):
        common = [
            "--cluster-repo",
            "ephemeral/cluster",
            "--ssh-host",
            "user@host",
            "--node-ip",
            "127.0.0.1",
            "--source-revision",
            "a" * 40,
            "--results-dir",
            "results",
            "--execute",
            "--image-map",
            "images.json",
        ]
        with self.assertRaises(SystemExit):
            campaign.arguments([*common, "--attempt-id", "fixed"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "schedule.json"
            path.write_text(
                json.dumps(
                    [
                        {
                            "targetRps": 300,
                            "transitionSeconds": 0,
                            "stableSeconds": 1,
                            "unknown": 1,
                        },
                    ]
                )
            )
            with self.assertRaisesRegex(RuntimeError, "stages"):
                campaign.schedule_for(argparse.Namespace(schedule_json=path))
            path.write_text(
                json.dumps(
                    [
                        {"targetRps": 300, "transitionSeconds": 0, "stableSeconds": 1},
                        {"targetRps": 300, "transitionSeconds": 0, "stableSeconds": 1},
                    ]
                )
            )
            with self.assertRaisesRegex(RuntimeError, "strictly ascending"):
                campaign.schedule_for(argparse.Namespace(schedule_json=path))

    def test_recorder_command_preserves_identity_and_local_only(self):
        args = argparse.Namespace(
            source_revision="app-revision",
            harness_source_revision="harness-revision",
            node_ip="192.168.1.4",
            namespace="my-api",
            results_dir=Path("results"),
            k6="k6",
            local_only=True,
            ssh_host="unused",
        )
        command = campaign.recorder_command(
            args,
            campaign.Variant(1, "go", "nethttp"),
            IMAGE,
            "attempt",
            campaign.PRODUCTION_SCHEDULE,
            "flux-revision",
        )
        self.assertIn("--local-only", command)
        self.assertNotIn("--ssh-host", command)
        self.assertEqual(command[command.index("--image") + 1], IMAGE)
        self.assertEqual(command[command.index("--flux-revision") + 1], "flux-revision")
        self.assertEqual(command[command.index("--preallocated-vus") + 1], "3200")


if __name__ == "__main__":
    unittest.main()
