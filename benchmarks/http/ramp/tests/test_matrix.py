import unittest

from benchmarks.http.ramp.tests.matrix import docker_command


class MatrixTests(unittest.TestCase):
    def test_container_contract_uses_production_filesystem_and_cpu_limits(self):
        command = docker_command("elixir", "image@sha256:test", "phoenix", 29000, "test-container")
        self.assertIn("--read-only", command)
        self.assertEqual(command[command.index("--cpus") + 1], "1")
        self.assertEqual(command[command.index("--group-add") + 1], "65532")
        self.assertIn("/data:rw,uid=10001,gid=65532,mode=2770", command)
        self.assertIn("/tmp:rw,uid=10001,gid=65532,mode=2770", command)
        self.assertNotIn("--rm", command)
