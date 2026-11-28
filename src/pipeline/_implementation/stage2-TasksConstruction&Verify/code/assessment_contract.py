"""Mechanical assessment checks shared by construction and review preflight.

Passing these gates proves declared consistency, not the scientific truth of
labels, adequacy of objectives, or correctness of arbitrary task code.
"""
from __future__ import annotations
import json
import math
from pathlib import Path, PurePosixPath
import re
import shlex

from resource_contract import source_errors, generated_resources


def assessment_errors(protocol):
    a = protocol.get('assessment')
    if not isinstance(a, dict): return ['protocol.assessment is required for new constructions']
    errors = []
    if a.get('mode') not in {'reference', 'analytical', 'objective', 'rubric'}:
        errors.append('assessment.mode must be reference/analytical/objective/rubric')
    for key in ('basis', 'reference_policy'):
        if not isinstance(a.get(key), str) or not a[key].strip():
            errors.append('assessment.' + key + ' must be a nonempty string')
    adaptations = a.get('adaptations')
    if isinstance(adaptations, str):
        if not adaptations.strip(): errors.append('assessment.adaptations must describe adaptations or explicitly state none')
    elif isinstance(adaptations, list):
        if not adaptations: errors.append('assessment.adaptations must contain at least one explanation; explicitly state none if unchanged')
        for index, item in enumerate(adaptations):
            explanation = item.get('description') if isinstance(item, dict) else item
            if not isinstance(explanation, str) or not explanation.strip():
                errors.append(f'assessment.adaptations[{index}] must be a nonempty string '
                              'or an object with a nonempty description string')
    else:
        errors.append('assessment.adaptations must be a nonempty string or a list of explanations; '
                      f'got {type(adaptations).__name__}')
    requirements = a.get('requirements')
    if not isinstance(requirements, list) or not requirements: return errors + ['assessment.requirements must be nonempty']
    outputs = {v.get('output') for v in protocol.get('aggregations', []) if isinstance(v, dict)}
    rewarded = {k for k, v in protocol.get('reward_components', {}).items() if v != 0} or {protocol.get('reward_metric')}
    seen, quality = set(), False
    for req in requirements:
        if not isinstance(req, dict): errors.append('assessment requirement must be an object'); continue
        rid = req.get('id')
        if not isinstance(rid, str) or not rid or rid in seen:
            errors.append('assessment requirements need unique IDs'); continue
        seen.add(rid)
        for key in ('statement', 'source', 'verification'):
            if not isinstance(req.get(key), str) or not req[key].strip(): errors.append(f'{rid}: missing {key}')
        if req.get('origin') not in {'paper', 'external', 'derived', 'task_policy'}:
            errors.append(f'{rid}: origin must distinguish paper/external/derived/task_policy')
        role = req.get('role')
        if role not in {'validity', 'quality', 'diagnostic', 'trajectory'}: errors.append(f'{rid}: unsupported requirement role')
        if role == 'quality':
            quality = True
            if req.get('metric') not in rewarded: errors.append(f'{rid}: quality requirement must affect the actual reward')
        if role in {'quality', 'diagnostic'} and req.get('metric') not in outputs:
            errors.append(f'{rid}: metric must name an aggregation output')
        if role in {'validity', 'trajectory'} and req.get('metric') not in (None, ''):
            errors.append(f'{rid}: nonmetric requirements must omit metric')
        artifacts = req.get('artifact_ids', [])
        known = {v.get('id') for v in protocol.get('required_artifacts', [])}
        if not isinstance(artifacts, list) or any(v not in known for v in artifacts):
            errors.append(f'{rid}: unknown artifact_ids')
    if not quality: errors.append('assessment needs a quality objective bound to reward')
    if a.get('mode') == 'reference' and not any(v.get('role') == 'reference' for v in protocol.get('resources', {}).values()):
        errors.append('reference assessment requires a declared formal reference resource')
    bands = a.get('performance_bands', [])
    if not isinstance(bands, list): return errors + ['performance_bands must be an array']
    labels = set()
    for b in bands:
        if not isinstance(b, dict): errors.append('performance band must be an object'); continue
        label = b.get('label')
        if not isinstance(label, str) or not label or label in labels: errors.append('performance bands need unique labels')
        labels.add(str(label))
        if (b.get('metric') not in outputs or b.get('operator') not in {'lt', 'le', 'gt', 'ge'}
                or type(b.get('value')) not in (float, int) or not math.isfinite(b['value'])):
            errors.append('performance bands need a metric, operator and finite value')
        if b.get('origin') not in {'paper', 'external', 'derived', 'task_policy'} or not b.get('source'):
            errors.append('performance bands must declare origin and source; task_policy needs no paper threshold')
    return errors


