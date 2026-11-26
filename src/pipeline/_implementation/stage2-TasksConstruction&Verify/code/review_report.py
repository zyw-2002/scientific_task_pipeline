"""Shared review-report interface for worker preflight and host acceptance.

The small schema reader implements only keywords used by the bundled schema.
Unknown validation keywords fail closed; it is not a general JSON Schema engine.
No task code is imported or executed here.
"""
from __future__ import annotations

import json
from pathlib import Path
import re


def _schema_errors(value, schema, root, where='review_report'):
    supported = {'$schema', '$id', '$defs', '$ref', 'title', 'description', 'type',
                 'required', 'properties', 'additionalProperties', 'items',
                 'minItems', 'minLength', 'pattern', 'enum', 'const'}
    unknown = schema.keys() - supported
    if unknown:
        raise ValueError('unsupported review schema keywords: ' + ', '.join(sorted(unknown)))
    if '$ref' in schema:
        ref = schema['$ref']
        if not ref.startswith('#/$defs/'):
            raise ValueError('review schema permits only local definitions')
        return _schema_errors(value, root['$defs'][ref.split('/')[-1]], root, where)
    types = {'object': lambda v: isinstance(v, dict), 'array': lambda v: isinstance(v, list),
             'string': lambda v: isinstance(v, str), 'boolean': lambda v: type(v) is bool,
             'integer': lambda v: type(v) is int, 'number': lambda v: type(v) in (int, float),
             'null': lambda v: v is None}
    expected = schema.get('type')
    if expected and not types[expected](value):
        return [f'{where} must be {expected}']
    errors = []
    if 'const' in schema and value != schema['const']:
        errors.append(f'{where} must equal {schema["const"]!r}')
    if 'enum' in schema and value not in schema['enum']:
        errors.append(f'{where} must be one of {schema["enum"]!r}')
    if isinstance(value, str):
        if len(value.strip()) < schema.get('minLength', 0):
            errors.append(f'{where} must be nonempty text')
        if 'pattern' in schema and not re.search(schema['pattern'], value):
            errors.append(f'{where} does not match {schema["pattern"]}')
    if isinstance(value, list):
        if len(value) < schema.get('minItems', 0):
            errors.append(f'{where} needs at least {schema["minItems"]} entries')
        for i, item in enumerate(value):
            errors.extend(_schema_errors(item, schema.get('items', {}), root, f'{where}[{i}]'))
    if isinstance(value, dict):
        for key in schema.get('required', []):
            if key not in value:
                errors.append(f'{where}.{key} is required')
        props = schema.get('properties', {})
        for key, item in value.items():
            if key in props:
                errors.extend(_schema_errors(item, props[key], root, f'{where}.{key}'))
            elif schema.get('additionalProperties') is False:
                errors.append(f'{where}.{key} is unsupported')
            elif isinstance(schema.get('additionalProperties'), dict):
                errors.extend(_schema_errors(item, schema['additionalProperties'], root, f'{where}.{key}'))
    return errors


