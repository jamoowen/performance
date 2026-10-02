"""Publish one isolated report per HTTP follow-up experiment."""

import argparse
from pathlib import Path

from .compare import render
from .publish import publish
from .results import load_records

EXPERIMENTS = ("scheduling", "sqlite", "memory", "frameworks", "scaling")
DESCRIPTIONS = {
    "scheduling": "Does Go GOMAXPROCS=1 or 2 behave better under one CPU?",
    "sqlite": "How do the three implementations behave with the SQLite workload?",
    "memory": "How do the implementations behave when database work is removed?",
    "frameworks": "What is the router overhead on the memory mixed workload?",
    "scaling": "What changes from one to two CPU limits on immutable memory reads?",
}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", default="results/http/followups")
    parser.add_argument("--output-dir", default="docs/reports/http")
    args = parser.parse_args(argv)
    root, output = Path(args.results_dir), Path(args.output_dir)
    links = []
    for experiment in EXPERIMENTS:
        source = root / experiment
        if not any(source.rglob("result.json")):
            continue
        report = source / "report"
        records = load_records(source)
        if experiment == "frameworks":
            # Reuse, but never rewrite, the three recorded stdlib memory baselines.
            records += [
                record
                for record in load_records(root / "memory")
                if record.get("metadata", {}).get("settings", {}).get("rate") == 600
                and record.get("metadata", {})
                .get("cluster", {})
                .get("workload", {})
                .get("configuration", {})
                .get("ROUTER")
                == "stdlib"
            ]
        render(records, report)
        comparison = report / "comparison.html"
        text = comparison.read_text()
        context = (
            f"<section class='experiment-context'><h2>{experiment.title()} experiment</h2>"
            f"<p>{DESCRIPTIONS[experiment]} Lines are individual recordings; median/range summaries are across whole-run values only."
            + (
                " Framework pages include immutable references recorded earlier from the memory/std-lib 600 RPS baselines; they are not contemporaneous matched pairs."
                if experiment == "frameworks"
                else ""
            )
            + "</p></section>"
        )
        comparison.write_text(
            text.replace(
                "<h1>HTTP benchmark comparison</h1>",
                "<h1>HTTP benchmark comparison</h1>" + context,
                1,
            )
        )
        publish(report.parent, output / experiment)
        links.append((experiment, f"reports/http/{experiment}/comparison.html"))
    index = output.parents[1] / "index.html"
    cards = "".join(
        f"<article><h2><a href='{href}'>{name.title()}</a></h2><p>{DESCRIPTIONS[name]}</p></article>"
        for name, href in links
    )
    index.write_text(
        "<!doctype html><meta name='viewport' content='width=device-width,initial-scale=1'><title>Performance reports</title>"
        "<style>body{font-family:system-ui;margin:auto;max-width:68rem;padding:1rem}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(16rem,100%),1fr));gap:1rem}article{min-width:0;border:1px solid #ddd;border-radius:.5rem;padding:1rem}a{color:#0759a5}</style>"
        f"<h1>HTTP follow-up reports</h1><p><a href='reports/http/comparison.html'>Original diagnostic comparison</a>. Pages contain recordings from one experiment; the framework page also shows explicitly labelled, earlier memory/std-lib references. Traces are never pooled across experiments.</p><main class='grid'>{cards}</main>"
    )


if __name__ == "__main__":
    main()