def main_experiment_errors(protocol):
    scope = protocol.get('assessment', {}).get('scope', {})
    experiments = scope.get('main_experiments') if isinstance(scope, dict) else None
    if not isinstance(experiments, dict) or not experiments:
        return ['assessment.scope.main_experiments must map every main experiment to its sources and scoring requirements']
    formal = {key for key, item in protocol.get('resources', {}).items()
              if isinstance(item, dict) and item.get('role') in {'input', 'reference'}}
    # A v3 scope declaration must point at the evaluator's actual scoring
    # units.  Keeping this binding in the assessment contract prevents a
    # package from claiming coverage of several paper experiments while its
    # protocol only evaluates one generic submission unit.
    protocol_units = {
        item.get('id') for item in protocol.get('units', [])
        if isinstance(item, dict) and isinstance(item.get('id'), str) and item.get('id')
    }
    aggregation_units = set()
    for aggregation in protocol.get('aggregations', []):
        if not isinstance(aggregation, dict):
            continue
        selected = aggregation.get('unit_ids')
        aggregation_units.update(protocol_units if selected is None else
                                 {uid for uid in selected if isinstance(uid, str)})
    requirements_by_id = {
        req.get('id'): req for req in protocol.get('assessment', {}).get('requirements', [])
        if isinstance(req, dict) and isinstance(req.get('id'), str)
    }
    scored = {item.get('id') for item in protocol['assessment'].get('requirements', [])
              if isinstance(item, dict) and item.get('role') == 'quality'}
    errors = []
    bound_resources = set()
    for key, experiment in experiments.items():
        prefix = f'main_experiments[{key}]'
        if not isinstance(key, str) or not key.strip() or not isinstance(experiment, dict):
            errors.append(f'{prefix}: provide a stable ID and experiment definition')
            continue
        for field in ('statement', 'population', 'split', 'deliverable', 'metric_unit', 'aggregation'):
            if not isinstance(experiment.get(field), str) or not experiment[field].strip():
                errors.append(f'{prefix}: missing {field}')
        for field, allowed in [('resource_ids', formal), ('requirement_ids', scored)]:
            ids = experiment.get(field)
            if (not isinstance(ids, list) or (field == 'requirement_ids' and not ids)
                    or any(not isinstance(i, str) or i not in allowed for i in ids)):
                errors.append(f'{prefix}: {field} must identify the actual formal inputs or scored requirements')
            elif field == 'resource_ids':
                bound_resources.update(ids)
        # ``unit_ids`` is the canonical binding.  ``evaluation_groups`` is
        # accepted for fixed panels generated from the source instruction and
        # must carry the same protocol unit IDs.  A boolean true means every
        # declared evaluator unit, matching fixed_task_alignment semantics.
        # Legacy analytical/source-only protocols may intentionally omit an
        # evaluator unit inventory.  Once units are declared, every main
        # experiment must bind them explicitly.  This preserves compatibility
        # for those fixture protocols while keeping the production gate strict.
        if not protocol_units:
            bound_units = []
        else:
            bound_units = experiment.get('unit_ids')
        if protocol_units and bound_units is None:
            groups = experiment.get('evaluation_groups')
            if isinstance(groups, dict):
                bound_units = groups.get('protocol_units')
        if protocol_units and bound_units is True:
            bound_units = sorted(protocol_units)
        if (protocol_units and (not isinstance(bound_units, list) or not bound_units or
                any(not isinstance(uid, str) or uid not in protocol_units for uid in bound_units) or
                len(set(bound_units)) != len(bound_units))):
            errors.append(f'{prefix}: unit_ids or evaluation_groups.protocol_units must bind declared evaluator units')
        elif protocol_units:
            bound = set(bound_units)
            if not bound <= aggregation_units:
                missing = ', '.join(sorted(bound - aggregation_units))
                errors.append(f'{prefix}: evaluator aggregations do not score bound unit(s): {missing}')
            metrics = {requirements_by_id[rid].get('metric') for rid in experiment.get('requirement_ids', [])
                       if rid in requirements_by_id and requirements_by_id[rid].get('metric')}
            for metric in metrics:
                metric_units = set()
                for aggregation in protocol.get('aggregations', []):
                    if isinstance(aggregation, dict) and (aggregation.get('metric') == metric or aggregation.get('output') == metric):
                        selected = aggregation.get('unit_ids')
                        metric_units.update(protocol_units if selected is None else
                                            {uid for uid in selected if isinstance(uid, str)})
                if bound - metric_units:
                    errors.append(f'{prefix}: metric {metric} does not aggregate bound unit(s): '
                                  + ', '.join(sorted(bound - metric_units)))
        if experiment.get('origin') not in ('paper', 'author_artifact') or not experiment.get('source_evidence'):
            errors.append(f'{prefix}: cite the original experiment with source_evidence')
    required = {key for key in formal if protocol['resources'][key].get('required', True)}
    if required - bound_resources:
        errors.append('main_experiments omit required formal resources: ' + ', '.join(sorted(required - bound_resources)))
    return errors


