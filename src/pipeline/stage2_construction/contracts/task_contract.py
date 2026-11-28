"""Public solver contract derived from the mounted resources and the actual Harbor collector."""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import re
import shlex
import tomllib
from urllib.parse import urlsplit

from pipeline.stage2_construction.contracts.contract import atomic_write_json, package_layout, package_path
from pipeline.stage2_construction.contracts.resource_contract import file_manifest_valid, has_payload, valid_digest

SCHEMA_VERSION = 'solver-task-contract-v2'
SOLVER_WORKDIR = '/workspace'
SOLVER_SCRIPT = '/workspace/run.py'


def submission_location_errors(package, spec):
    """The collector opens one file. Its parent alone is the shared volume."""
    from instruction_checks import _visible_lines
    value = spec.get('submission', {}).get('path')
    if not isinstance(value, str) or not PurePosixPath(value).is_absolute():
        return []  # execution schema reports malformed or missing paths
    submission = PurePosixPath(value)
    mount = submission.parent
    errors = []
    if (PurePosixPath(SOLVER_WORKDIR).is_relative_to(mount) or
            PurePosixPath(SOLVER_SCRIPT).is_relative_to(mount) or mount.is_relative_to(SOLVER_SCRIPT)):
        errors.append(f'submission.path={submission} is collected as a file. Its parent {mount} would mount the '
                      f'shared submission volume over the Solver workdir {SOLVER_WORKDIR} or entrypoint {SOLVER_SCRIPT}. '
                      'Set submission.path to the actual submitted file inside a dedicated output directory.')
    layout = package_layout(spec)
    directive = re.compile(r'^\s*[-*|\s]*(?:submission[ .]path\s*[:=|]|'
                           r'(?:submit|write|save)(?:\s+(?:your|the))?(?:\s+(?:predictions?|results?|outputs?|submission|file))?'
                           r'\s+(?:to|at|as)\s*:?|submit\s+)\s*(?:`(/[^\n`]+)`|(/[^\s|,;]+))', re.I)
    docs = [layout['instruction_path'], layout['readme_path'], layout['data_description_path']]
    # An unmounted top-level README can clarify an independently proven mount
    # conflict but cannot impose a Solver requirement by itself.
    if errors and 'README.md' not in docs:
        docs.append('README.md')
    for relative in docs:
        path = package_path(package, relative)
        if not path.is_file():
            continue
        document = path.read_text(encoding='utf-8')
        if str(submission).rstrip('/') + '/' not in document:
            continue
        try:
            lines = list(_visible_lines(document))
        except ValueError:
            # Other instruction checks diagnose malformed Markdown. Do not infer
            # a required file from an unterminated example or comment.
            continue
        for _, line in lines:
            match = directive.match(line)
            if not match:
                continue
            target = PurePosixPath(match[1] or match[2].rstrip('.:'))
            if '..' not in target.parts and target != submission and target.is_relative_to(submission):
                errors.append(f'{relative} names {target} below the configured submission file {submission}. '
                              'Align submission.path and the published file path. Use {submission} in '
                              'required_artifacts.paths for a directly submitted file, or name actual archive members.')
                break
    return errors


def public_path(relative, spec):
    layout = package_layout(spec)
    path = PurePosixPath(relative)
    mounts = [(layout['public_dir'], '/task/problem')]
    if spec['runtime']['data_dir'] not in ('/task/problem', '/task/input'):
        mounts.insert(0, (layout['data_path'], spec['runtime']['data_dir']))
    for local, mount in mounts:
        if path.is_relative_to(local):
            return str(PurePosixPath(mount) / path.relative_to(local))
    raise ValueError(f'public resource outside solver mounts: {relative}')


def has_bytes(package, relative):
    return has_payload(package_path(package, relative))


def unresolved_external_source(source):
    if source.get('availability') != 'unverified' or source.get('origin') not in {'observation', 'reference_implementation'}:
        return False
    if valid_digest(source.get('sha256')) or file_manifest_valid(source.get('files_sha256')):
        return False
    uri = source.get('url')
    if isinstance(uri, str) and urlsplit(uri).netloc:
        return False
    prep = source.get('prepare', {})
    if isinstance(prep, dict):
        if prep.get('inputs'):
            return False
        mount = prep.get('shared_mount')
        if (isinstance(mount, dict) and mount.get('read_only') is True and mount.get('name')
                and isinstance(mount.get('path'), str) and PurePosixPath(mount['path']).is_absolute()):
            return False
    return True


