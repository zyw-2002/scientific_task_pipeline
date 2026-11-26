"""Read-only Solver views for local cross-benchmark observations.

Harbor job manifests may contain administrative jobs and trajectories are not
complete task packages. Resolve the trial's task definition and collect only
Solver-facing documents; keep missing public material explicit.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import random

PUBLIC_DOCUMENTS = (
    ('instruction.md', 'task_description', 'instruction.md'),
    ('environment/input/README.md', 'data_description', '/task/problem/README.md'),
    ('environment/input/data_description.md', 'data_description', '/task/problem/data_description.md'),
    ('environment/input/task_contract.json', 'task_contract', '/task/problem/task_contract.json'),
    ('environment/input/guidance/method.md', 'provided_method', '/task/problem/guidance/method.md'),
)


def naturebench_sample(jobs_root: Path, harbor_root: Path, *, seed: int, count: int = 10) -> dict:
    """Select scientific trials before inspecting package completeness or rewards."""
    jobs_root, harbor_root = jobs_root.resolve(), harbor_root.resolve()
    manifest = jobs_root/'best-manifest.json'
    records = json.loads(manifest.read_text())
    eligible, excluded = [], []
    seen = set()
    for row in records:
        trial = Path(row['path']).resolve()
        # The best manifest also contains links to paper-screen jobs. Their
        # evaluator and trajectory assess paper admission, not science solving.
        if not trial.is_relative_to(jobs_root):
            excluded.append({'task_id': row['task'], 'reason': 'trial belongs to a different job population'})
            continue
        if row['task'] in seen:
            raise ValueError('duplicate scientific task in best manifest: '+row['task'])
        seen.add(row['task']); eligible.append(row)
    selected = random.Random(seed).sample(eligible, count)
    specs, evidence = [], []
    for row in selected:
        trial = Path(row['path']).resolve(); documents = []; package = None; errors = []
        try:
            config = json.loads((trial/'config.json').read_text())
            declared = Path(config['task']['path'])
            package = (declared if declared.is_absolute() else harbor_root/declared).resolve()
            if not package.is_relative_to(harbor_root/'datasets'):
                raise ValueError('task definition is outside the declared datasets root')
            if not package.is_dir():
                raise ValueError('task definition directory is absent')
            for relative, role, visible in PUBLIC_DOCUMENTS:
                path = package/relative
                if path.is_file():
                    data = path.read_bytes()
                    documents.append({'role': role, 'path': visible,
                                      'sha256': hashlib.sha256(data).hexdigest(),
                                      'text': data.decode('utf-8', errors='replace')})
            available = {d['path'] for d in documents}
            for required in ('instruction.md','/task/problem/README.md','/task/problem/data_description.md'):
                if required not in available: errors.append('missing public document: '+required)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(str(exc))
        tid = row['task']
        specs.append({'task_id': tid, 'source': 'naturebench',
                      'benchmark_version': 'naturebench-local-public-view-v2',
                      'metadata': {'case_id': tid},
                      'documents': documents,
                      'task_text': '\n\n'.join('['+d['role']+']\n'+d['text'] for d in documents),
                      'package_path': str(package) if package else None,
                      'static_evaluation_only': True, 'trajectory_attached': False})
        evidence.append({'task_id': tid, 'trial': str(trial), 'package': str(package) if package else None,
                         'public_document_errors': errors})
    return {'seed': seed, 'selection': 'uniform_without_replacement_before_readiness',
            'manifest_sha256': hashlib.sha256(manifest.read_bytes()).hexdigest(),
            'eligible_count': len(eligible), 'excluded': excluded,
            'selected_task_ids': [r['task_id'] for r in specs], 'tasks': specs, 'evidence': evidence}
