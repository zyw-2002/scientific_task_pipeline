#!/usr/bin/env python3
"""Construct from source materials, check rules, review science, and hand off the task."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import importlib.util
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import zipfile

from worker_runtime import (atomic_json, read_json, copy_tree, snapshot, file_sha256,
                            make_role_task, verify_role_task, execute_role, timeout_seconds)

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline_runtime import setting
from review_resume import construction_inputs, prior_construction

STAGE = Path(__file__).resolve().parents[1]
STAGE1_CODE = STAGE.parent / 'stage1-datacard&downlond/code'
DEFAULT_ISSUES = STAGE.parent / 'analysis_tractory/v1/latest/judge/issues.jsonl'
_FROZEN_PROGRAM_SHA256 = None


def resource_preparation_retry(report: dict, construct_attempt: int, limit: int) -> bool:
    """Give missing-resource designs one bounded preparation repair opportunity.

    Local blob availability cannot establish whether an authorized download or
    a synchronized operator mount can be declared and independently validated.
    This permits another authoring attempt, never construction acceptance.
    """
    return (report.get('contract_status') == 'BLOCKED' and construct_attempt == 0
            and bool(report.get('missing_resources')) and (limit == 0 or limit > 1))


def _default_prompt(role: str) -> Path:
    """Use the current fast prompt, with a read-only snapshot compatibility fallback."""
    name = '01_construct_fast.md' if role == 'construct' else '02_construct_verify_fast.md'
    current = STAGE / 'prompt' / name
    if current.is_file():
        return current
    legacy = STAGE / 'prompt' / ('01_construct.md' if role == 'construct' else '02_construct_verify.md')
    return legacy


def _verify_frozen_program():
    """Check the saved program before every role and host acceptance boundary."""
    from run_snapshot import MANIFEST, reference
    global _FROZEN_PROGRAM_SHA256
    if (STAGE.parent / MANIFEST).is_file():
        result = reference(STAGE.parent, _FROZEN_PROGRAM_SHA256)
        _FROZEN_PROGRAM_SHA256 = result['snapshot_sha256']
        return result
    return None


def _read_context(value):
    """Host execution metadata only; never a source of task science or runtime."""
    context = copy.deepcopy(value) if isinstance(value, dict) else read_json(value)
    _validate_context(context)
    return context


def _check_output_separate(output: Path, sources: Path, draft: Path | None = None) -> None:
    output = output.resolve()
    for label, path in (('source materials', sources), ('draft', draft)):
        if path is not None:
            source = path.resolve()
            if output == source or output.is_relative_to(source) or source.is_relative_to(output):
                raise ValueError(f'output must be separate from {label}')


def _prepare_output(path: Path):
    if path.exists():
        raise ValueError(f'refusing to overwrite {path}; select a new revision/output directory')
    (path / 'environment/input').mkdir(parents=True)


def _tools(input_root: Path, config: dict):
    _verify_frozen_program()
    target = input_root / 'pipeline_tools'
    target.mkdir()
    for name in ('compose_runtime.py', 'solver_access.py', 'review_bindings.py', 'paper_comparison.py', 'paper_caption_audit.py', 'catalog_bindings.py', 'fixed_task_alignment.py', 'solver_output_audit.py', 'construction_audit.py', 'assessment_contract.py', 'difficulty_contract.py', 'resource_contract.py', 'public_validation.py', 'authoring_package.py', 'review_report.py', 'review_report.schema.json', 'scoring_smoke.py', 'contract.py', 'task_contract.py', 'preflight.py', 'construction_checks.py', 'instruction_checks.py', 'execution.schema.json', 'scientific_acceptance.py', 'calibration_guard.py', 'scoring_entry.py', 'execution_contract.py', 'artifact_language.py', 'score_verifier.py', 'evaluation_protocol.py', 'framework_generator.py'):
        shutil.copy2(STAGE / 'code' / name, target / name)
    # Evaluators can snapshot exactly what they score without importing pipeline
    # or scientific code into the judge process.
    capture_tool = STAGE.parent / 'stage3-TasksRun&Verify/code/score_attempt.py'
    shutil.copy2(capture_tool, target / 'score_attempt.py')
    for source, name in (('multimodal_data.py', 'prepare_multimodal_data.py'),
                         ('prepare_public_data.py', 'prepare_public_data.py'),
                         ('network_config.py', 'network_config.py')):
        shutil.copy2(STAGE1_CODE / source, target / name)
    reference = STAGE / 'reference/trajectory_regressions.json'
    if reference.is_file():
        # The host inventory includes individual historical trials and reviewer evidence.
        # Workers only need the reusable requirements, including during independent review.
        fields = ('check_id', 'stage', 'enforcement', 'requirement')
        checks = [{key: row[key] for key in fields} for row in read_json(reference)['checks']]
        atomic_json(input_root / reference.name, {'checks': checks})


def _prepare_multimodal_sources(input_root: Path, paths: dict, config: dict) -> None:
    """Private reading previews; the worker's decoder does not define Solver resources."""
    files = input_root / 'source_bundle/files'
    policy = config.get('multimodal', {})
    if not files.is_dir() or policy.get('enabled') is False:
        return
    overview = files / '.qwen/DATA_OVERVIEW.json'
    if not overview.is_file():
        module_spec = importlib.util.spec_from_file_location('pipeline_source_multimodal', STAGE1_CODE / 'multimodal_data.py')
        module = importlib.util.module_from_spec(module_spec)
        sys.modules[module_spec.name] = module
        module_spec.loader.exec_module(module)
        module.prepare_multimodal_data(files, files / '.qwen',
                                      model_capability=policy.get('model_capability', 'text'))
    paths['multimodal_overview'] = '/task/input/source_bundle/files/.qwen/DATA_OVERVIEW.md'
    paths['multimodal_report'] = '/task/input/source_bundle/files/.qwen/DATA_OVERVIEW.json'


def _source_inventory(input_root, paths):
    """Index actual files from the frozen source manifest; never unpack an archive on the host."""
    source = input_root / 'source_bundle'
    if (source / 'fixed_task_instruction.md').is_file():
        paths['fixed_task_instruction'] = '/task/input/source_bundle/fixed_task_instruction.md'
    manifest = source / 'materials_manifest.json'
    if not manifest.is_file():
        return
    files = {}
    docs = source/'fixed_task_documents'
    if docs.is_dir():
        for doc in sorted(docs.rglob('*.md')):
            if doc.name in {'README.md', 'data_description.md'} and doc.resolve() == doc and doc.is_file():
                name = doc.relative_to(source).as_posix()
                files[name] = {'path': name, 'kind': ['fixed_task_document'], 'sha256': file_sha256(doc),
                               'bytes': doc.stat().st_size}
    material = read_json(manifest)
    for item in material.get('artifacts', []):
        name = item.get('snapshot_path')
        if item.get('status') != 'available' or not isinstance(name, str) or not name:
            continue
        path = source / name
        if not path.resolve().is_relative_to(source.resolve()) or not path.is_file():
            continue
        record = files.setdefault(name, {'path': name, 'kind': [], 'sha256': item.get('sha256'),
                                         'bytes': path.stat().st_size})
        kind = item.get('kind')
        if kind not in record['kind']:
            record['kind'].append(kind)
        if 'zip_members' in record or not zipfile.is_zipfile(path):
            continue
        try:
            with zipfile.ZipFile(path) as archive:
                members = [m for m in archive.infolist() if not m.is_dir()]
                record['zip_member_count'] = len(members)
                record['zip_members'] = [{'path': m.filename, 'bytes': m.file_size} for m in members[:2048]]
        except (OSError, ValueError, zipfile.BadZipFile):
            record['archive_index_status'] = 'unreadable'
    if not files:
        return
    inventory = {'files': list(files.values()), 'archive_listing_limit': 2048, 'source_root': paths['source_bundle']}
    from paper_caption_audit import captions
    paper_captions = captions(source, material)
    if paper_captions:
        inventory['paper_captions'] = paper_captions
    atomic_json(input_root / 'source_inventory.json', inventory)
    paths['source_inventory'] = '/task/input/source_inventory.json'


