"""Install the submission lifecycle into run-owned effective task copies only."""
import copy
import json
import os
from pathlib import Path
import shlex
import shutil
import tomllib

from compose_runtime import normalize_runtime_compose


def normalize_mounts(compose):
    for service in compose['services'].values():
        mounts = []
        for volume in service.get('volumes', []):
            if isinstance(volume, str):
                pieces = volume.split(':')
                if len(pieces) in (2, 3) and pieces[0].startswith(('.', '/')):
                    options = pieces[2].split(',') if len(pieces) == 3 else []
                    if set(options) <= {'ro', 'rw'}:
                        volume = {'type': 'bind', 'source': pieces[0], 'target': pieces[1], 'read_only': 'ro' in options}
            mounts.append(copy.deepcopy(volume))
        service['volumes'] = mounts


def prepare_nested_mountpoints(task, compose_path, compose):
    """Create missing destinations in run-owned readonly parent bind sources."""
    for service in compose['services'].values():
        mounts = [v for v in service.get('volumes', []) if isinstance(v, dict)]
        for child in mounts:
            child_target = Path(child['target'])
            child_source = (compose_path.parent / child['source']).resolve() if child.get('type') == 'bind' else None
            for parent in mounts:
                parent_target = Path(parent['target'])
                if parent.get('type') != 'bind' or not parent.get('read_only') or child_target == parent_target or not child_target.is_relative_to(parent_target):
                    continue
                source = (compose_path.parent / parent['source']).resolve()
                destination = source / child_target.relative_to(parent_target)
                if destination.exists():
                    if child_source is not None and child_source.exists() and destination.is_dir() != child_source.is_dir():
                        raise ValueError('Nested mountpoint file/directory mismatch: ' + str(child_target))
                    continue
                owned_roots = (task.resolve(), (task.parent / 'runtime_data').resolve())
                if not any(destination.resolve().is_relative_to(root) for root in owned_roots):
                    raise ValueError('Nested mountpoint is outside the run-owned task: ' + str(child_target))
                if child_source is not None and child_source.is_file():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.touch()
                else:
                    destination.mkdir(parents=True, exist_ok=True)


EXPERIMENT_LOG_PATHS = ('/workspace/experiment_log.jsonl', '/workspace/run_log.md')


def record_experiment_artifacts(document):
    entries = document.setdefault('artifacts', [])
    main_sources = {item if isinstance(item, str) else item.get('source')
                    for item in entries if isinstance(item, str) or item.get('service') in (None, 'main')}
    for path in EXPERIMENT_LOG_PATHS:
        if path not in main_sources:
            entries.append({'source': path, 'service': 'main'})


