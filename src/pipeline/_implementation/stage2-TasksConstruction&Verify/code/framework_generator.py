"""Generate framework-owned wiring from explicit task declarations, never science."""
from __future__ import annotations
import copy
import json
import math
import os
import re
import hashlib
from pathlib import Path, PurePosixPath
import shlex
import shutil
import tomllib
import yaml
from contract import atomic_write_json, package_layout, package_manifest, package_path, sha256_file, load_execution, save_execution, validate_execution
from evaluation_protocol import bind_submission, config_errors
from instruction_checks import _visible_lines, expected_public_facts, parse_sections, section_heading_line
from task_contract import build_contract, contract_errors, write_contract, submission_location_errors, SOLVER_WORKDIR
from compose_runtime import rename_service, normalize_exec_worker, service_reference_errors, verifier_compose_document

DEFAULT_EVIDENCE_ROOT = '/evaluator_output/score_attempts'
OWNED_VOLUMES = {'pipeline_submission', 'pipeline_score_evidence'}


def _normalize_submission_artifacts(spec):
    """Materialize the unambiguous single-file artifact shorthand.

    Workers frequently provide one ``required_artifacts`` entry with an id,
    format and description while omitting ``paths``.  The submission itself is
    already a fixed file, so ``{submission}`` is the only safe expansion.  A
    multi-artifact declaration remains strict and must name paths or a pattern
    explicitly.
    """
    submission = spec.get('submission') if isinstance(spec, dict) else None
    artifacts = submission.get('required_artifacts') if isinstance(submission, dict) else None
    if not isinstance(artifacts, list) or len(artifacts) != 1:
        return False
    item = artifacts[0]
    if not isinstance(submission.get('path'), str) or not submission['path'].startswith('/'):
        return False
    changed = False
    # A frequent first-pass shorthand is a bare filename.  It is unambiguous
    # when the task declares exactly one direct submission, so expand it into
    # the canonical artifact object before schema validation.  Multi-file
    # declarations remain strict and are left for the author to repair.
    if isinstance(item, str) and item.strip():
        name = Path(item).name or 'submission'
        suffix = Path(name).suffix.lower().lstrip('.')
        artifacts[0] = item = {
            'id': re.sub(r'[^A-Za-z0-9_-]+', '_', Path(name).stem).strip('_') or 'submission',
            'description': f'Direct submission file {name}',
            'format': suffix or 'text',
            'paths': ['{submission}'],
        }
        changed = True
    if not isinstance(item, dict) or 'pattern' in item:
        return changed
    if not isinstance(item.get('id'), str) or not item['id'].strip():
        label = item.get('name') or Path(submission['path']).name
        item['id'] = re.sub(r'[^A-Za-z0-9_-]+', '_', Path(str(label)).stem).strip('_') or 'submission'
        changed = True
    if not isinstance(item.get('description'), str) or not item['description'].strip():
        item['description'] = f"Direct submission file {Path(submission['path']).name}"
        changed = True
    item.pop('name', None)
    item.pop('required', None)
    # Some early drafts put the shorthand on the submission object. Move it
    # into the sole artifact where the framework consumes it.
    submission_paths = submission.pop('paths', None)
    if 'paths' not in item and isinstance(submission_paths, list):
        item['paths'] = submission_paths
        changed = True
    if item.get('paths') != ['{submission}']:
        item['paths'] = ['{submission}']
        changed = True
    if changed:
        return True
    return False


def _normalize_mechanical_execution(package, spec):
    """Repair deterministic authoring omissions without inventing science.

    The model edits a deliberately sparse scaffold.  These fields describe
    mounts, resource identities and ABI syntax, so their safe defaults belong
    to the framework.  Formal bytes, labels and experiment scope remain the
    author's responsibility and are never fabricated here.
    """
    changed = []
    runtime = spec.setdefault('runtime', {})
    # Keep this helper focused on fields already implied by an authored
    # scaffold.  A complete scaffold is filled by _normalize_execution_scaffold
    # below.  This makes the helper safe to use in unit tests and repair tools.
    budget_defaults = {'cpu': 4, 'memory_mb': 8192, 'gpus': 0, 'time_limit_sec': 7200}
    authored_budget = all(key in runtime for key in budget_defaults)
    for key, value in budget_defaults.items():
        current = runtime.get(key)
        if key not in runtime or isinstance(current, bool) or not isinstance(current, type(value)):
            runtime[key] = value
            changed.append(f'runtime.{key} defaulted')
    if not authored_budget:
        for key, value in {
            'data_dir': '/task/problem/data', 'image_policy': 'deferred',
            'network_policy': 'enabled', 'search_policy': 'direct',
        }.items():
            if key not in runtime:
                runtime[key] = value
                changed.append(f'runtime.{key} defaulted')
        network = runtime.get('network')
        if not isinstance(network, dict):
            runtime['network'] = {'inherit_env': True}
            changed.append('runtime.network defaulted')
        elif 'inherit_env' not in network:
            network['inherit_env'] = True
            changed.append('runtime.network.inherit_env defaulted')

    submission = spec.setdefault('submission', {})
    artifacts = submission.get('required_artifacts')
    if isinstance(artifacts, list) and len(artifacts) == 1 and isinstance(artifacts[0], str) and artifacts[0].strip():
        name = Path(artifacts[0]).name or 'submission'
        artifacts[0] = {'id': re.sub(r'[^A-Za-z0-9_-]+', '_', Path(name).stem).strip('_') or 'submission',
                        'description': f'Direct submission file {name}',
                        'format': Path(name).suffix.lower().lstrip('.') or 'text',
                        'paths': ['{submission}']}
        changed.append('submission.required_artifacts string normalized')
    elif not isinstance(artifacts, list) or not artifacts:
        path = submission.get('path', '/workspace/output/submission')
        name = Path(path).name or 'submission'
        submission['required_artifacts'] = [{
            'id': re.sub(r'[^A-Za-z0-9_-]+', '_', Path(name).stem).strip('_') or 'submission',
            'description': f'Direct submission file {name}',
            'format': Path(name).suffix.lower().lstrip('.') or 'text',
            'paths': ['{submission}'],
        }]
        changed.append('submission.required_artifacts defaulted')

    data = spec.setdefault('data', {})
    sources = data.get('sources')
    if not isinstance(sources, list):
        sources = []
        data['sources'] = sources
        changed.append('data.sources defaulted')
    if data.get('mode') not in {'bundled', 'referenced', 'mixed', 'generator', 'no_external_data'}:
        data['mode'] = 'referenced' if sources else 'no_external_data'
        changed.append('data.mode inferred')
    public_data = 'common_task/environment/input/data'
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            continue
        rid = source.get('id')
        if not isinstance(rid, str) or not rid.strip():
            rid = f'resource_{index + 1}'
            source['id'] = rid
            changed.append(f'data.sources[{index}].id defaulted')
        if not isinstance(source.get('name'), str) or not source['name'].strip():
            identity = source.get('identity')
            if isinstance(identity, dict):
                identity = identity.get('name') or identity.get('type')
            source['name'] = str(identity or rid)
            changed.append(f'data.sources[{index}].name defaulted')
        if not any(isinstance(source.get(k), str) and source[k].strip()
                   for k in ('materialization', 'path', 'url', 'acquisition')):
            source['acquisition'] = str(source.get('stage3_preparation') or source.get('description') or rid)
            changed.append(f'data.sources[{index}].acquisition defaulted')
        identity_hint = source.get('identity')
        identity_kind_hint = (str(identity_hint.get('type') or '').lower()
                              if isinstance(identity_hint, dict) else '')
        source_label = str(source.get('id') or source.get('name') or '').lower()
        original_hint = source.get('metadata', {}).get('pipeline_original_package_path') if isinstance(source.get('metadata'), dict) else None
        restored_from_metadata = False
        if isinstance(original_hint, str) and original_hint.startswith('common_task/environment/input/'):
            source['package_path'] = original_hint
            relative = original_hint
            restored_from_metadata = True
            changed.append(f'data.sources[{index}].package_path restored from metadata')
        if identity_kind_hint in {'source_archive', 'code', 'reference_implementation'} or any(token in source_label for token in ('source-code', 'source_code', 'reference-code')):
            identity_kind_hint = 'source_archive'
        relative = source.get('package_path') or source.get('path')
        original_relative = relative
        if isinstance(relative, str) and relative.startswith('/task/problem/data/'):
            relative = public_data + '/' + relative.removeprefix('/task/problem/data/').strip('/')
            source['package_path'] = relative
            source.setdefault('metadata', {})['pipeline_original_package_path'] = original_relative
            changed.append(f'data.sources[{index}].package_path mapped to package')
        elif isinstance(relative, str) and relative.startswith('/'):
            source['package_path'] = public_data + '/' + Path(relative).name
            source.setdefault('metadata', {})['pipeline_original_package_path'] = original_relative
            changed.append(f'data.sources[{index}].package_path mapped to package')
        elif not isinstance(relative, str) or not relative:
            source['package_path'] = public_data + '/' + re.sub(r'[^A-Za-z0-9_.-]+', '_', rid)
            changed.append(f'data.sources[{index}].package_path defaulted')
        elif (not restored_from_metadata
              and identity_kind_hint not in {'source_archive', 'code', 'reference_implementation'}
              and source.get('role') not in {'support', 'reference', 'fixture'}
              and source.get('visibility') not in {'private', 'build_only'}
              and not str(source['package_path']).startswith(public_data + '/')):
            # Public Solver inputs must live under the mounted public input
            # tree.  Preserve the authored basename while moving an older
            # ``data/...`` or ``fixtures/...`` shorthand into that tree.
            old = source['package_path']
            source['package_path'] = public_data + '/' + str(old).strip('/').split('/')[-1]
            source.setdefault('metadata', {})['pipeline_original_package_path'] = old
            changed.append(f'data.sources[{index}].package_path moved under public data')
        if not isinstance(source.get('identity'), str) or not source['identity'].strip():
            identity = source.get('identity')
            if isinstance(identity, dict):
                identity = identity.get('name') or identity.get('type')
            source['identity'] = str(identity or source['name'])
            changed.append(f'data.sources[{index}].identity normalized')
        if not isinstance(source.get('version'), str) or not source['version'].strip():
            source['version'] = 'unknown'
            changed.append(f'data.sources[{index}].version defaulted')
        identity_kind = identity_kind_hint
        # A source archive is authoring/build evidence.  Treating it as a
        # required public dataset makes every generated task demand bytes that
        # belong to the paper bundle.  Keep it available to the builder while
        # excluding it from the Solver's formal input contract.
        if identity_kind in {'source_archive', 'code', 'reference_implementation'}:
            if source.get('role') not in {'support', 'fixture'}:
                source['role'] = 'support'
                changed.append(f'data.sources[{index}].role set to support for source archive')
            source['visibility'] = 'build_only'
            source['required'] = True
            source['origin'] = 'reference_implementation'
        else:
            if source.get('role') not in {'input', 'reference', 'fixture', 'support'}:
                source['role'] = 'input'
                changed.append(f'data.sources[{index}].role defaulted')
            if source.get('visibility') not in {'public', 'private', 'build_only', 'guided'}:
                source['visibility'] = 'public'
                changed.append(f'data.sources[{index}].visibility defaulted')
            if source.get('origin') not in {'observation', 'reference_implementation', 'analytical', 'simulation', 'benchmark_policy', 'fixture'}:
                source['origin'] = 'observation'
                changed.append(f'data.sources[{index}].origin defaulted')
        if not isinstance(source.get('schema_description'), str) or not source['schema_description'].strip():
            source['schema_description'] = str(source.get('description') or source['name'])
            changed.append(f'data.sources[{index}].schema_description defaulted')
        content = source.get('content')
        if not isinstance(content, dict) or content.get('format') not in {'csv', 'tsv', 'json', 'text', 'binary', 'directory', 'npy'}:
            suffix = Path(str(source.get('package_path', ''))).suffix.lower()
            source['content'] = {'format': 'json' if suffix == '.json' else 'directory'}
            changed.append(f'data.sources[{index}].content.format defaulted')
        elif Path(str(source.get('package_path', ''))).suffix.lower() == '.json' and content.get('format') == 'directory':
            source['content'] = dict(content, format='json')
            changed.append(f'data.sources[{index}].content.format corrected to json')
        # Older drafts call the acquisition recipe ``preparation`` and use a
        # shell string.  Convert it to the argv form consumed by the resource
        # contract.  The command itself remains task-authored.
        preparation = source.pop('preparation', None)
        if preparation is not None and not isinstance(source.get('prepare'), dict):
            source['prepare'] = preparation if isinstance(preparation, dict) else {}
            changed.append(f'data.sources[{index}].prepare normalized from preparation')
        availability = source.get('availability')
        if availability == 'bundled':
            source.pop('prepare', None)
        if availability in {'runtime_download', 'preprovided', 'unverified'}:
            prep = source.get('prepare')
            if not isinstance(prep, dict):
                prep = {}
                source['prepare'] = prep
            for key in ('command', 'validate'):
                value = prep.get(key)
                if isinstance(value, str) and value.strip():
                    prep[key] = shlex.split(value)
                    changed.append(f'data.sources[{index}].prepare.{key} split into argv')
            rel_name = Path(source['package_path']).name
            check = ("from pathlib import Path; p=Path('/task/problem/data/') / "
                     + repr(rel_name)
                     + "; assert p.exists() and (any(p.iterdir()) if p.is_dir() else p.stat().st_size > 0)")
            if not isinstance(prep.get('command'), list) or not prep['command']:
                prep['command'] = ['python', '-c', check]
                changed.append(f'data.sources[{index}].prepare.command defaulted')
            if not isinstance(prep.get('validate'), list) or not prep['validate']:
                prep['validate'] = ['python', '-c', check]
                changed.append(f'data.sources[{index}].prepare.validate defaulted')
            prep.setdefault('access', 'Stage3 prepares the complete formal resource on the mounted DATA_DIR')
            prep.setdefault('timeout_sec', 3600)
    return changed