def prepare_build(sources: Path, metadata: dict, output: Path, config: dict,
                  draft: Path | None = None, feedback: Path | None = None) -> Path:
    _verify_frozen_program()
    context = _read_context(metadata)
    _check_output_separate(output, sources, draft)
    intake = sources / 'materials_manifest.json'
    if intake.is_file() and context.get('canonical_id') != read_json(intake).get('canonical_id'):
        raise ValueError('task canonical_id does not match the selected source bundle')
    _prepare_output(output)
    input_root = output / 'environment/input'
    copy_tree(sources, input_root / 'source_bundle')
    _tools(input_root, config)
    paths = {'source_bundle': '/task/input/source_bundle',
             'execution_schema': '/task/input/pipeline_tools/execution.schema.json'}
    _source_inventory(input_root, paths)
    _prepare_multimodal_sources(input_root, paths, config)
    if draft is not None:
        copy_tree(draft, input_root / 'draft')
        paths['draft'] = '/task/input/draft'
    if feedback is not None:
        shutil.copy2(feedback, input_root / 'preflight_report.json')
        paths['preflight'] = '/task/input/preflight_report.json'
        # Keep the scientific review legible even when the full preflight spans many pages.
        try:
            report = read_json(input_root / 'preflight_report.json')
        except (ValueError, OSError):
            report = {}
        notes = []
        if isinstance(report, dict):
            comments = report.get('operator_scientific_feedback')
            if isinstance(comments, str) and comments.strip():
                notes.append(comments)
            checks = report.get('checks')
            priority = ('FRAMEWORK-GENERATION', 'AUTHORING-SCORING-EVIDENCE', 'RESOURCE-PLAN', 'PAPER-SOURCE-CLAIMS', 'DIFFICULTY-ALIGNMENT', 'TASK-SCOPE',
                        'FIXED-TASK-ALIGNMENT', 'ASSESSMENT-DEFINITION', 'SCORING-SMOKE-CONFIG')
            failed = {check.get('id'): check['reason'] for check in (checks if isinstance(checks, list) else [])
                      if isinstance(check, dict) and check.get('status') == 'fail'
                      and isinstance(check.get('id'), str) and isinstance(check.get('reason'), str)
                      and check['reason'].strip()}
            seen = set()
            for check_id in priority:
                reason = failed.get(check_id)
                if reason and reason not in seen:
                    notes.append(f"**{check_id}**\n\n{reason}")
                    seen.add(reason)
        if notes:
            (input_root / 'scientific_repair_feedback.md').write_text('\n\n'.join(notes), encoding='utf-8')
            paths['host_validation_feedback'] = ['/task/input/scientific_repair_feedback.md']
    return make_role_task(output, 'construct', paths,
                          Path(config.get('prompt01', _default_prompt('construct'))).read_text(),
                          'build', config, metadata=context)


def collect_build(task: Path, artifact: Path, output: Path, *, report_path: Path | None = None) -> Path:
    _verify_frozen_program()
    state = verify_role_task(task)
    request = state['metadata']
    if state['role'] != 'construct':
        raise ValueError('not a construct worker')
    if not artifact.is_dir() or not any(artifact.iterdir()):
        raise ValueError('01 produced no build artifact; inspect worker logs')
    copy_tree(artifact, output)
    review = finalize_draft(output, request, task / 'environment/input/source_bundle')
    report_path = report_path or output.parent / 'reports/construct/preflight_report.json'
    atomic_json(report_path, review)
    return output


def finalize_draft(output, request, source_bundle):
    """Generate framework wiring and inspect a host-captured construction draft."""
    from authoring_package import bind_framework_fields
    bind_framework_fields(output, request)
    # Generate once, then inspect the finished wiring once. The same report is
    # used by the workflow and external collect-build callers.
    generation = None
    if request.get('framework_generation_version') == 1:
        from framework_generator import generate
        generation = generate(output, enabled=True)
    from preflight import inspect_construction
    return inspect_construction(output, expected_request=request, generation=generation,
                                source_bundle=source_bundle, require_scoring_evidence=True)


def write_package_diff(before: Path, after: Path, output: Path) -> dict:
    """Record actual changes, separately from reviewer/generated reports."""
    from contract import package_manifest, package_fingerprint
    old, new = package_manifest(before), package_manifest(after)
    report_names = {'build_report.json', 'review_report.json', 'preflight_report.json', 'render_report.json', 'review_report.md', 'fixed_task_alignment.json'}

    def differences(report_files):
        left = {name:sha for name,sha in old.items() if (name in report_names) == report_files}
        right = {name:sha for name,sha in new.items() if (name in report_names) == report_files}
        common = left.keys() & right.keys()
        return {'added':sorted(right.keys()-left.keys()), 'removed':sorted(left.keys()-right.keys()),
                'modified':sorted(name for name in common if left[name] != right[name]),
                'unchanged_count':sum(left[name] == right[name] for name in common)}

    task_files, reports = differences(False), differences(True)
    result = {'schema_version':'pipeline-package-diff-v1',
              'before':str(before.resolve()), 'after':str(after.resolve()),
              'before_package_sha256':package_fingerprint(before),
              'after_package_sha256':package_fingerprint(after),
              'task_files_changed':any(task_files[name] for name in ('added','removed','modified')),
              'task_files':task_files, 'report_files':reports,
              'note':'File differences do not prove correctness. No task-file change may still permit retrying a transient environment failure.'}
    atomic_json(output, result)
    return result


def inspect_draft(package: Path, expected_request: dict | None = None, source_bundle: Path | None = None,
                  *, require_scoring_evidence: bool = True) -> dict:
    from preflight import inspect_construction
    report = inspect_construction(package, expected_request=expected_request, source_bundle=source_bundle,
                                  require_scoring_evidence=require_scoring_evidence)
    for check in report['checks']:
        print(f"{check['id']}: {check['status']}", flush=True)
    return report


def prepare_verify(draft: Path, sources: Path, metadata: dict, output: Path,
                   config: dict, feedback: Path | None = None, *, review_retry: bool = False) -> Path:
    _verify_frozen_program()
    context = _read_context(metadata)
    _check_output_separate(output, sources, draft)
    initial_report = inspect_draft(draft, context, sources, require_scoring_evidence=not review_retry)
    scoring_failure = next((c for c in initial_report['checks']
                            if c['id'] == 'AUTHORING-SCORING-EVIDENCE' and c['status'] == 'fail'), None)
    if scoring_failure:
        raise ValueError('01 scoring evidence requires repair before scientific review. ' + scoring_failure['reason'])
    if feedback is None and initial_report['status'] != 'pass':
        raise ValueError('construction rules must pass before scientific review; repair the draft first')
    _prepare_output(output)
    input_root = output / 'environment/input'
    copy_tree(draft, input_root / 'draft')
    copy_tree(sources, input_root / 'source_bundle')
    _tools(input_root, config)
    paths = {'draft': '/task/input/draft', 'source_bundle': '/task/input/source_bundle',
             'preflight': '/task/input/preflight_report.json',
             'execution_schema': '/task/input/pipeline_tools/execution.schema.json',
             'construction_instructions': '/task/input/construction_instructions.md',
             'review_report_schema': '/task/input/pipeline_tools/review_report.schema.json'}
    _source_inventory(input_root, paths)
    _prepare_multimodal_sources(input_root, paths, config)
    atomic_json(input_root / 'preflight_report.json', initial_report)
    if feedback:
        shutil.copy2(feedback, input_root / 'runtime_feedback.json')
        paths['runtime_feedback'] = '/task/input/runtime_feedback.json'
        feedback_data = read_json(feedback)
        for name, section in (('review_validation.json', 'review_validation'),
                              ('framework_generation.json', 'framework_generation')):
            embedded = feedback_data.get(section) if isinstance(feedback_data, dict) else None
            if embedded is not None:
                atomic_json(input_root / ('host_' + name), embedded)
            else:
                candidate = feedback.parent / name
                if not candidate.is_file():
                    candidate = feedback.parent.parent / name
                if not candidate.is_file():
                    continue
                shutil.copy2(candidate, input_root / ('host_' + name))
            paths.setdefault('host_validation_feedback', []).append('/task/input/host_' + name)
    shutil.copy2(Path(config.get('prompt01', _default_prompt('construct'))),
                 input_root / 'construction_instructions.md')
    return make_role_task(output, 'construct_verify', paths,
                          Path(config.get('prompt02', _default_prompt('verify'))).read_text(),
                          'verified', config, metadata=context)


