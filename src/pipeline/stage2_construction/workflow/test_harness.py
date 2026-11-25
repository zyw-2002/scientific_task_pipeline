"""Offline Stage2 Harbor construction/verification harness.

This is deliberately separate from the real Harbor adapter. It exercises the
same artifact/result boundaries without requiring Docker, Harbor, or a model.
"""
from __future__ import annotations
from pathlib import Path
import json
import shutil
from pipeline.shared.runtime.harbor import FakeHarbor
from pipeline.stage2_construction.harbor import normalize_harbor_result


def _copy_package(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination)


def _verify_package(package: Path) -> dict:
    required = [package / 'execution.json', package / 'common_task/instruction.md']
    missing = [str(path.relative_to(package)) for path in required if not path.is_file()]
    status = 'complete' if not missing else 'blocked'
    report = {'schema_version': 'pipeline-review-report-v1', 'status': status,
              'errors': [{'id': 'MISSING_FILE', 'path': item} for item in missing]}
    (package / 'review_report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return report


def run_harbor_test(*, draft: str | Path, output_root: str | Path) -> dict:
    draft = Path(draft).resolve()
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    construct = FakeHarbor(root / 'construct').run(
        input_dir=draft, artifact_name='build',
        worker=lambda source, workspace: _copy_package(source, workspace / 'build'),
        metadata={'role': 'construct'})
    constructed = construct.trials[0].workspace / 'build'
    review = FakeHarbor(root / 'verify').run(
        input_dir=constructed, artifact_name='verified',
        worker=lambda source, workspace: (_copy_package(source, workspace / 'verified'),
                                         _verify_package(workspace / 'verified')),
        metadata={'role': 'construct_verify'})
    verified = review.trials[0].workspace / 'verified'
    review_report = json.loads((verified / 'review_report.json').read_text())
    status = 'ready' if review_report.get('status') == 'complete' else 'blocked'
    manifest = {
        'schema_version': 'pipeline-stage2-harbor-test-v1',
        'status': status,
        'tasks': [
            {'role': 'construct', 'job_id': construct.job_id,
             'result': normalize_harbor_result(json.loads(construct.result_path.read_text())),
             'artifact': str(constructed)},
            {'role': 'construct_verify', 'job_id': review.job_id,
             'result': normalize_harbor_result(json.loads(review.result_path.read_text())),
             'artifact': str(verified), 'review_report': review_report},
        ],
        'verification': {'required_files': ['execution.json', 'common_task/instruction.md', 'review_report.json'],
                         'status': review_report.get('status')},
    }
    (root / 'harbor_test_manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    return manifest
