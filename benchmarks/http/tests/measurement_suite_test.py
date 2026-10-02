import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from measure import suite


class SuiteTest(unittest.TestCase):
    def test_isolates_pages_and_adds_framework_memory_reference_without_rewrite(self):
        with TemporaryDirectory() as temporary:
            root, output = Path(temporary) / "results", Path(temporary) / "docs/reports/http"
            original = Path(temporary) / "docs/reports/http/comparison.html"
            original.parent.mkdir(parents=True)
            original.write_bytes(b"original")
            for experiment, router in (("frameworks", "chi"), ("memory", "stdlib")):
                path = root / experiment / "run" / "result.json"
                path.parent.mkdir(parents=True)
                path.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "status": "invalid",
                            "metadata": {
                                "settings": {"rate": 600},
                                "cluster": {"workload": {"configuration": {"ROUTER": router}}},
                            },
                        }
                    )
                )
            captured = []

            def render(records, directory):
                captured.append(records)
                directory.mkdir(parents=True)
                (directory / "comparison.html").write_text("<h1>HTTP benchmark comparison</h1>")
                (directory / "comparison.csv").write_text("run_id\n")

            def publish(source, destination):
                destination.mkdir(parents=True)
                (destination / "comparison.html").write_bytes(
                    (source / "report/comparison.html").read_bytes()
                )
                (destination / "comparison.csv").write_text("run_id\n")

            with (
                patch.object(suite, "render", side_effect=render),
                patch.object(suite, "publish", side_effect=publish),
            ):
                suite.main(["--results-dir", str(root), "--output-dir", str(output)])
            self.assertEqual(len(captured), 2)
            self.assertEqual(len(captured[1]), 2)  # frameworks + memory stdlib reference
            self.assertEqual(
                captured[0][0]["metadata"]["cluster"]["workload"]["configuration"]["ROUTER"],
                "stdlib",
            )
            self.assertIn(
                "Original diagnostic comparison", (output.parents[1] / "index.html").read_text()
            )
            self.assertEqual(original.read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main()
