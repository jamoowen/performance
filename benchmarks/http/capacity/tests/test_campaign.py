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
