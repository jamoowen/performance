import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmarks.http.capacity import campaign


class CampaignTests(unittest.TestCase):
    def test_dry_plan_omits_plug_and_rocket(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = campaign.arguments(
                [
                    "--cluster-repo",
                    temporary,
                    "--ssh-host",
                    "user@host",
                    "--node-ip",
                    "127.0.0.1",
                    "--source-revision",
                    "a" * 40,
                    "--results-dir",
                    temporary,
                ]
            )
            plan = campaign.plan(args)
        pairs = {(item["runtime"], item["framework"]) for item in plan["runs"]}
        self.assertEqual(len(pairs), 13)
        self.assertNotIn(("elixir", "plug"), pairs)
        self.assertNotIn(("rust", "rocket"), pairs)

    def test_completed_resume_requires_matching_readable_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = campaign.arguments(
                [
                    "--cluster-repo",
                    temporary,
                    "--ssh-host",
                    "user@host",
                    "--node-ip",
                    "127.0.0.1",
                    "--source-revision",
                    "a" * 40,
                    "--harness-source-revision",
                    "b" * 40,
                    "--results-dir",
                    temporary,
                ]
            )
            result = Path(temporary) / "result.json"
            row = {
                "attemptId": "id",
                "runtime": "go",
                "framework": "nethttp",
                "image": "image",
                "loadHash": "load",
                "protocolHash": "protocol",
                "resultPath": str(result),
            }
            result.write_text(
                json.dumps(
                    {
                        "metadata": {
                            **row,
                            "sourceRevision": "a" * 40,
                            "harnessSourceRevision": "b" * 40,
                        }
                    }
                )
            )
            campaign._validate_completed(args, row)
            result.write_text("{}")
            with self.assertRaisesRegex(RuntimeError, "metadata"):
                campaign._validate_completed(args, row)

    def test_completed_resume_rejects_cleanup_failure_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = campaign.arguments(
                [
                    "--cluster-repo",
                    temporary,
                    "--ssh-host",
                    "user@host",
                    "--node-ip",
                    "127.0.0.1",
                    "--source-revision",
                    "a" * 40,
                    "--results-dir",
                    temporary,
                ]
            )
            attempt = Path(temporary) / "id"
            attempt.mkdir()
            (attempt / "owned-process-cleanup-failure.json").write_text("{}")
            with self.assertRaisesRegex(RuntimeError, "owned_process_cleanup_failure"):
                campaign._validate_completed(args, {"attemptId": "id"})

    def test_execute_restores_baseline_when_activation_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # Use valid distinct hex digests while keeping the test fixture local.
            images = {
                runtime: f"ghcr.io/jamoowen/performance-http-ramp-{runtime}@sha256:{index:064x}"
                for index, runtime in enumerate(campaign.ramp.RUNTIMES, 1)
            }
            image_map = root / "images.json"
            image_map.write_text(json.dumps(images))
            args = campaign.arguments(
                [
                    "--cluster-repo",
                    str(root),
                    "--ssh-host",
                    "user@host",
                    "--node-ip",
                    "127.0.0.1",
                    "--source-revision",
                    "a" * 40,
                    "--harness-source-revision",
                    "b" * 40,
                    "--results-dir",
                    str(root / "results"),
                    "--image-map",
                    str(image_map),
                    "--execute",
                ]
            )
            with (
                patch.object(campaign.record, "preflight", return_value={}),
                patch.object(campaign.ramp, "safe_cluster_repo", return_value=root),
                patch.object(campaign.ramp, "snapshot", return_value={}),
                patch.object(campaign.ramp, "persist_baseline"),
                patch.object(campaign.ramp, "set_variant", side_effect=RuntimeError("activation")),
                patch.object(campaign.ramp, "restore_remote") as restore,
            ):
                with self.assertRaisesRegex(RuntimeError, "activation"):
                    campaign.execute(args)
            self.assertGreaterEqual(restore.call_count, 1)

    def test_cleanup_failure_marker_stops_before_next_adapter_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            images = {
                runtime: f"ghcr.io/jamoowen/performance-http-ramp-{runtime}@sha256:{index:064x}"
                for index, runtime in enumerate(campaign.ramp.RUNTIMES, 1)
            }
            image_map = root / "images.json"
            image_map.write_text(json.dumps(images))
            results = root / "results"
            args = campaign.arguments(
                [
                    "--cluster-repo",
                    str(root),
                    "--ssh-host",
                    "user@host",
                    "--node-ip",
                    "127.0.0.1",
                    "--source-revision",
                    "a" * 40,
                    "--harness-source-revision",
                    "b" * 40,
                    "--results-dir",
                    str(results),
                    "--image-map",
                    str(image_map),
                    "--execute",
                ]
            )

            def record_with_unreaped_child(command, **_kwargs):
                attempt = command[command.index("--attempt-id") + 1]
                directory = results / attempt
                directory.mkdir(parents=True)
                (directory / "owned-process-cleanup-failure.json").write_text("{}")
                return type("Completed", (), {"returncode": 1})()

            with (
                patch.object(campaign.record, "preflight", return_value={}),
                patch.object(campaign.ramp, "safe_cluster_repo", return_value=root),
                patch.object(campaign.ramp, "snapshot", return_value={}),
                patch.object(campaign.ramp, "persist_baseline"),
                patch.object(campaign.ramp, "set_variant") as set_variant,
                patch.object(campaign.ramp, "_commit_push", return_value="c" * 40),
                patch.object(campaign.ramp, "wait_for_flux"),
                patch.object(
                    campaign.subprocess, "run", side_effect=record_with_unreaped_child
                ) as run_record,
                patch.object(campaign.ramp, "_recovery"),
                patch.object(campaign.ramp, "restore_remote") as restore,
            ):
                with self.assertRaisesRegex(RuntimeError, "owned_process_cleanup_failure"):
                    campaign.execute(args)
            self.assertEqual(set_variant.call_count, 1)
            self.assertEqual(run_record.call_count, 1)
            self.assertGreaterEqual(restore.call_count, 2)
            journal = json.loads((results / "campaign-journal.json").read_text())
            self.assertEqual(journal["runs"][0]["status"], "failed")

    def test_resume_rejects_failed_cleanup_marker_before_adapter_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            images = {
                runtime: f"ghcr.io/jamoowen/performance-http-ramp-{runtime}@sha256:{index:064x}"
                for index, runtime in enumerate(campaign.ramp.RUNTIMES, 1)
            }
            image_map = root / "images.json"
            image_map.write_text(json.dumps(images))
            results = root / "results"
            args = campaign.arguments(
                [
                    "--cluster-repo",
                    str(root),
                    "--ssh-host",
                    "user@host",
                    "--node-ip",
                    "127.0.0.1",
                    "--source-revision",
                    "a" * 40,
                    "--harness-source-revision",
                    "b" * 40,
                    "--results-dir",
                    str(results),
                    "--image-map",
                    str(image_map),
                    "--execute",
                    "--resume",
                ]
            )
            row = campaign.plan(args, images)["runs"][0]
            row.update({"attemptId": "previous", "status": "failed"})
            results.mkdir()
            (results / "campaign-journal.json").write_text(json.dumps({"runs": [row]}))
            previous = results / "previous"
            previous.mkdir()
            (previous / "owned-process-cleanup-failure.json").write_text("{}")
            with (
                patch.object(campaign.record, "preflight", return_value={}),
                patch.object(campaign.ramp, "safe_cluster_repo", return_value=root),
                patch.object(campaign.ramp, "set_variant") as set_variant,
            ):
                with self.assertRaisesRegex(RuntimeError, "owned_process_cleanup_failure"):
                    campaign.execute(args)
            set_variant.assert_not_called()