def install(task, spec, layout, *, prepared=False):
    from framework_generator import toml_text
    task_path = task / 'task.toml'
    document = tomllib.loads(task_path.read_text())
    record_experiment_artifacts(document)
    evidence = spec.get('verification', {}).get('score_evidence')
    if not evidence or (not evidence.get('evaluation_protocol') and not spec.get('evaluation_protocol_version')):
        import yaml
        path = task / Path(layout['compose_file']).relative_to('common_task')
        compose = yaml.safe_load(path.read_text())
        normalize_mounts(compose)
        solver_names = [name for name in layout.get('solver_services', []) if name in compose.get('services', {})]
        normalize_runtime_compose(compose, solver=solver_names[0] if solver_names else 'main', legacy_solver='solver')
        prepare_nested_mountpoints(task, path, compose)
        path.write_text(yaml.safe_dump(compose, sort_keys=False))
        instruction = task / 'instruction.md'
        instruction.write_text(instruction.read_text() + '''

## Submission control for this run: legacy compatibility

This task uses legacy_scoring_export. No check/submit client or acceptance-receipt interface is installed. The general guidance's explicit-submit commands apply only to tasks that provide that interface. Use only the submission and validation interface actually documented by this task; do not invent commands, receipts or scores. Repeat your own permitted checks and repairs within this same session. This legacy mode does not claim the new one-time submission-freeze guarantee.
''')
        task_path.write_text(toml_text(document))
        return {'mode': 'legacy_scoring_export', 'formal_submission_required': False}
    if not evidence.get('evaluation_protocol'):
        raise ValueError('Formal submission requires the declared evaluation protocol')
    import yaml
    from score_attempt import write_json
    compose_path = task / Path(layout['compose_file']).relative_to('common_task')
    compose = yaml.safe_load(compose_path.read_text())
    normalize_mounts(compose)
    service = evidence['service']
    solver_names = [s for s in layout['solver_services'] if s in compose['services']]
    solver = solver_names[0] if solver_names else 'main'
    normalize_runtime_compose(compose, solver=solver, legacy_solver='solver')
    evaluator = compose['services'][service]
    collects = document['verifier']['collect']
    matching = [entry for entry in collects if entry.get('service') == service and evidence['entrypoint_container'] in entry.get('command', '')]
    if len(matching) != 1:
        raise ValueError('Expected one canonical evaluator collect command')
    collect = matching[0]
    argv = shlex.split(collect['command'])
    if '--' not in argv:
        raise ValueError('Scoring entry must declare an evaluator command')
    command = argv[argv.index('--') + 1:]
    tools_dir = task / 'submission_runtime'
    tools_dir.mkdir()
    source = Path(__file__).parent
    for name in ('submission_control.py', 'submission_rules.py', 'score_attempt.py'):
        shutil.copyfile(source / name, tools_dir / name)
    client_dir = task / 'submission_public'
    client_dir.mkdir()
    shutil.copyfile(source / 'submission_client.py', client_dir / 'submission_client.py')
    mount = '/task/submission_runtime'
    public = '/task/submission_tools'
    mailbox = '/task/submission_control'
    old_entry = evaluator.get('entrypoint') or []
    old_command = evaluator.get('command') or []
    if isinstance(old_entry, str): old_entry = shlex.split(old_entry)
    if isinstance(old_command, str): old_command = shlex.split(old_command)
    protocol = evidence['evaluation_protocol']['config_container']
    config = {'submission': spec['submission']['path'], 'protocol': protocol,
              'evidence_root': evidence['artifact_source'],
              'outbox': mailbox + '/outbox', 'inbox': mailbox + '/inbox',
              'scoring_command': command, 'timeout': max(1, int(collect.get('timeout_sec', 360)) - 60),
              'bootstrap': old_entry + old_command}
    write_json(tools_dir / 'config.json', config)
    for name in ('pipeline_submit_outbox', 'pipeline_submit_inbox'):
        compose.setdefault('volumes', {})[name] = {}
    def bind(source_path, target):
        return {'type': 'bind', 'source': os.path.relpath(source_path, compose_path.parent), 'target': target, 'read_only': True}
    evaluator.setdefault('volumes', []).extend([
        bind(tools_dir, mount),
        {'type': 'volume', 'source': 'pipeline_submit_outbox', 'target': mailbox + '/outbox'},
        {'type': 'volume', 'source': 'pipeline_submit_inbox', 'target': mailbox + '/inbox'}])
    compose['services'][solver].setdefault('volumes', []).extend([
        bind(client_dir, public),
        {'type': 'volume', 'source': 'pipeline_submit_outbox', 'target': mailbox + '/outbox'},
        {'type': 'volume', 'source': 'pipeline_submit_inbox', 'target': mailbox + '/inbox', 'read_only': True}])
    evaluator['entrypoint'] = []
    evaluator['command'] = ['python', mount + '/submission_control.py', 'serve', '--config', mount + '/config.json']
    collect['command'] = shlex.join(['python', mount + '/submission_control.py', 'finalize', '--config', mount + '/config.json'])
    # Only acquisition-designated public data becomes writable. Runtime storage is
    # outside immutable task fingerprints, seeded from any supplied original data.
    resources = json.loads((task / Path(evidence['evaluation_protocol']['config_path']).relative_to('common_task')).read_text()).get('resources', {})
    data_mounts = []
    for rid, resource in resources.items():
        if prepared or resource.get('visibility') != 'public' or resource.get('availability') == 'bundled':
            continue
        relative = Path(resource['package_path']).relative_to('common_task')
        runtime_path = Path(resource['runtime_path'])
        is_directory = resource.get('content', {}).get('format') == 'directory'
        origin = task / (relative if is_directory else relative.parent)
        runtime_target = runtime_path if is_directory else runtime_path.parent
        public_root = task / Path(layout['public_dir']).relative_to('common_task')
        file_mount = not is_directory and origin == public_root
        if file_mount:
            # Root-level public files need individual binds. Never make the
            # whole task description directory writable just to download one file.
            origin = task / relative
            runtime_target = runtime_path
            if origin.is_dir():
                raise ValueError('Resource contract conflict: ' + rid + ' declares a file but package_path is a directory')
        if not origin.is_relative_to(public_root) or origin == public_root:
            raise ValueError('Downloadable public resource must stay below the public task root: ' + rid)
        if any(origin == item[0] for item in data_mounts):
            continue
        # Docker cannot create a nested bind destination inside a read-only
        # parent bind. Seed the empty directory in the run-owned public copy
        # before mounting it read-only, then mount the writable data there.
        # This creates no data and never touches the accepted source package.
        if not origin.resolve().is_relative_to(public_root.resolve()):
            raise ValueError('Public acquisition directory escapes the run-owned public root: ' + rid)
        storage = task.parent / 'runtime_data' / str(len(data_mounts))
        if file_mount:
            origin.parent.mkdir(parents=True, exist_ok=True)
            origin.touch(exist_ok=True)
            storage.mkdir(parents=True)
            storage = storage / origin.name
            shutil.copyfile(origin, storage)
        else:
            origin.mkdir(parents=True, exist_ok=True)
            shutil.copytree(origin, storage)
        # Images may run the solver under a non-root UID.
        for item in [storage, *(storage.rglob('*') if storage.is_dir() else [])]:
            if not item.is_symlink(): item.chmod(0o777 if item.is_dir() else 0o666)
        data_mounts.append((origin, storage))
        for name in (solver, service):
            volumes = compose['services'][name].setdefault('volumes', [])
            targets = {str(runtime_target)}
            for volume in volumes:
                if isinstance(volume, dict) and volume.get('type') == 'bind':
                    mounted = (compose_path.parent / volume['source']).resolve()
                    if origin.resolve().is_relative_to(mounted):
                        targets.add(str(Path(volume['target']) / origin.resolve().relative_to(mounted)))
            for target in targets:
                volumes[:] = [v for v in volumes if not isinstance(v, dict) or v.get('target') != target]
                volumes.append({'type': 'bind', 'source': str(storage.resolve()), 'target': target, 'read_only': name != solver})
    prepare_nested_mountpoints(task, compose_path, compose)
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False))
    task_path.write_text(toml_text(document))
    instruction = task / 'instruction.md'
    instruction.write_text(instruction.read_text() + '''\n\n## Submission control for this run\n
This section defines the actual submission procedure for this run. It supersedes any older operational directions about automatic submission, repeated official scoring or immediate official feedback; it does not change the scientific objective, data scope, artifact requirements, metrics or permissions.
Continue within this same Solver session, preserving files and useful intermediate results.
You may repeat `python /task/submission_tools/submission_client.py check`, repair the reported issues, and rerun affected steps. Checks do not calculate or return hidden scientific scores.
When ready, run `python /task/submission_tools/submission_client.py submit` exactly once and require an `accepted` receipt. This freezes the configured submission file (including all required members if it is an archive). Do not modify it afterwards. If a request times out, retry the same action with the returned `--request-id` until its outcome is known.
The official score and rule checks run after this session ends, followed by content-validity review only if the applicable hard checks pass. No 04 feedback is returned for repairs in this run. Do not wait for an official score in this session. After acceptance, provide the final summary and end the session. A final chat message alone is not a submission. If blocked, report the blocker honestly; do not fabricate a submission.
''' + ('The formal data have been prepared and verified before this run. Read the supplied resources at the declared paths.\n'
       if prepared else 'Use acquisition-designated writable paths for authorized downloads. Validate full resources before use.\n'))
    return {'mode': 'explicit-submit-v1', 'formal_submission_required': True,
            'public_client': public + '/submission_client.py', 'evidence_service': service,
            'runtime_data_directories': [str(storage if storage.is_dir() else storage.parent) for _, storage in data_mounts]}
