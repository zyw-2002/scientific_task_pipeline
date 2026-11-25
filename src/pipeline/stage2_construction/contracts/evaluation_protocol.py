"""Shared evaluator ABI and explicit single/grouped scientific aggregation.

Task adapters implement measure(path, context) -> nonempty numeric metric dict.
The framework owns result status and aggregation, not scientific interpretation.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import csv
import fnmatch
import importlib.util
import json
import math
from pathlib import Path, PurePosixPath
import statistics
import sys
import tempfile
import zipfile


class InvalidSubmission(ValueError):
    """Task adapters raise this only for actual submission validation failures."""


def safe_path(value, *, pattern=False):
    if not isinstance(value, str) or not value or '\\' in value:
        return False
    if value == '{submission}':
        return not pattern
    path = PurePosixPath(value)
    return not path.is_absolute() and '..' not in path.parts and str(path) != '.' and (pattern or not any(c in value for c in '*?['))


def declaration_errors(artifacts):
    if not isinstance(artifacts, list) or not artifacts:
        return ['required_artifacts must be a nonempty array']
    errors, ids = [], set()
    for item in artifacts:
        if not isinstance(item, dict):
            errors.append('artifact declaration must be an object')
            continue
        aid = item.get('id')
        if not isinstance(aid, str) or not aid or aid in ids:
            errors.append('required artifacts need unique nonempty IDs')
        ids.add(str(aid))
        paths, pattern = item.get('paths'), item.get('pattern')
        if (paths is None) == (pattern is None):
            errors.append(f'{aid}: declare exactly one of paths or pattern; prose does not bind files')
        elif paths is not None and (not isinstance(paths, list) or not paths or
                any(not safe_path(p) for p in paths) or len(paths) != len(set(paths))):
            errors.append(f'{aid}: paths must name distinct safe submission members')
        elif pattern is not None and not safe_path(pattern, pattern=True):
            errors.append(f'{aid}: pattern must stay inside submission')
        count = item.get('count')
        if count is not None and (type(count) is not int or count < 1):
            errors.append(f'{aid}: count must be a positive integer')
        elif paths is not None and isinstance(paths, list) and count is not None and len(paths) != count:
            errors.append(f'{aid}: number of paths differs from declared count')
        if type(item.get('min_bytes', 1)) is not int or item.get('min_bytes', 1) < 0:
            errors.append(f'{aid}: min_bytes must be a nonnegative integer')
        for key in ('columns', 'json_required_keys'):
            if key in item and (not isinstance(item[key], list) or not all(isinstance(v,str) for v in item[key])):
                errors.append(f'{aid}: {key} must be an array of strings')
        if 'row_count' in item and (type(item['row_count']) is not int or item['row_count'] < 0):
            errors.append(f'{aid}: row_count must be a nonnegative integer')
        if any(k in item for k in ('columns', 'row_count')) and item.get('format') not in {'csv', 'tsv', 'text'}:
            errors.append(f'{aid}: columns/row_count require csv, tsv or text; unsupported constraints cannot be ignored')
        if 'json_required_keys' in item and item.get('format') != 'json':
            errors.append(f'{aid}: json_required_keys requires JSON members, not the outer archive')
        id_binding = item.get('id_set')
        if id_binding is not None:
            if not isinstance(id_binding, dict) or not all(isinstance(id_binding.get(k), str) and id_binding[k] for k in ('resource', 'column')):
                errors.append(f'{aid}: id_set needs resource and reference column')
            elif item.get('format') in {'csv', 'tsv'}:
                if not isinstance(item.get('id_column'), str): errors.append(f'{aid}: CSV ID matching needs id_column')
            elif pattern is not None:
                import re
                try:
                    rx = re.compile(item.get('member_id_pattern', ''))
                    if 'id' not in rx.groupindex: raise ValueError('named id group required')
                except (ValueError, re.error): errors.append(f'{aid}: archive ID matching needs member_id_pattern with named id group')
            else: errors.append(f'{aid}: id_set is supported for CSV rows or archive member patterns')
    return errors


def bind_submission(config, spec):
    """Bind the editable execution contract to the same protocol used for grading."""
    artifacts = spec.get('submission', {}).get('required_artifacts')
    errors = declaration_errors(artifacts)
    if 'required_files' in config:
        previous = config['required_files']
        if not isinstance(previous, list) or any(not isinstance(v, str) or not v for v in previous):
            errors.append('required_files must be a path array before migration')
        elif not errors:
            for name in previous:
                if not any(name in item.get('paths', []) for item in artifacts):
                    errors.append('Declare former required_files entry in submission.required_artifacts: ' + name)
    if errors:
        raise ValueError('; '.join(errors))
    config.pop('required_files', None)
    config['required_artifacts'] = copy.deepcopy(artifacts)


def _unique(pairs):
    result = {}
    for k,v in pairs:
        if k in result:
            raise ValueError('duplicate JSON key: ' + k)
        result[k] = v
    return result


def validate_artifacts(artifacts, locate, names):
    """Validate declared members before any task metric is calculated."""
    errors = declaration_errors(artifacts)
    if errors:
        raise RuntimeError('; '.join(errors))
    checks = []
    for item in artifacts:
        aid = item['id']
        required = item.get('required', True)
        declared_paths = item.get('paths')
        if declared_paths is None:
            paths = sorted(name for name in names if fnmatch.fnmatchcase(name, item['pattern']))
        elif required:
            paths = list(declared_paths)
        else:
            # Optional groups validate any members that were submitted. An absent
            # optional group is recorded and does not invalidate the submission.
            paths = [name for name in declared_paths if name in names]
        if not paths:
            if not required:
                checks.append({'id': aid, 'status': 'optional_missing', 'members': [],
                               'scope': 'declared_structure_only'})
                continue
            raise ValueError(f'{aid}: required artifact count mismatch (found 0, expected {item.get("count", "at least one")})')
        if item.get('count') is not None and len(paths) != item['count']:
            if not required and declared_paths is not None and len(paths) < len(declared_paths):
                # A partially supplied optional group remains optional. Validate
                # the members present and make the omission explicit in evidence.
                pass
            else:
                raise ValueError(f'{aid}: required artifact count mismatch (found {len(paths)}, expected {item.get("count", "at least one")})')
        if item.get('id_set') and '_expected_ids' not in item:
            raise RuntimeError(f'{aid}: reference IDs have not been resolved')
        actual_ids = []
        if item.get('member_id_pattern'):
            import re
            for name in paths:
                match = re.fullmatch(item['member_id_pattern'], name)
                if not match: raise ValueError(f'{aid}: member does not match ID pattern: {name}')
                actual_ids.append(match['id'])
        for name in paths:
            path = locate(name)
            if path.stat().st_size < item.get('min_bytes', 1):
                raise ValueError(f'{aid}: empty or truncated artifact: {name}')
            try:
                if item.get('format') == 'json':
                    content = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_unique,
                        parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite JSON number')))
                    if item.get('json_required_keys') and (not isinstance(content,dict) or set(item['json_required_keys']) - content.keys()):
                        raise ValueError('missing required JSON keys')
                elif item.get('format') in {'csv', 'tsv'}:
                    with path.open(encoding='utf-8',newline='') as stream:
                        reader = csv.reader(stream, delimiter='\t' if item['format'] == 'tsv' else ',')
                        header = next(reader, None)
                        if not header or len(header) != len(set(header)):
                            raise ValueError('missing or duplicate CSV columns')
                        if item.get('columns') is not None and header != item['columns']:
                            raise ValueError('CSV columns differ from declaration')
                        count = 0
                        id_index = header.index(item['id_column']) if item.get('id_column') else None
                        for row in reader:
                            if len(row) != len(header):
                                raise ValueError('ragged CSV row')
                            count += 1
                            if id_index is not None: actual_ids.append(row[id_index])
                        if item.get('row_count') is not None and count != item['row_count']:
                            raise ValueError('CSV row count differs from declaration')
                elif item.get('format') == 'text':
                    rows = [line.split() for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
                    if 'row_count' in item and len(rows) != item['row_count']:
                        raise ValueError('text row count differs from declaration')
                    if 'columns' in item and any(len(row) != len(item['columns']) for row in rows):
                        raise ValueError('text field count differs from declaration')
            except (ValueError, UnicodeError) as exc:
                raise ValueError(f'{aid}: invalid {name}: {exc}') from exc
        if item.get('id_column') or item.get('member_id_pattern'):
            if any(not value for value in actual_ids) or len(actual_ids) != len(set(actual_ids)):
                raise ValueError(f'{aid}: missing or duplicate IDs')
            if '_expected_ids' in item and set(actual_ids) != set(item['_expected_ids']):
                raise ValueError(f'{aid}: submission IDs differ from required reference set')
        checks.append({'id':aid,'status':'pass','members':paths,'scope':'declared_structure_only'})
    return checks


def config_errors(config):
    errors = []
    if not isinstance(config, dict):
        return ['evaluation protocol must be an object']
    units = config.get('units')
    if not isinstance(units, list) or not units:
        return ['evaluation protocol needs explicit units']
    seen = set()
    for unit in units:
        if not isinstance(unit, dict) or not isinstance(unit.get('id'), str) or not unit['id'] or unit['id'] in seen:
            errors.append('evaluation units need unique nonempty IDs')
            continue
        seen.add(unit['id'])
        if not isinstance(unit.get('path'), str) or not unit['path']:
            errors.append('each evaluation unit needs its actual submission member path')
    if 'required_files' in config:
        errors.append('required_files is obsolete; declare files in submission.required_artifacts')
    for field in ('aggregations',):
        if not isinstance(config.get(field), list):
            errors.append(field + ' must be an explicit array')
    if not isinstance(config.get('context', {}), dict):
        errors.append('context must be an object')
    output_keys = set()
    covered_units = set()
    for aggregation in config.get('aggregations', []) if isinstance(config.get('aggregations'), list) else []:
        if not isinstance(aggregation, dict) or aggregation.get('operation') not in {'identity', 'mean', 'std', 'sum', 'min', 'max'}:
            errors.append('declare a supported aggregation operation or implement a whole-submission measurement unit')
            continue
        if 'weights' in aggregation:
            # Aggregation operations intentionally have one defined meaning
            # each.  The executor computes ``mean`` with equal unit weights.
            # A task-specific weighted reward belongs in reward_components,
            # where it is actually applied after the named aggregation
            # outputs are computed.  Rejecting this unsupported field during
            # construction prevents a runtime evaluator failure or a silently
            # ignored scientific weighting declaration.
            errors.append(f"{aggregation.get('output', '<unnamed>')}: aggregation weights are unsupported; use reward_components")
        if not all(isinstance(aggregation.get(k), str) and aggregation[k] for k in ('metric', 'output')):
            errors.append('aggregation needs metric and output names')
            continue
        if aggregation['output'] in output_keys:
            errors.append('aggregation output names must be unique')
        output_keys.add(aggregation['output'])
        selected = aggregation.get('unit_ids', list(seen))
        if (not isinstance(selected, list) or not selected or any(not isinstance(uid, str) or uid not in seen for uid in selected)
                or len(set(selected)) != len(selected)):
            errors.append(f"{aggregation['output']}: unit_ids must list distinct declared evaluation units")
            continue
        covered_units.update(selected)
        if aggregation['operation'] == 'identity' and len(selected) != 1:
            errors.append('identity aggregation requires one unit')
        if aggregation['operation'] == 'std' and (type(aggregation.get('ddof')) is not int or not 0 <= aggregation['ddof'] < len(selected)):
            errors.append('standard deviation requires explicit valid ddof')
    if seen - covered_units:
        errors.append('evaluation units missing from aggregations: ' + ', '.join(sorted(seen - covered_units)))
    components = config.get('reward_components')
    if components is not None:
        if (not isinstance(components, dict) or not components or
                any(k not in output_keys or type(v) not in (int, float) or not math.isfinite(v) for k, v in components.items())
                or not any(v != 0 for v in components.values())):
            errors.append('reward_components must map aggregation outputs to finite weights, at least one nonzero')
        if not isinstance(config.get('reward_metric'), str) or config['reward_metric'] in output_keys:
            errors.append('composite reward_metric must be a new distinct metric name')
    elif config.get('reward_metric') not in output_keys:
        errors.append('reward_metric must name an actual aggregation output')
    if 'required_artifacts' in config:
        artifacts = config['required_artifacts']
        errors.extend(declaration_errors(artifacts))
        if not errors:
            paths = {unit['path'] for unit in units}
            for artifact in artifacts:
                paths.update(artifact.get('paths', []))
            members = {PurePosixPath(path).as_posix() for path in paths if path != '{submission}'}
            for artifact in artifacts:
                if 'pattern' in artifact and 'count' in artifact:
                    matches = sorted(path for path in members if fnmatch.fnmatchcase(path, artifact['pattern']))
                    if len(matches) > artifact['count']:
                        examples = ', '.join(matches[:5]) + (', ...' if len(matches) > 5 else '')
                        errors.append(f"{artifact['id']}: pattern {artifact['pattern']!r} requires exactly "
                                      f"{artifact['count']} members, but scoring units and required paths "
                                      f"already need {len(matches)} distinct matches: {examples}")
    return errors


def member(root, name):
    if not isinstance(name, str) or not name or '\\' in name:
        raise InvalidSubmission('invalid submission member path')
    relative = PurePosixPath(name)
    if relative.is_absolute() or '..' in relative.parts:
        raise InvalidSubmission('submission member escapes its root')
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink() or not path.is_file():
        raise InvalidSubmission('missing or invalid required submission member: ' + name)
    return path


def measure_units(config, locate, measure):
    """Call the task measurement separately for every declared unit, then aggregate."""
    errors = config_errors(config)
    if errors:
        raise RuntimeError('; '.join(errors))
    results = []
    expected_keys = None
    scoped = any('unit_ids' in operation for operation in config['aggregations'])
    for unit in config['units']:
        values = measure(locate(unit['path']), {**config.get('context', {}), 'unit_id': unit['id']})
        if not isinstance(values, dict) or not values or any(
                not isinstance(k, str) or type(v) not in (int, float) or not math.isfinite(v) for k, v in values.items()):
            raise RuntimeError('task measure must return finite numeric metrics, without status or reward fields')
        if expected_keys is None:
            expected_keys = set(values)
        if not scoped and set(values) != expected_keys:
            raise RuntimeError('evaluation units returned inconsistent metric names')
        results.append({'id': unit['id'], 'metrics': values})
    aggregated = {}
    by_id = {row['id']: row['metrics'] for row in results}
    for operation in config['aggregations']:
        values = []
        for uid in operation.get('unit_ids', by_id):
            if operation['metric'] not in by_id[uid]:
                raise RuntimeError(f"metric adapter omitted a declared aggregate metric {operation['metric']} for unit {uid}")
            values.append(by_id[uid][operation['metric']])
        name = operation['operation']
        if name == 'std':
            mean = statistics.mean(values)
            value = math.sqrt(sum((x - mean) ** 2 for x in values) / (len(values) - operation['ddof']))
        else:
            value = {'identity': lambda xs: xs[0], 'mean': statistics.mean,
                     'sum': sum, 'min': min, 'max': max}[name](values)
        if not math.isfinite(value):
            raise RuntimeError('aggregation produced nonfinite output')
        aggregated[operation['output']] = value
    if config.get('reward_components'):
        reward = sum(aggregated[k] * weight for k, weight in config['reward_components'].items())
        if not math.isfinite(reward): raise RuntimeError('composite reward is nonfinite')
        aggregated[config['reward_metric']] = reward
    return {'status': 'success', 'metrics': aggregated,
            'reward': aggregated[config['reward_metric']], 'unit_results': results}


def evaluate(submission, config, measure):
    if not submission.is_file():
        return {'status': 'missing_submission', 'metrics': None, 'error': 'Submission is missing'}
    errors = config_errors(config)
    if errors:
        raise RuntimeError('; '.join(errors))
    try:
        with tempfile.TemporaryDirectory(prefix='scientific-eval-') as temporary:
            root = Path(temporary)
            archive = zipfile.is_zipfile(submission)
            if archive:
                with zipfile.ZipFile(submission) as bundle:
                    names = bundle.namelist()
                    if len(names) != len(set(names)):
                        raise InvalidSubmission('duplicate archive members')
                    for info in bundle.infolist():
                        name = PurePosixPath(info.filename)
                        if name.is_absolute() or '..' in name.parts or '\\' in info.filename or (info.external_attr >> 16) & 0o170000 == 0o120000:
                            raise InvalidSubmission('unsafe archive member')
                    bundle.extractall(root)
            def locate(name):
                if name == '{submission}':
                    return submission
                if not archive:
                    raise InvalidSubmission('member-based protocol requires an archive')
                return member(root, name)
            artifact_checks = None
            if 'required_artifacts' in config:
                names = [p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()] if archive else ['{submission}']
                try:
                    artifact_checks = validate_artifacts(config['required_artifacts'], locate, names)
                except ValueError as error:
                    raise InvalidSubmission(str(error)) from error
            result = measure_units(config, locate, measure)
            if artifact_checks is not None:
                result['artifact_checks'] = artifact_checks
            return result
    except (InvalidSubmission, zipfile.BadZipFile) as error:
        return {'status': 'invalid_submission', 'metrics': None, 'error': str(error)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--resource-mode', choices=['runtime', 'authoring'], default='runtime')
    parser.add_argument('--test-resources', type=Path, help='Trusted smoke configuration only; never accepted from a submission')
    parser.add_argument('--bind-draft', action='store_true',
                        help='Bind the editable execution.json for a selected authoring fixture before packaging')
    args = parser.parse_args()
    try:
        config_bytes = args.config.read_bytes()
        protocol_sha256 = hashlib.sha256(config_bytes).hexdigest()
        config = json.loads(config_bytes)
        if args.bind_draft:
            if args.resource_mode != 'authoring' or not args.test_resources:
                raise ValueError('draft bindings require authoring mode and a fixture configuration')
            config_path = args.config.resolve()
            package = config_path.parents[2]
            if config_path != package / 'common_task/evaluation/protocol.json':
                raise ValueError('draft bindings require common_task/evaluation/protocol.json')
            execution_path = package / 'execution.json'
            execution_bytes = execution_path.read_bytes()
            execution = json.loads(execution_bytes)
            binding = execution.get('verification', {}).get('score_evidence', {}).get('evaluation_protocol', {})
            if PurePosixPath(binding.get('config_path', 'common_task/evaluation/protocol.json')) != PurePosixPath('common_task/evaluation/protocol.json'):
                raise ValueError('draft protocol differs from execution.json evaluation_protocol.config_path')
            if binding.get('config_container', '/task/evaluation/protocol.json') != '/task/evaluation/protocol.json':
                raise ValueError('execution.json evaluation_protocol.config_container must be /task/evaluation/protocol.json')
            bind_submission(config, execution)
            if execution.get('assessment_contract_version') == 1:
                from pipeline.stage2_construction.contracts.resource_contract import bind_protocol_resources
                bind_protocol_resources(config, execution, 'common_task/evaluation', '/task/evaluation')
        errors = config_errors(config)
        if errors:
            raise RuntimeError('; '.join(errors))
        resource_evidence = None
        if 'resources' in config:
            from pipeline.stage2_construction.contracts.resource_contract import resolve
            swaps = None
            if args.test_resources:
                smoke = json.loads(args.test_resources.read_text())
                swaps = smoke.get('resource_bindings', {})
            config, resource_evidence = resolve(config, args.config, mode=args.resource_mode, test_bindings=swaps)
        adapter = (args.config.parent / config['adapter']).resolve()
        if not adapter.is_relative_to(args.config.parent.resolve()):
            raise RuntimeError('adapter must be inside the evaluator directory')
        # The early authoring command runs this same framework from pipeline_tools.
        # Expose the task's sibling metric modules as in the packaged evaluator.
        if str(args.config.parent.resolve()) not in sys.path:
            sys.path.insert(1, str(args.config.parent.resolve()))
        spec = importlib.util.spec_from_file_location('task_metric_adapter', adapter)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = evaluate(args.input, config, module.measure)
        if args.bind_draft:
            if execution_path.read_bytes() != execution_bytes:
                raise RuntimeError('draft execution contract changed during evaluation')
            result['draft_execution_sha256'] = hashlib.sha256(execution_bytes).hexdigest()
        if args.config.read_bytes() != config_bytes:
            raise RuntimeError('scoring protocol changed during evaluation')
        result['protocol_sha256'] = protocol_sha256
        result['resource_evidence'] = resource_evidence if resource_evidence is not None else {
            'mode': args.resource_mode, 'scope': 'fixture' if args.test_resources else 'formal',
            'resources': {}, 'instance_sizes': []}
        if result['status'] == 'success':
            operators = {'lt': lambda x,y: x < y, 'le': lambda x,y: x <= y,
                         'gt': lambda x,y: x > y, 'ge': lambda x,y: x >= y}
            result['performance_bands'] = {b['label']: operators[b['operator']](result['metrics'][b['metric']], b['value'])
                for b in config.get('assessment', {}).get('performance_bands', [])}
    except Exception as error:
        result = {'status': 'runtime_error', 'metrics': None, 'error': str(error)}
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'result.json').write_text(json.dumps(result, allow_nan=False, indent=2) + '\n')
    return 0 if result['status'] == 'success' else 1


if __name__ == '__main__':
    sys.modules['evaluation_protocol'] = sys.modules[__name__]
    raise SystemExit(main())
