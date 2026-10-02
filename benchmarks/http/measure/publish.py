"""Publish a self-contained HTTP benchmark report snapshot for GitHub Pages."""

import argparse
import os
import shutil
import tempfile
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

PROFILE_NAMES = frozenset(
    {"cpu.pprof", "jsc-cpu.json", "cpu-top.txt"}
    | {
        f"{kind}-{phase}.pprof"
        for kind in ("heap", "allocs", "goroutine", "block", "mutex")
        for phase in ("before", "after")
    }
)


class PublishError(ValueError):
    """The local report cannot safely be made into a Pages snapshot."""


def is_within(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class ReportLinks(HTMLParser):
    def __init__(self, report, results_root, csv):
        super().__init__(convert_charrefs=False)
        self.report = report
        self.results_root = results_root
        self.csv = csv
        self.parts = []
        self.profiles = []

    def handle_starttag(self, tag, attrs):
        rewritten = []
        for name, value in attrs:
            if tag == "a" and name.lower() == "href" and value is not None:
                value = self.local_href(value)
            rewritten.append((name, value))
        attributes = "".join(
            f" {name}" if value is None else f' {name}="{escape(value, quote=True)}"'
            for name, value in rewritten
        )
        self.parts.append(f"<{tag}{attributes}>")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.parts[-1] = self.parts[-1][:-1] + " />"

    def handle_endtag(self, tag):
        self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        self.parts.append(data)

    def handle_comment(self, data):
        self.parts.append(f"<!--{data}-->")

    def handle_decl(self, decl):
        self.parts.append(f"<!{decl}>")

    def handle_pi(self, data):
        self.parts.append(f"<?{data}>")

    def handle_entityref(self, name):
        self.parts.append(f"&{name};")

    def handle_charref(self, name):
        self.parts.append(f"&#{name};")

    def local_href(self, href):
        parsed = urlsplit(href)
        if parsed.scheme or parsed.netloc or href.startswith("#"):
            return href
        if parsed.query or parsed.fragment:
            raise PublishError(f"unsupported local report link: {href}")
        if not parsed.path:
            return href
        try:
            resolved = (self.report.parent / unquote(parsed.path)).resolve(strict=True)
        except FileNotFoundError as error:
            raise PublishError(f"missing report link target: {href}") from error
        if resolved == self.csv:
            return "comparison.csv"
        if not is_within(resolved, self.results_root):
            raise PublishError(f"report link escapes results directory: {href}")
        relative = resolved.relative_to(self.results_root)
        if (
            len(relative.parts) != 3
            or relative.parts[1] != "diagnostics"
            or relative.parts[2] not in PROFILE_NAMES
            or not resolved.is_file()
        ):
            raise PublishError(f"unsupported local report link: {href}")
        self.profiles.append((resolved, relative.parts[0], relative.parts[2]))
        return f"profiles/{relative.parts[0]}/diagnostics/{relative.parts[2]}"


def validate_paths(results_dir, output_dir):
    results_root = results_dir.resolve(strict=True)
    output_root = output_dir.resolve(strict=False)
    if is_within(output_root, results_root) or is_within(results_root, output_root):
        raise PublishError("output directory must not overlap the results directory")
    return results_root, output_root


def source_file(path, results_root, description):
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as error:
        raise PublishError(f"missing {description}") from error
    if not is_within(resolved, results_root) or not resolved.is_file():
        raise PublishError(f"{description} escapes results directory")
    return resolved


def publish(results_dir, output_dir):
    results_root, output_root = validate_paths(Path(results_dir), Path(output_dir))
    report = results_root / "report"
    comparison = source_file(report / "comparison.html", results_root, "comparison.html")
    csv = source_file(report / "comparison.csv", results_root, "comparison.csv")

    parser = ReportLinks(comparison, results_root, csv)
    parser.feed(comparison.read_text())
    parser.close()

    output_root.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".http-report-", dir=output_root.parent))
    try:
        (stage / "comparison.html").write_text("".join(parser.parts))
        shutil.copy2(csv, stage / "comparison.csv")
        profiles = stage / "profiles"
        for source, run, name in parser.profiles:
            destination = profiles / run / "diagnostics" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        findings = report / "findings.md"
        if findings.is_file():
            shutil.copy2(source_file(findings, results_root, "findings.md"), stage / "findings.md")

        old_profiles = output_root / "profiles"
        if old_profiles.exists() or old_profiles.is_symlink():
            if not old_profiles.is_dir() or old_profiles.is_symlink():
                raise PublishError("existing profiles export is not a directory")
        output_root.mkdir(parents=True, exist_ok=True)
        os.replace(stage / "comparison.html", output_root / "comparison.html")
        os.replace(stage / "comparison.csv", output_root / "comparison.csv")
        if old_profiles.exists():
            shutil.rmtree(old_profiles)
        if profiles.exists():
            os.replace(profiles, old_profiles)
        old_findings = output_root / "findings.md"
        staged_findings = stage / "findings.md"
        if staged_findings.exists():
            os.replace(staged_findings, old_findings)
        elif old_findings.exists():
            old_findings.unlink()
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", default="results/http")
    parser.add_argument("--output-dir", default="docs/reports/http")
    args = parser.parse_args()
    try:
        publish(args.results_dir, args.output_dir)
    except (OSError, PublishError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
