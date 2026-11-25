"""Stage2 Harbor result normalization and deterministic authoring harness."""
from __future__ import annotations
from pathlib import Path
from typing import Any, Callable
from pipeline.shared.runtime.harbor import FakeHarbor, HarborRun
from pipeline.shared.io.jsonio import read_json


def normalize_harbor_result(result: dict[str, Any], *, expected_trials: int = 1) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise ValueError("Harbor result must be an object")
    status = result.get("status")
    stats = result.get("stats") or {}
    trials = result.get("trials")
    if status != "completed":
        raise ValueError(f"Harbor job did not complete: {status}")
    if stats.get("n_errored_trials", 0) != 0:
        raise ValueError("Harbor reported errored trials")
    if not isinstance(trials, list) or len(trials) != expected_trials:
        raise ValueError("Harbor result has an unexpected trial count")
    if any(not isinstance(t, dict) or t.get("status") != "completed" for t in trials):
        raise ValueError("Harbor trial is not completed")
    return {"status": "completed", "trial_count": len(trials),
            "finished_at": result.get("finished_at"), "stats": stats}


def run_fake_authoring(*, input_dir: str | Path, artifact_name: str,
                       output_root: str | Path,
                       worker: Callable[[Path, Path], None]) -> dict[str, Any]:
    run = FakeHarbor(output_root).run(input_dir=input_dir, artifact_name=artifact_name, worker=worker)
    receipt = normalize_harbor_result(read_json(run.result_path))
    return {**receipt, "job_id": run.job_id, "result_path": str(run.result_path),
            "workspace": str(run.trials[0].workspace)}