def _normalize_execution_scaffold(spec):
    """Complete pipeline-owned defaults after authoring shorthand repair."""
    runtime = spec.setdefault('runtime', {})
    defaults = {
        'data_dir': '/task/problem/data', 'cpu': 4, 'memory_mb': 8192,
        'gpus': 0, 'time_limit_sec': 7200, 'image_policy': 'deferred',
        'network_policy': 'enabled', 'search_policy': 'direct',
    }
    changed = []
    for key, value in defaults.items():
        if key not in runtime or isinstance(runtime.get(key), bool) or not isinstance(runtime.get(key), type(value)):
            runtime[key] = value
            changed.append(f'runtime.{key} defaulted')
    network = runtime.get('network')
    if not isinstance(network, dict):
        runtime['network'] = {'inherit_env': True}
        changed.append('runtime.network defaulted')
    elif 'inherit_env' not in network:
        network['inherit_env'] = True
        changed.append('runtime.network.inherit_env defaulted')
    estimate = spec.get('resource_estimate')
    if isinstance(estimate, dict):
        if not isinstance(estimate.get('expected_solver_minutes'), int):
            minutes = estimate.get('time_hours')
            estimate['expected_solver_minutes'] = max(30, int(minutes * 60)) if isinstance(minutes, (int, float)) else 120
            changed.append('resource_estimate.expected_solver_minutes inferred')
        if not isinstance(estimate.get('gpu_model'), str) or not estimate['gpu_model'].strip():
            estimate['gpu_model'] = 'CPU or deferred accelerator'
            changed.append('resource_estimate.gpu_model defaulted')
        estimate.setdefault('min_gpu_memory_gb', 0)
        if not isinstance(estimate.get('basis'), str) or not estimate['basis'].strip():
            estimate['basis'] = 'Bounded estimate derived from the paper workload and declared runtime.'
            changed.append('resource_estimate.basis defaulted')
        if not isinstance(estimate.get('assumptions'), list) or not estimate['assumptions']:
            estimate['assumptions'] = ['Formal data and dependencies are prepared by Stage 3.']
            changed.append('resource_estimate.assumptions defaulted')
    return changed


def _normalize_public_support_files(package, spec):
    """Keep public metadata and required authoring files in stable locations."""
    changed = []
    layout = package_layout(spec)
    public = package_path(package, layout['public_dir'])
    public.mkdir(parents=True, exist_ok=True)
    for root_name, relative in (
            ('README.md', layout['readme_path']),
            ('data_description.md', layout['data_description_path']),
            ('data_sources.json', layout['data_sources_path']),
            ('environment_requirements.md', 'common_task/environment/environment_requirements.md'),
            ('requirements.txt', 'common_task/environment/requirements.txt')):
        target = package_path(package, relative)
        source = package / root_name
        if not target.exists() and source.is_file() and source.stat().st_size:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            changed.append(f'{relative} copied from {root_name}')
    sources = spec.get('data', {}).get('sources', [])
    target = package_path(package, layout['data_sources_path'])
    # A bundled task with no declared external resources has no public
    # acquisition manifest to expose.  Avoid creating a new Solver input for
    # analytical fixtures, while referenced/mixed tasks still receive the
    # manifest required by their public contract.
    if (spec.get('data', {}).get('mode') in {'referenced', 'mixed'} and
            (not target.is_file() or not target.read_text(encoding='utf-8').strip()) and isinstance(sources, list)):
        rows = []
        for source in sources:
            if not isinstance(source, dict) or source.get('visibility', 'public') != 'public':
                continue
            rows.append({key: source[key] for key in ('id', 'name', 'package_path', 'availability',
                                                       'identity', 'version', 'schema_description', 'acquisition')
                         if key in source})
        atomic_write_json(target, {'schema_version': 'pipline-data-sources-v1', 'sources': rows})
        changed.append(f'{layout["data_sources_path"]} generated from execution sources')
    instruction_path = package_path(package, layout['instruction_path'])
    if instruction_path.is_file():
        text = instruction_path.read_text(encoding='utf-8')
        rewritten = re.sub(r'(?m)^(##) Inputs\s*$', r'\1 Input', text)
        marker = '<!-- runtime facts -->'
        if marker in rewritten:
            env_start = re.search(r'(?m)^#{1,6}\s+Environment\s*#*\s*$', rewritten)
            marker_start = rewritten.find(marker)
            if env_start:
                next_heading = re.search(r'(?m)^#{1,6}\s+.+$', rewritten[env_start.end():])
                env_end = env_start.end() + next_heading.start() if next_heading else len(rewritten)
                if marker_start >= env_end:
                    env_start = None
            if env_start is None:
                # A misplaced marker commonly follows Time Budget. Move only
                # the marker/table block, preserving authored prose around it.
                block = re.search(r'(?ms)<!-- runtime facts -->\s*\n(?:\|.*\n)+', rewritten[marker_start:])
                if block:
                    absolute_end = marker_start + block.end()
                    rewritten = rewritten[:marker_start] + rewritten[absolute_end:]
                    target = re.search(r'(?m)^#{1,6}\s+Environment\s*#*\s*$', rewritten)
                    if target:
                        insert_at = target.end()
                        rewritten = rewritten[:insert_at] + '\n' + marker + '\n' + rewritten[insert_at:]
        headings = {m.group(1).strip() for m in re.finditer(r'(?m)^#{1,6}\s+(.+?)\s*#*$', rewritten)}
        if 'Constraints' not in headings:
            rewritten += '\n\n## Constraints\n\nUse only the declared inputs and follow the evaluator contract.\n'
        if rewritten != text:
            instruction_path.write_text(rewritten, encoding='utf-8')
            changed.append('instruction headings normalized')
    method = package_path(package, layout['method_path'])
    if not method.exists():
        method.parent.mkdir(parents=True, exist_ok=True)
        method.write_text('Implement the method and evaluation protocol declared by the task author.\n', encoding='utf-8')
        changed.append(f'{layout["method_path"]} initialized')
    return changed


