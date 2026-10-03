import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from benchmarks.http.capacity import telemetry, telemetry_remote

UID = "db22d766-97c7-4e5c-ae16-ecf736230b4b"
INITIAL = "a" * 64
RESTARTED = "b" * 64


def identity(container_id=INITIAL, restarts=0, reason=None, ready=True):
    return {
        "uid": UID,
        "deleting": False,
        "ready": ready,
        "container_id": container_id,
        "restarts": restarts,
        "reason": reason,
        "exitCode": 137 if reason else None,
        "finishedAt": "2026-10-03T00:00:00Z" if reason else None,
        "state": "running",
        "lastState": "terminated" if reason else None,
    }


def raw_sample(kind="sample", container_id=None, usage=0, oom=0, monotonic=0):
    value = {
        "kind": kind,
        "scope": "pod" if kind == "sample" else "container",
        "monotonic_ns": monotonic,
        "realtime_ns": monotonic + 1_700_000_000_000_000_000,
        "cpu_stat": {"usage_usec": usage, "throttled_usec": 0, "nr_periods": 1, "nr_throttled": 0},
        "memory_current": 100,
        "inactive_file": 0,
        "memory_events": {"oom_kill": oom},
        "cpu_pressure": "some avg10=0.00 avg60=0.00 avg300=0.00 total=0",
        "memory_pressure": "some avg10=0.00 avg60=0.00 avg300=0.00 total=0",
        "node_cpu": {"user": monotonic, "system": 0, "idle": monotonic, "iowait": 0},
    }
    if container_id:
        value["containerId"] = container_id
    return value


