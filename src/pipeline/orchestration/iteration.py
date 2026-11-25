"""Reproducible ten-task iteration control for the OriginBench pool.

The pool is deliberately treated as a fixed population.  Selection is random
without replacement, while readiness is assessed *after* selection so that
missing data cannot silently turn into an easier benchmark.  This module does
not manufacture scientific inputs or scores: unavailable evidence is recorded
and score fields remain null until a real task run supplies them.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


POOL_SIZE = 100
DEFAULT_BATCH_SIZE = 10
REQUIRED_PACKAGE_FILES = ("instruction.md", "task.toml")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_rows(path: Path) -> list[dict[str, Any]]:
    """Read JSONL, JSON-array/object, or CSV task pools."""
    if not path.is_file():
        raise FileNotFoundError(f"task pool does not exist: {path}")
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".csv":
        return [dict(row) for row in csv.DictReader(text.splitlines())]
    if path.suffix.lower() == ".json":
        value = json.loads(text)
        if isinstance(value, dict):
            for key in ("tasks", "experiments", "records", "variants"):
                if isinstance(value.get(key), list):
                    value = value[key]
                    break
        if not isinstance(value, list):
            raise ValueError("JSON task pool must be an array or contain a task array")
        return [dict(row) for row in value]
    rows = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"task pool row {line_number} is not an object")
        rows.append(dict(value))
    return rows


def _task_id(row: Mapping[str, Any], index: int) -> str:
    for key in ("sample_id", "task_id", "experiment_id", "experiment", "canonical_id", "id"):
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    raise ValueError(f"task pool row {index} has no stable task identifier")


def normalize_pool(rows: Iterable[Mapping[str, Any]], *, require_size: int | None = POOL_SIZE) -> list[dict[str, Any]]:
    normalized = []
    seen: set[str] = set()
    for index, original in enumerate(rows, 1):
        row = dict(original)
        task_id = _task_id(row, index)
        if task_id in seen:
            raise ValueError(f"duplicate task identifier in pool: {task_id}")
        seen.add(task_id)
        row["task_id"] = task_id
        normalized.append(row)
    if require_size is not None and len(normalized) != require_size:
        raise ValueError(f"expected exactly {require_size} tasks in seed pool, found {len(normalized)}")
    return normalized


def select_tasks(pool_path: Path, *, seed: int, count: int = DEFAULT_BATCH_SIZE,
                 require_pool_size: int | None = POOL_SIZE) -> dict[str, Any]:
    if count < 1:
        raise ValueError("batch size must be positive")
    rows = normalize_pool(_read_rows(pool_path), require_size=require_pool_size)
    if count > len(rows):
        raise ValueError(f"batch size {count} exceeds pool size {len(rows)}")
    rng = random.Random(seed)
    selected = rng.sample(rows, count)
    # Keep pool order in the artifact; the random order is separately recorded
    # and is the order used by a batch runner.
    selected_ids = [row["task_id"] for row in selected]
    return {
        "schema_version": "pipeline-iteration-selection-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "pool_path": str(pool_path.resolve()),
        "pool_sha256": _sha256(pool_path),
        "pool_size": len(rows),
        "batch_size": count,
        "seed": seed,
        "selection": "uniform_without_replacement",
        "selected_task_ids": selected_ids,
        "tasks": [{**row, "selection_index": index} for index, row in enumerate(selected, 1)],
    }


def _path_value(row: Mapping[str, Any]) -> Path | None:
    for key in ("verified_package_path", "package_path", "task_path", "path"):
        value = row.get(key)
        if value:
            return Path(str(value)).expanduser()
    return None


def _instruction_urls(package: Path | None) -> list[str]:
    if not package:
        return []
    texts = []
    for relative in ("instruction.md", "environment/input/data_description.md",
                     "environment/problem/input_description/data_description.md"):
        path = package / relative
        if path.is_file():
            try:
                texts.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
    urls = []
    for text in texts:
        for url in re.findall(r"https?://[^\s)\]>'\"`]+", text):
            url = url.rstrip(".,;:)")
            if url not in urls:
                urls.append(url)
    return urls


def preflight_task(row: Mapping[str, Any]) -> dict[str, Any]:
    """Classify evidence without claiming that files are scientifically complete."""
    package = _path_value(row)
    sources = row.get("data_urls") or row.get("source_urls") or row.get("urls") or []
    if isinstance(sources, str):
        sources = [sources]
    sources = list(sources) if isinstance(sources, list) else []
    for source in _instruction_urls(package):
        if source not in sources:
            sources.append(source)
    evidence = {
        "package_path": str(package.resolve()) if package and package.exists() else str(package) if package else None,
        "required_files": {},
        "data_evidence": "unknown",
        "source_urls": sources,
    }
    declared = str(row.get("readiness_status") or row.get("package_status") or "").lower()
    if package and package.is_dir():
        evidence["required_files"] = {name: (package / name).is_file() for name in REQUIRED_PACKAGE_FILES}
        data_dirs = [package / name for name in ("data", "environment/input/data", "input/data")]
        nonempty = any(path.is_dir() and any(path.iterdir()) for path in data_dirs)
        evidence["data_evidence"] = "bundled_nonempty" if nonempty else "directory_empty_or_absent"
        if all(evidence["required_files"].values()) and (nonempty or declared in {"ready", "validated"}):
            status = "ready"
        elif sources:
            status = "downloadable"
        else:
            status = "partial"
    elif sources:
        status = "downloadable"
    else:
        status = "blocked"
    reason = {
        "ready": "task package and data evidence are present; runtime validation still required",
        "downloadable": "task package or data is incomplete but declared/public sources are available",
        "partial": "task definition exists but complete data/package evidence is absent",
        "blocked": "no local package and no declared source from which the task can be recovered",
    }[status]
    return {"readiness_status": status, "readiness_reason": reason, "readiness_evidence": evidence}


def add_preflight(selection: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(selection)
    tasks = []
    for task in selection.get("tasks", []):
        tasks.append({**task, **preflight_task(task)})
    result["tasks"] = tasks
    result["preflight"] = {
        status: sum(1 for task in tasks if task["readiness_status"] == status)
        for status in ("ready", "downloadable", "partial", "blocked")
    }
    return result


def score_summary(rows: Iterable[Mapping[str, Any]], *, target: float = 90.0,
                  score_kind: str = "scientific") -> dict[str, Any]:
    """Summarize one rubric namespace without converting missing scores to zero.

    ``scientific`` is the Stage3 outcome namespace.  ``task_quality`` is the
    preserved q0/NatureBench-style construction rubric.  They are intentionally
    selected explicitly so a high task-quality score cannot masquerade as a
    scientific run score.
    """
    if score_kind not in {"scientific", "task_quality"}:
        raise ValueError("score_kind must be scientific or task_quality")
    records = []
    for row in rows:
        if score_kind == "scientific":
            score = row.get("scientific_score")
        else:
            score = row.get("q0", row.get("quality_score"))
        if isinstance(score, Mapping):
            score = score.get("total", score.get("score", score.get("value")))
        try:
            score = float(score) if score is not None else None
        except (TypeError, ValueError):
            score = None
        status = str(row.get("status") or row.get("readiness_status") or "").lower()
        valid = row.get("is_valid")
        if valid is None and row.get("validity_status") is not None:
            valid = row.get("validity_status") in {"valid", "pass"}
        scoreable = score is not None and valid is not False and status not in {"blocked", "data_unavailable", "partial"}
        records.append({"task_id": row.get("task_id") or row.get("sample_id"), "score": score,
                        "score_kind": score_kind,
                        "scoreable": scoreable, "status": status or None,
                        "failure_reason": row.get("failure_reason") or row.get("readiness_reason")})
    scored = [r["score"] for r in records if r["scoreable"]]
    total = len(records)
    return {
        "schema_version": "pipeline-iteration-score-summary-v1",
        "score_kind": score_kind,
        "target": target,
        "tasks_total": total,
        "tasks_scoreable": len(scored),
        "tasks_blocked_or_unscored": total - len(scored),
        "coverage": len(scored) / total if total else 0.0,
        "mean_score_scoreable": sum(scored) / len(scored) if scored else None,
        "passed": len(scored) == total and bool(scored) and sum(scored) / len(scored) >= target,
        "records": records,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    select = sub.add_parser("select")
    select.add_argument("--pool", type=Path, required=True)
    select.add_argument("--output", type=Path, required=True)
    select.add_argument("--seed", type=int, required=True)
    select.add_argument("--count", type=int, default=DEFAULT_BATCH_SIZE)
    select.add_argument("--allow-pool-size", action="store_true")
    preflight = sub.add_parser("preflight")
    preflight.add_argument("--selection", type=Path, required=True)
    preflight.add_argument("--output", type=Path, required=True)
    score = sub.add_parser("score")
    score.add_argument("--results", type=Path, required=True, help="JSONL or JSON array of task results")
    score.add_argument("--output", type=Path, required=True)
    score.add_argument("--target", type=float, default=90.0)
    score.add_argument("--score-kind", choices=("scientific", "task_quality"), default="scientific")
    args = parser.parse_args(argv)
    if args.command == "select":
        result = select_tasks(args.pool, seed=args.seed, count=args.count,
                              require_pool_size=None if args.allow_pool_size else POOL_SIZE)
    elif args.command == "preflight":
        result = add_preflight(json.loads(args.selection.read_text(encoding="utf-8")))
    else:
        result = score_summary(_read_rows(args.results), target=args.target, score_kind=args.score_kind)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "written", "output": str(args.output.resolve())}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