def _deferred_evidence(package, source, indexed, smoke_bindings):
    from pipeline.stage2_construction.contracts.resource_contract import argv, verify_content
    if source.get('availability') not in {'preprovided', 'unverified', 'runtime_download'}:
        return None
    prep = source.get('prepare')
    if not isinstance(prep, dict) or not argv(prep.get('command')) or not argv(prep.get('validate')):
        return None
    if not prep.get('access') or type(prep.get('timeout_sec')) is not int or prep['timeout_sec'] <= 0:
        return None
    unknown = {'', 'unknown', 'none', 'unavailable', 'tbd', 'unspecified'}
    if any(str(source.get(k) or '').strip().lower() in unknown for k in ('id', 'identity', 'version', 'package_path')):
        return None
    for command in (prep['command'], prep['validate']):
        if any(re.fullmatch(r'\{[a-z_]+\}', a) and a not in {'{package}','{output}','{execution}','{resource_id}'}
               for a in command):
            return None
        if Path(command[0]).name in {'true', 'echo', 'false', ':'}:
            return None
        if command[-1].strip() in {'pass', 'exit 0', 'true', ':'}:
            return None
        script = command[0]
        if Path(script).name in {'bash', 'sh', 'python', 'python3'} and len(command) > 1:
            script = command[1]
        if script.endswith(('.py', '.sh')) and not script.startswith('/'):
            try:
                available = has_bytes(package, script.removeprefix('{package}/'))
            except (ValueError, OSError):
                available = False
            if not available:
                return None
    # `package_path` is the canonical field.  Older authoring drafts and
    # the public execution schema also use `path`, so normalize both forms
    # before producing deferred-resource evidence.
    source_package_path = source.get('package_path') or source.get('path')
    if not isinstance(source_package_path, str) or not source_package_path.strip():
        return None
    evidence = {'id': source['id'], 'package_path': source_package_path}
    # External observations/weights need an identifiable origin. Analytical or
    # simulation records may be produced by a self-contained versioned generator.
    if source.get('origin') not in {'analytical', 'simulation'}:
        uri = source.get('url')
        mount = prep.get('shared_mount')
        # Formal public inputs may be supplied by the Stage3 problem mount.
        # Construction deliberately carries only fixtures, so accept a
        # declared preprovided preparation recipe that reads the fixed
        # /task/problem/data path and validates the copied package artifact.
        if (source.get('availability') in {'preprovided', 'runtime_download'}
                and any('/task/problem/data' in str(arg) for arg in prep['command'])
                and isinstance(source.get('schema_description'), str)
                and source.get('schema_description').strip()):
            return dict(evidence, verification_stage='runtime_preparation', acquisition='problem_mount')
        shared = (source.get('availability') == 'preprovided' and isinstance(mount, dict)
                  and isinstance(mount.get('name'), str) and bool(mount['name'].strip())
                  and isinstance(mount.get('path'), str) and PurePosixPath(mount['path']).is_absolute()
                  and '..' not in PurePosixPath(mount['path']).parts and mount.get('read_only') is True
                  and any(mount['path'] in arg for arg in prep['command']))
        if not shared and not (isinstance(uri, str) and urlsplit(uri).netloc):
            return None
    content = source.get('content', {})
    if isinstance(content, dict):
        if content.get('format') in {'binary', 'directory'} and valid_digest(source.get('sha256')):
            return dict(evidence, sha256=source['sha256'])
        if content.get('format') == 'directory' and file_manifest_valid(source.get('files_sha256')):
            return dict(evidence, manifest_files=len(source['files_sha256']))
    # Operator mounts are resolved against the private registry by Stage 3. The
    # registry, data bytes and measured hashes may all be unavailable during 02.
    mount = prep.get('shared_mount')
    if (source.get('availability') == 'preprovided' and isinstance(mount, dict)
            and set(mount) == {'name', 'path', 'read_only'} and mount.get('read_only') is True
            and isinstance(mount.get('name'), str) and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]*', mount['name'])
            and isinstance(mount.get('path'), str) and PurePosixPath(mount['path']).is_absolute()
            and '..' not in PurePosixPath(mount['path']).parts
            and any(arg == mount['path'] or (arg.startswith(mount['path'].rstrip('/') + '/')
                    and '..' not in PurePosixPath(arg).parts) for arg in prep['command'])
            and isinstance(content, dict) and content.get('format') in {'binary','directory','json','csv','tsv','npy','text'}
            and (source.get('schema_description') or any(content.get(k) for k in ('columns','shape','members','items_path')))):
        return dict(evidence, operator_mount=mount['name'], verification_stage='runtime_preparation')
    uri = source.get('url')
    if (source.get('availability') == 'runtime_download' and isinstance(uri, str)
            and urlsplit(uri).scheme in {'http','https'} and urlsplit(uri).netloc and uri in prep['command']
            and isinstance(content, dict) and content.get('format')
            and (source.get('schema_description') or any(content.get(k) for k in ('columns','shape','members','items_path')))):
        return dict(evidence, verification_stage='runtime_preparation', acquisition='declared_download')
    sample_id = source.get('representative_sample') or smoke_bindings.get(source['id'])
    sample = indexed.get(sample_id) if isinstance(sample_id, str) else None
    if not sample or sample_id == source['id'] or sample.get('availability') != 'bundled':
        return None
    sample_content = sample.get('content', {})
    if not isinstance(content, dict) or not isinstance(sample_content, dict) or not content.get('format'):
        return None
    if any(content[k] != sample_content.get(k) for k in ('format', 'columns', 'id_column', 'items_path', 'forbidden_keys')
           if k in content):
        return None
    if content['format'] == 'npy' and 'shape' in content:
        shape = sample_content.get('shape')
        if not isinstance(shape, list) or len(shape) != len(content['shape']) or shape[1:] != content['shape'][1:]:
            return None
    try:
        sample_path = package_path(package, sample['package_path'])
        if not has_bytes(package, sample['package_path']):
            return None
        verify_content(sample_path, sample)
    except (KeyError, OSError, TypeError, ValueError):
        return None
    return dict(evidence, sample_id=sample_id)