class RemoteObserverTests(unittest.TestCase):
    def test_optional_memory_peak_is_not_a_missing_sample_failure(self):
        with patch.object(telemetry_remote, "text", side_effect=FileNotFoundError):
            self.assertIsNone(telemetry_remote.optional_int("/missing/memory.peak"))

    def test_parent_root_requires_uid_ancestor(self):
        root = f"/sys/fs/cgroup/kubepods.slice/pod{UID.replace('-', '_')}.slice/cri-containerd-{INITIAL}.scope"
        self.assertIn("pod", telemetry_remote.pod_cgroup_for(root, UID))
        with self.assertRaisesRegex(RuntimeError, "pod_cgroup_uid_mismatch"):
            telemetry_remote.pod_cgroup_for(f"/sys/fs/cgroup/kubepods.slice/x/{INITIAL}", UID)

    def test_initial_identity_rejects_stale_container(self):
        emitted = []
        with (
            patch.object(telemetry_remote, "cgroup_for", return_value="/container"),
            patch.object(telemetry_remote, "pod_cgroup_for", return_value="/pod"),
            patch.object(telemetry_remote, "pod_identity", return_value=identity(RESTARTED)),
            patch.object(telemetry_remote, "emit", side_effect=emitted.append),
            patch.object(telemetry_remote.signal, "signal"),
        ):
            with self.assertRaisesRegex(RuntimeError, "initial_container_identity_mismatch"):
                telemetry_remote.main(["ns", "pod", "app", UID, INITIAL, "3", "1"])
        self.assertEqual(emitted[-1]["code"], "initial_container_identity_mismatch")

    def test_readiness_change_is_not_a_restart(self):
        self.assertIsNone(telemetry_remote.lifecycle_event(identity(), identity(ready=False)))

    def test_termination_is_deduplicated_by_restart_count_and_finished_at(self):
        terminated = identity(RESTARTED, 1, "OOMKilled")
        event = telemetry_remote.lifecycle_event(identity(), terminated)
        self.assertEqual(event[0], "oom")
        self.assertEqual(event[1], (1, "2026-10-03T00:00:00Z"))
        self.assertIsNone(telemetry_remote.lifecycle_event(terminated, terminated))

    def test_kubectl_identity_uses_bounded_timeout(self):
        payload = {"metadata": {"uid": UID}, "status": {"containerStatuses": []}}
        with patch.object(
            telemetry_remote.subprocess, "check_output", return_value=json.dumps(payload)
        ) as run:
            telemetry_remote.pod_identity("ns", "pod", "app")
        self.assertEqual(run.call_args.kwargs["timeout"], 5)

    def test_remote_loop_keeps_pod_samples_and_reason_through_restart(self):
        emitted = []
        identities = iter([identity(), identity(), identity(RESTARTED, 1, "OOMKilled")])
        calls = {"pod": 0, "container": 0}

        def fake_sample(_root, scope):
            calls[scope] += 1
            if scope == "container" and calls[scope] == 2:
                raise FileNotFoundError("deleted after OOM")
            return raw_sample(
                "sample" if scope == "pod" else "container_sample",
                INITIAL if scope == "container" else None,
                usage=calls[scope] * 100,
                oom=1 if scope == "pod" else 0,
                monotonic=calls[scope] * 1_000_000_000,
            )

        ticks = iter([0, 0, 3, 3, 6, 6, 9])
        with (
            patch.object(telemetry_remote, "cgroup_for", return_value="/container-b"),
            patch.object(telemetry_remote, "pod_cgroup_for", return_value="/pod"),
            patch.object(
                telemetry_remote,
                "pod_identity",
                side_effect=lambda *_: next(identities, identity(RESTARTED, 1, "OOMKilled")),
            ),
            patch.object(telemetry_remote, "sample", side_effect=fake_sample),
            patch.object(telemetry_remote, "emit", side_effect=emitted.append),
            patch.object(telemetry_remote.time, "monotonic", side_effect=lambda: next(ticks, 10)),
            patch.object(telemetry_remote.time, "sleep", side_effect=lambda _: None),
            patch.object(telemetry_remote.signal, "signal"),
        ):
            telemetry_remote.STOP_REQUESTED = False
            telemetry_remote.main(["ns", "pod", "app", UID, INITIAL, "7", "1"])

        self.assertGreaterEqual(len([item for item in emitted if item.get("scope") == "pod"]), 2)
        self.assertTrue(
            any(item.get("type") == "oom" and item.get("reason") == "OOMKilled" for item in emitted)
        )
        self.assertTrue(any(item.get("type") == "container_missing" for item in emitted))
        self.assertTrue(
            any(
                item.get("type") == "oom" and item.get("source") == "cgroup_memory_events"
                for item in emitted
            )
        )
        self.assertEqual(emitted[-1]["kind"], "end")

    def test_uid_replacement_is_capture_error(self):
        emitted = []
        replaced = {**identity(), "uid": "00000000-0000-0000-0000-000000000000"}
        with (
            patch.object(telemetry_remote, "cgroup_for", return_value="/container"),
            patch.object(telemetry_remote, "pod_cgroup_for", return_value="/pod"),
            patch.object(telemetry_remote, "pod_identity", side_effect=[identity(), replaced]),
            patch.object(telemetry_remote, "emit", side_effect=emitted.append),
            patch.object(telemetry_remote.time, "monotonic", side_effect=[0, 0, 0]),
            patch.object(telemetry_remote.signal, "signal"),
        ):
            telemetry_remote.STOP_REQUESTED = False
            with self.assertRaisesRegex(RuntimeError, "pod_identity_changed"):
                telemetry_remote.main(["ns", "pod", "app", UID, INITIAL, "3", "1"])
        self.assertEqual(emitted[-1]["kind"], "error")

    def test_final_identity_reports_termination_since_last_poll(self):
        emitted = []
        identities = iter([identity(), identity(), identity(RESTARTED, 1, "OOMKilled")])
        with (
            patch.object(telemetry_remote, "cgroup_for", return_value="/container"),
            patch.object(telemetry_remote, "pod_cgroup_for", return_value="/pod"),
            patch.object(telemetry_remote, "pod_identity", side_effect=lambda *_: next(identities)),
            patch.object(
                telemetry_remote,
                "sample",
                side_effect=lambda _root, scope: raw_sample(
                    "sample" if scope == "pod" else "container_sample"
                ),
            ),
            patch.object(telemetry_remote, "emit", side_effect=emitted.append),
            patch.object(telemetry_remote.time, "monotonic", side_effect=[0, 0, 0, 5]),
            patch.object(telemetry_remote.time, "sleep"),
            patch.object(telemetry_remote.signal, "signal"),
        ):
            telemetry_remote.STOP_REQUESTED = False
            telemetry_remote.main(["ns", "pod", "app", UID, INITIAL, "3", "1"])
        event = next(item for item in emitted if item.get("source") == "kubernetes")
        self.assertEqual(event["type"], "oom")
        self.assertEqual(event["finishedAt"], "2026-10-03T00:00:00Z")


