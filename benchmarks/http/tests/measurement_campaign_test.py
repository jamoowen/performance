import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from measure import campaign


class CampaignPlanTest(unittest.TestCase):
    def args(self, *extra):
        return campaign.arguments(
            [
                "--cluster-repo",
                "/tmp/cluster",
                "--ssh-host",
                "bench@example",
                "--node-ip",
                "192.168.1.2",
                "--stage",
                "scheduling",
                "--dry-run",
                *extra,
            ]
        )

    def test_scheduling_is_alternating_three_repetitions_per_setting(self):
        plan = campaign.build_plan("scheduling")
        self.assertEqual([entry.workers for entry in plan], [2, 1, 2, 1, 2, 1])
        self.assertEqual([entry.repetition for entry in plan], [1, 1, 2, 2, 3, 3])
        self.assertEqual({entry.rate for entry in plan}, {600})
        self.assertEqual({entry.backend for entry in plan}, {"sqlite"})

    def test_plans_are_deterministic_and_bounded(self):
        first = campaign.build_plan("all")
        self.assertEqual(first, campaign.build_plan("all"))
        self.assertEqual(len(first), 54)
        self.assertEqual(len({entry.fingerprint for entry in first}), len(first))

    def test_scaling_uses_immutable_memory_list_at_both_rates(self):
        plan = campaign.build_plan("scaling")
        self.assertEqual(len(plan), 12)
        self.assertEqual({entry.rate for entry in plan}, {600, 3000})
        self.assertEqual({entry.workload for entry in plan}, {"list"})
        self.assertEqual({entry.backend for entry in plan}, {"memory"})
        self.assertEqual(
            {entry.router for entry in plan if entry.implementation == "rust"}, {"axum"}
        )

    def test_rust_uses_axum_in_cross_runtime_stages(self):
        for stage in ("sqlite", "memory", "scaling"):
            self.assertEqual(
                {
                    entry.router
                    for entry in campaign.build_plan(stage)
                    if entry.implementation == "rust"
                },
                {"axum"},
            )

    def test_dry_run_renders_safe_recorder_command(self):
        payload = campaign.render_plan(self.args())
        self.assertEqual(payload["run_count"], 6)
        self.assertNotIn("--diagnostics", payload["runs"][0]["command"])
        self.assertIn("--experiment", payload["runs"][0]["command"])
        self.assertIn("30080", " ".join(payload["runs"][0]["command"]))

    def test_execution_requires_exact_digest(self):
        with self.assertRaises(SystemExit):
            campaign.arguments(
                [
                    "--cluster-repo",
                    str(Path("/tmp/cluster")),
                    "--ssh-host",
                    "host",
                    "--node-ip",
                    "10.0.0.1",
                    "--stage",
                    "scheduling",
                ]
            )

    def test_activation_only_changes_benchmark_manifests_and_sets_literals(self):
        with TemporaryDirectory(dir="/private/tmp") as temporary:
            repo = Path(temporary) / "ephemeral" / "cluster"
            app = repo / "apps/performance-http"
            app.mkdir(parents=True)
            (repo / ".git").mkdir()
            for runtime in ("go", "bun"):
                (app / f"{runtime}-deployment.yaml").write_text(
                    "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: http-"
                    + runtime
                    + "\nspec:\n  replicas: 0\n  template:\n    metadata: {}\n    spec:\n      containers:\n        - name: http-"
                    + runtime
                    + "\n          image: old\n          env: []\n"
                )
            entry = campaign.build_plan("scheduling")[0]
            with patch.object(campaign, "_git", return_value=type("R", (), {"stdout": ""})()):
                campaign.activate_manifest(
                    repo,
                    entry,
                    "ghcr.io/owner/performance-http-go@sha256:" + "a" * 64,
                )
            go = (app / "go-deployment.yaml").read_text()
            bun = (app / "bun-deployment.yaml").read_text()
            self.assertIn("benchmark.jamoowen.dev/run-id", go)
            self.assertIn("BACKEND", go)
            self.assertIn("DIAGNOSTICS", go)
            self.assertIn("replicas: 1", go)
            self.assertIn("replicas: 0", bun)

    def executor_args(self, directory):
        return SimpleNamespace(
            dry_run=False,
            cluster_repo=directory,
            stage="scheduling",
            go_procs=2,
            results_dir=directory / "results",
            go_image="ghcr.io/owner/performance-http-go@sha256:" + "a" * 64,
            bun_image=None,
            rust_image=None,
            ssh_host="host",
            namespace="my-api",
            node_ip="192.168.1.2",
        )

    def test_threshold_results_continue_and_restore_once(self):
        with TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary)
            app = root / "apps/performance-http"
            app.mkdir(parents=True)
            go = app / "go-deployment.yaml"
            bun = app / "bun-deployment.yaml"
            go.write_text("replicas: 0\n")
            bun.write_text("replicas: 1\n")
            args = self.executor_args(root)
            entries = campaign.build_plan("scheduling")[:2]
            result = {"error": None}
            with (
                patch.object(campaign, "_safe_cluster_repo", return_value=(root, app)),
                patch.object(campaign, "build_plan", return_value=entries),
                patch.object(campaign, "_completed_fingerprints", return_value=set()),
                patch.object(campaign, "activate_manifest"),
                patch.object(
                    campaign, "_commit_activation", side_effect=["one", "two", "restore"]
                ) as commit,
                patch.object(campaign, "_wait_for_rollout") as wait,
                patch.object(campaign, "_wait_for_baseline") as baseline,
                patch.object(campaign, "_top_pods", return_value=None),
                patch.object(
                    campaign, "_result_for_fingerprint", return_value=(root / "result.json", result)
                ),
                patch.object(
                    campaign,
                    "_git",
                    return_value=SimpleNamespace(
                        stdout=" M apps/performance-http/go-deployment.yaml\n"
                    ),
                ),
                patch.object(
                    campaign.subprocess, "run", return_value=SimpleNamespace(returncode=99)
                ) as run,
            ):
                campaign.execute(args)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(wait.call_count, 2)
            self.assertEqual(baseline.call_count, 1)
            self.assertEqual(commit.call_count, 3)

    def test_infrastructure_failure_halts_and_restores(self):
        with TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary)
            app = root / "apps/performance-http"
            app.mkdir(parents=True)
            (app / "go-deployment.yaml").write_text("replicas: 0\n")
            (app / "bun-deployment.yaml").write_text("replicas: 1\n")
            args, entries = self.executor_args(root), campaign.build_plan("scheduling")[:2]
            with (
                patch.object(campaign, "_safe_cluster_repo", return_value=(root, app)),
                patch.object(campaign, "build_plan", return_value=entries),
                patch.object(campaign, "_completed_fingerprints", return_value=set()),
                patch.object(campaign, "activate_manifest"),
                patch.object(
                    campaign, "_commit_activation", side_effect=["one", "restore"]
                ) as commit,
                patch.object(campaign, "_wait_for_rollout"),
                patch.object(campaign, "_wait_for_baseline") as baseline,
                patch.object(campaign, "_top_pods", return_value=None),
                patch.object(campaign, "_result_for_fingerprint", return_value=(None, None)),
                patch.object(
                    campaign,
                    "_git",
                    return_value=SimpleNamespace(
                        stdout=" M apps/performance-http/go-deployment.yaml\n"
                    ),
                ),
                patch.object(
                    campaign.subprocess, "run", return_value=SimpleNamespace(returncode=1)
                ) as run,
            ):
                with self.assertRaisesRegex(RuntimeError, "operational"):
                    campaign.execute(args)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(baseline.call_count, 1)
            self.assertEqual(commit.call_count, 2)

    def test_dirty_failed_activation_restores_known_benchmark_files(self):
        with TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary)
            app = root / "apps/performance-http"
            app.mkdir(parents=True)
            go, bun = app / "go-deployment.yaml", app / "bun-deployment.yaml"
            go.write_text("replicas: 0\n")
            bun.write_text("replicas: 1\n")
            args, entry = self.executor_args(root), campaign.build_plan("scheduling")[:1]

            def dirty_activation(*_):
                go.write_text("replicas: 1\n")
                raise RuntimeError("activation failed")

            with (
                patch.object(campaign, "_safe_cluster_repo", return_value=(root, app)),
                patch.object(campaign, "build_plan", return_value=entry),
                patch.object(campaign, "_completed_fingerprints", return_value=set()),
                patch.object(campaign, "activate_manifest", side_effect=dirty_activation),
                patch.object(campaign, "_commit_activation", return_value="restore"),
                patch.object(campaign, "_wait_for_baseline"),
                patch.object(
                    campaign,
                    "_git",
                    return_value=SimpleNamespace(
                        stdout=" M apps/performance-http/go-deployment.yaml\n"
                    ),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "activation failed"):
                    campaign.execute(args)
            self.assertEqual(go.read_text(), "replicas: 0\n")
            self.assertEqual(bun.read_text(), "replicas: 1\n")

    def test_resume_requires_the_full_recorded_configuration(self):
        with TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary)
            args = self.executor_args(root)
            entry = campaign.build_plan("scheduling")[0]
            source = Path(campaign.__file__).resolve().parents[3]
            settings = {
                "profile": "steady",
                "duration": "2m",
                "warmup_duration": "60s",
                "seed_count": 5000,
                "preallocated_vus": 1000,
                "max_vus": 2000,
                "p95_ms": 1000,
                "max_error_rate": 0.01,
                "sample_interval": 5,
                "rate": entry.rate,
                "workload": entry.workload,
                "diagnostics": False,
            }
            defaults = deepcopy(settings)
            record = {
                "status": "invalid",
                "k6_exit_code": 99,
                "error": None,
                "collector_errors": [],
                "resource": {"warnings": [], "coverage": {"cpu_span_seconds": 1}},
                "metadata": {
                    "campaign_fingerprint": entry.fingerprint,
                    "source_git_commit": "commit",
                    "source_git_dirty": False,
                    "k6_version": "v",
                    "settings": settings,
                    "load_script_sha256": __import__("hashlib")
                    .sha256((source / "benchmarks/http/load.js").read_bytes())
                    .hexdigest(),
                    "cluster": {
                        "pod": {"image": args.go_image},
                        "workload": {
                            "configuration": {
                                "BACKEND": entry.backend,
                                "ROUTER": entry.router,
                                "WORKERS": str(entry.workers),
                                "GOMAXPROCS": str(entry.workers),
                                "DIAGNOSTICS": "0",
                            },
                            "resources": {
                                "requests": {"cpu": "1", "memory": "512Mi"},
                                "limits": {"cpu": "1", "memory": "512Mi"},
                            },
                        },
                    },
                },
            }
            result = root / "results/scheduling/run/result.json"
            result.parent.mkdir(parents=True)
            result.write_text(__import__("json").dumps(record))
            with (
                patch.object(campaign, "_git", return_value=SimpleNamespace(stdout="commit\n")),
                patch.object(campaign.subprocess, "check_output", return_value="v\n"),
            ):
                self.assertEqual(
                    campaign._completed_fingerprints(args, "scheduling", (entry,)),
                    {entry.fingerprint},
                )
                for key, value in (
                    ("duration", "9m"),
                    ("warmup_duration", "1s"),
                    ("preallocated_vus", 1),
                    ("max_vus", 2),
                    ("p95_ms", 2),
                    ("sample_interval", 2),
                ):
                    record["metadata"]["settings"][key] = value
                    result.write_text(__import__("json").dumps(record))
                    self.assertEqual(
                        campaign._completed_fingerprints(args, "scheduling", (entry,)), set()
                    )
                    record["metadata"]["settings"][key] = defaults[key]
                record["metadata"]["cluster"]["workload"]["resources"]["limits"]["memory"] = "1Mi"
                result.write_text(__import__("json").dumps(record))
                self.assertEqual(
                    campaign._completed_fingerprints(args, "scheduling", (entry,)), set()
                )

    def test_rollout_wait_rejects_stale_revision_then_accepts_new_revision(self):
        entry = campaign.build_plan("scheduling")[0]
        args = self.executor_args(Path("/private/tmp/cluster"))
        attempt = "attempt"
        env = {
            "SEED_COUNT": 5000,
            "BACKEND": "sqlite",
            "ROUTER": "stdlib",
            "WORKERS": 2,
            "DIAGNOSTICS": 0,
            "GOMAXPROCS": 2,
            "MAX_OPEN_CONNS": 1,
        }
        deployment = {
            "metadata": {"namespace": "my-api", "generation": 1},
            "spec": {
                "replicas": 1,
                "template": {
                    "metadata": {"annotations": {"benchmark.jamoowen.dev/run-id": attempt}},
                    "spec": {
                        "containers": [
                            {
                                "name": "http-go",
                                "image": args.go_image,
                                "env": [
                                    {"name": key, "value": str(value)} for key, value in env.items()
                                ],
                                "resources": {
                                    "requests": {"cpu": "1", "memory": "512Mi"},
                                    "limits": {"cpu": "1", "memory": "512Mi"},
                                },
                            }
                        ]
                    },
                },
            },
            "status": {"observedGeneration": 1, "readyReplicas": 1},
        }
        pods = {
            "items": [
                {
                    "metadata": {"labels": {"app.kubernetes.io/name": "http-go"}},
                    "status": {"phase": "Running"},
                }
            ]
        }
        responses = [
            {"status": {"lastAppliedRevision": "main@old"}},
            deployment,
            {"items": [deployment]},
            pods,
            {"status": {"lastAppliedRevision": "main@commit"}},
            deployment,
            {"items": [deployment]},
            pods,
        ]
        with (
            patch.object(campaign, "_request_reconcile"),
            patch.object(campaign, "_remote_json", side_effect=responses),
            patch.object(campaign.time, "monotonic", side_effect=[0, 0, 1]),
            patch.object(campaign.time, "sleep"),
        ):
            campaign._wait_for_rollout(args, entry, "commit", args.go_image, attempt)

    def test_push_rebases_narrow_commit_after_remote_advance_without_force(self):
        with (
            patch.object(
                campaign.subprocess,
                "run",
                side_effect=[SimpleNamespace(returncode=1), SimpleNamespace(returncode=0)],
            ) as push,
            patch.object(campaign, "_git", return_value=SimpleNamespace(stdout="")) as git,
        ):
            campaign._push_narrow_commit(Path("/private/tmp/ephemeral/cluster"))
        self.assertEqual(push.call_count, 2)
        self.assertEqual(
            git.call_args_list[0].args,
            (Path("/private/tmp/ephemeral/cluster"), "fetch", "origin", "main"),
        )
        self.assertEqual(
            git.call_args_list[1].args,
            (Path("/private/tmp/ephemeral/cluster"), "rebase", "origin/main"),
        )
        self.assertNotIn("--force", " ".join(push.call_args_list[1].args[0][0]))

    def test_push_aborts_rebase_on_conflict(self):
        with (
            patch.object(
                campaign.subprocess,
                "run",
                side_effect=[SimpleNamespace(returncode=1), SimpleNamespace(returncode=0)],
            ) as command,
            patch.object(
                campaign,
                "_git",
                side_effect=[
                    SimpleNamespace(stdout=""),
                    __import__("subprocess").CalledProcessError(1, "rebase"),
                ],
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "conflicts"):
                campaign._push_narrow_commit(Path("/private/tmp/ephemeral/cluster"))
        self.assertEqual(command.call_args_list[-1].args[0][-2:], ["rebase", "--abort"])


if __name__ == "__main__":
    unittest.main()