def resource_delivery(package, spec):
    """Classify construction evidence without executing authored programs or fetching resources."""
    layout = package_layout(spec)
    sources = spec.get('data', {}).get('sources', [])
    indexed = {s['id']: s for s in sources if isinstance(s, dict) and isinstance(s.get('id'), str)}
    smoke_path = spec.get('verification', {}).get('scoring_smoke', {}).get('config_path')
    if not smoke_path:
        smoke_path = str(PurePosixPath(layout['score_path']).with_name('scoring_smoke.json'))
    try:
        smoke_file = package_path(package, smoke_path)
        if not smoke_file.is_file():
            evaluation = package_path(package, layout['score_path']).parent
            candidate = evaluation / smoke_path
            if candidate.is_file() and candidate.resolve().is_relative_to(evaluation.resolve()):
                smoke_file = candidate
        smoke_bindings = json.loads(smoke_file.read_text()).get('resource_bindings', {})
    except (OSError, TypeError, ValueError, AttributeError):
        smoke_bindings = {}
    if not isinstance(smoke_bindings, dict):
        smoke_bindings = {}
    missing, deferred = [], []
    for source in sources:
        if not isinstance(source, dict) or source.get('visibility') == 'build_only' or source.get('required', True) is not True:
            continue
        relative = source.get('package_path') or source.get('path')
        if not relative:
            continue  # Invalid bindings are repairable.
        if source.get('availability') == 'unavailable':
            missing.append(f"{source.get('id') or source.get('name')}: explicitly unavailable {relative}")
        elif (source.get('availability') in {'preprovided', 'runtime_download'}
              and isinstance(source.get('prepare'), dict)):
            # A bundled sample documents the schema only. The prepare recipe
            # always makes the complete formal input authoritative at Stage3,
            # including recipes that materialize from a private package script
            # or an operator-approved preprovided source. Never infer that a
            # schema-compatible sample is the formal split.
            evidence = _deferred_evidence(package, source, indexed, smoke_bindings)
            if evidence:
                deferred.append(evidence)
            else:
                missing.append(f"{source.get('id') or source.get('name')}: invalid runtime preparation")
        elif (source.get('availability') == 'unverified'
              and isinstance(source.get('prepare'), dict)
              and not has_bytes(package, relative)):
            # A formal source may be intentionally absent during construction
            # when its preparation recipe is complete and an independent
            # bundled sample binds the declared schema. Stage 3 materializes
            # the formal bytes before Solver execution.
            evidence = _deferred_evidence(package, source, indexed, smoke_bindings)
            if evidence:
                deferred.append(evidence)
            else:
                missing.append(f"{source.get('id') or source.get('name')}: invalid deferred preparation {relative}")
        elif unresolved_external_source(source):
            missing.append(f"{source.get('id') or source.get('name')}: unverified external resource {relative} "
                           "has no pinned digest, acquisition URL, preparation mount or upstream resource")
        elif not has_bytes(package, relative):
            evidence = _deferred_evidence(package, source, indexed, smoke_bindings)
            if evidence:
                deferred.append(evidence)
            else:
                missing.append(f"{source.get('id') or source.get('name')}: missing or empty {relative}")
    planned_data = any(PurePosixPath(r['package_path']).is_relative_to(layout['data_path']) for r in deferred)
    if spec.get('data', {}).get('mode') != 'no_external_data' and not planned_data and not has_bytes(package, layout['data_path']):
        missing.append(f"empty or unavailable data directory: {layout['data_path']}")
    return missing, deferred


