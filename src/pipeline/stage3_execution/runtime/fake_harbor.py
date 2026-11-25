"""Fake Stage3 solver runner for contract and field-level integration tests."""
from __future__ import annotations
from pathlib import Path
from typing import Callable
import csv
import shutil
from pipeline.shared.runtime.harbor import FakeHarbor


def validate_prediction_csv(path: str | Path, expected_ids: list[str]) -> list[str]:
    errors=[]
    with Path(path).open(newline='', encoding='utf-8') as stream:
        reader=csv.DictReader(stream)
        columns=set(reader.fieldnames or [])
        rows=list(reader)
    if columns != {'sample_id','prediction'}:
        errors.append('submission columns must be sample_id,prediction')
    actual=[row.get('sample_id') for row in rows]
    if actual != expected_ids:
        errors.append('submission IDs do not match the declared test order')
    for row in rows:
        try:
            value=float(row.get('prediction',''))
        except (TypeError, ValueError):
            errors.append('prediction is not numeric')
            continue
        if value != value or value in (float('inf'), float('-inf')):
            errors.append('prediction is not finite')
    return errors


def run_fake_solver(*, package: str | Path, output_root: str | Path,
                    solver: Callable[[Path, Path], None]) -> dict:
    package=Path(package).resolve()
    public=package/'common_task/environment/input/data'
    test_file=public/'test.csv'
    if not test_file.is_file():
        raise ValueError('task package is missing public test.csv')
    import csv
    with test_file.open(newline='', encoding='utf-8') as stream:
        expected_ids=[row['sample_id'] for row in csv.DictReader(stream)]
    def worker(source: Path, workspace: Path):
        output=workspace/'output'; output.mkdir()
        solver(source, output)
    run=FakeHarbor(output_root).run(input_dir=public, artifact_name='output/predictions.csv', worker=worker)
    submission=run.trials[0].workspace/'output/predictions.csv'
    errors=validate_prediction_csv(submission, expected_ids)
    return {"status": "passed" if not errors else "failed", "errors": errors,
            "submission": str(submission), "job_id": run.job_id,
            "trial_count": len(run.trials)}
