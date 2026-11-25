"""Small Harbor boundary used by production adapters and deterministic tests."""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
import json
import shutil
import uuid


@dataclass(frozen=True)
class HarborTrial:
    trial_id: str
    status: str
    workspace: Path
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HarborRun:
    status: str
    job_id: str
    trials: tuple[HarborTrial, ...]
    result_path: Path

    @property
    def completed(self) -> bool:
        return self.status == "completed" and bool(self.trials) and all(t.status == "completed" for t in self.trials)

    def manifest(self) -> dict[str, Any]:
        return {
            "schema_version": "pipeline-harbor-run-v1",
            "job_id": self.job_id,
            "status": self.status,
            "trials": [{"trial_id": t.trial_id, "status": t.status,
                         "workspace": str(t.workspace), **t.metadata} for t in self.trials],
            "result_path": str(self.result_path),
        }


class FakeHarbor:
    """Deterministic local Harbor substitute.

    It creates the same observable boundaries as a one-trial Harbor job while
    never starting Docker or calling a model endpoint. Tests can inject a
    worker callback that writes the artifact expected from a real worker.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def run(self, *, input_dir: str | Path, artifact_name: str,
            worker: Callable[[Path, Path], None], metadata: dict[str, Any] | None = None) -> HarborRun:
        source = Path(input_dir).resolve()
        if not source.is_dir():
            raise ValueError(f"Harbor input directory is missing: {source}")
        job_id = "fake-" + uuid.uuid4().hex[:12]
        job = self.root / job_id
        workspace = job / "artifacts" / "workspace"
        workspace.mkdir(parents=True)
        worker(source, workspace)
        artifact = workspace / artifact_name
        if not artifact.exists():
            raise ValueError(f"fake Harbor worker did not produce {artifact_name}")
        trial = HarborTrial("trial-" + uuid.uuid4().hex[:10], "completed", workspace,
                            metadata or {})
        result_path = job / "result.json"
        result_path.write_text(json.dumps({
            "schema_version": "fake-harbor-result-v1",
            "status": "completed",
            "finished_at": "fake",
            "stats": {"n_errored_trials": 0, "n_completed_trials": 1},
            "trials": [{"id": trial.trial_id, "status": trial.status,
                        "artifact": f"artifacts/workspace/{artifact_name}"}],
        }, indent=2) + "\n", encoding="utf-8")
        return HarborRun("completed", job_id, (trial,), result_path)
