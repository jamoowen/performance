import tempfile
import unittest
from pathlib import Path

from measure.publish import PublishError, publish


class PublishTest(unittest.TestCase):
    def make_results(self, directory, document, findings=None):
        root = Path(directory) / "results"
        report = root / "report"
        report.mkdir(parents=True)
        (report / "comparison.html").write_text(document)
        (report / "comparison.csv").write_text("run_id,p95_ms\nrun-one,10\n")
        if findings is not None:
            (report / "findings.md").write_text(findings)
        return root

    def profile(self, root, run, name, content=b"profile"):
        path = root / run / "diagnostics" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_rewrites_linked_profiles_and_preserves_report_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_results(
                directory,
                "<html><body><a href='../run-one/diagnostics/cpu.pprof' download>CPU</a>"
                "<a href='../report/comparison.csv'>CSV</a><a href='https://example.com'>external</a>"
                "<a href='#run-one'>fragment</a>"
                "<script>const fake = \"<a href='../not-a-run/diagnostics/cpu.pprof'>\";</script>"
                "</body></html>",
                "campaign notes",
            )
            self.profile(root, "run-one", "cpu.pprof", b"cpu bytes")
            output = Path(directory) / "docs" / "reports" / "http"

            publish(root, output)

            document = (output / "comparison.html").read_text()
            self.assertIn('href="profiles/run-one/diagnostics/cpu.pprof"', document)
            self.assertIn('href="comparison.csv"', document)
            self.assertNotIn('href="../report/comparison.csv"', document)
            self.assertIn('href="https://example.com"', document)
            self.assertIn('href="#run-one"', document)
            self.assertIn(
                "const fake = \"<a href='../not-a-run/diagnostics/cpu.pprof'>\";", document
            )
            self.assertEqual((output / "comparison.csv").read_text(), "run_id,p95_ms\nrun-one,10\n")
            self.assertEqual(
                (output / "profiles" / "run-one" / "diagnostics" / "cpu.pprof").read_bytes(),
                b"cpu bytes",
            )
            self.assertEqual((output / "findings.md").read_text(), "campaign notes")

    def test_copies_only_profiles_linked_from_anchors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_results(
                directory, "<a href='../run-one/diagnostics/cpu.pprof'>CPU</a>"
            )
            self.profile(root, "run-one", "cpu.pprof")
            self.profile(root, "run-one", "heap-before.pprof")
            output = Path(directory) / "export"

            publish(root, output)

            self.assertTrue((output / "profiles/run-one/diagnostics/cpu.pprof").is_file())
            self.assertFalse((output / "profiles/run-one/diagnostics/heap-before.pprof").exists())

    def test_invalid_link_does_not_change_existing_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_results(
                directory, "<a href='../run-one/diagnostics/missing.pprof'>bad</a>"
            )
            output = Path(directory) / "export"
            output.mkdir()
            (output / "comparison.html").write_text("previous report")
            (output / "comparison.csv").write_text("previous csv")

            with self.assertRaisesRegex(PublishError, "missing report link target"):
                publish(root, output)

            self.assertEqual((output / "comparison.html").read_text(), "previous report")
            self.assertEqual((output / "comparison.csv").read_text(), "previous csv")

    def test_rejects_profile_symlink_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_results(
                directory, "<a href='../run-one/diagnostics/cpu.pprof'>bad</a>"
            )
            outside = Path(directory) / "outside.pprof"
            outside.write_bytes(b"secret")
            profile = root / "run-one" / "diagnostics" / "cpu.pprof"
            profile.parent.mkdir(parents=True)
            profile.symlink_to(outside)

            with self.assertRaisesRegex(PublishError, "escapes results directory"):
                publish(root, Path(directory) / "export")

    def test_invalid_existing_profiles_does_not_change_existing_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_results(directory, "<a href='comparison.csv'>CSV</a>")
            output = Path(directory) / "export"
            output.mkdir()
            (output / "comparison.html").write_text("previous report")
            (output / "comparison.csv").write_text("previous csv")
            (output / "profiles").write_text("not a directory")

            with self.assertRaisesRegex(PublishError, "existing profiles export"):
                publish(root, output)

            self.assertEqual((output / "comparison.html").read_text(), "previous report")
            self.assertEqual((output / "comparison.csv").read_text(), "previous csv")

    def test_refreshes_stale_profiles_and_absent_findings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_results(directory, "<a href='../new/diagnostics/cpu.pprof'>new</a>")
            self.profile(root, "new", "cpu.pprof", b"new")
            output = Path(directory) / "export"
            stale = output / "profiles" / "old" / "diagnostics" / "cpu.pprof"
            stale.parent.mkdir(parents=True)
            stale.write_bytes(b"old")
            (output / "findings.md").write_text("old notes")
            (output / "unrelated.txt").write_text("keep")

            publish(root, output)

            self.assertFalse(stale.exists())
            self.assertEqual((output / "profiles/new/diagnostics/cpu.pprof").read_bytes(), b"new")
            self.assertFalse((output / "findings.md").exists())
            self.assertEqual((output / "unrelated.txt").read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