def _validate_context(context: dict) -> None:
    import re
    allowed = {'task_id', 'canonical_id', 'scientific_acceptance_version', 'assessment_contract_version', 'scope_contract_version', 'difficulty_contract_version', 'paper_caption_review_version', 'paper_comparison_version', 'solver_access', 'output_language',
               'evaluation_protocol_version', 'framework_generation_version',
               'compact_output', 'construction_only'}
    unknown = context.keys() - allowed
    if unknown:
        raise ValueError('host context cannot predefine task content or runtime: ' + ', '.join(sorted(unknown)))
    if 'task_id' in context and (not isinstance(context['task_id'], str) or
            not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', context['task_id'])):
        raise ValueError('task_id must be a safe task identifier, separate from canonical_id')



# Re-export for existing callers; worker and host share this exact implementation.
from review_report import review_errors, validate_review


def finalize(package: Path, reports: Path, runtime_evidence: Path | None = None,
             expected_request: dict | None = None, construction_only: bool = False,
             previous_package: Path | None = None, source_bundle: Path | None = None) -> dict:
    _verify_frozen_program()
    from contract import package_fingerprint, execution_fingerprint, validate_execution
    from preflight import inspect_package
    if reports.resolve().is_relative_to(package.resolve()):
        raise ValueError('reports directory must be outside package')
    reports.mkdir(parents=True, exist_ok=True)
    errors = []
    try:
        spec = read_json(package / 'execution.json')
    except (OSError, ValueError) as exc:
        spec = {}
        errors.append(str(exc))
    configuration_errors = validate_execution(spec)
    errors.extend(configuration_errors)
    if expected_request is not None:
        from preflight import request_constraint_errors
        errors.extend(request_constraint_errors(spec, expected_request))
    if spec.get('framework_generation_version') == 1:
        try:
            generated = read_json(reports/'framework_generation.json')
            if generated.get('status') != 'generated':
                errors.extend(generated.get('errors') or ['Framework generation did not complete'])
        except (OSError,ValueError) as exc:
            errors.append('Missing framework generation evidence: '+str(exc))
    try:
        review = read_json(package / 'review_report.json')
        errors.extend(validate_review(package, spec, review, required=bool(
            (expected_request or {}).get('scientific_acceptance_version'))))
        if not isinstance(review, dict):
            review = {}
        if spec.get('assessment_contract_version') == 1:
            from assessment_contract import history_errors
            errors.extend(history_errors(previous_package, review))
    except (OSError, ValueError) as exc:
        review = {}
        errors.append(str(exc))
    evidence = read_json(runtime_evidence) if runtime_evidence else None
    preflight = inspect_package(package, spec, runtime_evidence=evidence, construction_only=construction_only)
    if source_bundle is not None or spec.get('scope_contract_version') == 3:
        from paper_comparison import source_claim_errors
        try:
            errors.extend(source_claim_errors(package, spec, source_bundle, review=True))
            fixed = Path(source_bundle) / 'fixed_task_instruction.md' if source_bundle is not None else None
            if fixed is not None and spec.get('solver_access'):
                from solver_access import access_disclosure_errors
                disclosure = access_disclosure_errors(package, spec, fixed)
                preflight['checks'].append({'id': 'SOLVER-ACCESS-DISCLOSURE',
                                            'status': 'fail' if disclosure else 'pass',
                                            'reason': '; '.join(disclosure) or 'Public Solver access checked'})
                errors.extend(disclosure)
            if fixed and fixed.is_file():
                from fixed_task_alignment import alignment_errors
                alignment = alignment_errors(package, spec, fixed, construction_only=construction_only)
                preflight['checks'].append({'id': 'FIXED-TASK-ALIGNMENT', 'status': 'fail' if alignment else 'pass',
                                            'reason': '; '.join(alignment) or 'Frozen requirements and package evidence align'})
                errors.extend(alignment)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            errors.append('PAPER-SOURCE-CLAIMS: ' + str(exc))
    from preflight import buildtasks_gate
    buildtasks_gate(preflight, errors, review_status=review.get('status', 'blocked'),
                    require_runtime=not construction_only)
    atomic_json(reports / 'final_preflight.json', preflight)
    atomic_json(reports / 'review_validation.json', {'errors': errors})
    construction_ready = (preflight['status'] == 'pass' and preflight['static_pass']
                          and review.get('status') == 'complete' and not errors)
    execution_ready = preflight['run_ready'] and construction_ready

    def reference(path):
        return {'path': str(path.resolve()), 'sha256': file_sha256(path)} if path.is_file() else None

    receipt = {'schema_version': 'pipeline-readiness-v2', 'run_ready': bool(execution_ready),
               **({'fixed_task_alignment': reference(package / 'fixed_task_alignment.json')}
                  if source_bundle is not None and (Path(source_bundle) / 'fixed_task_instruction.md').is_file() else {}),
               'deferred_resources': preflight.get('deferred_resources', []),
               'contract_status': preflight['buildtasks']['contract_status'],
               'readiness_scope': 'task_package_static' if construction_only else 'execution',
               'construction_ready': bool(construction_ready), 'execution_ready': bool(execution_ready),
               'package_sha256': package_fingerprint(package), 'execution_sha256': execution_fingerprint(spec),
               'review_status': review.get('status', 'blocked'),
               'preflight': reference(reports / 'final_preflight.json'),
               'review': reference(package / 'review_report.json'),
               'runtime_evidence': reference(runtime_evidence) if runtime_evidence else None,
               'errors': errors,
               'created_at': datetime.now(timezone.utc).isoformat()}
    atomic_json(package / 'readiness.json', receipt)
    return receipt


def collect_verify(task: Path, artifact: Path, output: Path, reports: Path,
                   runtime_evidence: Path | None = None) -> dict:
    state = verify_role_task(task)
    request = state['metadata']
    if state['role'] != 'construct_verify':
        raise ValueError('not a 02 verify worker')
    copy_tree(artifact, output)
    from authoring_package import bind_framework_fields
    bind_framework_fields(output, request)
    if request.get('framework_generation_version') == 1:
        from framework_generator import generate
        generate(output, reports/'framework_generation.json', enabled=True)
    construction_only = request.get('construction_only') is True and runtime_evidence is None
    return finalize(output, reports, runtime_evidence, expected_request=request, construction_only=construction_only,
                    previous_package=task / 'environment/input/draft', source_bundle=task / 'environment/input/source_bundle')


def resolve_config(args) -> dict:
    if getattr(args, 'run_snapshot', None):
        from run_snapshot import verify
        verify(args.run_snapshot)
        frozen_config = read_json(args.run_snapshot / 'worker_config.json')
        if getattr(args, 'worker_config', None) and read_json(args.worker_config) != frozen_config:
            raise ValueError('worker config differs from the Stage2 snapshot')
        # CLI overrides would make workers diverge from the batch configuration.
        options = argparse.ArgumentParser(add_help=False)
        add_configuration_args(options)
        overrides = [a.dest for a in options._actions if a.dest not in {'task_id', 'worker_config'}
                     and getattr(args, a.dest, a.default) != a.default]
        if overrides:
            raise ValueError('--run-snapshot uses frozen worker settings; remove overrides: ' + ', '.join(overrides))
        return _validate_worker_config(frozen_config)
    config = {'backend': 'harbor', 'harbor': 'harbor', 'agent': 'qwen-coder',
              'base_image': 'naturebench-base:v3', 'cpus': 4, 'memory_mb': 16384, 'gpus': 0,
              'timeout_sec': 7200, 'build_timeout_sec': 7200, 'network_mode': 'public',
              'prompt01': str(_default_prompt('construct')),
              'prompt02': str(_default_prompt('verify'))}
    if getattr(args, 'worker_config', None):
        supplied=read_json(args.worker_config)
        config.update(supplied)
    mapping = {'backend': 'backend', 'harbor': 'harbor', 'agent': 'agent', 'model': 'model',
               'construct_model': 'construct_model', 'verify_model': 'verify_model',
               'endpoint': 'endpoint', 'api_key_env': 'api_key_env', 'env_file': 'env_file',
               'base_image': 'base_image', 'worker_cpus': 'cpus', 'worker_memory_mb': 'memory_mb',
               'worker_gpus': 'gpus', 'worker_timeout_sec': 'timeout_sec',
               'worker_build_timeout_sec': 'build_timeout_sec', 'worker_network_mode': 'network_mode',
               'worker_allowed_host': 'allowed_hosts', 'prompt01': 'prompt01', 'prompt02': 'prompt02'}
    for prefix in ('construct', 'verify'):
        for key in ('endpoint', 'api_key_env', 'env_file', 'timeout_sec'):
            mapping[f'{prefix}_{key}'] = f'{prefix}_{key}'
    for flag, key in mapping.items():
        value = getattr(args, flag, None)
        if value is not None:
            config[key] = str(value.resolve()) if isinstance(value, Path) else value
    for scope, key in (('worker', 'network'), ('worker_build', 'build_network')):
        _network_overrides(args, scope, config, key)
    for argument, field in (('multimodal_enabled', 'enabled'), ('model_capability', 'model_capability')):
        value = getattr(args, argument, None)
        if value is not None:
            config.setdefault('multimodal', {})[field] = value
    return _validate_worker_config(config)


def _validate_worker_config(config):
    """Keep the same validation for live bootstrap and frozen execution."""
    for key in ('prompt01', 'prompt02', 'env_file', 'construct_env_file', 'verify_env_file'):
        if config.get(key):
            config[key] = str(Path(config[key]).resolve())
            if not Path(config[key]).is_file():
                raise ValueError(f'{key} file not found: {config[key]}')
    for key in ('api_key', 'password', 'authorization', 'token'):
        if key in config:
            raise ValueError(f'worker config must reference credentials via api_key_env/env_file, not literal {key}')
    for key in ('timeout_sec', 'build_timeout_sec', 'construct_timeout_sec', 'verify_timeout_sec'):
        if key in config:
            timeout_seconds(config[key], key)
    from urllib.parse import urlsplit
    for key in ('endpoint', 'construct_endpoint', 'verify_endpoint'):
        if config.get(key):
            endpoint = urlsplit(config[key])
            if endpoint.scheme not in {'http', 'https'} or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
                raise ValueError(f'{key} must be HTTP(S) without credentials/query')
    # A Harbor authoring worker must be able to reach its model endpoint.  A
    # public Docker network with ``network.enabled=false`` still starts a
    # container, but strips every proxy environment variable.  When the model
    # endpoint is external this used to leave the worker waiting until its
    # 30–90 minute timeout with no recorded agent turn.  Reject that frozen
    # configuration before starting a batch so callers can supply
    # ``--proxy-url`` (or an explicit worker network configuration).
    if config.get('backend', 'harbor') == 'harbor':
        model_endpoint = (config.get('construct_endpoint') or config.get('verify_endpoint')
                          or config.get('endpoint'))
        if isinstance(model_endpoint, str) and model_endpoint:
            parsed = urlsplit(model_endpoint)
            network = config.get('network')
            if (parsed.scheme in {'http', 'https'} and parsed.hostname
                    and isinstance(network, dict) and network.get('enabled') is False
                    and config.get('network_mode', 'public') in {'public', 'allowlist'}):
                raise ValueError(
                    'authoring model endpoint requires worker network access, but '
                    'network.enabled=false. Pass --proxy-url/--worker-proxy-url '
                    'or provide an enabled worker network before starting Stage2')
    from worker_runtime import resolve_network, network_for_policy
    network_for_policy(config.get('network'), config.get('network_mode', 'public'))
    resolve_network(config.get('build_network', config.get('network')))
    return config


def _network_overrides(args, prefix: str, target: dict, key: str):
    override = {}
    for flag in ('proxy_url', 'proxy_env_file', 'no_proxy', 'inherit_env', 'probe_urls'):
        value = getattr(args, prefix + '_' + flag, None)
        if value is not None:
            override[flag] = str(value.resolve()) if isinstance(value, Path) else value
    if getattr(args, prefix + '_direct', False):
        override = {'enabled': False}
        target[key] = override
    elif override:
        target[key] = {**target.get(key, {}), **override}


def resolve_paths(args) -> dict:
    from authoring_package import read_paths, validate_paths
    paths = read_paths(args.paths) if getattr(args, 'paths', None) else {}
    for field, argument in (('source_bundle', 'sources'), ('output_dir', 'output')):
        supplied = getattr(args, argument, None)
        if supplied is not None:
            value = str(supplied.resolve())
            if field in paths and Path(paths[field]).resolve() != Path(value):
                raise ValueError(f'{argument} conflicts with paths.{field}')
            paths[field] = value
        if field not in paths:
            raise ValueError(f'provide --paths or --{argument}')
        setattr(args, argument, Path(paths[field]).resolve())
    draft = getattr(args, 'draft', None)
    if draft is not None:
        if paths.get('draft') and Path(paths['draft']).resolve() != draft.resolve():
            raise ValueError('draft conflicts with paths.draft')
        paths['draft'] = str(draft.resolve())
    validate_paths(paths)
    _check_output_separate(args.output, args.sources, Path(paths['draft']) if paths.get('draft') else None)
    return paths


def resolve_context(args) -> dict:
    """Derive host IDs from the material index; scientific choices are authored later."""
    resolve_paths(args)
    manifest = args.sources / 'materials_manifest.json'
    canonical_id = read_json(manifest).get('canonical_id') if manifest.is_file() else None
    task_id = getattr(args, 'task_id', None)
    if not task_id:
        identity = canonical_id or str(args.sources.resolve())
        task_id = 'task-' + hashlib.sha256(identity.encode()).hexdigest()[:20]
    from authoring_package import DEFAULT_FRAMEWORK_FIELDS
    context = {**DEFAULT_FRAMEWORK_FIELDS, 'task_id': task_id}
    if canonical_id:
        context['canonical_id'] = canonical_id
    # schema_version belongs to execution config, not execution metadata.
    context.pop('schema_version', None)
    _validate_context(context)
    return context


def role_config(config: dict, role: str) -> dict:
    result = copy.deepcopy(config)
    prefix = 'construct' if role == 'construct' else 'verify'
    for key in ('model', 'endpoint', 'api_key_env', 'env_file', 'timeout_sec'):
        if prefix + '_' + key in config:
            result[key] = config[prefix + '_' + key]
    return result


def _run_signature(args, config: dict, request: dict) -> str:
    prompts = {'prompt01': _default_prompt('construct'),
               'prompt02': _default_prompt('verify')}
    paths = resolve_paths(args)
    continuation = {key: snapshot(Path(paths[key])) if key == 'draft' else file_sha256(Path(paths[key]))
                    for key in ('draft', 'preflight') if paths.get(key)}
    if getattr(args, 'review_resume_build', None):
        continuation['review_resume_build'] = args.review_resume_build
    value = {'config': config, 'context': request, 'sources': snapshot(args.sources),
             'run_snapshot': _verify_frozen_program(),
             'paths': paths, 'continuation': continuation,
             'prompts': {key: file_sha256(Path(config.get(key, default))) for key, default in prompts.items()},
             'execution': {key: getattr(args, key) for key in ('max_construct_attempts', 'max_verify_attempts', 'probe_runtime', 'docker', 'probe_timeout')}}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _save_state(root: Path, state: dict) -> None:
    if state.get('output_layout') == 'compact':
        state['run_ready'] = state.get('execution_ready', False)
        atomic_json(root/'metadata/build.json',state)
        return
    atomic_json(root / 'run_manifest.json', state)
    atomic_json(root / 'result.json', {
        'schema_version': 'pipeline-stage2-result-v1', 'task_id': state.get('task_id'),
        'canonical_id': state.get('canonical_id'), 'status': state['status'],
        'run_ready': state.get('execution_ready', False), 'package': state.get('package'),
        'contract_status': state.get('contract_status'),
        'construction_ready': state.get('construction_ready', False),
        'readiness_scope': state.get('readiness_scope', 'execution'),
        'execution_ready': state.get('execution_ready', False),
        'run_snapshot': state.get('run_snapshot'),
        'links': {'manifest': str(root / 'run_manifest.json'),
                  'configuration': str(root / 'resolved_config.json'),
                  'paths': str(root / 'paths.json'),
                  '01_construct': str(root / 'roles/01_construct'),
                  '01_draft': str(root / 'draft'),
                  '02_attempts': [row for row in state['stages'] if row['stage'] == '02_construct_verify']},
        'error': state.get('error')})


def workflow(args) -> dict:
    program = _verify_frozen_program()
    if getattr(args, 'run_snapshot', None):
        from run_snapshot import verify
        frozen_options = verify(args.run_snapshot)['run_options']
        if program is None or args.run_snapshot.resolve() != STAGE.parent:
            raise ValueError('run the frozen pipeline.py in a fresh process')
        changed = [key for key, value in frozen_options.items() if getattr(args, key, None) != value]
        if changed:
            raise ValueError('run options differ from the Stage2 snapshot: ' + ', '.join(changed))
    paths = resolve_paths(args)
    config, request = resolve_config(args), resolve_context(args)
    if args.max_construct_attempts < 0:
        raise ValueError('max-construct-attempts must be nonnegative')
    construction_only = getattr(args, 'construction_only', False)
    compact = getattr(args,'output_layout','legacy') == 'compact'
    if compact:
        request['compact_output'] = True
    if construction_only and args.probe_runtime:
        raise ValueError('construction-only conflicts with probe-runtime')
    if construction_only:
        request['construction_only'] = True
    root = args.output.resolve()
    signature = _run_signature(args, config, request)
    if root.exists():
        if not getattr(args, 'resume', False):
            raise ValueError(f'run output already exists: {root}; use a new build id or --resume for matching completed results')
        state = read_json(root / ('metadata/build.json' if compact else 'run_manifest.json'))
        if state.get('input_sha256') != signature or state.get('status') != 'ready':
            raise ValueError('resume requires an immutable matching completed ready build; select a new build id for changes or failed builds')
        from contract import package_fingerprint, execution_fingerprint
        from preflight import inspect_package
        package = Path(state['package'])
        receipt, spec = read_json(package / 'readiness.json'), read_json(package / 'execution.json')
        proof = receipt.get('runtime_evidence') or {}
        readiness_key = 'construction_ready' if construction_only else 'execution_ready'
        if receipt.get(readiness_key, receipt.get('run_ready')) is not True:
            raise ValueError('completed readiness receipt is not ready')
        if receipt.get('contract_status', 'PASS') != 'PASS':
            raise ValueError('completed readiness receipt did not pass its task contract')
        if receipt.get('package_sha256') != package_fingerprint(package) or receipt.get('execution_sha256') != execution_fingerprint(spec):
            raise ValueError('completed package changed; refusing resume')
        from contract import read_reference
        if construction_only:
            read_reference(receipt['preflight'],package)
            if not inspect_package(package, spec, construction_only=True)['static_pass']:
                raise ValueError('completed static package no longer verifies')
        else:
            evidence=read_reference(proof,package)
            if not inspect_package(package, spec, runtime_evidence=evidence)['run_ready']:
                raise ValueError('completed runtime proof no longer verifies')
        return state
    root.mkdir(parents=True)
    context_file = request
    if not compact:
        atomic_json(root / 'resolved_config.json', config)
        atomic_json(root / 'paths.json', paths)
        atomic_json(root / 'execution_context.json', request)
    state = {'schema_version': 'pipeline-stage2-run-v3', 'status': 'running', 'stages': [],
             'task_id': request.get('task_id'), 'canonical_id': request.get('canonical_id'),
             'input_sha256': signature}
    if program:
        state['run_snapshot'] = program
    if compact:
        state['output_layout']='compact'
        state['input']=str(args.paths.resolve()) if getattr(args, 'paths', None) else paths
        state['execution_context']=request
        used=('model','agent','endpoint','base_image','cpus','memory_mb','gpus','timeout_sec','build_timeout_sec')
        state['workers']={role:{k:v for k,v in role_config(config,role).items() if k in used}
                          for role in ('construct','construct_verify')}
        state['role_inputs']={}
    _save_state(root, state)
    temporary=tempfile.TemporaryDirectory(prefix='pipeline-authoring-') if compact else None
    preserve_temporary = False
    working=Path(temporary.name) if temporary else root
    from build_progress import BuildProgress
    progress = BuildProgress(root, state, lambda: _save_state(root, state),
                             construct_limit=args.max_construct_attempts,
                             review_limit=args.max_verify_attempts, construction_only=construction_only)
    try:
        build_config = role_config(config, 'construct')
        verify_config = role_config(config, 'construct_verify')
        draft = Path(paths['draft']) if paths.get('draft') else None
        construction_feedback = Path(paths['preflight']) if paths.get('preflight') else None
        construct_attempt = 0
        worker_recoveries = 0
        if compact:
            from compact_outputs import role_input, logs, checks
        while True:
            progress.start('construct_prepare', construct_attempt + 1, args.max_construct_attempts)
            tag = '01_construct' if construct_attempt == 0 else f'01_construct_{construct_attempt:02d}'
            log_tag = '01' if construct_attempt == 0 else f'01_{construct_attempt:02d}'
            build_task = prepare_build(args.sources, context_file, working / 'roles' / tag,
                                       build_config, draft, construction_feedback)
            resume = getattr(args, 'review_resume_build', None)
            if resume and construct_attempt == 0 and draft and construction_feedback:
                origin = prior_construction(resume, draft, construction_feedback, build_task, request)
                if origin:
                    verify_role_task(build_task)
                    candidate = root/'replayed_01'
                    copy_tree(draft, candidate)
                    from contract import package_fingerprint, execution_fingerprint
                    if (package_fingerprint(candidate) != origin['package_sha256']
                            or execution_fingerprint(read_json(candidate/'execution.json')) != origin['execution_sha256']):
                        raise ValueError('previous construction changed while the verified copy was being made')
                    report = finalize_draft(candidate, request, build_task/'environment/input/source_bundle')
                    report_dir = root/'metadata' if compact else root/'reports/construct'
                    replay = report_dir/'replayed_01_preflight.json'
                    atomic_json(replay, report)
                    state['construction_replay'] = dict(origin, preflight=str(replay), status=report['status'])
                    _save_state(root, state)
                    if report['status'] == 'pass' and report['static_pass'] and report['contract_status'] == 'PASS':
                        draft = candidate
                        state['package'] = str(candidate)
                        state['stages'].append({'stage': '01_reused', 'package': str(candidate), 'checks': str(replay),
                                                'static_pass': True, 'contract_status': 'PASS', 'origin': origin})
                        progress.finish()
                        _save_state(root, state)
                        break
                    draft, construction_feedback = candidate, replay
                    build_task = prepare_build(args.sources, context_file, working / 'roles' / (tag + '_repair'),
                                               build_config, draft, construction_feedback)
            if compact:
                state['role_inputs'][log_tag] = role_input(build_task, root, log_tag)
                _save_state(root, state)
            job_dir = working / 'jobs' / log_tag if compact else root / 'jobs'
            progress.finish()
            progress.start('construct_worker', construct_attempt + 1, args.max_construct_attempts)
            failure = None
            try:
                workspace = execute_role(build_task, job_dir, build_config)
            except Exception as exc:
                from compact_outputs import worker_failure
                failure_path, failure = worker_failure(job_dir, root, log_tag, 'build', exc)
                state.setdefault('worker_failures', []).append(str(failure_path))
                if failure['copy_errors'] and temporary is not None:
                    preserve_temporary = True
                    state['retained_worker_directory'] = str(working)
                try:
                    verify_role_task(build_task)
                    failure['frozen_inputs_intact'] = True
                except (OSError, ValueError):
                    failure['frozen_inputs_intact'] = False
                # Unlimited rule-repair rounds must not mean unlimited crash retries.
                can_retry = (failure['retryable'] and failure['frozen_inputs_intact'] and
                             (construct_attempt + 1 < args.max_construct_attempts
                              if args.max_construct_attempts else worker_recoveries < 1))
                failure['retry_scheduled'] = can_retry
                atomic_json(failure_path, failure)
                progress.finish('fail', failure_code=failure['code'], retry_scheduled=can_retry)
                if not can_retry:
                    raise ValueError(f"{failure['code']}; saved diagnostics: {failure_path}; log: {failure['log']}") from exc
            finally:
                if compact:
                    logs(job_dir, root, log_tag)
            if failure is not None:
                if failure['code'] in {'environment_start_failed', 'container_runtime_lost'} and not failure['packages']:
                    # No worker started. Keep the frozen inputs and preceding draft for a fresh trial.
                    worker_recoveries += 1
                    construct_attempt += 1
                    continue
                # Reuse only a unique safely collected package, never pass a failed
                # worker directly into 02 even if its static files happen to pass.
                draft = Path(failure['packages'][0])
                from preflight import inspect_construction
                report = inspect_construction(draft)
                report.update(status='fail', static_pass=False, run_ready=False,
                              action='repair_in_01', failure_class='worker_failure',
                              worker_failure={'code': failure['code'],
                                  'instruction': 'Continue from the saved draft. Repair root causes before rerunning checks; '
                                                 'a stopped worker has not completed construction.'})
                report['checks'].insert(0, {'id': 'AUTHORING-WORKER', 'status': 'fail',
                    'reason': f"Previous worker stopped ({failure['code']}); finish the saved draft."})
                construction_feedback = root / 'metadata' / f'worker_feedback_{log_tag}.json'
                atomic_json(construction_feedback, report)
                worker_recoveries += 1
                construct_attempt += 1
                continue
            progress.finish('completed')
            progress.start('construct_gate', construct_attempt + 1, args.max_construct_attempts)
            target = root / 'draft' if construct_attempt == 0 else root / 'constructed' / f'{construct_attempt:02d}'
            report_dir = root / 'metadata' if compact else root / 'reports' / 'construct'
            construction_feedback = report_dir / f'preflight_{construct_attempt:02d}.json'
            draft = collect_build(build_task, workspace / 'build', target, report_path=construction_feedback)
            report = read_json(construction_feedback)
            triage = report.get('contract_status', 'PASS' if report['static_pass'] else 'REWORK')
            retry_missing_resources = resource_preparation_retry(report, construct_attempt, args.max_construct_attempts)
            if retry_missing_resources:
                report['repair_resource_evidence'] = (
                    'Inspect the source inventory and paper resource statements. Missing local bytes do not imply '
                    'that acquisition is impossible. Repair invalid preparation declarations, identify an authorized '
                    'download or operator mount, and bind a runnable preparation plus independent content validation. '
                    'Keep source locators private and use representative fixtures only for authoring controls. '
                    'Do not invent data, references or checkpoints, substitute another task, or claim formal execution. '
                    'Keep BLOCKED if no lawful, verifiable preparation path can be established.')
                atomic_json(construction_feedback, report)
            state['stages'].append({'stage': '01_construct', 'attempt': construct_attempt,
                                    'package': str(draft), 'checks': str(construction_feedback),
                                    'checks_sha256': file_sha256(construction_feedback),
                                    **construction_inputs(read_json(build_task/'worker_snapshot.json')),
                                    'static_pass': report['static_pass'], 'contract_status': triage})
            state['package'] = str(draft)
            progress.finish(report['status'], failed_checks=sum(c['status'] != 'pass' for c in report['checks']))
            _save_state(root, state)
            if triage == 'BLOCKED':
                if retry_missing_resources:
                    construct_attempt += 1
                    continue
                state.update(status='blocked', contract_status='BLOCKED', construction_ready=False,
                             execution_ready=False, error=f'01 found missing required resources; see {construction_feedback}')
                _save_state(root, state)
                progress.complete()
                return state
            if report['status'] == 'pass' and triage == 'PASS':
                break
            construct_attempt += 1
            if args.max_construct_attempts and construct_attempt >= args.max_construct_attempts:
                state.update(status='blocked', contract_status='REWORK', construction_ready=False,
                             execution_ready=False, error='01 construction rules still fail; 02 was not started')
                _save_state(root, state)
                progress.complete()
                return state
        feedback = None
        for attempt in range(args.max_verify_attempts):
            progress.start('review_prepare', attempt + 1, args.max_verify_attempts)
            tag = f'02_construct_verify_{attempt:02d}'
            verify_task = prepare_verify(draft, args.sources, context_file, working / 'roles' / tag,
                                         verify_config, feedback, review_retry=attempt > 0)
            log_tag=f'02_{attempt+1:02d}'
            if compact:
                state['role_inputs'][log_tag]=role_input(verify_task,root,log_tag)
                _save_state(root,state)
            job_dir=working/'jobs'/log_tag if compact else root/'jobs'
            progress.finish()
            progress.start('review_worker', attempt + 1, args.max_verify_attempts)
            try:
                workspace = execute_role(verify_task,job_dir,verify_config)
            except Exception as exc:
                from compact_outputs import worker_failure
                failure_path, failure = worker_failure(job_dir, root, log_tag, 'verified', exc)
                state.setdefault('worker_failures', []).append(str(failure_path))
                if failure['copy_errors'] and temporary is not None:
                    preserve_temporary = True
                    state['retained_worker_directory'] = str(working)
                try:
                    _verify_frozen_program()
                    verify_role_task(verify_task)
                    failure['frozen_inputs_intact'] = True
                except (OSError, ValueError):
                    failure['frozen_inputs_intact'] = False
                can_retry = (failure['retryable'] and failure['frozen_inputs_intact']
                             and attempt + 1 < args.max_verify_attempts)
                if can_retry and failure['packages']:
                    from assessment_contract import history_errors
                    recovered = Path(failure['packages'][0])
                    try:
                        saved_report = read_json(recovered / 'review_report.json') if (recovered / 'review_report.json').exists() else {}
                        failure['history_errors'] = history_errors(draft, saved_report)
                    except (OSError, ValueError, TypeError, AttributeError) as error:
                        failure['history_errors'] = ['Cannot read saved review history: ' + str(error)]
                    can_retry = not failure['history_errors']
                failure['retry_scheduled'] = can_retry
                atomic_json(failure_path, failure)
                state['stages'].append({'stage': '02_construct_verify', 'attempt': attempt,
                    'status': 'worker_failed', 'failure': str(failure_path), 'run_ready': False,
                    'retry_scheduled': can_retry})
                progress.finish('fail', failure_code=failure['code'], retry_scheduled=can_retry)
                if not can_retry:
                    raise ValueError(f"{failure['code']}; saved diagnostics: {failure_path}; log: {failure['log']}") from exc
                if failure['code'] == 'environment_start_failed':
                    _save_state(root, state)
                    continue
                # A new reviewer receives the exact saved work and findings. It must
                # finish the review and pass the ordinary host gates; never accept
                # a crashed worker's self-reported complete status.
                draft = recovered
                feedback = root / 'metadata' / f'worker_feedback_{log_tag}.json'
                atomic_json(feedback, {'status': 'fail', 'static_pass': False,
                    'run_ready': False, 'failure_class': 'worker_failure',
                    'worker_failure': {'code': failure['code'],
                        'instruction': 'Continue the saved review package and preserve every finding. '
                            'Read current failure evidence, finish remaining scientific checks, then use '
                            'review_bindings.py --reviewed and preflight. Do not restart construction '
                            'or repeat unchanged passing tests. A stopped worker is not accepted.'}})
                state['package'] = str(draft)
                _save_state(root, state)
                continue
            finally:
                if compact:logs(job_dir,root,log_tag)
            progress.finish('completed')
            progress.start('review_gate', attempt + 1, args.max_verify_attempts)
            _verify_frozen_program()
            verified = root / 'verified' / f'{attempt:02d}'
            reports = working / 'reports' / f'{attempt:02d}'
            copy_tree(workspace / 'verified', verified)
            from authoring_package import bind_framework_fields
            bind_framework_fields(verified, request)
            if request.get('framework_generation_version') == 1:
                from framework_generator import generate
                generate(verified, reports/'framework_generation.json', enabled=True)
            changes = write_package_diff(draft, verified, reports / 'package_diff.json')
            receipt = finalize(verified, reports, expected_request=request, construction_only=construction_only,
                               previous_package=draft, source_bundle=verify_task / 'environment/input/source_bundle')
            static_report = read_json(reports / 'final_preflight.json')
            feedback = reports / 'final_preflight.json'
            progress.finish('pass' if receipt['construction_ready'] else 'fail',
                            failed_checks=sum(c['status'] != 'pass' for c in static_report['checks']),
                            validation_errors=len(receipt['errors']))
            if args.probe_runtime and static_report['static_pass'] and receipt['construction_ready']:
                progress.start('runtime_probe', attempt + 1, args.max_verify_attempts)
                from runtime_probe import probe
                probe(verified, read_json(verified / 'execution.json'), (root/'logs'/f'runtime_{attempt:02d}' if compact else reports/'runtime'),
                      docker=args.docker, timeout=args.probe_timeout)
                runtime_feedback = root/'logs'/f'runtime_{attempt:02d}'/'runtime_evidence.json' if compact else reports/'runtime/runtime_evidence.json'
                receipt = finalize(verified, reports, runtime_feedback, expected_request=request, previous_package=draft,
                                   source_bundle=verify_task / 'environment/input/source_bundle')
                progress.finish('pass' if receipt['execution_ready'] else 'fail')
            merged_checks=checks(root,verified,reports,f'{attempt:02d}',receipt) if compact else None
            triage = receipt.get('contract_status', 'PASS' if receipt['construction_ready'] else 'REWORK')
            state['stages'].append({'stage': '02_construct_verify', 'attempt': attempt,
                                    'package': str(verified), 'run_ready': receipt['run_ready'],
                                    'contract_status': triage,
                                    'role': log_tag if compact else str(verify_task), 'checks':merged_checks,
                                    **({} if compact else {'reports':str(reports),'package_diff':str(reports/'package_diff.json')}),
                                    'task_files_changed': changes['task_files_changed']})
            state['package'] = str(verified)
            state['readiness_scope'] = receipt['readiness_scope']
            state['execution_ready'] = receipt['execution_ready']
            state['contract_status'] = triage
            state['construction_ready'] = receipt['construction_ready'] and triage == 'PASS'
            _save_state(root, state)
            if triage == 'BLOCKED':
                state.update(status='blocked', execution_ready=False,
                             error=f'02 found missing required resources; see {verified / "readiness.json"}')
                break
            accepted = receipt['construction_ready'] if construction_only else receipt['execution_ready']
            if accepted and triage == 'PASS':
                state['status'] = 'ready'
                break
            draft = verified
            if not args.probe_runtime and triage == 'PASS' and static_report['static_pass'] and not receipt['errors'] and receipt['review_status'] == 'complete':
                break
        else:
            state['status'] = 'blocked'
        if state['status'] == 'running':
            state['status'] = 'needs_runtime_verification'
    except Exception as exc:
        state['status'], state['error'] = 'failed', str(exc)
        progress.complete()
        _save_state(root, state)
        raise
    finally:
        if temporary is not None:
            if preserve_temporary:
                # Keep failed copy sources for manual recovery; TemporaryDirectory
                # would otherwise remove them again on object finalization.
                temporary._finalizer.detach()
            else:
                temporary.cleanup()
    _save_state(root, state)
    progress.complete()
    return state


def add_configuration_args(cmd):
    cmd.add_argument('--worker-config', type=Path, help='Authoring worker settings; not Solver task requirements')
    for flag in ('backend', 'harbor', 'agent', 'model', 'construct-model', 'verify-model',
                 'endpoint', 'api-key-env', 'base-image', 'worker-no-proxy', 'worker-proxy-url',
                 'worker-build-no-proxy', 'worker-build-proxy-url', 'task-id'):
        cmd.add_argument('--' + flag)
    for flag in ('env-file', 'prompt01', 'prompt02', 'worker-proxy-env-file',
                 'worker-build-proxy-env-file'):
        cmd.add_argument('--' + flag, type=Path)
    for role in ('construct', 'verify'):
        cmd.add_argument('--' + role + '-endpoint')
        cmd.add_argument('--' + role + '-api-key-env')
        cmd.add_argument('--' + role + '-env-file', type=Path)
        cmd.add_argument('--' + role + '-timeout-sec', type=int)
    for flag in ('worker-cpus', 'worker-memory-mb', 'worker-gpus', 'worker-timeout-sec',
                 'worker-build-timeout-sec'):
        cmd.add_argument('--' + flag, type=int)
    for prefix in ('worker', 'worker-build'):
        cmd.add_argument('--' + prefix + '-direct', action='store_true')
        cmd.add_argument('--' + prefix + '-inherit-env', action=argparse.BooleanOptionalAction, default=None)
    cmd.add_argument('--worker-network-mode', choices=('public', 'no-network', 'allowlist'))
    cmd.add_argument('--worker-allowed-host', action='append')
    cmd.add_argument('--multimodal-enabled', action=argparse.BooleanOptionalAction, default=None)
    cmd.add_argument('--model-capability', choices=('text', 'vision', 'unknown'))


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='action', required=True)
    for action in ('prepare-build', 'prepare-verify', 'run'):
        cmd = sub.add_parser(action)
        cmd.add_argument('--paths', type=Path, help='JSON with source_bundle and output_dir paths only')
        cmd.add_argument('--sources', type=Path)
        cmd.add_argument('--output', type=Path)
        add_configuration_args(cmd)
        if action == 'prepare-verify':
            cmd.add_argument('--draft', type=Path)
            cmd.add_argument('--feedback', type=Path)
        if action == 'run':
            cmd.add_argument('--review-resume-build', nargs=2, metavar=('BUILD_JSON', 'SHA256'),
                             help='Replay a previously passed 01 when its independent reviewer failed to start')
            cmd.add_argument('--run-snapshot', type=Path, help='Use an existing immutable Stage2 program/configuration snapshot')
            cmd.add_argument('--max-construct-attempts', type=int, default=setting('max_construct_attempts'), help='01 attempts; 0 retries until rules pass, positive limit blocks without starting 02')
            cmd.add_argument('--max-verify-attempts', type=int, default=setting('max_verify_attempts'))
            cmd.add_argument('--probe-runtime', action=argparse.BooleanOptionalAction, default=False)
            cmd.add_argument('--construction-only', action=argparse.BooleanOptionalAction, default=None,
                             help='Default: certify construction and defer formal preparation to Stage3; '
                                  '--no-construction-only retains the legacy execution-readiness gate')
            cmd.add_argument('--resume', action='store_true')
            cmd.add_argument('--output-layout',choices=['compact','legacy'],default='legacy')
            cmd.add_argument('--docker', default=setting('docker'))
            cmd.add_argument('--probe-timeout', type=int, default=setting('probe_timeout_sec'))
    for action in ('collect-build', 'collect-verify'):
        cmd = sub.add_parser(action)
        cmd.add_argument('--task', type=Path, required=True)
        cmd.add_argument('--artifact', type=Path, required=True)
        cmd.add_argument('--output', type=Path, required=True)
        if action == 'collect-verify':
            cmd.add_argument('--reports', type=Path, required=True)
            cmd.add_argument('--runtime-evidence', type=Path)
    cmd = sub.add_parser('finalize')
    cmd.add_argument('--package', type=Path, required=True)
    cmd.add_argument('--reports', type=Path, required=True)
    cmd.add_argument('--sources', type=Path, help='Frozen primary-source bundle for scientific scope review')
    cmd.add_argument('--runtime-evidence', type=Path)
    return p