def scope_errors(protocol):
    """Check a traceable scope declaration, not scientific equivalence to a paper.

    New builds also check the main-experiment inventory against primary sources.
    """
    assessment = protocol.get('assessment')
    scope = assessment.get('scope') if isinstance(assessment, dict) else None
    if not isinstance(scope, dict):
        return ['assessment.scope must describe original scope, task scope and data coverage']
    errors = []
    for key in ('source_basis', 'paper_scope', 'task_scope', 'difficulty_impact'):
        if not isinstance(scope.get(key), str) or not scope[key].strip():
            errors.append('assessment.scope.' + key + ' must be nonempty text')
    if scope.get('scope_policy', 'full') not in {'auto', 'bounded', 'full'}:
        errors.append('assessment.scope.scope_policy must be auto, bounded, or full')
    coverage = scope.get('data_coverage')
    if not isinstance(coverage, list):
        return errors + ['assessment.scope.data_coverage must be an array (empty only without formal data)']
    resources = protocol.get('resources', {})
    if not isinstance(resources, dict) or any(not isinstance(r, dict) for r in resources.values()):
        return errors + ['scope data coverage requires declared resource objects']
    formal = {rid for rid, r in resources.items() if r.get('role') in {'input', 'reference'}}
    seen = set()
    for row in coverage:
        if not isinstance(row, dict):
            errors.append('scope data coverage entries must be objects'); continue
        rid = row.get('resource_id')
        if not isinstance(rid, str) or rid not in formal or rid in seen:
            errors.append('scope data coverage must uniquely reference formal input/reference resources'); continue
        seen.add(rid)
        if row.get('coverage') not in {'full', 'subset', 'generated'}:
            errors.append(f'{rid}: coverage must be full/subset/generated')
        for key in ('source_scope', 'task_scope', 'rationale'):
            if not isinstance(row.get(key), str) or not row[key].strip():
                errors.append(f'{rid}: scope coverage needs {key}')
    if formal - seen:
        errors.append('scope data coverage missing formal resources: ' + ', '.join(sorted(formal - seen)))
    return errors


def scope_review_errors(package, report):
    """Bind a substantive source-based review to the current formal protocol."""
    from contract import sha256_file
    path = Path(package) / 'common_task/evaluation/protocol.json'
    protocol = json.loads(path.read_text())
    errors = scope_errors(protocol)
    if report.get('status') != 'complete':
        return errors
    check = report.get('scientific_checks', {}).get('scope_fidelity')
    if not isinstance(check, dict) or check.get('status') != 'pass':
        return errors + ['complete review requires scope_fidelity pass based on original materials']
    if not isinstance(check.get('evidence'), str) or not check['evidence'].strip():
        errors.append('scope_fidelity needs source-based evidence about coverage, adaptations and core difficulty')
    if check.get('protocol_sha256') != sha256_file(path):
        errors.append('scope_fidelity refers to a missing or changed protocol; review the current task scope')
    return errors


def binding_errors(protocol):
    errors = []
    resources, bindings = protocol.get('resources', {}), protocol.get('resource_bindings', {})
    if not isinstance(resources, dict) or not isinstance(bindings, dict): return ['resources/resource_bindings must be objects']
    for key, rid in bindings.items():
        if not isinstance(rid, str) or rid not in resources:
            errors.append(f'{key}: unknown formal resource'); continue
        if resources[rid].get('role') == 'fixture': errors.append(f'{key}: formal scoring cannot bind a fixture')
        if resources[rid].get('required') is False: errors.append(f'{key}: formal scoring cannot bind optional resource {rid}')
        if key in protocol.get('context', {}): errors.append(f'{key}: do not duplicate resource bindings in context')
    for key, value in protocol.get('context', {}).items():
        if isinstance(value, str) and (key.endswith(('_path', '_dir', '_file')) or value.startswith('/')):
            errors.append(f'{key}: use resource_bindings instead of hard-coded resource paths')
        if (re.search(r'(?:^|_)(?:reference|truth|ground_truth)(?:_|$)', key, re.I)
                and isinstance(value, dict)
                and any(isinstance(p, str) and ('/' in p or Path(p).suffix.lower() in
                        {'.bin', '.csv', '.tsv', '.json', '.npy', '.npz', '.h5', '.h5ad'})
                        for p in value.values())
                and not any(isinstance(r, str) and resources.get(r, {}).get('role') == 'reference'
                            for r in bindings.values())):
            errors.append(f'{key}: reference file names need an independent reference resource bound to the evaluator. '
                          'Read expected data from that resource and candidate outputs from the submission.')
    for artifact in protocol.get('required_artifacts', []):
        ids = artifact.get('id_set')
        if ids and (ids.get('resource') not in resources or resources.get(ids.get('resource'), {}).get('role') == 'fixture'):
            errors.append(f"{artifact['id']}: ID set must use a formal resource")
        elif ids:
            if resources[ids['resource']].get('required') is False:
                errors.append(f"{artifact['id']}: submission IDs cannot depend on an optional resource")
            content = resources[ids['resource']].get('content', {})
            if content.get('format') not in {'csv', 'tsv'} or ('columns' in content and ids.get('column') not in content['columns']):
                errors.append(f"{artifact['id']}: ID set must reference a declared CSV/TSV column")
    return errors