def missing_resources(package, spec):
    return resource_delivery(package, spec)[0]


def _inputs(package, spec):
    layout = package_layout(spec)
    deferred = {r['id'] for r in resource_delivery(package, spec)[1]}
    inputs = []
    for key, purpose, fields in (
        ('readme_path', 'Package usage and file layout', 'Markdown usage instructions'),
        ('data_description_path', 'Input provenance and field definitions', 'Markdown dataset documentation'),
        # A bundled analytical fixture may carry an internal acquisition
        # manifest for authoring, but it is not a Solver input unless the
        # execution mode exposes external/reference data.  Referenced and
        # mixed tasks publish the manifest and must declare it publicly.
        ('data_sources_path', 'Public acquisition manifest', 'Public source records and acquisition information'),
    ):
        if key == 'data_sources_path' and spec.get('data', {}).get('mode') not in {'referenced', 'mixed'}:
            continue
        relative = layout[key]
        if package_path(package, relative).is_file():
            inputs.append(dict(id=key, path=public_path(relative, spec), purpose=purpose,
                               required=True, format=Path(relative).suffix.lstrip('.'),
                               fields=fields, provided=has_bytes(package, relative)))
    for source in spec.get('data', {}).get('sources', []):
        if source.get('visibility', 'public') != 'public':
            continue
        relative = source.get('package_path') or source.get('path')
        if not relative:
            raise ValueError(f"public source has no package path: {source.get('name')}")
        content = source.get('content', {})
        fields = {k: v for k, v in content.items() if k != 'format'}
        if source.get('schema_description'):
            fields['description'] = source['schema_description']
        # A bundled authoring fixture is useful for construction and smoke
        # tests, but it is not the Solver's formal input when the resource is
        # materialized by the Stage3 problem mount.  Keep both facts explicit
        # in the machine-readable contract instead of reporting a misleading
        # ``provided: true`` alongside ``materialization_status: deferred``.
        sample_available = has_bytes(package, relative) and not unresolved_external_source(source)
        prep = source.get('prepare', {})
        command = prep.get('command', []) if isinstance(prep, dict) else []
        problem_mount_source = (source.get('availability') in {'preprovided', 'runtime_download'}
                                and any('/task/problem/data' in str(arg) for arg in command))
        provided = sample_available and source.get('id') not in deferred and not problem_mount_source
        solver_path = source.get('runtime_path')
        if not isinstance(solver_path, str) or not solver_path.startswith('/'):
            if (source.get('availability') == 'preprovided'
                    and any('/task/problem/data' in str(arg) for arg in command)):
                solver_path = '/task/problem/data/' + Path(str(relative)).name
        entry = dict(id=source.get('id') or source.get('name'), path=solver_path or public_path(relative, spec),
                     purpose=source.get('purpose') or source.get('identity') or source.get('name'),
                     required=source.get('required', True), format=content.get('format'),
                     fields=fields, provided=provided,
                     availability=source.get('availability'), package_path=relative)
        if source.get('id') in deferred or problem_mount_source:
            entry.update(materialization_status='deferred', sample_available=sample_available)
        inputs.append(entry)
    return inputs