def _normalize_scoring_smoke_config(package, spec):
    """Normalize legacy smoke fixture vocabulary into the evaluator ABI."""
    evaluation = package / 'common_task/evaluation'
    path = evaluation / 'scoring_smoke.json'
    if not path.is_file():
        return []
    try:
        config = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return []
    if not isinstance(config, dict):
        return []
    changed = []
    if config.get('schema_version') != 'pipeline-scoring-smoke-v1':
        config['schema_version'] = 'pipeline-scoring-smoke-v1'
        changed.append('scoring_smoke.schema_version normalized')
    config.setdefault('protocol', 'protocol.json')
    config.setdefault('timeout_sec', 120)
    config.setdefault('weak_valid_applicability', {
        'applicable': True,
        'reason': 'The weak-valid control is a structurally valid submission with a deliberately lower score.'})
    try:
        protocol = json.loads((evaluation / str(config['protocol'])).read_text(encoding='utf-8'))
        requirements = [r for r in protocol.get('assessment', {}).get('requirements', [])
                        if isinstance(r, dict) and isinstance(r.get('id'), str)]
    except (OSError, ValueError, TypeError):
        protocol = {}
        requirements = []
    cases = config.get('cases')
    if not isinstance(cases, list):
        return changed
    fixture_dir = evaluation / 'checks/fixtures'
    fixture_dir.mkdir(parents=True, exist_ok=True)
    for case in cases:
        if not isinstance(case, dict):
            continue
        # Older authoring workers commonly emit ``submission`` as
        # ``{"path": "fixtures/file.csv"}``, while the smoke ABI expects
        # the path string itself.  Normalize this before inspect_config sees
        # the case; otherwise a valid relative fixture is passed to ``_file``
        # as a mapping and is reported as a misleading absolute/relative-path
        # error.  This is a framework vocabulary repair, independent of any
        # task's scientific content.
        submission = case.get('submission')
        if isinstance(submission, dict):
            submission_path = submission.get('path') or submission.get('file')
            if isinstance(submission_path, str) and submission_path.strip():
                case['submission'] = submission_path
                changed.append(f'scoring smoke submission path normalized for {case.get("id", "unknown")}')
        source = case.pop('submission_fixture', None)
        if source is None and isinstance(case.get('submission'), str) and Path(case['submission']).is_absolute():
            source = case['submission']
        if source is not None:
            source_path = Path(str(source))
            if source_path.is_absolute():
                # Common model convention is /workspace/build/fixtures/... .
                source_path = package / str(source_path).removeprefix('/workspace/build/').lstrip('/')
            if source_path.is_file():
                target = fixture_dir / source_path.name
                if source_path.resolve() != target.resolve():
                    shutil.copyfile(source_path, target)
                case['submission'] = str(target.relative_to(evaluation))
                changed.append(f'scoring smoke fixture {case.get("id", "unknown")} moved into evaluation')
        status = case.get('expected_status')
        if status in {'pass', 'partial'}:
            case['expected_status'] = 'success'
        elif status in {'reject', 'invalid'}:
            case['expected_status'] = 'invalid_submission'
        if case.get('kind') == 'invalid':
            for field in ('expected_metrics', 'expected_reward', 'expected_bounds', 'expected_reward_bounds'):
                case.pop(field, None)
            if case.get('rejection_reason') and 'expected_error_contains' not in case:
                case['expected_error_contains'] = case.pop('rejection_reason')
        if not isinstance(case.get('requirement_ids'), list) or not case['requirement_ids']:
            quality = [r['id'] for r in requirements if r.get('role') in {'quality', 'validity'}]
            if quality:
                case['requirement_ids'] = quality
                changed.append(f'scoring smoke case {case.get("id", "unknown")} requirement bindings inferred')
        elif 'expected_bounds' in case and 'reward_bounds' not in case:
            bounds = case.pop('expected_bounds')
            case['reward_bounds'] = bounds
        if (isinstance(case.get('reward_bounds'), dict) and set(case['reward_bounds']) == {'reward'}
                and isinstance(case['reward_bounds']['reward'], dict)):
            case['reward_bounds'] = case['reward_bounds']['reward']
            changed.append(f'scoring smoke case {case.get("id", "unknown")} reward bounds flattened')
        # Expected values are independent scientific controls. Unit metrics
        # and aggregation outputs have distinct namespaces and may differ in
        # both values and units; vocabulary repair must never invent either.
        changed.append(f'scoring smoke case {case.get("id", "unknown")} vocabulary normalized')
    atomic_write_json(path, config)
    return changed


def _normalize_scoring_smoke_binding(package, spec):
    """Fill the default smoke configuration path for an opted-in binding."""
    verification = spec.get('verification') if isinstance(spec, dict) else None
    binding = verification.get('scoring_smoke') if isinstance(verification, dict) else None
    if not isinstance(binding, dict) or binding.get('config_path'):
        return False
    default = package_path(package, package_layout(spec)['score_path']).parent / 'scoring_smoke.json'
    if default.is_file():
        binding['config_path'] = str(default.relative_to(package))
        return True
    return False


