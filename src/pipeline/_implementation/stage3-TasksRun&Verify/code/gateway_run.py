#!/usr/bin/env python3
"""Optional model-search gateway command adapter for the verified stage3 runner.

This is intentionally a thin adapter. The gateway already owns model proxying,
web search, caching, trajectory logging, and Harbor finalization. This script
only:

1. validates the gateway block in ``execution.json``;
2. checks ``/health`` (or optionally starts the gateway);
3. builds the Harbor command with gateway environment variables.

Actual execution uses ``pipeline.py run --gateway`` and its stage2 readiness
gate. This legacy CLI is retained only for command previews. A finalized gateway
session is never enough to certify a particular scoring attempt.

It never edits the gateway and never implements provider/search/cache logic.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

SCHEMA_VERSION = "pipline-run-manifest-v1"
DEFAULT_GATEWAY_SCRIPT = (
    Path("/u01/zhengyawen/s1-harbor/model-search-gateway/scripts/start-autoresearch-gateway.sh")
)


def _load_execution(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"spec must be a JSON object: {path}")
    return value


def _gateway(spec: dict[str, Any]) -> dict[str, Any]:
    runtime = spec.get("runtime")
    if not isinstance(runtime, dict):
        raise ValueError("runtime must be an object")
    gateway = runtime.get("gateway")
    if not isinstance(gateway, dict):
        raise ValueError("runtime.gateway is required to run through the gateway")
    for key in ("base_url", "config_path", "model", "finalize_url", "log_dir", "export_dir"):
        value = gateway.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"runtime.gateway.{key} is required")
    return gateway


def health_check(base_url: str, timeout: float = 3.0) -> bool:
    url = base_url.rstrip("/") + "/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (OSError, urllib.error.URLError, ValueError):
        return False


def start_gateway(gateway: dict[str, Any], *, script: Path = DEFAULT_GATEWAY_SCRIPT, timeout: float = 30.0) -> None:
    if not script.is_file():
        raise ValueError(f"gateway start script not found: {script}")
    port = gateway["base_url"].rstrip("/").rsplit(":", 1)[-1]
    env = dict(os.environ)
    env.setdefault("AUTORESEARCH_GATEWAY_PORT", port)
    completed = subprocess.run(
        [str(script)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout + 10,
    )
    if completed.returncode != 0:
        raise ValueError(f"gateway start failed: {completed.stderr[-1000:]}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if health_check(gateway["base_url"]):
            return
        time.sleep(1)
    raise ValueError("gateway did not become healthy")


def build_harbor_command(
    *,
    spec: dict[str, Any],
    package: Path,
    harbor: str,
    agent: str,
    job_name: str,
    jobs_dir: Path,
    max_attempts: int = 1,
) -> list[str]:
    gateway = _gateway(spec)
    command = [
        harbor,
        "run",
        "-p",
        str(package),
        "-a",
        agent,
        "-m",
        gateway["model"],
        "--jobs-dir",
        str(jobs_dir),
        "--job-name",
        job_name,
        "-n",
        "1",
        "-k",
        str(max_attempts),
        "--max-retries",
        "0",
        "--yes",
    ]
    if agent in {"claude-code", "claude"}:
        command += [
            "--ae",
            f"ANTHROPIC_BASE_URL={gateway['base_url'].rstrip('/')}",
            "--ae",
            "ANTHROPIC_API_KEY=local-gateway",
        ]
    else:
        command += [
            "--ae",
            f"OPENAI_BASE_URL={gateway['base_url'].rstrip('/')}/v1",
            "--ae",
            "OPENAI_API_KEY=local-gateway",
        ]
    command += ["--ae", f"MODEL_SEARCH_GATEWAY_FINALIZE_URL={gateway['finalize_url']}"]
    return command


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def write_run_manifest(
    *,
    spec: dict[str, Any],
    package: Path,
    jobs_dir: Path,
    output: Path,
    session_dir: Path | None,
    submission_path: str | None,
    dry_run: bool = False,
) -> dict[str, Any]:
    gateway = _gateway(spec)
    trajectory = session_dir / "trajectory.json" if session_dir else None
    searches = session_dir / "searches.jsonl" if session_dir else None
    finalize_found = False
    if trajectory and trajectory.is_file():
        payload = _read_json(trajectory) or {}
        finalize_found = isinstance(payload.get("harbor"), dict)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "task_id": spec.get("task_id"),
        "dry_run": dry_run,
        "package": str(package.resolve()),
        "jobs_dir": str(jobs_dir.resolve()),
        "gateway": {
            "base_url": gateway["base_url"],
            "config_path": gateway["config_path"],
            "model": gateway["model"],
            "finalize_url": gateway["finalize_url"],
        },
        "session_dir": str(session_dir.resolve()) if session_dir else None,
        "trajectory": str(trajectory.resolve()) if trajectory else None,
        "searches": str(searches.resolve()) if searches else None,
        "submission": submission_path,
        "finalize_record_found": finalize_found,
        "hermes_export_dir": gateway["export_dir"],
        "status": "dry_run" if dry_run else "needs_evidence",
    }
    _atomic_write_json(output, manifest)
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a Harbor task through model-search-gateway")
    parser.add_argument("--execution", type=Path, required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--harbor", default="harbor")
    parser.add_argument("--agent", default="qwen-coder")
    parser.add_argument("--job-name", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--max-attempts", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--start-gateway", action="store_true")
    parser.add_argument("--gateway-script", type=Path, default=DEFAULT_GATEWAY_SCRIPT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output_dir = args.output_dir.resolve()
    jobs_dir = output_dir / "jobs"
    manifest_path = args.manifest or (output_dir / "run_manifest.json")
    try:
        spec = _load_execution(args.execution)
        gateway = _gateway(spec)
        submission = spec.get("submission", {}).get("path")
        command = build_harbor_command(
            spec=spec,
            package=args.package.resolve(),
            harbor=args.harbor,
            agent=args.agent,
            job_name=args.job_name,
            jobs_dir=jobs_dir,
            max_attempts=args.max_attempts,
        )
        if args.dry_run:
            manifest = write_run_manifest(
                spec=spec,
                package=args.package,
                jobs_dir=jobs_dir,
                output=manifest_path,
                session_dir=None,
                submission_path=submission,
                dry_run=True,
            )
            print(json.dumps({"status": "dry_run", "command": command, "manifest": manifest}, ensure_ascii=False))
            return 0
        raise ValueError("execution moved to pipeline.py run --gateway --pair PAIR_MANIFEST --variant blind|guided --output RUN; the verified readiness gate and exact trial evidence are mandatory")
    except (OSError, ValueError, KeyError, TypeError) as error:
        _atomic_write_json(
            manifest_path,
            {
                "schema_version": SCHEMA_VERSION,
                "task_id": None,
                "status": "error",
                "error": str(error),
            },
        )
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