def _output(submission):
    artifacts = submission['required_artifacts']
    members = []
    archive = False
    for artifact in artifacts:
        member = {k: artifact[k] for k in ('id', 'description', 'paths', 'pattern', 'count',
                  'format', 'columns', 'json_required_keys', 'row_count', 'min_bytes',
                  'id_column', 'member_id_pattern', 'schema_description') if k in artifact}
        if member.get('paths') != ['{submission}']:
            archive = True
        members.append(member)
    return dict(id='submission', path=submission['path'], required=True,
                purpose='Submission evaluated by the scientific scorer',
                format='zip' if archive else artifacts[0].get('format'),
                fields=members)


def _private_path_pattern(path):
    path = path.rstrip('/')
    name = re.escape(path)
    start = r'(?<![\w.-])'
    end = r'(?![\w-]|\.[\w.-])'
    if '/' not in path and '.' not in path:
        # A bare resource directory can also be a scientific term, e.g. "labels".
        return re.compile(start + rf'(?:(?:\./|/){name}{end}|{name}/)')
    return re.compile(start + name + end)


def _public_experiments(spec, protocol, inputs):
    experiments = protocol.get('assessment', {}).get('scope', {}).get('main_experiments')
    if not isinstance(experiments, dict) or not experiments:
        raise ValueError('scope contract v3 needs an inventory of main experiments')
    public_ids = {item['id'] for item in inputs}
    protected = set(package_layout(spec)['private_paths'])
    protected.update({'/task/evaluation/', '/task/tests/'})
    for source in spec.get('data', {}).get('sources', []):
        if source.get('visibility', 'public') != 'public':
            protected.update(path for path in (source.get('package_path'), source.get('runtime_path'),
                                                source.get('path'), source.get('url')) if isinstance(path,str) and path)
    private_paths = [_private_path_pattern(path) for path in protected if path.rstrip('/')]
    result = {}
    for identifier, row in experiments.items():
        if not isinstance(identifier, str) or not identifier.strip() or not isinstance(row, dict):
            raise ValueError('each main experiment needs a public identifier and specification')
        public = {}
        for field in ('statement', 'population', 'split', 'deliverable', 'metric_unit', 'aggregation'):
            value = row.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f'main experiment {identifier} needs a public {field}')
            public[field] = value
        if any(path.search(text) for path in private_paths for text in (identifier, *public.values())):
            raise ValueError('main experiment public fields contain a private package or resource path')
        resource_ids = row.get('resource_ids')
        if not isinstance(resource_ids, list) or not all(isinstance(rid, str) for rid in resource_ids):
            raise ValueError('main experiment resource_ids must list declared resources')
        result[identifier] = dict(**public, resource_ids=[rid for rid in resource_ids if rid in public_ids])
    return result


def build_contract(package, spec, protocol, task):
    package = Path(package)
    evidence = spec['verification']['score_evidence']
    hooks = [hook for hook in task['verifier']['collect'] if hook['service'] == evidence['service']]
    if len(hooks) != 1:
        raise ValueError('expected one formal evaluator collector')
    submission = spec['submission']
    metric = protocol['reward_metric']
    runtime = spec['runtime']
    output_dir = str(PurePosixPath(submission['path']).parent)
    # Formal scoring runs in the private verifier service after submission.
    # Do not advertise an inaccessible command as a Solver entrypoint.
    evaluation = dict(mode='automatic_private_verifier',
                      metric=metric, direction=protocol['reward_direction'],
                      success_condition=f'Exit code 0, status=success, finite {metric} and finite reward')
    validation = spec['verification'].get('public_validation')
    if isinstance(validation, dict):
        evaluation['public_validation'] = {k: validation[k] for k in (
            'baseline_command', 'command', 'prediction_path', 'input_paths', 'metric', 'direction') if k in validation}
    inputs = _inputs(package, spec)
    experiments = ({'main_experiments': _public_experiments(spec, protocol, inputs)}
                   if spec.get('scope_contract_version') == 3 else {})
    return dict(schema_version=SCHEMA_VERSION, inputs=inputs, **experiments,
                outputs=[_output(submission)], evaluation=evaluation,
                runtime=dict(data_dir=runtime['data_dir'], output_dir=output_dir,
                             DATA_DIR=runtime['data_dir'], OUTPUT_DIR=output_dir,
                             working_dir=SOLVER_WORKDIR, entrypoint='python '+SOLVER_SCRIPT,
                             contract_path='/task/problem/task_contract.json'),
                experiment_log=dict(one_of=['/workspace/experiment_log.jsonl', '/workspace/run_log.md'],
                                    fields=['algorithm', 'rationale', 'parameters', 'inputs', 'command',
                                            'metrics', 'metric_changes', 'change_reason', 'next_plan']))