def _normalize_scoring_smoke_cases(package, spec):
    """Give authoring controls stable IDs when a draft omitted them.

    Case IDs are framework metadata used to address a control and to bind
    equivalence groups.  They do not affect the submission, expected metric,
    or evaluator code.  Assigning deterministic IDs here repairs a harmless
    authoring omission while retaining the original value in case metadata.
    Duplicate authored IDs are renamed after the first occurrence and all
    equivalence-group references are updated through the same mapping.
    """
    verification = spec.get('verification') if isinstance(spec, dict) else None
    binding = verification.get('scoring_smoke') if isinstance(verification, dict) else None
    if not isinstance(binding, dict):
        return False
    config_path = binding.get('config_path')
    if not isinstance(config_path, str) or not config_path:
        config_path = str(package_layout(spec)['score_path'])
    try:
        config_file = package_path(package, config_path)
    except (TypeError, ValueError):
        return False
    if not config_file.is_file():
        return False
    try:
        config = json.loads(config_file.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return False
    cases = config.get('cases') if isinstance(config, dict) else None
    if not isinstance(cases, list) or not cases:
        return False
    used = set()
    remap = {}
    changed = False
    for index, case in enumerate(cases, 1):
        if not isinstance(case, dict):
            continue
        old = case.get('id')
        if isinstance(old, str) and old.strip() and old not in used:
            used.add(old)
            continue
        kind = case.get('kind') if isinstance(case.get('kind'), str) else 'control'
        base = f'{kind}_{index}'
        candidate = base
        suffix = 2
        while candidate in used:
            candidate = f'{base}_{suffix}'
            suffix += 1
        # A duplicate old ID is ambiguous for an existing equivalence group.
        # Keep group references on the first occurrence instead of silently
        # redirecting every reference to the renamed duplicate.
        case.setdefault('metadata', {})['pipeline_original_id'] = old
        case['id'] = candidate
        used.add(candidate)
        changed = True
    if not changed:
        return False
    groups = config.get('equivalence_groups')
    if isinstance(groups, dict):
        for group in groups.values():
            if not isinstance(group, dict) or not isinstance(group.get('cases'), dict):
                continue
            for label, cid in list(group['cases'].items()):
                if cid in remap:
                    group['cases'][label] = remap[cid]
    config_file.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return True


def _normalize_protocol_aliases(config, spec):
    """Normalize unambiguous vocabulary aliases in a drafted evaluator.

    Older authoring examples used ``unit_id``/``unit_id`` and ``method``
    while the evaluator ABI uses ``id``, ``unit_ids`` and ``operation``.
    These translations preserve the declared metric calculation.  Missing
    unit paths are inferred only for a single unit from the fixed submission
    filename.  A multi-unit protocol remains strict because guessing paths
    would change the scientific task.
    """
    changed = []
    units = config.get('units')
    if isinstance(units, list):
        for index, unit in enumerate(units):
            if not isinstance(unit, dict):
                continue
            if 'id' not in unit and isinstance(unit.get('unit_id'), str) and unit['unit_id']:
                unit['id'] = unit['unit_id']
                changed.append(f'units[{index}].id <- unit_id')
            if 'path' not in unit and len(units) == 1:
                # A direct single-file submission is addressed by the
                # evaluator ABI as ``{submission}``.  Using its basename
                # would incorrectly force a ZIP and make the generated
                # smoke fixtures disagree with the actual collector.
                submission = spec.get('submission', {}).get('path') if isinstance(spec, dict) else None
                artifacts = spec.get('submission', {}).get('required_artifacts', []) if isinstance(spec, dict) else []
                direct = ((not artifacts) or (isinstance(artifacts, list) and len(artifacts) == 1
                          and isinstance(artifacts[0], dict)
                          and artifacts[0].get('paths') == ['{submission}']))
                if isinstance(submission, str) and submission.startswith('/') and direct:
                    unit['path'] = '{submission}'
                    changed.append('units[0].path inferred as {submission} from direct submission')
    aggregations = config.get('aggregations')
    if isinstance(aggregations, list):
        for index, aggregation in enumerate(aggregations):
            if not isinstance(aggregation, dict):
                continue
            if 'operation' not in aggregation and isinstance(aggregation.get('method'), str):
                aggregation['operation'] = aggregation['method']
                changed.append(f'aggregations[{index}].operation <- method')
            if 'output' not in aggregation and isinstance(aggregation.get('aggregation_id'), str) and aggregation['aggregation_id']:
                aggregation['output'] = aggregation['aggregation_id']
                changed.append(f'aggregations[{index}].output <- aggregation_id')
            if 'metric' not in aggregation and len(units or []) == 1:
                metric = units[0].get('metric') if isinstance(units[0], dict) else None
                if isinstance(metric, str) and metric:
                    aggregation['metric'] = metric
                    changed.append(f'aggregations[{index}].metric inferred from the single evaluation unit')
            if 'unit_ids' not in aggregation and isinstance(aggregation.get('unit_id'), str):
                aggregation['unit_ids'] = [aggregation['unit_id']]
                changed.append(f'aggregations[{index}].unit_ids <- unit_id')
            if 'metric' not in aggregation:
                unit_metrics = [unit.get('metric') for unit in units or []
                                if isinstance(unit, dict) and isinstance(unit.get('metric'), str) and unit['metric']]
                if len(set(unit_metrics)) == 1:
                    aggregation['metric'] = unit_metrics[0]
                    changed.append(f'aggregations[{index}].metric inferred from the sole unit metric')
    assessment = config.get('assessment')
    requirements = assessment.get('requirements') if isinstance(assessment, dict) else None
    if isinstance(requirements, list):
        for index, requirement in enumerate(requirements):
            if (isinstance(requirement, dict) and 'id' not in requirement
                    and isinstance(requirement.get('requirement_id'), str)
                    and requirement['requirement_id']):
                requirement['id'] = requirement['requirement_id']
                changed.append(f'assessment.requirements[{index}].id <- requirement_id')
    # A single aggregate named ``mean_ari`` is an unambiguous public reward
    # alias for the unit metric ``ARI``.  Align requirements with the actual
    # evaluator output so the static assessment contract and runtime agree.
    outputs = [row.get('output') for row in aggregations or []
               if isinstance(row, dict) and isinstance(row.get('output'), str)]
    reward = config.get('reward_metric')
    if len(outputs) == 1 and isinstance(reward, str) and reward != outputs[0]:
        unit_metrics = {row.get('metric') for row in units or [] if isinstance(row, dict)}
        if reward in unit_metrics:
            old = reward
            config['reward_metric'] = outputs[0]
            changed.append(f'reward_metric {old!r} -> {outputs[0]!r} (single aggregate alias)')
            if isinstance(requirements, list):
                for requirement in requirements:
                    if isinstance(requirement, dict) and requirement.get('metric') == old:
                        requirement['metric'] = outputs[0]
    return changed


def _normalize_assessment_bindings(config, spec):
    """Fill only one-to-one or all-unit binding omissions in scope metadata."""
    changed = []
    assessment = config.get('assessment') if isinstance(config, dict) else None
    if not isinstance(assessment, dict):
        return changed
    if assessment.get('mode') not in {'reference', 'analytical', 'objective', 'rubric'}:
        sources = spec.get('data', {}).get('sources', []) if isinstance(spec, dict) else []
        assessment['mode'] = 'reference' if any(isinstance(s, dict) and s.get('role') == 'reference' for s in sources) else 'objective'
        changed.append('assessment.mode inferred')
    if not isinstance(assessment.get('basis'), str) or not assessment['basis'].strip():
        assessment['basis'] = 'Declared paper objective and independently computed evaluator metrics.'
        changed.append('assessment.basis defaulted')
    if not isinstance(assessment.get('reference_policy'), str) or not assessment['reference_policy'].strip():
        assessment['reference_policy'] = 'Use only formal inputs and evaluator-owned references declared in this package.'
        changed.append('assessment.reference_policy defaulted')
    if not isinstance(assessment.get('adaptations'), (str, list)) or not assessment['adaptations']:
        assessment['adaptations'] = 'None beyond the fixed Solver-facing file and runtime contract.'
        changed.append('assessment.adaptations defaulted')
    units = [u for u in config.get('units', []) if isinstance(u, dict) and isinstance(u.get('id'), str)]
    unit_ids = [u['id'] for u in units]
    requirements = assessment.get('requirements')
    if not isinstance(requirements, list):
        requirements = []
        assessment['requirements'] = requirements
    outputs = [a.get('output') for a in config.get('aggregations', []) if isinstance(a, dict) and isinstance(a.get('output'), str)]
    reward = config.get('reward_metric') if isinstance(config.get('reward_metric'), str) else (outputs[0] if outputs else '')
    known_artifacts = [a.get('id') for a in config.get('required_artifacts', []) if isinstance(a, dict) and isinstance(a.get('id'), str)]
    for index, req in enumerate(requirements):
        if not isinstance(req, dict):
            continue
        if not isinstance(req.get('id'), str) or not req['id'].strip():
            req['id'] = req.get('requirement_id') or f'requirement_{index + 1}'
            changed.append(f'assessment.requirements[{index}].id defaulted')
        req.setdefault('statement', req.get('description') or f'Declared task requirement {req["id"]}.')
        req.setdefault('source', 'Task contract and frozen paper materials.')
        req.setdefault('verification', 'Checked by the evaluator and contract preflight.')
        if req.get('origin') not in {'paper', 'external', 'derived', 'task_policy'}:
            req['origin'] = 'paper' if req.get('source_evidence') else 'task_policy'
            changed.append(f'assessment.requirements[{index}].origin defaulted')
        if req.get('role') not in {'validity', 'quality', 'diagnostic', 'trajectory'}:
            req['role'] = 'quality' if index == 0 else 'validity'
            changed.append(f'assessment.requirements[{index}].role defaulted')
        if req.get('role') in {'quality', 'diagnostic'} and not isinstance(req.get('metric'), str):
            req['metric'] = reward if req['role'] == 'quality' else (outputs[index] if index < len(outputs) else reward)
            changed.append(f'assessment.requirements[{index}].metric defaulted')
        if 'artifact_ids' not in req:
            req['artifact_ids'] = known_artifacts
            changed.append(f'assessment.requirements[{index}].artifact_ids defaulted')
    scope = assessment.get('scope')
    if not isinstance(scope, dict):
        scope = {}
        assessment['scope'] = scope
    for key, default in {
            'source_basis': 'Frozen primary paper and declared task materials.',
            'paper_scope': 'The principal experiments identified in the paper materials.',
            'task_scope': 'The same experiments represented by the declared evaluator units.',
            'difficulty_impact': 'The Solver contract fixes paths, formats and evaluation.'}.items():
        if not isinstance(scope.get(key), str) or not scope[key].strip():
            scope[key] = default
            changed.append(f'assessment.scope.{key} defaulted')
    formal_sources = [s for s in spec.get('data', {}).get('sources', [])
                      if isinstance(s, dict) and s.get('role') in {'input', 'reference'} and isinstance(s.get('id'), str)]
    coverage = scope.get('data_coverage')
    if not isinstance(coverage, list):
        coverage = []
    converted = []
    for row in coverage:
        if isinstance(row, dict):
            converted.append(row)
        elif isinstance(row, str):
            converted.append({'resource_id': row, 'coverage': 'full', 'source_scope': row,
                              'task_scope': row, 'rationale': 'Declared formal task input.'})
            changed.append('assessment.scope.data_coverage string normalized')
    seen = {row.get('resource_id') for row in converted if isinstance(row, dict)}
    for source in formal_sources:
        if source['id'] not in seen:
            converted.append({'resource_id': source['id'], 'coverage': 'full',
                              'source_scope': source.get('schema_description') or source['id'],
                              'task_scope': source.get('name') or source['id'],
                              'rationale': 'Formal resource declared for the task.'})
            changed.append(f'assessment.scope.data_coverage added {source["id"]}')
    scope['data_coverage'] = converted
    experiments = scope.get('main_experiments')
    if not isinstance(experiments, dict):
        experiments = {}
        scope['main_experiments'] = experiments
    exp_items = list(experiments.items())
    req_ids = [r.get('id') for r in requirements if isinstance(r, dict) and isinstance(r.get('id'), str)]
    resource_ids = [s['id'] for s in formal_sources]
    for index, (_, exp) in enumerate(exp_items):
        if not isinstance(exp, dict):
            continue
        if not isinstance(exp.get('unit_ids'), list) or not exp['unit_ids']:
            selected = ([unit_ids[index]] if len(unit_ids) == len(exp_items) and index < len(unit_ids)
                        else unit_ids)
            if selected:
                exp['unit_ids'] = selected
                changed.append(f'main_experiments[{index}].unit_ids inferred')
        if not isinstance(exp.get('requirement_ids'), list) or not exp['requirement_ids']:
            selected = ([req_ids[index]] if len(req_ids) == len(exp_items) and index < len(req_ids)
                        else req_ids)
            if selected:
                exp['requirement_ids'] = selected
                changed.append(f'main_experiments[{index}].requirement_ids inferred')
        if not isinstance(exp.get('resource_ids'), list) or not exp['resource_ids']:
            selected = ([resource_ids[index]] if len(resource_ids) == len(exp_items) and index < len(resource_ids)
                        else resource_ids)
            if selected:
                exp['resource_ids'] = selected
                changed.append(f'main_experiments[{index}].resource_ids inferred')
    return changed


def _normalize_resource_availability(package, spec):
    """Canonicalize harmless availability aliases before schema validation.

    Authoring models sometimes use ``available`` or ``download`` as prose
    values.  Those are not execution states.  We preserve the original value
    for audit and infer a state only when the package itself provides enough
    evidence.  An absent resource remains deferred/unavailable and therefore
    still fails the resource contract when no preparation recipe exists.
    """
    data = spec.get('data') if isinstance(spec, dict) else None
    sources = data.get('sources') if isinstance(data, dict) else None
    if not isinstance(sources, list):
        return []
    aliases = {
        'available': None,
        'present': None,
        'provided': 'preprovided',
        'download': 'runtime_download',
        'deferred': 'runtime_download',
        'remote': 'runtime_download',
        'missing': 'unavailable',
        'unavailable': 'unavailable',
    }
    changed = []
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            continue
        value = source.get('availability')
        if not isinstance(value, str):
            continue
        key = value.strip().lower().replace('-', '_').replace(' ', '_')
        if key not in aliases:
            continue
        target = aliases[key]
        if target is None:
            # ``available`` is only bundled when the declared package path
            # is actually present.  Otherwise leave it as an explicit
            # unavailable resource so the normal preparation checks reject
            # a task that has no runnable Stage3 recipe.
            package_path = source.get('package_path')
            candidate = None
            if isinstance(package_path, str) and package_path:
                try:
                    candidate = Path(package) / PurePosixPath(package_path)
                except (TypeError, ValueError):
                    candidate = None
            target = 'bundled' if candidate is not None and candidate.exists() else 'unavailable'
        if value != target:
            source.setdefault('metadata', {})['pipeline_original_availability'] = value
            source['availability'] = target
            changed.append(f'data.sources[{index}].availability={target}')
    return changed


def _evaluation_file(package, evaluation_dir, value, *, default_name=None):
    """Resolve an evaluation binding from package- or evaluation-relative form."""
    if value is None and default_name is not None:
        value = default_name
    if not isinstance(value, str) or not value:
        raise ValueError('evaluation binding needs a relative configuration path')
    candidates = []
    try:
        candidates.append(package_path(package, value))
    except ValueError:
        pass
    if not Path(value).is_absolute() and '..' not in PurePosixPath(value).parts:
        candidates.append(evaluation_dir / value)
    for candidate in candidates:
        if candidate.is_file() and candidate.resolve().is_relative_to(evaluation_dir.resolve()):
            return candidate.resolve()
    raise ValueError('missing evaluation configuration inside evaluation directory: ' + value)


def with_declared_tables(instruction, section, slot, tables):
    """Refresh opted-in tables, preserving surrounding prose and literal examples."""
    marker = f'<!-- {slot} -->'
    end = f'<!-- end {slot} -->'
    if marker not in instruction and end not in instruction:
        return instruction
    # Substituting standalone markers in a read-only view lets the existing
    # Markdown parser distinguish real slots from code samples and comments.
    lines = instruction.splitlines(keepends=True)
    tokens = {marker: 'PIPELINE_RUNTIME_FACTS_START', end: 'PIPELINE_RUNTIME_FACTS_END'}
    view = ''.join(tokens[line.strip()] + '\n' if line.strip() in tokens else line for line in lines)
    positions = [(i, line) for i,line in _visible_lines(view)
                 if line in tokens.values() and lines[i].strip() in tokens]
    if not positions:
        return instruction
    names = [line for _,line in positions]
    if names not in ([tokens[marker]], [tokens[marker], tokens[end]]):
        raise ValueError(f'expected one {slot} slot with an optional matching end marker')
    bodies = [body.splitlines() for title,body in parse_sections(view, additional_titles=(section,)) if title == section]
    if not any(all(token in body for token in names) for body in bodies):
        raise ValueError(f'place the {slot} markers in the {section} section')
    start = positions[0][0]
    last = positions[-1][0]
    if len(positions) == 2 and any(line.strip() and not line.strip().startswith('|') for line in lines[start+1:last]):
        raise ValueError(f'{slot} block contains authored prose; move it outside the generated table')
    newline = '\r\n' if lines[start].endswith('\r\n') else '\n'
    table = [marker]
    for header, rows in tables.items():
        if not rows:
            continue
        for cells in (header, ['---'] * len(header), *rows):
            clean = [str(value).replace('|', r'\|') for value in cells]
            if any('\n' in cell or '\r' in cell for cell in clean):
                raise ValueError(f'{slot} table requires single-line values')
            table.append('| ' + ' | '.join(clean) + ' |')
        table.append('')
    if table[-1] == '':
        table.pop()
    table.append(end)
    tail = newline if lines[last].endswith('\n') else ''
    return ''.join(lines[:start]) + newline.join(table) + tail + ''.join(lines[last+1:])


def with_runtime_facts(instruction, spec):
    tables = {('Setting', 'Value'): list(expected_public_facts(spec).items())}
    return with_declared_tables(instruction, 'Environment', 'runtime facts', tables)


def with_output_table(instruction, spec):
    if '<!-- output artifacts -->' not in instruction and '<!-- end output artifacts -->' not in instruction:
        return instruction
    rows = []
    submission = spec['submission']
    for item in submission.get('required_artifacts', []):
        paths = item.get('paths')
        fields = item.get('columns', item.get('json_required_keys', []))
        for name in paths or [item['pattern']]:
            path = submission['path'] if name == '{submission}' else name
            rows.append([path, 1 if paths else item.get('count', 'at least one'), item.get('format','any'),
                         json.dumps(fields, ensure_ascii=False) if fields else '-'])
    return with_declared_tables(instruction, 'Output', 'output artifacts',
                                {('File or pattern', 'Count', 'Format', 'Fields'): rows})


def with_solver_bindings(instruction, contract):
    """Refresh only the framework's public wiring, preserving authored task text."""
    begin = '<!-- BEGIN generated solver bindings -->'
    end = '<!-- END generated solver bindings -->'
    runtime, evaluation = contract['runtime'], contract['evaluation']
    lines = [begin, '**Solver runtime contract**', '',
             f"Read `{runtime['contract_path']}` for the input and output schemas.",
             f"- `DATA_DIR` = `{runtime['DATA_DIR']}`",
             f"- `OUTPUT_DIR` = `{runtime['OUTPUT_DIR']}`",
             f"- Run `{runtime['entrypoint']}` from `{runtime['working_dir']}`."]
    for item in contract['inputs']:
        status = 'required' if item['required'] else 'optional'
        lines.append(f"- `{item['path']}` ({item['format']}, {status}). {item['purpose']}")
    for item in contract['outputs']:
        lines.append(f"- Submit `{item['path']}` ({item['format']}, required).")
    if evaluation.get('public_validation'):
        lines.append('Run `evaluation.public_validation.baseline_command` and then '
                     '`evaluation.public_validation.command` from the contract to obtain a public score.')
    if evaluation.get('mode') == 'automatic_private_verifier':
        lines.append('Automatic private verification runs after submission; the formal scorer is not a Solver command.')
    else:
        lines.append(f"The verifier runs the argv in `evaluation.command` on service `{evaluation['service']}`.")
    lines.extend([f"Success condition: {evaluation['success_condition']}.", end])
    block = '\n'.join(lines)
    existing = None
    if begin in instruction or end in instruction:
        if (instruction.count(begin) != 1 or instruction.count(end) != 1
                or instruction.index(end) < instruction.index(begin)):
            raise ValueError('instruction.md has malformed generated solver binding markers')
        left, _, tail = instruction.partition(begin)
        _, _, right = tail.partition(end)
        if not (left + right).strip():
            raise ValueError('instruction.md has no authored task text')
        # A previously generated package may have the bindings in a footer.
        existing = left + block + right
        instruction = left + re.sub(r'^\r?\n', '', right, count=1)
    if not instruction.strip():
        raise ValueError('instruction.md has no authored task text')
    position = section_heading_line(instruction, 'Evaluation')
    if position is not None:
        source_lines = instruction.splitlines(keepends=True)
        prefix = ''.join(source_lines[:position + 1])
        if not prefix.endswith(('\r', '\n')):
            prefix += '\n'
        return prefix + block + '\n' + ''.join(source_lines[position + 1:])
    if existing is not None:
        return existing
    return instruction + ('\n' if instruction.endswith('\n') else '\n\n') + block + '\n'


def valid_harbor_name(value):
    return isinstance(value, str) and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]*/[a-zA-Z0-9][a-zA-Z0-9._-]*', value) is not None and '..' not in value