def public_tables(protocol):
    """Expected declarations; used for comparison, never replace authored prose."""
    a = protocol.get('assessment', {})
    tables = {
        ('Requirement', 'Role', 'Metric'): [[r['id'], r['role'], r.get('metric') or '-'] for r in a.get('requirements', [])],
        ('Score component', 'Weight'): [[k, format(v, '.12g')] for k, v in
            (protocol.get('reward_components') or {protocol.get('reward_metric'): 1}).items()],
        ('Performance band', 'Metric', 'Operator', 'Value', 'Origin'): [
            [b['label'], b['metric'], b['operator'], format(b['value'], '.12g'), b['origin']]
            for b in a.get('performance_bands', [])],
    }
    representations = a.get('submission_representations', {})
    if isinstance(representations, dict) and representations:
        tables['Submission representation', 'Description'] = [[k,v] for k,v in representations.items()]
    return tables


def instruction_errors(markdown, protocol):
    from instruction_checks import parse_sections, _cells
    errors = []
    sections = parse_sections(markdown)
    evaluation = '\n'.join(body for title, body in sections if title == 'Evaluation')
    expected = public_tables(protocol)
    representations = protocol.get('assessment', {}).get('submission_representations', {})
    if not isinstance(representations, dict) or len(representations) < 2:
        packaging = r'\b(?:one file|single file|one archive|combined|packed|separate files?|split files?|individual files?|multiple files?|concatenated|sharded)\b'
        for line in markdown.splitlines():
            if (re.search(r'\b(?:submit(?:ted)?|output|deliver|return|accept(?:ed)?)\b', line, re.I)
                    and re.search(packaging + r'[^.\n]{0,110}\bor\b[^.\n]{0,100}' + packaging, line, re.I)):
                errors.append('declare the alternative submission representations in the protocol and test equivalent results')
                break
    found = {key: [] for key in expected}
    header = None
    for line in evaluation.splitlines():
        cells = _cells(line)
        if cells and tuple(cells) in expected: header = tuple(cells)
        elif cells and header:
            if len(cells) == len(header): found[header].append(cells)
        elif line.strip() and not line.strip().startswith('|'): header = None
    for key, values in expected.items():
        if sorted(found[key]) != sorted(values):
            from collections import Counter
            expected_rows = Counter(tuple(row) for row in values)
            actual_rows = Counter(tuple(row) for row in found[key])
            missing = list((expected_rows - actual_rows).elements())
            extra = list((actual_rows - expected_rows).elements())
            errors.append('Evaluation table differs from protocol: ' + ' | '.join(key)
                          + '; missing expected rows=' + json.dumps(missing[:8])
                          + '; unexpected actual rows=' + json.dumps(extra[:8])
                          + ('; showing first 8 differences per side' if max(len(missing),len(extra)) > 8 else ''))
    from solver_output_audit import score_formula_errors
    errors.extend(score_formula_errors(markdown, protocol))
    return errors