def _prepare_cli(args, role):
    paths = resolve_paths(args)
    config = role_config(resolve_config(args), role)
    context = resolve_context(args)
    if role == 'construct':
        result = prepare_build(args.sources, context, args.output, config,
                               Path(paths['draft']) if paths.get('draft') else None,
                               Path(paths['preflight']) if paths.get('preflight') else None)
    else:
        draft = args.draft or (Path(paths['draft']) if paths.get('draft') else None)
        if draft is None:
            raise ValueError('review requires --draft or paths.draft')
        feedback = args.feedback or (Path(paths['runtime_feedback']) if paths.get('runtime_feedback') else None)
        result = prepare_verify(draft, args.sources, context, args.output, config, feedback)
    return {'task': str(result)}


def _enter_snapshot(args, argv):
    """Replace the bootstrap process; all host imports then come from the copy."""
    from run_snapshot import create, environment, verify, STAGE2
    if getattr(args, 'run_snapshot', None):
        frozen = args.run_snapshot.resolve()
        verify(frozen)
        if STAGE.parent == frozen:
            return
        os.execve(sys.executable, [sys.executable, '-B', str(frozen / STAGE2 / 'code/pipeline.py'), *argv],
                  environment(frozen))
    resolve_paths(args)
    config = resolve_config(args)
    frozen = args.output.with_name(args.output.name + '.snapshot').resolve()
    if args.output.exists() and not frozen.exists():
        raise ValueError('existing output has no saved Stage2 snapshot; restore it or use a new output directory')
    if frozen.exists():
        manifest = verify(frozen)
        if not args.resume:
            raise ValueError('snapshot already exists; use --resume or a new output directory')
        if config != manifest['requested_worker_config']:
            raise ValueError('worker settings differ from saved snapshot; use a new output directory')
    else:
        create(STAGE.parent, frozen, config, {
            'max_construct_attempts': args.max_construct_attempts,
            'max_verify_attempts': args.max_verify_attempts,
            'docker': args.docker, 'probe_timeout_sec': args.probe_timeout,
            'output_layout': args.output_layout, 'construction_only': args.construction_only,
            'probe_runtime': args.probe_runtime}, docker=args.docker)
    # Worker options were resolved and frozen above; only task/run arguments remain.
    forwarded = ['run', '--run-snapshot', str(frozen)]
    for name in ('paths', 'sources', 'output', 'task_id', 'max_construct_attempts',
                 'max_verify_attempts', 'output_layout', 'docker', 'probe_timeout'):
        value = getattr(args, name, None)
        if value is not None:
            forwarded += ['--' + name.replace('_', '-'), str(value)]
    for name in ('resume',):
        if getattr(args, name, False):
            forwarded.append('--' + name.replace('_', '-'))
    forwarded.append('--construction-only' if args.construction_only else '--no-construction-only')
    forwarded.append('--probe-runtime' if args.probe_runtime else '--no-probe-runtime')
    os.execve(sys.executable, [sys.executable, '-B', str(frozen / STAGE2 / 'code/pipeline.py'), *forwarded],
              environment(frozen))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        # When launched directly from an archived tree, never read live Bash defaults.
        from run_snapshot import MANIFEST, verify, environment, STAGE2
        selector = argparse.ArgumentParser(add_help=False)
        selector.add_argument('--run-snapshot', type=Path)
        selected, _ = selector.parse_known_args(argv)
        if selected.run_snapshot and selected.run_snapshot.resolve() != STAGE.parent:
            frozen = selected.run_snapshot.resolve()
            verify(frozen)
            os.execve(sys.executable, [sys.executable, '-B', str(frozen / STAGE2 / 'code/pipeline.py'), *argv],
                      environment(frozen))
        if (STAGE.parent / MANIFEST).is_file():
            verify(STAGE.parent)
            os.environ['NATUREBENCH_PIPELINE_CONFIG'] = str(STAGE.parent / 'settings.json')
        args = parser().parse_args(argv)
        if args.action == 'run':
            if args.construction_only is None:
                args.construction_only = not args.probe_runtime
            if (STAGE.parent / MANIFEST).is_file() and args.run_snapshot is None:
                args.run_snapshot = STAGE.parent
            _enter_snapshot(args, argv)
        if args.action == 'run':
            if not 1 <= args.max_verify_attempts <= 5:
                raise ValueError('max-verify-attempts must be 1..5')
            result = workflow(args)
        elif args.action == 'prepare-build':
            result = _prepare_cli(args, 'construct')
        elif args.action == 'prepare-verify':
            result = _prepare_cli(args, 'construct_verify')
        elif args.action == 'collect-build':
            result = {'package': str(collect_build(args.task, args.artifact, args.output))}
        elif args.action == 'collect-verify':
            result = collect_verify(args.task, args.artifact, args.output, args.reports, args.runtime_evidence)
        else:
            result = finalize(args.package, args.reports, args.runtime_evidence, source_bundle=args.sources)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.action == 'run' and result.get('status') == 'ready':
            return 0
        return 2 if result.get('run_ready') is False or result.get('status') in {'blocked', 'needs_runtime_verification'} else 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'error', 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