def normalize_task_identity(document, task_id):
    """Adapt registry metadata, preserving authored names and scientific text."""
    info = document.setdefault('task', {})
    old = info.get('name')
    if valid_harbor_name(old):
        return False
    if old is not None:
        document.setdefault('metadata', {})['pipeline_original_task_name'] = old
    token = re.sub(r'[^a-zA-Z0-9_-]+', '-', str(task_id)).strip('-')[:64] or 'task'
    suffix = hashlib.sha256(str(task_id).encode()).hexdigest()[:10]
    info['name'] = 'scientific/' + token + '-' + suffix
    return True


def _overlap(left, right):
    left, right = PurePosixPath(left), PurePosixPath(right)
    return left.is_relative_to(right) or right.is_relative_to(left)


def _mount_parts(mount):
    if isinstance(mount, dict):
        return mount.get('source'), mount.get('target')
    if isinstance(mount, str) and ':' in mount:
        return tuple(mount.split(':')[:2])
    raise ValueError('Compose mounts must explicitly declare source and target')


def _literal(value):
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if type(value) is bool:
        return str(value).lower()
    if type(value) in (int,float) and math.isfinite(value):
        return repr(value)
    if isinstance(value, list):
        return '[' + ', '.join(_literal(v) for v in value) + ']'
    if isinstance(value, dict):
        return '{' + ', '.join(_literal(str(k)) + ' = ' + _literal(v) for k,v in value.items()) + '}'
    raise ValueError('Unsupported TOML configuration value')


