"""Freeze the Stage2 program and authoring inputs before starting any worker.

The saved tree preserves repository-relative imports. Execute its pipeline.py in
a fresh Python process so lazy host imports cannot fall back to live source code.
Credentials stay in the process environment / referenced env files, never here.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit

STAGE2 = 'stage2-TasksConstruction&Verify'
MANIFEST = 'snapshot_manifest.json'
SCHEMA = 'pipeline-stage2-snapshot-v1'


def _jsonable(value):
    if isinstance(value, Path):
        return str(value.resolve())
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def _check_config(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in {'api_key', 'token', 'password', 'authorization', 'secret',
                              'access_token', 'client_secret'} and item:
                raise ValueError('snapshot configuration must reference credentials, not store ' + key)
            _check_config(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_config(item)
    elif isinstance(value, str) and '://' in value:
        url = urlsplit(value)
        if url.username or url.password:
            raise ValueError('snapshot configuration cannot contain URL credentials; use an env file')


def resolve_image(image: str, docker: str = 'docker') -> str:
    """Resolve a local tag once; all workers subsequently use the immutable ID."""
    try:
        result = subprocess.run([docker, 'image', 'inspect', image, '--format', '{{.Id}}'],
                                capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError('cannot resolve authoring image identity before Stage2 starts') from exc
    identity = result.stdout.strip()
    if result.returncode or not re.fullmatch(r'sha256:[0-9a-f]{64}', identity):
        raise ValueError('authoring image is not locally inspectable; prepare it before starting Stage2: ' + image)
    return identity


def create(source_root: Path, target: Path, worker_config: dict, settings: dict, *,
           docker: str = 'docker', image_resolver=None) -> dict:
    """Publish one complete snapshot atomically; never overwrite an existing one."""
    source_root, target = source_root.resolve(), target.resolve()
    if target.exists():
        raise ValueError('snapshot already exists; verify/reuse it or choose a new run: ' + str(target))
    worker = _jsonable(copy.deepcopy(worker_config))
    defaults = _jsonable(copy.deepcopy(settings))
    _check_config(worker)
    _check_config(defaults)
    requested_worker = copy.deepcopy(worker)
    image = {'requested': worker.get('base_image', 'naturebench-base:v3'), 'id': None}
    if worker.get('backend', 'harbor') == 'harbor':
        image['id'] = (image_resolver or resolve_image)(image['requested'], docker)
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', image['id']):
            raise ValueError('image resolver did not return an immutable Docker image ID')
        worker['base_image'] = image['id']
    else:
        image['reason'] = 'command backend does not use a container image'

    sources = {}
    # Include the complete Stage2 module set, including schemas and future helpers.
    for path in sorted((source_root / STAGE2 / 'code').iterdir()):
        if path.suffix in {'.py', '.json'}:
            sources[path.relative_to(source_root).as_posix()] = path
    for relative in ['pipeline_runtime.py', 'orchestrator/settings.py', 'orchestrator/cli.py', 'orchestrator/interrupted_review.py',
                     'stage3-TasksRun&Verify/code/score_attempt.py',
                     *('stage1-datacard&downlond/code/' + name for name in
                       ('network_config.py', 'multimodal_data.py', 'prepare_public_data.py',
                        'paper_material_provenance.py', '05_extract_resource_candidates.py', 'data_samples.py'))]:
        sources[relative] = source_root / relative
    reference = source_root / STAGE2 / 'reference/trajectory_regressions.json'
    if reference.is_file():
        sources[reference.relative_to(source_root).as_posix()] = reference
    origins = {}
    for key, name in [('prompt01', '01_construct.md'), ('prompt02', '02_construct_verify.md')]:
        relative = f'{STAGE2}/prompt/{name}'
        original = Path(worker.get(key, source_root / relative)).resolve()
        sources[relative] = original
        origins[key] = {'path': str(original), 'sha256': _sha(original)}
        worker[key] = str(target / relative)
        defaults[key] = worker[key]
    defaults['base_image'] = worker.get('base_image', image['requested'])
    for path in sources.values():
        if path.is_symlink() or not path.is_file():
            raise ValueError('snapshot input must be a regular file: ' + str(path))
    before = {name: _sha(path) for name, path in sources.items()}
    for key, name in [('prompt01', '01_construct.md'), ('prompt02', '02_construct_verify.md')]:
        original = origins[key]
        captured = before[f'{STAGE2}/prompt/{name}']
        planned = defaults.get('input_fingerprints', {}).get(original['path'], captured)
        if original['sha256'] != captured or planned != captured:
            raise ValueError('prompt changed after run configuration was recorded; start a new run')
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.stage2-snapshot-', dir=target.parent) as temporary:
        staging = Path(temporary) / 'tree'
        staging.mkdir()
        for name, source in sources.items():
            dest = staging / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest)
            if _sha(dest) != before[name] or _sha(source) != before[name]:
                raise ValueError('source changed while creating snapshot; start a new run: ' + name)
        if any(_sha(path) != before[name] for name, path in sources.items()):
            raise ValueError('source changed while creating snapshot; start a new run')
        _write(staging / 'worker_config.json', worker)
        _write(staging / 'settings.json', defaults)
        files = {p.relative_to(staging).as_posix(): _sha(p)
                 for p in sorted(staging.rglob('*')) if p.is_file()}
        run_options = {dest: defaults[key] for key, dest in [
            ('max_construct_attempts', 'max_construct_attempts'),
            ('max_verify_attempts', 'max_verify_attempts'), ('docker', 'docker'),
            ('probe_timeout_sec', 'probe_timeout'), ('output_layout', 'output_layout'),
            ('construction_only', 'construction_only'), ('probe_runtime', 'probe_runtime')]
            if key in defaults}
        if 'stop_after' in defaults:
            run_options.update(construction_only=defaults['stop_after'] == 'stage2',
                               probe_runtime=defaults['stop_after'] != 'stage2')
        manifest = {'schema_version': SCHEMA, 'files': files, 'image': image,
                    'prompt_sources': origins, 'source_root': str(source_root),
                    'requested_worker_config': requested_worker, 'run_options': run_options}
        manifest['snapshot_sha256'] = _digest(manifest)
        _write(staging / MANIFEST, manifest)
        staging.rename(target)
    return verify(target)


def verify(root: Path, expected_sha256: str | None = None) -> dict:
    root = root.resolve()
    manifest = json.loads((root / MANIFEST).read_text())
    identity = manifest.get('snapshot_sha256')
    if (manifest.get('schema_version') != SCHEMA or
            identity != _digest({k: v for k, v in manifest.items() if k != 'snapshot_sha256'}) or
            (expected_sha256 is not None and identity != expected_sha256)):
        raise ValueError('Stage2 snapshot manifest changed or is invalid')
    actual = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('symlink in Stage2 snapshot: ' + str(path))
        relative = path.relative_to(root)
        if '__pycache__' in relative.parts or relative.as_posix() == MANIFEST:
            continue
        if path.is_file():
            actual[relative.as_posix()] = _sha(path)
        elif not path.is_dir():
            raise ValueError('special file in Stage2 snapshot: ' + str(path))
    if actual != manifest.get('files'):
        changed = sorted(k for k in actual.keys() | manifest['files'].keys()
                         if actual.get(k) != manifest['files'].get(k))
        raise ValueError('Stage2 snapshot files changed: ' + ', '.join(changed))
    return manifest


def environment(root: Path, base=None) -> dict:
    env = dict(os.environ if base is None else base)
    env['NATUREBENCH_PIPELINE_CONFIG'] = str(root.resolve() / 'settings.json')
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    # Never import a live repository through inherited Python module search paths.
    env.pop('PYTHONPATH', None)
    return env


def reference(root: Path, expected_sha256: str | None = None) -> dict:
    manifest = verify(root, expected_sha256)
    return {'path': str(root.resolve()), 'snapshot_sha256': manifest['snapshot_sha256'],
            'image': manifest['image']}


def main(argv=None):
    import argparse
    import sys
    parser = argparse.ArgumentParser(description='Verify a saved Stage2 program; no workers are started.')
    parser.add_argument('--snapshot', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        print(json.dumps({'status': 'verified', **reference(args.snapshot)}, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'error', 'error': str(exc)}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