def inspect(package, spec, protocol, *, construction_only=False):
    checks = {}
    checks['RESOURCE-PLAN'] = source_errors(package, spec, construction_only=construction_only)
    checks['ASSESSMENT-DEFINITION'] = assessment_errors(protocol)
    # Production scope v3 packages must carry a source-backed difficulty
    # vector. Historical analytical fixtures remain on the legacy path until
    # they opt into scientific-acceptance-v3.
    from difficulty_contract import difficulty_errors
    assessment_mode = protocol.get('assessment', {}).get('mode')
    checks['DIFFICULTY-ALIGNMENT'] = difficulty_errors(
        protocol,
        # Historical analytical interface fixtures have no paper/task
        # comparison and remain on the legacy analytical contract. Every
        # production scientific task opts in with difficulty_contract_version.
        required=(spec.get('difficulty_contract_version') == 1 and
                  assessment_mode != 'analytical'),
    )
    if spec.get('scope_contract_version') in (1, 2, 3):
        checks['TASK-SCOPE'] = scope_errors(protocol)
        if spec['scope_contract_version'] == 3:
            checks['TASK-SCOPE'].extend(main_experiment_errors(protocol))
            if protocol.get('scope_contract_version') != 3:
                checks['TASK-SCOPE'].append('regenerate the v3 scoring protocol to bind its construction resource scope')
    from paper_comparison import declaration_errors as paper_errors, source_errors as paper_sources
    checks['PAPER-COMPARISON'] = paper_errors(protocol, required=spec.get('paper_comparison_version') == 1)
    if not checks['PAPER-COMPARISON']:
        checks['PAPER-COMPARISON'].extend(paper_sources(package, protocol))
    checks['FORMAL-RESOURCE-BINDINGS'] = binding_errors(protocol)
    if not checks['RESOURCE-PLAN']:
        try:
            expected = generated_resources(spec, 'common_task/evaluation', '/task/evaluation')
            if protocol.get('resources') != expected:
                checks['FORMAL-RESOURCE-BINDINGS'].append('generated protocol resources differ from execution.data.sources; regenerate framework')
        except (ValueError, KeyError) as exc: checks['FORMAL-RESOURCE-BINDINGS'].append(str(exc))
    from resource_contract import digest
    tool = Path(package) / 'common_task/evaluation/resource_contract.py'
    if not tool.is_file() or digest(tool) != digest(Path(__file__).with_name('resource_contract.py')):
        checks['FORMAL-RESOURCE-BINDINGS'].append('resource_contract.py must be the unmodified generated tool')
    expected_check = {'service': spec.get('verification', {}).get('score_evidence', {}).get('service', 'evaluator'),
        'command': ['python', '/task/evaluation/resource_contract.py', '--config', '/task/evaluation/protocol.json',
                    '--receipt', str(PurePosixPath(spec.get('verification', {}).get('score_evidence', {}).get(
                        'artifact_source', '/evaluator_output/score_attempts')).parent / 'resource_readiness.json')]}
    if spec.get('verification', {}).get('commands', {}).get('resource_readiness') != expected_check:
        checks['FORMAL-RESOURCE-BINDINGS'].append('resource_readiness must use generated formal runtime checker')
    if not (Path(package) / 'common_task/evaluation/scoring_smoke.json').is_file():
        checks['ASSESSMENT-DEFINITION'].append('scoring_smoke.json is required for new constructions')
    if not checks['ASSESSMENT-DEFINITION']:
        try: checks['ASSESSMENT-INSTRUCTION'] = instruction_errors((Path(package) / 'common_task/instruction.md').read_text(), protocol)
        except (OSError, ValueError, TypeError, KeyError) as exc: checks['ASSESSMENT-INSTRUCTION'] = [str(exc)]
    else: checks['ASSESSMENT-INSTRUCTION'] = ['repair assessment definition before comparing public tables']
    return checks