def toml_text(document):
    """Serialize declarative task config without adding a writer dependency."""
    lines = []
    def section(value, path):
        if path:
            lines.extend(['', '[' + '.'.join(_literal(k) for k in path) + ']'])
        for key,item in value.items():
            if not isinstance(item,dict):
                lines.append(_literal(key) + ' = ' + _literal(item))
        for key,item in value.items():
            if isinstance(item,dict):
                section(item,[*path,key])
    section(document,[])
    text = '\n'.join(lines) + '\n'
    tomllib.loads(text)
    return text


def _write(path, content):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(content,encoding='utf-8')


def _absolute(value, name):
    if not isinstance(value,str) or not value.startswith('/') or '..' in PurePosixPath(value).parts or value == '/':
        raise ValueError(name + ' must be an explicit absolute container path')
    return value


def _plan(package, spec):
    if spec.get('evaluation_protocol_version') != 1:
        raise ValueError('Generated framework requires evaluation_protocol_version=1')
    layout = package_layout(spec)
    evidence = copy.deepcopy(spec.get('verification',{}).get('score_evidence',{}))
    protocol = evidence.get('evaluation_protocol',{})
    config_path = package_path(package,protocol.get('config_path','common_task/evaluation/protocol.json'))
    if spec.get('assessment_contract_version') == 1 and (
            config_path != package / 'common_task/evaluation/protocol.json' or
            protocol.get('config_container', '/task/evaluation/protocol.json') != '/task/evaluation/protocol.json'):
        raise ValueError('In execution.json, verification.score_evidence.evaluation_protocol.config_path must be '
                         'common_task/evaluation/protocol.json and config_container must be /task/evaluation/protocol.json. '
                         'Both fields can be omitted to use these defaults.')
    config = json.loads(config_path.read_text())
    protocol_aliases = _normalize_protocol_aliases(config, spec)
    assessment_bindings = _normalize_assessment_bindings(config, spec)
    from scoring_smoke import authoring_metric_adapter
    authoring_metric_adapter(config_path, config)
    bind_submission(config, spec)
    errors = config_errors(config)
    if config.get('reward_direction') not in {'higher_is_better', 'lower_is_better'}:
        errors.append('protocol.reward_direction must be higher_is_better or lower_is_better')
    if errors:
        raise ValueError('; '.join(errors))
    eval_dir = config_path.parent
    public = package_path(package,layout['public_dir'])
    if eval_dir.is_relative_to(public):
        raise ValueError('Evaluator configuration must remain private')
    protocol_path = eval_dir / 'evaluation_protocol.py'
    if config_path == protocol_path:
        raise ValueError('Framework evaluator must be distinct from task adapter and configuration')
    container_dir = str(PurePosixPath(protocol.get('config_container', '/task/evaluation/' + config_path.name)).parent)
    _absolute(container_dir, 'evaluator directory')
    protocol_container = container_dir + '/evaluation_protocol.py'
    config_container = container_dir + '/' + config_path.name
    compose = package_path(package,layout['compose_file'])
    existing = yaml.safe_load(compose.read_text()) if compose.is_file() else {'services':{}}
    if not isinstance(existing,dict) or not isinstance(existing.get('services',{}),dict):
        raise ValueError('Invalid Compose document')
    services = existing.setdefault('services',{})
    solvers = [s for s in layout['solver_services'] if s in services]
    if len(solvers) > 1:
        raise ValueError('Declare one Solver service')
    solver = solvers[0] if solvers else 'main'
    if solver != 'main':
        if 'main' in services:
            raise ValueError('Conflicting main service')
        rename_service(existing, solver, 'main')
        for check in spec.get('verification',{}).get('commands',{}).values():
            if isinstance(check,dict) and check.get('service') == solver:
                check['service'] = 'main'
    evaluator = evidence.get('service','evaluator')
    if evaluator == 'main':
        raise ValueError('Private evaluator must be a separate service')
    original_evidence_root = evidence.get('artifact_source', DEFAULT_EVIDENCE_ROOT)
    evidence_root = _absolute(original_evidence_root,'score evidence root')
    submission = _absolute(spec['submission']['path'],'submission path')
    output_dir = str(PurePosixPath(submission).parent)
    evidence_dir = str(PurePosixPath(evidence_root).parent)
    protected = [container_dir, '/task/problem', '/task/input', spec['runtime']['data_dir'], output_dir]
    if any(_overlap(evidence_dir, path) for path in protected):
        # Evidence wiring is program-owned. Repair an unsafe old declaration,
        # not the scientific data, scripts, or archived authoring test evidence.
        evidence_root = DEFAULT_EVIDENCE_ROOT
        evidence_dir = str(PurePosixPath(evidence_root).parent)
    if any(_overlap(evidence_dir, path) for path in protected):
        raise ValueError(f'Reserved scoring evidence directory {evidence_dir} overlaps '
                         'a declared code/input/submission directory; choose separate task paths')
    if output_dir == '/' or any(_overlap(output_dir, path) for path in protected[:-1]):
        raise ValueError(f'Submission directory {output_dir} must not overlap evaluator code or input directories. '
                         f'In execution.json, submission.path is the full submitted file path ({submission}). '
                         'Set its parent to a dedicated output directory such as /workspace/output and '
                         'runtime.data_dir to the Solver data mount.')
    runtime = spec['runtime']
    policy = runtime.get('image_policy')
    if policy not in {'deferred','prebuilt'}:
        raise ValueError('Generated framework requires explicit deferred or prebuilt image policy')
    if policy == 'prebuilt' and not runtime.get('base_image'):
        raise ValueError('Prebuilt mode requires an explicitly bound image')
    # Standard mounts are owned by the framework. Preserve task-specific mounts
    # at other targets, then let preflight check their visibility and safety.
    def mount_source(path):
        relative = os.path.relpath(path,compose.parent)
        return relative if relative.startswith('.') else './'+relative
    common_mounts = [dict(type='bind',source=mount_source(public),target=t,read_only=True)
                     for t in ('/task/problem','/task/input')]
    data = package_path(package,layout['data_path'])
    if data.is_dir() and runtime['data_dir'] not in ('/task/problem','/task/input',output_dir,evidence_dir):
        common_mounts.append(dict(type='bind',source=mount_source(data),target=runtime['data_dir'],read_only=True))
    owned = {m['target'] for m in common_mounts} | {output_dir,evidence_dir,container_dir}
    for name, service in services.items():
        if name not in {'main', evaluator}:
            for mount in service.get('volumes', []):
                if _mount_parts(mount)[0] in OWNED_VOLUMES:
                    raise ValueError(f'Service {name} uses a framework-owned volume; '
                                     'submission/evidence volumes are managed only on main/evaluator')
    def configure(name, mounts):
        service = services.setdefault(name,{})
        extra = []
        for v in service.get('volumes',[]):
            source, target = _mount_parts(v)
            # Retire old mounts by owned volume identity, not only current target.
            # Otherwise an old /task/evaluation/checks mount survives migration.
            if source in OWNED_VOLUMES:
                continue
            _absolute(target, 'mount target')
            if target not in owned:
                if _overlap(target, evidence_dir) or _overlap(target, output_dir):
                    raise ValueError(f'Service {name}: task mount {target} overlaps generated '
                                     f'evidence/submission directory {evidence_dir} or {output_dir}; '
                                     'relocate the task-specific mount explicitly')
                extra.append(v)
        service['volumes'] = [*extra,*mounts]
        service.pop('build',None)
        service.pop('cpu_count',None)
        if policy == 'prebuilt':
            service['image'] = runtime['base_image']
        else:
            service.pop('image',None)
        service.setdefault('entrypoint',[])
        service.setdefault('command',['sleep','infinity'])
        normalize_exec_worker(service, name)
        service.update(cpus=runtime['cpu'],mem_limit=str(runtime['memory_mb'])+'m')
    configure('main',[*common_mounts,dict(type='volume',source='pipeline_submission',target=output_dir)])
    main = services['main']
    main['working_dir'] = SOLVER_WORKDIR
    environment = main.get('environment') or {}
    if isinstance(environment, list):
        environment = {entry.partition('=')[0]: entry.partition('=')[2] if '=' in entry else None
                       for entry in environment}
    main['environment'] = {**environment, 'DATA_DIR': runtime['data_dir'], 'OUTPUT_DIR': output_dir}
    configure(evaluator,[*common_mounts,
        dict(type='bind',source=mount_source(eval_dir),target=container_dir,read_only=True),
        dict(type='volume',source='pipeline_submission',target=output_dir,read_only=True),
        dict(type='volume',source='pipeline_score_evidence',target=evidence_dir)])
    from solver_access import configure_compose
    configure_compose(existing, 'main', runtime['network_policy'], solver_only=spec.get('solver_access') == 'online_search')
    reference_errors = service_reference_errors(existing)
    if reference_errors:
        raise ValueError('; '.join(reference_errors))
    existing.setdefault('volumes',{}).update(pipeline_submission={},pipeline_score_evidence={})
    for v in ('pipeline_submission','pipeline_score_evidence'):
        existing['volumes'][v] = {}
    evidence.update(service=evaluator,artifact_source=evidence_root,
        entrypoint=str((eval_dir/'scoring_entry.py').relative_to(package)),
        entrypoint_container=container_dir+'/scoring_entry.py',
        evaluation_protocol=dict(path=str(protocol_path.relative_to(package)),config_path=str(config_path.relative_to(package)),
                                 container_path=protocol_container,config_container=config_container))
    collect = shlex.join(['python',evidence['entrypoint_container'],'--submission',submission,
        '--evidence-root',evidence_root,'--','python',protocol_container,'--config',config_container,
        '--input','{submission}','--output','{output}'])
    task_path = package_path(package,layout['task_config_path'])
    task = tomllib.loads(task_path.read_text()) if task_path.is_file() else {}
    legacy_task = task.pop('common_task',{})
    if not task.get('task') and isinstance(legacy_task,dict):
        task['task'] = {k:v for k,v in legacy_task.items() if k in {'name','description'}}
    normalize_task_identity(task, spec['task_id'])
    task.pop('resources',None)
    task.pop('submission',None)
    task['schema_version'] = '1.4'
    task.setdefault('agent',{})['timeout_sec'] = runtime['time_limit_sec']
    env = task.setdefault('environment',{})
    env.update(cpus=runtime['cpu'],memory_mb=runtime['memory_mb'],gpus=runtime['gpus'],
               build_timeout_sec=runtime.get('build_timeout_sec',0),
               network_mode={'enabled':'public','disabled':'no-network','gateway_search_only':'no-network','allowlisted':'allowlist'}[runtime['network_policy']])
    if runtime['network_policy'] == 'allowlisted':
        env['allowed_hosts'] = runtime['allowed_hosts']
    else:
        env.pop('allowed_hosts',None)
    env.pop('docker_image',None)
    if policy == 'prebuilt':
        env['docker_image'] = runtime['base_image']
    verifier = task.setdefault('verifier',{})
    verifier.pop('script',None)
    verifier['environment_mode'] = 'separate'
    # A fresh verifier has only collected artifacts and /tests; no task mounts.
    verifier['environment'] = {'network_mode':'no-network'}
    if policy == 'prebuilt':
        verifier['environment']['docker_image'] = runtime['base_image']
    hooks = verifier.get('collect',[])
    if not isinstance(hooks,list):
        raise ValueError('verifier.collect must be an array')
    verifier['collect'] = [h for h in hooks if h.get('service') != evaluator] + [{'service':evaluator,'command':collect,'timeout_sec':360}]
    if solver != 'main':
        for hook in verifier['collect']:
            if hook.get('service') == solver:
                hook['service'] = 'main'
    artifacts_config = task.get('artifacts',[])
    if not isinstance(artifacts_config,list):
        raise ValueError('task artifacts must be an array')
    task['artifacts'] = [a for a in artifacts_config if not (
        isinstance(a,dict) and a.get('service') == evaluator
        and a.get('source') in {evidence_root, original_evidence_root})]
    task['artifacts'].append({'source':evidence_root,'service':evaluator})
    if solver != 'main':
        for artifact in task['artifacts']:
            if isinstance(artifact,dict) and artifact.get('service') == solver:
                artifact['service'] = 'main'
    spec.setdefault('verification',{})['score_evidence'] = evidence
    spec.setdefault('framework',{}).update(solver_services=['main'],evaluator_services=[evaluator],
        score_path=str(protocol_path.relative_to(package)))
    return layout,config_path,config,eval_dir,protocol_path,compose,existing,task_path,task


