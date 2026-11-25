#!/usr/bin/env python3
"""Stage1 downloaded materials -> frozen Stage2 queue -> verified Stage3 pair."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

HERE = Path(__file__).resolve().parent
PIPELINE_ROOT = HERE.parents[1]
STAGE2 = PIPELINE_ROOT / 'stage2-TasksConstruction&Verify'
STAGE3 = PIPELINE_ROOT / 'stage3-TasksRun&Verify'
sys.path.insert(0, str(STAGE2 / 'code'))
from source_intake import prepare_sources
from worker_runtime import atomic_json, read_json, snapshot, file_sha256
from authoring_package import read_paths


def read_rows(path: Path) -> list[dict]:
    rows = []
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError('duplicate JSON key: ' + key)
            result[key] = value
        return result
    with path.open() as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            row = json.loads(line, object_pairs_hook=pairs,
                             parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
            if not isinstance(row, dict):
                raise ValueError(f'{path}:{number}: expected object')
            rows.append(row)
    return rows


def safe_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError('canonical_id must be nonempty')
    prefix = re.sub(r'[^A-Za-z0-9._-]', '-', value).strip('._-')[:90] or 'paper'
    return prefix + '-' + hashlib.sha256(value.encode()).hexdigest()[:12]


def _converter():
    module_spec = importlib.util.spec_from_file_location('pipeline_multimodal', HERE / 'multimodal_data.py')
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    return module


def prepare_handoff(records: Path, download_results: Path, output: Path,
                    worker_config: dict, *, canonical_ids=None,
                    material_roots=(), multimodal=True, model_capability='text') -> dict:
    """Freeze a reviewed selection; materials are private authoring input only."""
    output = output.resolve()
    if output.exists():
        raise ValueError(f'refusing to overwrite handoff: {output}')
    if not isinstance(worker_config, dict):
        raise ValueError('worker_config must describe the authoring process')
    papers = read_rows(records)
    downloads = read_rows(download_results)
    index = {}
    for paper in papers:
        key = paper.get('canonical_id')
        if key in index:
            raise ValueError(f'duplicate canonical_id in input: {key}')
        safe_id(key)
        index[key] = paper
    selected = canonical_ids if canonical_ids is not None else list(dict.fromkeys(row.get('canonical_id') for row in downloads))
    if not selected or any(key not in index for key in selected):
        raise ValueError('selection empty or canonical_id absent from paper records')
    if len(selected) != len(set(selected)):
        raise ValueError('duplicate selected canonical_id')
    records_hash, download_hash = file_sha256(records), file_sha256(download_results)
    output.mkdir(parents=True)
    manifest = {'schema_version': 'pipeline-stage1-handoff-v1', 'status': 'preparing',
                'records': {'path': str(records.resolve()), 'sha256': records_hash},
                'downloads': {'path': str(download_results.resolve()), 'sha256': download_hash},
                'public_data_policy': 'All Stage1 materials are private Builder input. Stage2 must classify public/guided/private resources before Solver delivery.',
                'run_ready': False, 'items': []}
    atomic_json(output / 'handoff_manifest.json', manifest)
    try:
        for canonical_id in selected:
            directory = output / 'papers' / safe_id(canonical_id)
            directory.mkdir(parents=True)
            sources = directory / 'sources'
            intake = prepare_sources(records, canonical_id, sources, download_results,
                                     material_roots=material_roots,
                                     review_paper_materials=bool(index[canonical_id].get('identifiers') and index[canonical_id].get('title')))
            conversion = None
            if multimodal:
                conversion = _converter().prepare_multimodal_data(
                    sources / 'files', sources / 'files/.qwen', model_capability=model_capability)
            destination = output / 'stage2' / safe_id(canonical_id)
            paths_file = directory / 'paths.json'
            atomic_json(paths_file, {'source_bundle': str(sources), 'output_dir': str(destination)})
            atomic_json(directory / 'worker_config.json', worker_config)
            related = [row for row in downloads if row.get('canonical_id') == canonical_id]
            row = {'canonical_id': canonical_id, 'sources': str(sources),
                   'paths_file': str(paths_file), 'worker_config': str(directory / 'worker_config.json'),
                   'input_files': snapshot(directory), 'material_summary': intake['summary'],
                   'pending_resources': [{'resource_id': item.get('resource_id'), 'status': item.get('status'),
                                          'kind': item.get('kind'), 'reason': item.get('error') or item.get('reason')}
                                         for item in related if item.get('status') not in {'downloaded', 'materialized_local', 'reused_verified'}],
                   'multimodal': conversion.get('compatibility') if conversion else {'status': 'not_requested'},
                   'stage2_eligible': True, 'run_ready': False,
                   'stage2_command': [sys.executable, str(STAGE2 / 'code/pipeline.py'), 'run',
                       '--paths', str(paths_file), '--task-id', safe_id(canonical_id),
                       '--worker-config', str(directory / 'worker_config.json'), '--construction-only']}
            manifest['items'].append(row)
            atomic_json(output / 'handoff_manifest.json', manifest)
        if file_sha256(records) != records_hash or file_sha256(download_results) != download_hash:
            raise ValueError('Stage1 input indexes changed during export; select a completed snapshot')
        queue = output / 'stage2_queue.jsonl'
        queue.write_text(''.join(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n' for row in manifest['items']))
        manifest.update(status='prepared', queue={'path': str(queue), 'sha256': file_sha256(queue)},
                        paper_count=len(manifest['items']))
    except Exception as error:
        manifest.update(status='failed', error=str(error))
        atomic_json(output / 'handoff_manifest.json', manifest)
        raise
    atomic_json(output / 'handoff_manifest.json', manifest)
    return manifest


def run_stage2(handoff: Path, *, canonical_id=None, all_papers=False, probe_runtime=False) -> dict:
    handoff = handoff.resolve()
    manifest = read_json(handoff)
    if manifest.get('status') != 'prepared':
        raise ValueError('handoff is not prepared')
    queue = manifest['queue']
    if file_sha256(Path(queue['path'])) != queue['sha256']:
        raise ValueError('Stage2 queue changed after handoff')
    items = read_rows(Path(queue['path']))
    selected = items if all_papers else [item for item in items if item['canonical_id'] == canonical_id]
    if not selected:
        raise ValueError('no selected Stage2 work; use explicit --canonical-id or --all')
    results = []
    for item in selected:
        root = Path(item['paths_file']).parent
        if snapshot(root) != item['input_files']:
            raise ValueError('frozen materials/config changed for ' + item['canonical_id'])
        # Reconstruct executable argv from checked fields; never trust a queue's shell string.
        destination = handoff.parent / 'stage2' / safe_id(item['canonical_id'])
        paths = read_paths(Path(item['paths_file']))
        if Path(paths['source_bundle']).resolve() != Path(item['sources']).resolve() or Path(paths['output_dir']).resolve() != destination:
            raise ValueError('handoff paths do not match the selected materials/output')
        command = [sys.executable, str(STAGE2 / 'code/pipeline.py'), 'run',
                   '--paths', item['paths_file'], '--task-id', safe_id(item['canonical_id']),
                   '--worker-config', item['worker_config'],
                   '--probe-runtime' if probe_runtime else '--construction-only']
        result = subprocess.run(command, check=False)
        results.append({'canonical_id': item['canonical_id'], 'exit_code': result.returncode,
                        'run_manifest': str(destination / 'run_manifest.json')})
    status = 'completed' if all(row['exit_code'] == 0 for row in results) else 'blocked'
    report = {'status': status, 'results': results}
    atomic_json(handoff.parent / 'stage2_batch_result.json', report)
    return report


def prepare_stage3(stage2_manifest: Path, output: Path, *, solver_model: str, agent='qwen-coder') -> dict:
    result = read_json(stage2_manifest)
    if result.get('status') != 'ready' or not result.get('package') or result.get('execution_ready') is not True:
        raise ValueError('Stage2 has not completed real run_ready verification')
    if not solver_model.strip() or solver_model.startswith('REPLACE_'):
        raise ValueError('provide the actual Solver model')
    if output.exists():
        raise ValueError('Stage3 handoff output already exists')
    command = [sys.executable, str(STAGE3 / 'code/pipeline.py'), 'prepare', '--package', result['package'],
               '--output', str(output / 'pair')]
    subprocess.run(command, check=True)
    commands = {variant: [sys.executable, str(STAGE3 / 'code/pipeline.py'), 'run',
                  '--pair', str((output / 'pair/pair_manifest.json').resolve()), '--variant', variant,
                  '--agent', agent, '--model', solver_model, '--output', str((output / (variant + '-run')).resolve())]
                for variant in ('blind', 'guided')}
    report = {'schema_version': 'pipeline-stage3-handoff-v1', 'status': 'prepared',
              'stage2_manifest': str(stage2_manifest.resolve()), 'solver_commands': commands,
              'judge_policy': 'After execution, explicitly select actual SCORE_ATTEMPT, freeze RUN/run_export.json, then judge.'}
    atomic_json(output / 'stage3_plan.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--records', type=Path, required=True)
    prep.add_argument('--download-results', type=Path, required=True)
    prep.add_argument('--worker-config', type=Path, required=True)
    prep.add_argument('--output', type=Path, required=True)
    prep.add_argument('--canonical-id', action='append')
    prep.add_argument('--material-root', type=Path, action='append', default=[])
    prep.add_argument('--skip-multimodal', action='store_true')
    prep.add_argument('--model-capability', choices=['text', 'vision', 'unknown'], default='text')
    run = sub.add_parser('run-stage2')
    run.add_argument('--handoff', type=Path, required=True)
    run.add_argument('--probe-runtime', action='store_true',
                     help='Explicitly run container acceptance; default is construction only')
    selection = run.add_mutually_exclusive_group(required=True)
    selection.add_argument('--canonical-id')
    selection.add_argument('--all', dest='all_papers', action='store_true')
    third = sub.add_parser('prepare-stage3')
    third.add_argument('--stage2-manifest', type=Path, required=True)
    third.add_argument('--output', type=Path, required=True)
    third.add_argument('--solver-model', required=True)
    third.add_argument('--agent', default='qwen-coder')
    args = parser.parse_args()
    try:
        if args.action == 'prepare':
            value = prepare_handoff(args.records, args.download_results, args.output,
                read_json(args.worker_config),
                canonical_ids=args.canonical_id, material_roots=args.material_root,
                multimodal=not args.skip_multimodal, model_capability=args.model_capability)
            print(json.dumps({'status': value['status'], 'paper_count': value['paper_count'], 'queue': value['queue']}, ensure_ascii=False))
        elif args.action == 'run-stage2':
            value = run_stage2(args.handoff, canonical_id=args.canonical_id,
                               all_papers=args.all_papers, probe_runtime=args.probe_runtime)
            print(json.dumps(value, ensure_ascii=False))
        else:
            value = prepare_stage3(args.stage2_manifest, args.output, solver_model=args.solver_model, agent=args.agent)
            print(json.dumps(value, ensure_ascii=False))
        return 2 if value.get('status') == 'blocked' else 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        print(json.dumps({'status': 'error', 'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