class PodCollectorTests(unittest.TestCase):
    def collector(self, directory):
        return telemetry.PodCollector("user@host", "ns", "pod", "app", UID, INITIAL, 10, directory)

    def test_derives_container_counters_only_within_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = self.collector(directory)
            collector.raw_samples = [
                raw_sample(usage=0, monotonic=0),
                raw_sample(usage=1000, monotonic=1_000_000_000),
            ]
            collector.raw_container_samples = [
                raw_sample("container_sample", INITIAL, 0, monotonic=0),
                raw_sample("container_sample", INITIAL, 1000, monotonic=1_000_000_000),
                raw_sample("container_sample", RESTARTED, 0, monotonic=2_000_000_000),
            ]
            result = collector.result()
        self.assertEqual(len(result["samples"]), 1)
        self.assertEqual(len(result["containerSamples"]), 1)
        self.assertEqual(result["containerSamples"][0]["containerSegment"], 0)

    def test_same_container_counter_reset_is_a_reported_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = self.collector(directory)
            collector.raw_container_samples = [
                raw_sample("container_sample", INITIAL, 1000, monotonic=0),
                raw_sample("container_sample", INITIAL, 10, monotonic=1_000_000_000),
            ]
            result = collector.result()
        self.assertEqual(result["containerSamples"], [])
        self.assertIn("collector_gap:counter_reset", result["errors"])

    def test_durable_jsonl_preserves_prior_records_when_remote_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = self.collector(directory)
            collector._raw_log = (Path(directory) / "pod-telemetry.jsonl").open(
                "w", encoding="utf-8"
            )
            collector._stderr_log = (Path(directory) / "pod-telemetry.stderr").open(
                "w", encoding="utf-8"
            )
            os.chmod(Path(directory) / "pod-telemetry.jsonl", 0o600)
            collector._process = Mock(
                stdout=io.StringIO(json.dumps(raw_sample()) + "\n[]\n{"), stderr=io.StringIO()
            )
            collector._read_stdout()
            collector._raw_log.close()
            collector._stderr_log.close()
            text = (Path(directory) / "pod-telemetry.jsonl").read_text()
            self.assertIn('"kind": "sample"', text)
            self.assertIn("invalid_remote_json", collector.result()["errors"])
            self.assertIn("invalid_remote_record", collector.result()["errors"])
            self.assertEqual(
                os.stat(Path(directory) / "pod-telemetry.jsonl").st_mode & 0o777, 0o600
            )

    def test_graceful_stop_uses_only_metadata_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = self.collector(directory)
            collector._process = Mock()
            collector._process.poll.return_value = None
            collector._remote_pid = 17
            collector._metadata_event.set()
            with patch(
                "benchmarks.http.capacity.telemetry.subprocess.run", return_value=Mock(returncode=0)
            ) as run:
                collector.stop()
        self.assertIn("kill -TERM 17", run.call_args.args[0][-1])

    def test_invalid_root_rejected_before_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "log_dir"):
                self.collector(Path(directory) / "absent").start()

    def test_existing_telemetry_log_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pod-telemetry.jsonl"
            path.write_text("prior evidence\n")
            with self.assertRaises(FileExistsError):
                self.collector(directory).start()
            self.assertEqual(path.read_text(), "prior evidence\n")


if __name__ == "__main__":
    unittest.main()