# Fingerprints of the former generated copies; arbitrary task code is never
# deleted merely because it has a familiar filename.
RETIRED_HASHES = {
    'evaluate.py': 'e3c62b0a7a3672a34968533e1b907464f866ce000ff3390b66b9d0970a3a88e1',
    'submission_artifacts.py': 'f09d52cd2aa64533c36d9072ac7c7c772ffed60b7fb1f0528342b4f9fd12dedc',
    'score_attempt.py': '197a0dd530cc2a119db24b3dd3ca957b6338c5e6c1524944f527c13f170e90df',
}


def _retired_copies(package, eval_dir, tools, capture):
    retired = {}
    for path, replacement in (
        (eval_dir / 'evaluate.py', eval_dir / 'evaluation_protocol.py'),
        (eval_dir / 'submission_artifacts.py', eval_dir / 'evaluation_protocol.py'),
        (package / 'common_task/tests/score_attempt.py', package / 'common_task/tests/score_verifier.py'),
    ):
        if not path.exists():
            continue
        allowed = {RETIRED_HASHES[path.name]}
        if path.name == 'evaluate.py':
            allowed.add(sha256_file(tools / 'evaluation_protocol.py'))
        if path.name == 'score_attempt.py':
            allowed.add(sha256_file(capture))
        if not path.is_file() or sha256_file(path) not in allowed:
            raise ValueError('Obsolete filename contains task-specific or modified code; relocate it explicitly: ' + str(path.relative_to(package)))
        retired[str(path.relative_to(package))] = str(replacement.relative_to(package))
    return retired


def _calibration_bindings(package, spec, eval_dir):
    verification = spec.setdefault('verification', {})
    bindings = verification.get('calibration')
    if bindings is None:
        bindings = ([{'config_path': str((eval_dir / 'calibration.json').relative_to(package))}]
                    if (eval_dir / 'calibration.json').is_file() else [])
    if not isinstance(bindings, list):
        raise ValueError('verification.calibration must be an array')
    evidence = verification['score_evidence']
    container_dir = str(PurePosixPath(evidence['evaluation_protocol']['config_container']).parent)
    commands = verification.setdefault('commands', {})
    resolved, seen = [], set()
    for index, binding in enumerate(bindings):
        if not isinstance(binding, dict):
            raise ValueError('calibration binding must be an object')
        config = package_path(package, binding.get('config_path'))
        if not config.is_file() or not config.resolve().is_relative_to(eval_dir.resolve()):
            raise ValueError('calibration configuration must be a real file inside evaluation')
        name = binding.get('runtime_check', 'scientific_calibration' if len(bindings) == 1 else f'scientific_calibration_{index + 1}')
        if not isinstance(name, str) or not name.startswith('scientific_') or name in seen:
            raise ValueError('calibration runtime checks need distinct scientific_ names')
        seen.add(name)
        item = dict(config_path=str(config.relative_to(package)),
                    guard_path=str((eval_dir / 'calibration_guard.py').relative_to(package)),
                    config_container=container_dir + '/' + config.relative_to(eval_dir).as_posix(),
                    guard_container=container_dir + '/calibration_guard.py', runtime_check=name)
        argv = ['python', item['guard_container'], '--config', item['config_container']]
        commands[name] = {'service': evidence['service'], 'command': argv}
        resolved.append(item)
    if resolved or 'calibration' in verification:
        verification['calibration'] = resolved
    return bool(resolved)


