"""Compact private build records; temporary Harbor scaffolds are not deliverables."""
import hashlib
import json
from pathlib import Path
import shutil
import zipfile
from contract import atomic_write_json, json_reference
from worker_runtime import read_json, snapshot, copy_tree, AuthoringWorkerError


def role_input(task, output, tag):
    private=output/'metadata/inputs'
    private.mkdir(parents=True,exist_ok=True)
    prompt=private/(tag+'.md')
    shutil.copyfile(task/'instruction.md',prompt)
    inputs=task/'environment/input'
    # These shared control documents were read by the workers; keep their bytes
    # when the temporary Harbor task is removed. Source bundles live separately.
    for name in ('construction_instructions.md','trajectory_regressions.json'):
        source=inputs/name
        if source.is_file():
            target=private/name
            if target.exists() and target.read_bytes()!=source.read_bytes():
                raise ValueError('Shared authoring input changed between roles: '+name)
            if not target.exists():shutil.copyfile(source,target)
    role_reference = inputs / 'role_reference.md'
    extra = {}
    if role_reference.is_file():
        archived = private / (tag + '.reference.md')
        shutil.copyfile(role_reference, archived)
        extra['role_reference'] = str(archived)
    tools=inputs/'pipeline_tools'
    tools_ref=None
    if tools.is_dir():
        identity=hashlib.sha256(json.dumps(snapshot(tools),sort_keys=True).encode()).hexdigest()[:12]
        archive=private/('tools-'+identity+'.zip')
        if not archive.exists():
            with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as bundle:
                for path in sorted(tools.rglob('*')):
                    if path.is_file():bundle.write(path,path.relative_to(tools))
        tools_ref=str(archive)
    feedback={p.name:read_json(p) for p in inputs.glob('*.json')
              if p.name not in {'paths.json','trajectory_regressions.json'}}
    return {'prompt':str(prompt), **extra, 'paths':read_json(inputs/'paths.json'),
            'execution':read_json(task/'worker_state.json'),'feedback':feedback,
            'input_hashes':read_json(task/'worker_snapshot.json'),'tools':tools_ref}


def logs(jobs, output, tag):
    directory=output/'logs'
    directory.mkdir(parents=True,exist_ok=True)
    target=directory/(tag+'.log')
    files=sorted(jobs.rglob('job.log'))+sorted(jobs.rglob('qwen-code.txt'))+sorted(jobs.rglob('exception.txt'))
    if not files:files=sorted(jobs.glob('*.log'))
    with target.open('w',encoding='utf-8') as stream:
        if not files:stream.write('No Harbor log was produced; consult metadata/build.json for the execution error.\n')
        for source in files:
            stream.write('\n['+source.name+']\n')
            stream.write(source.read_text(errors='replace'))
    trajectories=sorted(jobs.rglob('agent/trajectory.json'))
    if len(trajectories)==1:
        shutil.copyfile(trajectories[0],directory/(tag+'.trajectory.json'))
    return str(target)


def worker_failure(jobs, output, tag, artifact_name, error):
    """Archive unfinished work separately, without accepting or modifying it."""
    jobs, output = Path(jobs), Path(output)
    if isinstance(error, AuthoringWorkerError) and error.job.is_relative_to(jobs):
        log_root = error.job
        candidates = sorted(log_root.glob(f'*/artifacts/workspace/{artifact_name}'))
        if (log_root / 'workspace' / artifact_name).exists():
            candidates.append(log_root / 'workspace' / artifact_name)
    else:
        log_root = jobs
        candidates = sorted(jobs.glob(f'*/*/artifacts/workspace/{artifact_name}'))
        candidates += sorted(jobs.glob(f'*/workspace/{artifact_name}'))
    record = {'schema_version': 'pipeline-worker-failure-v1', 'status': 'failed',
              'code': error.code if isinstance(error, AuthoringWorkerError) else 'worker_exception',
              'details': error.details if isinstance(error, AuthoringWorkerError) else {'exception_type': type(error).__name__},
              'log': logs(log_root, output, tag), 'candidate_count': len(candidates),
              'packages': [], 'copy_errors': [], 'retryable': False, 'retry_scheduled': False}
    for index, source in enumerate(candidates):
        target = output / 'failed' / tag / f'package_{index:02d}'
        try:
            # copy_tree checks descendants; check the root and its parents too.
            if any(p.is_symlink() for p in [source, *source.parents] if p.is_relative_to(jobs)):
                raise ValueError('symlink in artifact path')
            if not source.is_dir() or not any(source.iterdir()):
                raise ValueError('artifact directory missing or empty')
            target.parent.mkdir(parents=True, exist_ok=True)
            copy_tree(source, target)
            record['packages'].append(str(target))
        except (OSError, ValueError) as exc:
            record['copy_errors'].append({'candidate': index, 'exception_type': type(exc).__name__})
    if isinstance(error, AuthoringWorkerError) and error.retryable and not record['copy_errors']:
        if error.code == 'environment_start_failed':
            record['retryable'] = not candidates
        elif error.code == 'container_runtime_lost':
            # A stopped container may leave no artifact at all.  The frozen
            # task inputs are sufficient to start a clean retry.  If one
            # complete partial package was collected, pipeline.py can resume
            # from it and preserve the worker's scientific work.
            record['retryable'] = not candidates or len(candidates) == len(record['packages']) == 1
        else:
            record['retryable'] = len(candidates) == len(record['packages']) == 1
    path = output / 'metadata' / 'worker_failures' / (tag + '.json')
    atomic_write_json(path, record)
    return path, record


def checks(output, package, reports, attempt, receipt):
    names={'final_preflight.json':'preflight','review_validation.json':'review_validation',
           'package_diff.json':'package_diff','framework_generation.json':'framework_generation'}
    data={key:read_json(reports/name) for name,key in names.items() if (reports/name).is_file()}
    target=output/'metadata'/('attempt_'+attempt+'.json')
    atomic_write_json(target,data)
    if 'preflight' in data:
        receipt['preflight']=json_reference(target,'preflight')
    # Native runtime logs/evidence are already in persistent logs/runtime_NN.
    atomic_write_json(package/'readiness.json',receipt)
    return str(target)
