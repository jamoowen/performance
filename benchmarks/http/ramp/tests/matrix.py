#!/usr/bin/env python3
"""Start adapters with isolated writable mounts and run the shared HTTP contract."""

from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[4]
FRAMEWORKS = {
    "go": ["nethttp", "chi", "fiber"],
    "rust": ["axum", "actix", "rocket"],
    "node": ["express", "nest", "fastify"],
    "bun": ["native", "hono", "elysia"],
    "python": ["fastapi"],
    "elixir": ["plug", "phoenix"],
}


def wait(url: str) -> None:
    for _ in range(60):
        try:
            with urllib.request.urlopen(url + "/healthz", timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError(f"not ready: {url}")


def docker_command(runtime: str, image: str, framework: str, port: int, name: str) -> list[str]:
    del runtime
    return [
        "docker",
        "run",
        "--detach",
        "--name",
        name,
        "--read-only",
        "--cpus",
        "1",
        "--group-add",
        "65532",
        "--tmpfs",
        "/tmp:rw,uid=10001,gid=65532,mode=2770",
        "--tmpfs",
        "/data:rw,uid=10001,gid=65532,mode=2770",
        "-e",
        f"FRAMEWORK={framework}",
        "-e",
        f"PORT={port}",
        "-e",
        "SEED_COUNT=100",
        "-e",
        "SQLITE_PATH=/data/benchmark.sqlite",
        "-p",
        f"127.0.0.1:{port}:{port}",
        image,
    ]


def native_command(runtime: str, framework: str, port: int, database: pathlib.Path) -> list[str]:
    environment = {
        **os.environ,
        "FRAMEWORK": framework,
        "PORT": str(port),
        "SEED_COUNT": "100",
        "SQLITE_PATH": str(database),
    }
    if runtime == "go":
        binary = pathlib.Path(tempfile.gettempdir()) / "http-ramp-go-contract"
        subprocess.run(
            ["go", "build", "-o", str(binary), "."],
            cwd=ROOT / "benchmarks/http/ramp/go",
            check=True,
            env={**environment, "CGO_ENABLED": "0"},
        )
        return [str(binary)]
    if runtime == "rust":
        subprocess.run(
            ["cargo", "build", "--release"], cwd=ROOT / "benchmarks/http/ramp/rust", check=True
        )
        return [str(ROOT / "benchmarks/http/ramp/rust/target/release/performance-http-ramp-rust")]
    raise ValueError("native launcher supports go and rust; provide --image for other runtimes")


def run_contract(runtime: str, framework: str, base: str) -> None:
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "benchmarks/http/ramp/tests/contract_test.py"),
            "--base-url",
            base,
            "--runtime",
            runtime,
            "--framework",
            framework,
            "--seed-count",
            "100",
        ],
        check=True,
    )


def run_container(
    runtime: str, image: str, framework: str, port: int, log_path: pathlib.Path
) -> None:
    name = f"http-ramp-contract-{os.getpid()}-{port}"
    subprocess.run(docker_command(runtime, image, framework, port, name), check=True)
    try:
        base = f"http://127.0.0.1:{port}"
        wait(base)
        run_contract(runtime, framework, base)
    finally:
        with log_path.open("w") as logs:
            subprocess.run(
                ["docker", "logs", name], check=False, stdout=logs, stderr=subprocess.STDOUT
            )
        subprocess.run(
            ["docker", "rm", "-f", name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def run_native(
    runtime: str, framework: str, port: int, database: pathlib.Path, log_path: pathlib.Path
) -> None:
    environment = {
        **os.environ,
        "FRAMEWORK": framework,
        "PORT": str(port),
        "SEED_COUNT": "100",
        "SQLITE_PATH": str(database),
    }
    with log_path.open("w") as logs:
        process = subprocess.Popen(
            native_command(runtime, framework, port, database),
            env=environment,
            stdout=logs,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            base = f"http://127.0.0.1:{port}"
            wait(base)
            run_contract(runtime, framework, base)
        finally:
            process.terminate()
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(10)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", required=True, choices=FRAMEWORKS)
    parser.add_argument("--image")
    parser.add_argument("--framework", action="append")
    parser.add_argument("--port", type=int, default=29000)
    args = parser.parse_args()
    frameworks = args.framework or FRAMEWORKS[args.runtime]
    for index, framework in enumerate(frameworks):
        with tempfile.TemporaryDirectory(prefix="http-ramp-contract-") as directory:
            root = pathlib.Path(directory)
            port = args.port + index
            log_path = root / "adapter.log"
            if args.image:
                run_container(args.runtime, args.image, framework, port, log_path)
            else:
                run_native(args.runtime, framework, port, root / "benchmark.sqlite", log_path)


if __name__ == "__main__":
    main()