def contract_errors(contract):
    if not isinstance(contract, dict) or contract.get('schema_version') not in {SCHEMA_VERSION, 'solver-task-contract-v1'}:
        return ['unsupported solver contract schema']
    errors = []
    for key in ('inputs', 'outputs'):
        items = contract.get(key)
        if not isinstance(items, list) or not items:
            errors.append(f'{key} must be a nonempty array')
            continue
        paths, ids = set(), set()
        for item in items:
            if not isinstance(item, dict):
                errors.append(f'{key}: expected an object')
                continue
            for field in ('id', 'path', 'purpose', 'format'):
                if not isinstance(item.get(field), str) or not item[field].strip():
                    errors.append(f'{key}: missing {field}')
            path, identifier = item.get('path'), item.get('id')
            if not isinstance(path, str) or not path.startswith('/') or path in paths:
                errors.append(f'{key}: invalid or duplicate path {path}')
            if not isinstance(identifier, str) or identifier in ids:
                errors.append(f'{key}: invalid or duplicate id {identifier}')
            if not item.get('fields') or type(item.get('required')) is not bool:
                errors.append(f'{key}: {path} needs schema and required/optional status')
            paths.add(str(path))
            ids.add(str(identifier))
    evaluation = contract.get('evaluation')
    if not isinstance(evaluation, dict) or not evaluation.get('metric') or not evaluation.get('success_condition'):
        errors.append('missing evaluator metric or success condition')
    elif contract.get('schema_version') == SCHEMA_VERSION:
        if evaluation.get('mode') != 'automatic_private_verifier':
            errors.append('formal evaluation must identify automatic private verification')
        if any(key in evaluation for key in ('command', 'attempt_index', 'service')):
            errors.append('public contract must omit private evaluator command and evidence paths')
    elif (not isinstance(evaluation.get('command'), list) or not evaluation['command']
          or not all(isinstance(arg, str) and arg for arg in evaluation['command'])):
        errors.append('missing legacy evaluator invocation')
    runtime = contract.get('runtime', {})
    if not isinstance(runtime, dict) or not all(runtime.get(k) for k in ('data_dir', 'output_dir', 'DATA_DIR', 'OUTPUT_DIR', 'working_dir', 'entrypoint')):
        errors.append('missing solver runtime bindings')
    return errors


def binding_errors(package, spec, contract, protocol):
    layout = package_layout(spec)
    task = tomllib.loads(package_path(package, layout['task_config_path']).read_text())
    expected = build_contract(package, spec, protocol, task)
    errors = contract_errors(contract)
    for key in expected:
        if not isinstance(contract, dict) or contract.get(key) != expected[key]:
            errors.append(f'task contract {key} differs from mounted resources or evaluator binding')
    if protocol.get('required_artifacts') != spec['submission'].get('required_artifacts'):
        errors.append('evaluator artifact checks differ from submission declarations')
    return errors


def contract_path(package, spec):
    return package_path(package, package_layout(spec)['public_dir']) / 'task_contract.json'


def write_contract(package, spec, value):
    errors = contract_errors(value)
    if errors:
        raise ValueError('; '.join(errors))
    path = contract_path(package, spec)
    atomic_write_json(path, value)
    return path