def review_errors(report: dict) -> list[str]:
    if not isinstance(report, dict):
        return ['review_report must be an object']
    errors = []
    if report.get('schema_version') == 'pipline-review-report-v4':
        schema = json.loads(Path(__file__).with_name('review_report.schema.json').read_text())
        errors.extend(_schema_errors(report, schema, schema))
        if errors:
            return errors
    elif report.get('schema_version') not in (None, 'pipline-review-report-v3'):
        return ['unsupported review_report.schema_version']
    # Preserve historical reports while sharing their consistency rules too.
    for key in ('changes', 'resources', 'checks', 'unresolved_blockers'):
        if not isinstance(report.get(key), list):
            errors.append(f'review_report.{key} must be an array')
    for key in ('definition_complete', 'sources_verified', 'real_data_verified', 'runtime_verified'):
        if not isinstance(report.get(key), bool):
            errors.append(f'review_report.{key} must be boolean')
    if report.get('status') not in ('complete', 'blocked'):
        errors.append('review_report.status must be complete or blocked')
    complete = report.get('status') == 'complete'
    if complete and (report.get('unresolved_blockers') or not report.get('definition_complete')):
        errors.append('complete review contradicts unresolved blockers/incomplete definition')
    if complete and report.get('sources_verified') is not True:
        errors.append('complete review requires verified scientific sources')
    for key in ('construction_pending', 'execution_pending'):
        if key in report and not isinstance(report[key], list):
            errors.append(f'review_report.{key} must be an array')
    if complete and report.get('construction_pending'):
        errors.append('complete review contains unresolved construction_pending items')
    scientific = report.get('scientific_checks', {})
    if not isinstance(scientific, dict):
        errors.append('review_report.scientific_checks must be an object')
    elif complete:
        for name, check in scientific.items():
            if isinstance(check, dict) and check.get('status') == 'fail':
                errors.append(f'complete review contains failed scientific check: {name}')
    checks = report.get('checks', [])
    seen = set()
    for check in checks if isinstance(checks, list) else []:
        if (not isinstance(check, dict) or check.get('status') not in ('pass', 'fail', 'not_run')
                or not isinstance(check.get('name'), str) or not check['name'].strip()):
            errors.append('review_report.checks require name and status pass/fail/not_run')
            continue
        if check['name'] in seen:
            errors.append('duplicate review check: ' + check['name'])
        seen.add(check['name'])
        if check.get('status') == 'pass' and not check.get('evidence'):
            errors.append('passed review check requires actual evidence')
        phase = check.get('phase', 'construction')
        if phase not in {'construction', 'execution'}:
            errors.append('review check phase must be construction or execution')
        if complete and (check.get('status') == 'fail' or (
                phase == 'construction' and check.get('required') is True and check.get('status') == 'not_run')):
            errors.append('complete review contains a failed or unrun required final check')
    return list(dict.fromkeys(errors))


def declared_checks(spec: dict) -> set[str]:
    verification = spec.get('verification', {})
    names = set(verification.get('commands', {}))
    names.update(b['runtime_check'] for b in verification.get('calibration', []))
    smoke = verification.get('scoring_smoke')
    if smoke: names.add(smoke['runtime_check'])
    return names


def check_record_errors(spec: dict, report: dict) -> list[str]:
    """Record every declared check; deferred execution is allowed, silent omission is not."""
    if report.get('status') != 'complete': return []
    records = {c['name']: c for c in report.get('checks', [])}
    missing = declared_checks(spec) - records.keys()
    errors = ['missing check records (pass/fail or not_run with reason and completion_condition): '
              + ', '.join(sorted(missing))] if missing else []
    construction_checks = {b['runtime_check'] for b in spec.get('verification', {}).get('calibration', [])}
    smoke = spec.get('verification', {}).get('scoring_smoke')
    if smoke:
        construction_checks.add(smoke['runtime_check'])
        name = smoke['runtime_check']
        if spec.get('scientific_acceptance_version') == 3 and records.get(name, {}).get('status') != 'pass':
            errors.append(f'{name}: complete review requires passing scoring evidence on authoring fixtures')
    for name, check in records.items():
        if name in construction_checks and check.get('phase') == 'execution':
            errors.append(f'{name}: calibration/scoring smoke belongs to construction, not deferred execution')
        if check.get('status') == 'not_run':
            for key in ('evidence', 'completion_condition'):
                if not isinstance(check.get(key), str) or not check[key].strip():
                    errors.append(f'{name}: not_run needs {key}; state the gap, responsible execution stage and completion condition')
    return errors


def validate_review(package: Path, spec: dict, report: dict, *, required=False) -> list[str]:
    """Same report acceptance boundary in the worker and host (no execution)."""
    errors = review_errors(report)
    if errors:
        return errors
    from contract import validate_execution
    if not validate_execution(spec) and isinstance(report, dict):
        if spec.get('scope_contract_version') in (1, 2, 3):
            from assessment_contract import scope_review_errors
            try:
                errors.extend(scope_review_errors(package, report))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors.append('scope review: ' + str(exc))
        if spec.get('scope_contract_version') in (2, 3):
            from construction_audit import audit_errors
            try:
                errors.extend(audit_errors(package, spec, report))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors.append('construction audit: ' + str(exc))
        from paper_comparison import review_errors as paper_review_errors
        try:
            errors.extend(paper_review_errors(package, spec, report))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append('paper comparison review: ' + str(exc))
        from scientific_acceptance import acceptance_errors
        errors.extend(acceptance_errors(package, spec, report, required=required))
        if spec.get('assessment_contract_version') == 1:
            errors.extend(check_record_errors(spec, report))
            from assessment_contract import findings_errors
            try: errors.extend(findings_errors(package, spec, report))
            except (OSError, ValueError, KeyError, TypeError) as exc: errors.append('finding/assessment bindings: ' + str(exc))
    for key in ('task_id', 'canonical_id'):
        if isinstance(report, dict) and key in report and report[key] != spec.get(key):
            errors.append(f'review_report.{key} does not match execution.json')
    return list(dict.fromkeys(errors))