def check_evidence_errors(package, spec, report):
    """Validate actual saved results and freshness without requiring a complete review."""
    from contract import package_path, sha256_file
    errors = []
    calibration_bindings = {b['runtime_check']: b for b in spec.get('verification', {}).get('calibration', [])}
    for check in report.get('checks', []):
        if check.get('status') != 'pass': continue
        proofs = check.get('artifacts')
        if not isinstance(proofs, list) or not proofs:
            errors.append(f"{check.get('name')}: pass needs preserved evidence artifacts with path/sha256")
        smoke_proofs = []
        structured_proofs = []
        for binding in proofs or []:
            try:
                p = package_path(package, binding['path'])
                if not p.is_relative_to(Path(package) / 'common_task/evaluation/checks/evidence'):
                    raise ValueError('evidence must be kept in private checks/evidence')
                if sha256_file(p) != binding['sha256']: raise ValueError('evidence hash mismatch')
                if p.suffix == '.json':
                    saved = json.loads(p.read_text())
                    if isinstance(saved, dict): structured_proofs.append(saved)
                    if isinstance(saved, dict) and saved.get('schema_version') == 'pipeline-scoring-smoke-result-v1':
                        smoke_proofs.append(saved)
            except (KeyError, OSError, ValueError, TypeError) as exc:
                errors.append(f"{check.get('name')}: invalid evidence binding: {exc}")
        smoke_check = spec.get('verification', {}).get('scoring_smoke', {}).get('runtime_check', 'scientific_scoring_smoke')
        if check.get('name') in calibration_bindings:
            from calibration_guard import evidence_errors
            binding = calibration_bindings[check['name']]
            candidates = []
            for saved in structured_proofs:
                if saved.get('schema_version') == 'scientific-calibration-guard-v1':
                    candidates.append(saved)
                elif saved.get('schema_version') == 'scientific-calibration-batch-v1':
                    if saved.get('status') != 'pass':
                        errors.append(check['name'] + ': calibration batch did not pass')
                    entries = saved.get('calibrations', [])
                    if not isinstance(entries, list) or not entries or any(not isinstance(r, dict) for r in entries):
                        errors.append(check['name'] + ': malformed calibration batch'); continue
                    names = [r.get('runtime_check') for r in entries]
                    if (any(not isinstance(n, str) for n in names) or len(set(names)) != len(names)
                            or any(not isinstance(r.get('result'), dict) or r['result'].get('status') != 'pass' for r in entries)):
                        errors.append(check['name'] + ': calibration batch has failed or duplicate entries')
                    matches = [r for r in entries if r.get('runtime_check') == check['name']]
                    for entry in matches:
                        if entry.get('config') != binding['config_path']:
                            errors.append(check['name'] + ': calibration evidence refers to another config')
                        candidates.append(entry.get('result'))
            if len(candidates) != 1:
                errors.append(check['name'] + ': pass needs exactly one matching structured calibration result')
            else:
                try:
                    errors.extend(check['name'] + ': ' + e for e in
                                  evidence_errors(package_path(package, binding['config_path']), candidates[0]))
                except (OSError, ValueError, KeyError, TypeError, SyntaxError) as exc:
                    errors.append(check['name'] + ': cannot validate calibration evidence: ' + str(exc))
        if check.get('name') == smoke_check:
            from scoring_smoke import implementation_hashes, outcome_errors, resource_evidence_errors, inspect_config, equivalence_errors, _file
            evaluation = Path(package) / 'common_task/evaluation'
            if len(smoke_proofs) != 1:
                errors.append('passed scoring smoke needs exactly one preserved structured report')
            else:
                saved = smoke_proofs[0]
                config, protocol_path = inspect_config(evaluation / 'scoring_smoke.json', require_rejection_reasons=True)
                formal = json.loads(protocol_path.read_text())
                reward_metric = formal.get('reward_metric')
                if (saved.get('status') != 'pass' or
                        saved.get('config_sha256') != sha256_file(evaluation / 'scoring_smoke.json') or
                        saved.get('protocol_sha256') != sha256_file(evaluation / 'protocol.json') or
                        saved.get('implementation_sha256') != implementation_hashes(evaluation)):
                    errors.append('scoring smoke report is failed or stale; rerun current implementation/config')
                tested = saved.get('cases', [])
                if not isinstance(tested, list) or any(not isinstance(c, dict) for c in tested):
                    errors.append('scoring smoke evidence has malformed cases')
                    tested = []
                if ({c.get('id') for c in tested} != {c['id'] for c in config['cases']} or
                        len(tested) != len(config['cases']) or any(c.get('status') != 'pass' for c in tested)):
                    errors.append('scoring smoke evidence does not cover all passing cases')
                for case in config['cases']:
                    row = next((r for r in tested if r.get('id') == case['id']), {})
                    if row.get('submission_sha256') != sha256_file(_file(evaluation, case['submission'])):
                        errors.append('scoring smoke submission changed after test: ' + case['id'])
                    event = row.get('event')
                    errors.extend(case['id'] + ': ' + message for message in
                                  outcome_errors(case, event if isinstance(event, dict) else {}, reward_metric=reward_metric))
                errors.extend(resource_evidence_errors(protocol_path, config, tested))
                errors.extend(equivalence_errors(config, formal, tested))
    return errors


def _archive_errors(package, archive, config, records):
    """Compare portable archived artifacts, never follow the worker's absolute paths."""
    from contract import package_path, sha256_file
    errors = []
    for index, case in enumerate(config['cases']):
        cid = case['id']
        row = next((r for r in records if isinstance(r, dict) and r.get('id') == cid), {})
        try:
            event = row['event']
            attempt = event['attempt_id']
            if not isinstance(attempt, str) or not re.fullmatch(r'attempt-[0-9a-f]{32}', attempt):
                raise ValueError('invalid score attempt identifier')
            case_path = archive.relative_to(package) / f'case-{index:03d}'
            def artifact(relative):
                return package_path(package, (case_path / relative).as_posix())
            def read(relative):
                return json.loads(artifact(relative).read_bytes())
            capture_path = Path('captures') / attempt
            if ([json.loads(line) for line in artifact('timeline.jsonl').read_text().splitlines()] != [event]
                    or event.get('event') != 'SCORE_ATTEMPT'):
                raise ValueError('timeline differs from the reported score event')
            capture = read(capture_path / 'capture.json')
            name = Path(case['submission']).name
            frozen_sha = sha256_file(artifact(capture_path / 'submission' / name))
            if (read('last_attempt.json') != {'attempt_id': attempt} or capture.get('attempt_id') != attempt
                    or capture.get('captured_at') != event.get('captured_at')
                    or capture.get('frozen_submission') != event.get('submission_path')
                    or not all(x == frozen_sha for x in (capture.get('submission_sha256'),
                               row.get('submission_sha256'), event.get('submission_sha256')))):
                raise ValueError('captured submission or attempt differs from the score event')
            if read(capture_path / 'evaluation/result.json') != event.get('response'):
                raise ValueError('evaluator result differs from the score event')
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            errors.append(f'{cid}: invalid archived scoring evidence ({error})')
    return errors


