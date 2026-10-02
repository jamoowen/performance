import unittest
from unittest.mock import patch

from measure import cluster


class QuantityTest(unittest.TestCase):
    def test_cpu_and_memory_units(self):
        self.assertAlmostEqual(cluster.parse_quantity("82604n"), 0.000082604)
        self.assertEqual(cluster.parse_quantity("4Ki"), 4096)
        self.assertEqual(cluster.parse_quantity("2M"), 2_000_000)
        self.assertEqual(cluster.parse_quantity("1.5Gi"), 1.5 * 1024**3)

    def test_rejects_invalid_or_non_finite_quantities(self):
        for value in ("NaN", "Infinity", "1Pi", "", "1e999"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    cluster.parse_quantity(value)


class MetricParserTest(unittest.TestCase):
    def test_parses_quoted_commas_escapes_and_scientific_notation(self):
        line = 'container_cpu_usage_seconds_total{namespace="my-api",pod="http-go-abc",container="http-go",id="/x,\\"quoted\\"",cpu="total"} 1.25e+03 1234'
        self.assertEqual(
            cluster.exact_container_metric(
                line, "container_cpu_usage_seconds_total", "my-api", "http-go-abc", "http-go"
            ),
            {"value": 1250.0, "timestamp_ms": 1234, "id": '/x,"quoted"'},
        )

    def test_ignores_pause_parent_and_non_total_cpu(self):
        pause = 'container_cpu_usage_seconds_total{namespace="my-api",pod="p",container="POD",id="/x"} 2 1'
        self.assertIsNone(
            cluster.exact_container_metric(
                pause, "container_cpu_usage_seconds_total", "my-api", "p", "http-go"
            )
        )
        non_total = 'container_cpu_usage_seconds_total{namespace="my-api",pod="p",container="http-go",id="/x",cpu="1"} 2 1'
        self.assertIsNone(
            cluster.exact_container_metric(
                non_total, "container_cpu_usage_seconds_total", "my-api", "p", "http-go"
            )
        )


class CommandAndMetadataTest(unittest.TestCase):
    def test_rejects_hostile_namespace_and_host(self):
        with self.assertRaises(ValueError):
            cluster.ssh_command("host", "my-api;rm", "http-go", 1, "once")
        with self.assertRaises(ValueError):
            cluster.ssh_command("bad host", "my-api", "http-go", 1, "once")

    def test_ssh_command_is_one_quoted_remote_command(self):
        command = cluster.ssh_command("bench.example", "my-api", "http-go", 2.5, "stream")
        self.assertEqual(command[-1], "python3 -u - my-api http-go 2.5 stream")

    def test_metadata_rejects_other_active_runtime(self):
        deployment = {
            "metadata": {"name": "http-go"},
            "spec": {"replicas": 1, "template": {"spec": {"containers": [{"name": "http-go"}]}}},
        }
        selected = {
            "metadata": {
                "name": "go",
                "uid": "uid",
                "labels": {"app.kubernetes.io/name": "http-go"},
            },
            "spec": {"nodeName": "node"},
            "status": {
                "phase": "Running",
                "containerStatuses": [
                    {
                        "name": "http-go",
                        "ready": True,
                        "containerID": "containerd://one",
                        "state": {"running": {}},
                    }
                ],
            },
        }
        other = {
            "spec": {"replicas": 0},
            "metadata": {"name": "bun", "labels": {"app.kubernetes.io/name": "http-bun"}},
            "status": {"phase": "Pending"},
        }
        deployments = {"items": [{"metadata": {"name": "http-bun"}, "spec": {"replicas": 0}}]}
        with patch.object(
            cluster,
            "_json",
            side_effect=[deployment, deployments, {"items": [selected, other]}],
        ):
            with self.assertRaisesRegex(RuntimeError, "exactly one"):
                cluster.metadata("my-api", "http-go")

    def test_metadata_rejects_not_ready_selected_pod(self):
        deployment = {
            "metadata": {"name": "http-go"},
            "spec": {"replicas": 1, "template": {"spec": {"containers": [{"name": "http-go"}]}}},
        }
        pod = {
            "metadata": {
                "name": "go",
                "uid": "uid",
                "labels": {"app.kubernetes.io/name": "http-go"},
            },
            "spec": {"nodeName": "node"},
            "status": {
                "phase": "Running",
                "containerStatuses": [{"name": "http-go", "ready": False}],
            },
        }
        with patch.object(
            cluster,
            "_json",
            side_effect=[
                deployment,
                {"items": [{"metadata": {"name": "http-bun"}, "spec": {"replicas": 0}}]},
                {"items": [pod]},
                {"metadata": {"name": "node", "uid": "n"}},
            ],
        ):
            with self.assertRaisesRegex(RuntimeError, "not ready"):
                cluster.metadata("my-api", "http-go")

    def test_metadata_rejects_enabled_other_deployment(self):
        deployment = {
            "metadata": {"name": "http-go"},
            "spec": {"replicas": 1, "template": {"spec": {"containers": [{"name": "http-go"}]}}},
        }
        enabled = {"items": [{"metadata": {"name": "http-bun"}, "spec": {"replicas": 1}}]}
        with patch.object(cluster, "_json", side_effect=[deployment, enabled]):
            with self.assertRaisesRegex(RuntimeError, "zero replicas"):
                cluster.metadata("my-api", "http-go")


class SampleTest(unittest.TestCase):
    def test_essential_metrics_missing_fails(self):
        identity = {
            "node": {"name": "node"},
            "pod": {"name": "go", "uid": "p", "container_id": "containerd://current"},
        }
        with (
            patch.object(cluster, "_json", return_value={"usage": {"cpu": "1n", "memory": "1Ki"}}),
            patch.object(cluster, "kubectl", return_value=""),
        ):
            with self.assertRaisesRegex(RuntimeError, "CPU metric"):
                cluster.sample("my-api", identity, "http-go")

    def test_sample_ignores_stale_cgroup_before_current_runtime(self):
        identity = {
            "node": {"name": "node"},
            "pod": {"name": "go", "uid": "p", "container_id": "containerd://current"},
        }
        labels = 'namespace="my-api",pod="go",container="http-go",id="{}"'
        metrics = "\n".join(
            [
                f"container_cpu_usage_seconds_total{{{labels.format('/kubepods/stale')}}} 1 1000",
                f"container_cpu_usage_seconds_total{{{labels.format('/kubepods/current')}}} 2 2000",
                f"container_memory_working_set_bytes{{{labels.format('/kubepods/current')}}} 3 2000",
            ]
        )
        usage = {
            "usage": {"cpu": "82604n", "memory": "4100Ki"},
            "timestamp": "now",
            "window": "30s",
        }
        with (
            patch.object(cluster, "_json", return_value=usage),
            patch.object(cluster, "kubectl", return_value=metrics),
        ):
            event = cluster.sample("my-api", identity, "http-go")
        self.assertEqual(event["cpu_seconds"], 2)
        self.assertEqual(event["cgroup_id"], "/kubepods/current")
        self.assertEqual(event["cpu_timestamp_ms"], 2000)
        self.assertEqual(event["memory_working_set_timestamp_ms"], 2000)


if __name__ == "__main__":
    unittest.main()