def _scoring_smoke_binding(package, spec, eval_dir):
    verification = spec.setdefault('verification', {})
    binding = verification.get('scoring_smoke')
    if binding is None and (eval_dir / 'scoring_smoke.json').is_file():
        binding = {'config_path': str((eval_dir / 'scoring_smoke.json').relative_to(package))}
    if binding is None:
        return False
    if not isinstance(binding, dict):
        raise ValueError('verification.scoring_smoke must be an object')
    config = _evaluation_file(package, eval_dir, binding.get('config_path'),
                              default_name='scoring_smoke.json')
    from scoring_smoke import inspect_config
    _, protocol = inspect_config(config, require_rejection_reasons=spec.get('assessment_contract_version') == 1)
    evidence = verification['score_evidence']
    actual = package_path(package, evidence['evaluation_protocol']['config_path'])
    if protocol.resolve() != actual.resolve() or config.parent.resolve() != eval_dir.resolve():
        raise ValueError('scoring smoke configuration must be beside and use the actual evaluator protocol')
    container = str(PurePosixPath(evidence['evaluation_protocol']['config_container']).parent)
    binding = {'config_path': str(config.relative_to(package)),
               'runner_path': str((eval_dir / 'scoring_smoke.py').relative_to(package)),
               'config_container': container + '/' + config.name,
               'runner_container': container + '/scoring_smoke.py',
               'runtime_check': 'scientific_scoring_smoke'}
    verification['scoring_smoke'] = binding
    verification.setdefault('commands', {})[binding['runtime_check']] = {
        'service': evidence['service'],
        'command': ['python', binding['runner_container'], '--config', binding['config_container']]}
    return True


def generate(package: Path, report_path: Path | None = None, *, enabled: bool = False):
    """Only mutate newly collected, opted-in packages; leave incomplete drafts repairable."""
    package = package.resolve()
    before = package_manifest(package)
    report = {'schema_version':'pipeline-framework-generation-v1','status':'failed','changes':[],
              'errors':[],'scope':'framework wiring and declared artifact structure; not scientific certification'}
    try:
        spec = load_execution(package)
        if spec.get('framework_generation_version') != 1 and not enabled:
            report['status'] = 'not_requested'
            return report
        spec = copy.deepcopy(spec)
        spec["framework_generation_version"] = 1
        for change in _normalize_mechanical_execution(package, spec):
            report['changes'].append(change)
        if _normalize_submission_artifacts(spec):
            report['changes'].append('submission.required_artifacts[0].paths defaulted to ["{submission}"] for the declared single-file submission')
        if _normalize_scoring_smoke_binding(package, spec):
            report['changes'].append('verification.scoring_smoke.config_path defaulted to common_task/evaluation/scoring_smoke.json')
        if _normalize_scoring_smoke_cases(package, spec):
            report['changes'].append('scoring smoke controls received deterministic unique IDs and equivalence bindings were updated')
        for change in _normalize_resource_availability(package, spec):
            report['changes'].append(change)
        for change in _normalize_public_support_files(package, spec):
            report['changes'].append(change)
        for change in _normalize_scoring_smoke_config(package, spec):
            report['changes'].append(change)
        # Persist only these deterministic ABI repairs before validation.  If
        # scientific scope is still incomplete, the next review worker sees a
        # stable mechanical scaffold instead of repeating the same repairs.
        save_execution(package, spec)
        errors = validate_execution(spec)
        if errors:
            raise ValueError('; '.join(errors))
        location_errors = submission_location_errors(package, spec)
        if location_errors:
            raise ValueError(' '.join(location_errors))
        old_evidence_root = spec.get('verification', {}).get('score_evidence', {}).get('artifact_source', DEFAULT_EVIDENCE_ROOT)
        layout,config_path,config,eval_dir,protocol_path,compose,document,task_path,task = _plan(package,spec)
        contract = build_contract(package, spec, config, task)
        errors = contract_errors(contract)
        if errors:
            raise ValueError('; '.join(errors))
        instruction_path = package_path(package, layout['instruction_path'])
        instruction = instruction_path.read_bytes().decode('utf-8')
        solver_instruction = with_output_table(with_runtime_facts(instruction, spec), spec)
        if '<!-- assessment tables -->' in solver_instruction or '<!-- end assessment tables -->' in solver_instruction:
            from assessment_contract import public_tables
            solver_instruction = with_declared_tables(solver_instruction, 'Evaluation', 'assessment tables',
                                                      public_tables(config))
        solver_instruction = with_solver_bindings(solver_instruction, contract)
        readme_path = package_path(package, layout['readme_path'])
        readme = readme_path.read_bytes().decode('utf-8') if readme_path.is_file() else ''
        solver_readme = with_declared_tables(readme, 'Submission', 'submission path',
                                             {('Setting', 'Value'): [('Submission path', spec['submission']['path'])]})
        if spec.get('assessment_contract_version') == 1:
            from resource_contract import source_errors, bind_protocol_resources
            failures = source_errors(package, spec, construction_only=True)
            if failures: raise ValueError('; '.join(failures))
            container_dir = str(PurePosixPath(spec['verification']['score_evidence']['evaluation_protocol']['config_container']).parent)
            bind_protocol_resources(config, spec, str(eval_dir.relative_to(package)), container_dir)
            spec['verification'].setdefault('commands', {})['resource_readiness'] = {
                'service': spec['verification']['score_evidence']['service'],
                'command': ['python', container_dir + '/resource_contract.py', '--config',
                            container_dir + '/' + config_path.name, '--receipt',
                            str(PurePosixPath(spec['verification']['score_evidence']['artifact_source']).parent / 'resource_readiness.json')]}
        # Complete validation/serialization before writing any planned output.
        compose_text,task_text = yaml.safe_dump(document,sort_keys=False),toml_text(task)
        verifier_compose_path = package/'common_task/tests/docker-compose.yaml'
        previous_verifier = yaml.safe_load(verifier_compose_path.read_text()) if verifier_compose_path.exists() else None
        verifier_compose_text = yaml.safe_dump(verifier_compose_document(previous_verifier), sort_keys=False)
        tools = Path(__file__).resolve().parent
        capture = tools.parents[1]/'stage3-TasksRun&Verify/code/score_attempt.py'
        if not capture.is_file():
            capture = tools/'score_attempt.py'
        retired = _retired_copies(package, eval_dir, tools, capture)
        has_calibration = _calibration_bindings(package, spec, eval_dir)
        has_scoring_smoke = _scoring_smoke_binding(package, spec, eval_dir)
        copies = {eval_dir/'scoring_entry.py':tools/'scoring_entry.py',
                  protocol_path:tools/'evaluation_protocol.py',
                  eval_dir/'score_attempt.py':capture,
                  package/'common_task/tests/score_verifier.py':tools/'score_verifier.py'}
        if spec.get('assessment_contract_version') == 1:
            copies[eval_dir/'resource_contract.py'] = tools/'resource_contract.py'
        if has_calibration:
            copies[eval_dir/'calibration_guard.py'] = tools/'calibration_guard.py'
        if has_scoring_smoke:
            copies[eval_dir/'scoring_smoke.py'] = tools/'scoring_smoke.py'
        contract_path = write_contract(package, spec, contract)
        if solver_instruction != instruction:
            _write(instruction_path, solver_instruction)
        if solver_readme != readme:
            _write(readme_path, solver_readme)
        for dest,source in copies.items():
            dest.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source,dest)
        from execution_contract import verifier_script
        _write(package/'common_task/tests/test.sh',verifier_script(spec['verification']['score_evidence']['artifact_source']))
        (package/'common_task/tests/test.sh').chmod(0o755)
        for relative in retired:
            package_path(package, relative).unlink()
        atomic_write_json(config_path,config)
        _write(compose,compose_text)
        _write(task_path,task_text)
        _write(verifier_compose_path,verifier_compose_text)
        save_execution(package, spec)
        new_evidence_root = spec['verification']['score_evidence']['artifact_source']
        if old_evidence_root != new_evidence_root:
            report['wiring_adjustments'] = [{'field': 'verification.score_evidence.artifact_source',
                'before': old_evidence_root, 'after': new_evidence_root,
                'reason': 'Separate writable scoring records from code, inputs and submissions; archived authoring evidence is unchanged'}]
        # Refresh only framework-owned file hashes; never manufacture scientific
        # findings or change user-authored requirements/control statuses.
        review_path = package/'review_report.json'
        if review_path.is_file():
            review = json.loads(review_path.read_text())
            owned = {str(p.relative_to(package)):sha256_file(p) for p in [*copies,config_path,compose,task_path,verifier_compose_path,contract_path]}
            for row in review.get('scientific_requirements',[]):
                for binding in row.get('implementation',[]):
                    if binding.get('path') in retired:
                        binding['path'] = retired[binding['path']]
                    if binding.get('path') in owned:
                        binding['sha256'] = owned[binding['path']]
            atomic_write_json(review_path,review)
        report['status'] = 'generated'
    except (OSError,ValueError,TypeError,KeyError,AttributeError,yaml.YAMLError) as error:
        report['errors'].append(str(error))
    finally:
        after = package_manifest(package)
        report['changes'] = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
        if report_path is not None:
            atomic_write_json(report_path,report)
    return report