def initialize_review(package: Path) -> bool:
    """Create an explicitly incomplete report once. Never overwrite a review."""
    package = Path(package)
    path = package / 'review_report.json'
    if path.exists():
        return False
    spec = json.loads((package / 'execution.json').read_text())
    report = {'schema_version': 'pipline-review-report-v4', 'status': 'blocked',
              'task_id': spec['task_id'], 'changes': [], 'resources': [], 'checks': [],
              'unresolved_blockers': ['Scientific review has not been completed.'],
              'definition_complete': False, 'sources_verified': False,
              'real_data_verified': False, 'runtime_verified': False,
              'scientific_checks': {}, 'scientific_requirements': [],
              'construction_pending': [], 'execution_pending': []}
    if spec.get('assessment_contract_version') == 1:
        report['findings'] = []
        report['checks'] = [{'name': name, 'status': 'not_run'} for name in sorted(declared_checks(spec))]
        for check in report['checks']:
            check['phase'] = 'execution' if check['name'] == 'resource_readiness' else 'construction'
    if spec.get('scope_contract_version') in (1, 2, 3):
        report['scientific_checks']['scope_fidelity'] = {'status': 'not_run'}
        if spec['scope_contract_version'] == 3:
            report['scientific_checks']['scope_fidelity']['main_experiments'] = {}
    if spec.get('scope_contract_version') in (2, 3):
        # Transcribe the ABI and declared IDs, never the reviewer's judgments.
        # Empty evidence/rationale and not_reviewed verdicts keep this template
        # incomplete. A reviewer must inspect the source and execute controls.
        from contract import package_layout, package_path
        protocol = json.loads((package / 'common_task/evaluation/protocol.json').read_text())
        requirements = protocol.get('assessment', {}).get('requirements', [])
        core = [r['id'] for r in requirements if r.get('role') in {'quality', 'trajectory'}]
        resources = [r for r in spec.get('data', {}).get('sources', [])
                     if r.get('role') in {'input', 'reference'}]
        layout = package_layout(spec)
        documents = [layout[k] for k in ('instruction_path', 'readme_path',
                                         'data_description_path', 'data_sources_path')
                     if package_path(package, layout[k]).is_file()]
        report['construction_audit'] = {
            'input_sha256': {},
            'public_documents': {name: {'verdict': 'not_reviewed', 'claims': [], 'rationale': ''}
                                 for name in documents},
            'scope_decisions': [{'resource_ids': [r['id'] for r in resources],
                                 'requirement_ids': core, 'retained_capability': '',
                                 'removed_capability': '', 'justification': '',
                                 'difficulty_effect': '', 'source_evidence': [],
                                 'verdict': 'not_reviewed'}],
            'data_paths': [dict(resource_id=r['id'], content=r['content'],
                                availability=r['availability'],
                                prepare_command=r.get('prepare', {}).get('command', []),
                                validate_command=r.get('prepare', {}).get('validate', []),
                                consistency_reason='', missing_data_behavior='',
                                code_evidence=[], extent_check='', extent_evidence=[],
                                verdict='not_reviewed') for r in resources],
            'shortcut_checks': [dict(requirement_ids=[rid], mechanism='', reason='',
                                     code_evidence=[], disposition='', case_id=None)
                                for rid in core],
        }
    if spec.get('paper_comparison_version') == 1:
        report['scientific_checks']['paper_comparison'] = {'status': 'not_run'}
    if spec.get('canonical_id'):
        report['canonical_id'] = spec['canonical_id']
    # Exclusive creation protects existing authored findings, including concurrent writers.
    with path.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    return True