def _repair_smoke_commands(config_path, errors):
    from scoring_smoke import inspect_config
    runner = ['python', '/task/input/pipeline_tools/scoring_smoke.py']
    commands = []
    try:
        raw = json.loads(config_path.read_bytes())
        cases = [c for c in raw['cases'] if isinstance(c, dict) and isinstance(c.get('id'), str)]
        def rank(case):
            cid = case['id']
            implicated = any(e.startswith(cid + ':') or e == 'scoring smoke submission changed after test: ' + cid
                             for e in errors)
            return (not implicated, case.get('kind') == 'invalid')
        for case in sorted(cases, key=rank):
            cid = case['id']
            # Even a partial suite can replay an independently specified score or rejection.
            if not (case.get('expected_metrics') or 'expected_reward' in case or case.get('reward_bounds')
                    or case.get('metric_bounds') or case.get('expected_error_contains')):
                continue
            try:
                inspect_config(config_path, development_case=cid, require_rejection_reasons=True)
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                continue
            option = ['--case='+cid] if cid.startswith('-') else ['--case',cid]
            commands.append(shlex.join([*runner, *option]))
            break
        for gid in raw.get('equivalence_groups', {}):
            if isinstance(gid,str) and any(e.startswith(gid + '/') for e in errors):
                inspect_config(config_path, development_group=gid)
                option = ['--equivalence-group='+gid] if gid.startswith('-') else ['--equivalence-group',gid]
                commands.append(shlex.join([*runner, *option]))
                break
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    return [*commands, shlex.join(runner)]


def construction_smoke_check(package, spec):
    """Require a current complete authoring smoke archive at the first 01 handoff."""
    from contract import package_path, sha256_file
    from scoring_smoke import inspect_config
    package = Path(package).resolve()
    check = {'id': 'AUTHORING-SCORING-EVIDENCE', 'status': 'fail'}
    config_path = None
    errors = ['Missing complete scoring smoke archive from the construction worker.']
    try:
        config_path = package_path(package, 'common_task/evaluation/scoring_smoke.json')
        config, _ = inspect_config(config_path, require_rejection_reasons=True)
        binding = spec['verification']['scoring_smoke']
        evidence = package_path(package, 'common_task/evaluation/checks/evidence')
        # Prior retries may be retained. Only a verified current archive can pass.
        candidates = sorted(evidence.glob('*/report.json'),
                            key=lambda p: p.lstat().st_mtime_ns, reverse=True)
        for candidate in candidates:
            try:
                relative = candidate.relative_to(package).as_posix()
                path = package_path(package, relative)
                saved = json.loads(path.read_bytes())
                if saved.get('schema_version') != 'pipeline-scoring-smoke-result-v1':
                    if path.parent.name.startswith('scoring-smoke-') and saved.get('error'):
                        raise ValueError(str(saved['error']))
                    continue
                proof = {'path': relative, 'sha256': sha256_file(path)}
                check_row = {'name': binding['runtime_check'], 'status': 'pass', 'artifacts': [proof]}
                problems = check_evidence_errors(package, spec, {'checks': [check_row]})
                if saved.get('resource_mode') != 'authoring':
                    problems.append('Missing authoring fixture run.')
                problems.extend(_archive_errors(package, path.parent, config, saved.get('cases', [])))
                if not problems:
                    return dict(check, status='pass', reason='Current fixture scores, captures and timelines verified.',
                                artifact=proof)
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                problems = [f'Invalid scoring smoke archive: {error}']
            if 'artifact_path' not in check:
                check['artifact_path'] = candidate.relative_to(package).as_posix()
                errors = problems
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        errors = [f'Cannot check complete scoring smoke: {error}']
    # IDs are taken only from the public-to-author, task-authored development controls.
    check['repair_commands'] = _repair_smoke_commands(config_path, errors)
    check['reason'] = '\n'.join([*errors, 'Continue from the saved draft. After repairing the fixtures/evaluator, run:',
                                 *check['repair_commands']])
    return check


def findings_errors(package, spec, report):
    """Require each recorded finding to reach an explicit, evidence-bound state."""
    from contract import package_path, sha256_file
    errors = check_evidence_errors(package, spec, report)
    findings = report.get('findings')
    if not isinstance(findings, list): return ['review_report.findings must be an array (empty only when no issues found)']
    seen = set()
    for item in findings:
        if not isinstance(item, dict): errors.append('finding must be an object'); continue
        rid = item.get('id')
        if not isinstance(rid, str) or not rid or rid in seen: errors.append('findings need unique IDs')
        seen.add(str(rid))
        for field in ('issue', 'evidence', 'acceptance_condition'):
            if not isinstance(item.get(field), str) or not item[field].strip(): errors.append(f'{rid}: missing {field}')
        status = item.get('status')
        if status not in {'open', 'fixed', 'refuted', 'execution_pending'}: errors.append(f'{rid}: unsupported finding status')
        if status == 'open' and report.get('status') == 'complete': errors.append(f'{rid}: open finding prevents complete')
        if status == 'execution_pending' and (item.get('category') != 'execution' or not item.get('completion_condition')):
            errors.append(f'{rid}: only execution gaps may be deferred with completion_condition')
        if status == 'refuted' and not item.get('resolution'): errors.append(f'{rid}: refutation needs evidence in resolution')
        if status == 'fixed':
            if not item.get('resolution') or not item.get('retest_checks'): errors.append(f'{rid}: fixed finding needs resolution and retest_checks')
            known = {c.get('name'): c for c in report.get('checks', [])}
            for name in item.get('retest_checks', []):
                if name not in known or known[name].get('status') != 'pass': errors.append(f'{rid}: retest {name} has not passed')
            if not item.get('files'): errors.append(f'{rid}: fixed finding needs final file bindings')
            for binding in item.get('files', []):
                try:
                    p = package_path(package, binding['path'])
                    if sha256_file(p) != binding['sha256']: errors.append(f'{rid}: modified after retest: {binding["path"]}')
                except (KeyError, OSError, ValueError, TypeError): errors.append(f'{rid}: invalid finding file binding')
    protocol = json.loads((Path(package) / 'common_task/evaluation/protocol.json').read_text())
    declared = {r['id']: r for r in protocol['assessment']['requirements']}
    reviewed = {r.get('id'): r for r in report.get('scientific_requirements', [])}
    if declared.keys() != reviewed.keys(): errors.append('review requirements must exactly cover protocol.assessment.requirements')
    for rid, req in declared.items():
        row = reviewed.get(rid, {})
        if req['role'] == 'trajectory' and row.get('kind') != 'trajectory_review': errors.append(f'{rid}: trajectory evidence cannot be certified by output tests')
        if req['role'] != 'trajectory' and row.get('kind') != 'program': errors.append(f'{rid}: output requirement needs executable checks')
        if req['role'] == 'validity' and row.get('invalid_applicability', {}).get('applicable') is False:
            errors.append(f'{rid}: a validity rule must have an invalid control')
    smoke_path = Path(package) / 'common_task/evaluation/scoring_smoke.json'
    smoke = json.loads(smoke_path.read_text())
    cases = {c['id']: c for c in smoke['cases']}
    for rid, row in reviewed.items():
        if row.get('kind') != 'program': continue
        verified_cases = []
        for control in row.get('controls', []):
            case = cases.get(control.get('case'))
            if not case or rid not in case.get('requirement_ids', []):
                errors.append(f'{rid}: control must reference an actual smoke case covering this requirement')
            elif case['kind'] != control.get('kind'):
                errors.append(f'{rid}: control kind differs from executed case')
            else:
                verified_cases.append(case)
        if spec.get('scientific_acceptance_version') == 3 and declared.get(rid, {}).get('role') == 'quality':
            from scoring_smoke import quality_control_errors
            errors.extend(quality_control_errors(declared[rid], verified_cases, protocol['reward_metric'],
                          require_contrast=row.get('weak_valid_applicability', {}).get('applicable') is True))
    return errors


def history_errors(previous_package, report):
    """Check against the frozen input, so retry output cannot erase discoveries."""
    if previous_package is None: return []
    path = Path(previous_package) / 'review_report.json'
    if not path.is_file(): return []
    try:
        previous = json.loads(path.read_text())
        current = {f['id']: f for f in report.get('findings', [])}
        errors = []
        for finding in previous.get('findings', []):
            rid = finding['id']
            if rid not in current:
                errors.append(f'{rid}: finding from previous review was deleted'); continue
            for key in ('issue', 'evidence', 'acceptance_condition'):
                if current[rid].get(key) != finding.get(key):
                    errors.append(f'{rid}: preserve original {key}; explain corrections in resolution')
        return errors
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return ['cannot compare review history: ' + str(exc)]
