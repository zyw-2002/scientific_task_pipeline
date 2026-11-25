#!/usr/bin/env python3
"""Replay authored controls through submission capture, protocol and score events.

Run inside an isolated authoring/evaluator container with its required inputs.
inspect_config is static and safe for host preflight; run_smoke executes task code
and is NEVER invoked automatically by host construction/review checks.
Fixtures and expected values are scientific authoring responsibilities. Passing
these controls does not certify their scientific independence or data identity.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import sys
import tempfile
import zipfile


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


# These are byte-identical framework runners from previous published tool versions.
_PREVIOUS_FRAMEWORK_RUNNERS = {
    '1e8b9b2e59f62f5a6364c7bc2aa64f90cca074867d41e1ee0a1ae1529b28791c',
    '05ceeea2317f67a1140cf9c48e0a68145b652353f1776d38e9add1972baa5c1c',
    'bcd18350c285da5b544162b3c16af8f9f55e30f720c6042d22e6e358f53f4636',
}


def authoring_metric_adapter(protocol_path, config):
    """Resolve the explicitly selected metric and preserve conflicting authored code."""
    evaluation = protocol_path.parent.resolve()
    runner = evaluation/'evaluation_protocol.py'
    installed = Path(__file__).with_name('evaluation_protocol.py')
    errors = []
    if runner.exists() and (runner.is_symlink() or not runner.is_file() or
            (sha256(runner) not in _PREVIOUS_FRAMEWORK_RUNNERS and runner.read_bytes()!=installed.read_bytes())):
        errors.append('evaluation_protocol.py conflicts with the framework runner. Move it to an unused name '
                      'and call its scientific logic from your adapter before rerunning preflight.py.')
    relative = config.get('adapter')
    if not isinstance(relative,str) or not relative.strip():
        hint = 'Set protocol.json "adapter" to the relative Python module implementing measure(path, context).'
        if (evaluation/'metric_adapter.py').is_file():
            hint = 'In protocol.json set "adapter": "metric_adapter.py" if its measure(path, context) implements your metric.'
        errors.insert(0,hint)
    else:
        try:
            adapter = _file(evaluation,relative)
            if adapter.suffix!='.py' or adapter.resolve()==runner:
                raise ValueError('choose a Python module distinct from the framework evaluation_protocol.py')
        except ValueError as exc:
            errors.insert(0,f'Invalid protocol.json adapter {relative!r}. Point it to your measure(path, context) module. {exc}')
    if errors:
        raise ValueError(' '.join(errors))
    return adapter.resolve()


def _candidate_roots(root, relative):
    """Yield the config directory and an enclosing package root when present."""
    root = Path(root).resolve()
    yield root / relative, root
    # Authoring files are package-relative in execution.json, while smoke
    # cases are documented relative to scoring_smoke.json.  Accept the former
    # when running host preflight, retaining the same containment checks.
    parts = PurePosixPath(relative).parts
    if parts[:2] == ('common_task', 'evaluation') and len(parts) > 2:
        # In the evaluator container common_task/evaluation is mounted as the
        # config directory itself.  Resolve this spelling without requiring
        # the private execution.json to be mounted there.
        yield root.joinpath(*parts[2:]), root
    if parts[:1] == ('common_task',):
        for parent in (root, *root.parents):
            if (parent / 'execution.json').is_file():
                yield parent / relative, parent
                break


def _file(root, relative):
    if (not isinstance(relative, str) or not relative or '\\' in relative
            or Path(relative).is_absolute() or '..' in Path(relative).parts):
        raise ValueError('smoke paths must be relative to the configuration directory')
    root = Path(root).resolve()
    selected = next(((candidate, base) for candidate, base in _candidate_roots(root, relative)
                     if candidate.resolve().is_relative_to(base) and candidate.is_file()), None)
    if selected is None:
        raise ValueError('missing or escaping smoke file: ' + relative)
    path, root = selected
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('smoke files cannot use symlinks: ' + relative)
    return path


def _fixture_submission_errors(path: Path, protocol: dict, formal: dict) -> list[str]:
    """Check that a smoke fixture has the same submission container shape as the protocol."""
    units = protocol.get('units', [])
    member_paths = [u.get('path') for u in units if isinstance(u, dict) and isinstance(u.get('path'), str)]
    artifacts = formal.get('required_artifacts', [])
    required = {name for item in artifacts if isinstance(item, dict) and item.get('required', True)
                for name in item.get('paths', []) if isinstance(name, str) and name != '{submission}'}
    archive_artifact = any(
        isinstance(item, dict) and (
            item.get('format') in {'zip', 'archive'}
            or isinstance(item.get('members'), list)
            or isinstance(item.get('pattern'), str)
            or len([name for name in item.get('paths', [])
                    if isinstance(name, str) and name != '{submission}']) > 1)
        for item in artifacts)
    needs_archive = any(p != '{submission}' for p in member_paths) or bool(required) or archive_artifact
    if not needs_archive:
        return []
    if not zipfile.is_zipfile(path):
        return ['submission fixture must be a ZIP containing every protocol unit member']
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
    except (OSError, zipfile.BadZipFile) as exc:
        return ['cannot inspect submission fixture archive: ' + str(exc)]
    missing = sorted({member for member in member_paths if member != '{submission}'} - names)
    if missing:
        return ['submission fixture is missing protocol unit members: ' + ', '.join(missing)]
    missing_artifacts = sorted(required - names)
    if missing_artifacts:
        return ['submission fixture is missing required artifact members: ' + ', '.join(missing_artifacts)]
    return []


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _metric_interval(case, metric, reward_metric):
    """Return the values this control would accept for the named metric."""
    exact = case.get('expected_metrics', {})
    bounds = case.get('metric_bounds', {})
    values = [exact[metric]] if metric in exact else []
    limits = [bounds[metric]] if metric in bounds else []
    if metric == reward_metric:
        if 'expected_reward' in case:
            values.append(case['expected_reward'])
        if 'reward_bounds' in case:
            limits.append(case['reward_bounds'])
    if not values and not limits:
        return None
    tolerance = case.get('tolerance', 1e-9)
    lower = max([v - tolerance for v in values] + [b.get('min', -math.inf) for b in limits])
    upper = min([v + tolerance for v in values] + [b.get('max', math.inf) for b in limits])
    if lower > upper:
        raise ValueError(f"{case['id']}: conflicting expectations for metric {metric}")
    return lower, upper


def quality_control_errors(requirement, cases, reward_metric, *, require_contrast=False):
    """Check whether controls could detect a constant implementation of this objective."""
    rid, metric = requirement['id'], requirement['metric']
    correct, valid = [], []
    for case in cases:
        if rid not in case.get('requirement_ids', []) or case['kind'] == 'invalid':
            continue
        interval = _metric_interval(case, metric, reward_metric)
        if interval is not None:
            valid.append(interval)
            if case['kind'] == 'correct':
                correct.append(interval)
        if case['kind'] == 'weak_valid':
            require_contrast = True
    if not correct:
        return [f'{rid}: a correct scoring control must assert the value or bounds of its own metric {metric}']
    if require_contrast and max(lo for lo, hi in valid) <= min(hi for lo, hi in valid):
        return [f'{rid}: all controls admit a constant {metric}; supply two scientifically distinct '
                'valid submissions with non-overlapping expectations for this metric']
    return []


def equivalence_groups(protocol, config, root):
    """Bind each permitted representation of the same result to a distinct submission."""
    declared = protocol.get('assessment', {}).get('submission_representations', {})
    groups = config.get('equivalence_groups', {})
    if not isinstance(declared, dict) or any(not isinstance(i, str) or not i.strip()
            or not isinstance(v, str) or not v.strip() for i, v in declared.items()):
        raise ValueError('assessment.submission_representations must map IDs to public descriptions')
    if not isinstance(groups, dict):
        raise ValueError('equivalence_groups must map group IDs to paired cases')
    if len(declared) < 2:
        if groups:
            raise ValueError('equivalence_groups need at least two declared submission representations')
        return {}
    if not groups:
        raise ValueError('every permitted submission representation needs a paired scoring control')
    cases = {c['id']: c for c in config['cases']}
    for name, group in groups.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(group, dict):
            raise ValueError('invalid equivalence group ID or definition')
        if not isinstance(group.get('oracle'), str) or not group['oracle'].strip():
            raise ValueError(f'{name}: explain why the fixtures encode the same scientific result')
        bindings = group.get('cases')
        if not isinstance(bindings, dict) or bindings.keys() != declared.keys():
            raise ValueError(f'{name}: pair one case for each declared submission representation')
        contents = set()
        anchored = False
        for label, cid in bindings.items():
            c = cases.get(cid) if isinstance(cid, str) else None
            if not c or c.get('expected_status') != 'success':
                raise ValueError(f'{name}/{label}: a valid scoring case is required')
            digest = sha256(_file(root, c.get('submission')))
            if digest in contents:
                raise ValueError(f'{name}: representations must use different submission bytes')
            contents.add(digest)
            anchored |= 'expected_reward' in c or protocol['reward_metric'] in c.get('expected_metrics', {})
        if not anchored:
            raise ValueError(f'{name}: independently specify the expected reward on at least one case')
    return groups


def equivalence_errors(config, protocol, tested):
    """Recompute equivalence from actual score events, including on the reviewing host."""
    groups = config.get('equivalence_groups', {})
    if not groups:
        return []
    errors = []
    records = {row.get('id'): row for row in tested}
    names = {protocol['reward_metric']}
    names.update(protocol.get('reward_components', {}))
    names.update(r['metric'] for r in protocol.get('assessment', {}).get('requirements', [])
                 if r.get('role') == 'quality' and r.get('metric'))
    for name, group in groups.items():
        baseline = None
        for label, cid in group['cases'].items():
            event = (records.get(cid) or {}).get('event') or {}
            metrics = event.get('metrics') or {}
            if event.get('status') != 'success' or not isinstance(metrics, dict) or any(
                    not _number(metrics.get(m)) for m in names):
                errors.append(f'{name}/{label}: missing successful event with all scored metrics')
                continue
            if baseline is None:
                baseline = metrics
                continue
            for metric in sorted(names):
                if not math.isclose(metrics[metric], baseline[metric], rel_tol=1e-12, abs_tol=1e-12):
                    errors.append(f'{name}/{label}: equivalent submissions scored differently on {metric}: '
                                  f'{metrics[metric]} versus {baseline[metric]}')
    return errors


def inspect_config(path: Path, *, require_rejection_reasons=False, development_case=None, development_group=None) -> tuple[dict, Path]:
    """Check fixture contracts without importing the task adapter or running tests."""
    config = json.loads(path.read_text())
    if not isinstance(config, dict) or config.get('schema_version') != 'pipeline-scoring-smoke-v1':
        raise ValueError('scoring smoke requires schema_version=pipeline-scoring-smoke-v1')
    root = path.parent.resolve()
    protocol = _file(root, config.get('protocol', 'protocol.json'))
    formal = json.loads(protocol.read_text())
    if not isinstance(formal, dict):
        raise ValueError('smoke protocol must be a JSON object')
    timeout = config.get('timeout_sec', 120)
    if type(timeout) is not int or timeout <= 0:
        raise ValueError('smoke timeout_sec must be a positive integer')
    config = copy.deepcopy(config)
    cases = config.get('cases')
    if not isinstance(cases, list) or not cases:
        raise ValueError('scoring smoke requires nonempty cases')
    if development_case is not None:
        cases = [c for c in cases if isinstance(c, dict) and c.get('id') == development_case]
        if len(cases) != 1:
            raise ValueError(f'expected one development case named {development_case!r}, found {len(cases)}')
    elif development_group is not None:
        declared = config.get('equivalence_groups', {})
        if not isinstance(declared, dict) or development_group not in declared:
            raise ValueError(f'unknown equivalence group {development_group!r}')
        config = {**config, 'equivalence_groups': {development_group: declared[development_group]}}
        group = equivalence_groups(formal, config, root)[development_group]
        cases = [case for case in cases if case['id'] in group['cases'].values()]
    seen, kinds = set(), set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError('smoke case must be an object')
        cid, kind = case.get('id'), case.get('kind')
        if not isinstance(cid, str) or not cid.strip() or cid in seen:
            raise ValueError('smoke cases need unique nonempty IDs')
        seen.add(cid)
        if kind not in ('correct', 'invalid', 'weak_valid'):
            raise ValueError(f'{cid}: kind must be correct, invalid or weak_valid')
        kinds.add(kind)
        if kind == 'invalid':
            # Invalid controls validate rejection semantics only.  A copied
            # zero reward would make the control ambiguous and can hide an
            # evaluator that reports a scientific score for rejected input.
            score_fields = [field for field in
                            ('expected_metrics', 'expected_reward', 'reward_bounds', 'metric_bounds')
                            if field in case]
            if score_fields:
                raise ValueError(f'{cid}: invalid controls must not expect a scientific score; '
                                 f'remove {", ".join(score_fields)}')
        if not isinstance(case.get('description'), str) or not case['description'].strip():
            raise ValueError(f'{cid}: explain the scientific/structural control in description')
        submission = _file(root, case.get('submission'))
        if kind != 'invalid':
            fixture_errors = _fixture_submission_errors(submission, formal, formal)
            if fixture_errors:
                raise ValueError(f'{cid}: ' + '; '.join(fixture_errors))
        expected = 'invalid_submission' if kind == 'invalid' else 'success'
        if case.get('expected_status') != expected:
            raise ValueError(f'{cid}: expected_status must be {expected}; runtime failure is not rejection')
        if require_rejection_reasons and kind == 'invalid' and 'expected_error_contains' not in case:
            raise ValueError(f'{cid}: current construction requires expected_error_contains for the specific rejected rule')
        if 'expected_error_contains' in case:
            reason = case['expected_error_contains']
            if kind != 'invalid' or not isinstance(reason, str) or not reason.strip():
                raise ValueError(f'{cid}: expected_error_contains needs a nonempty rejection fragment on an invalid case')
        metrics = case.get('expected_metrics', {})
        if not isinstance(metrics, dict) or not all(isinstance(k, str) and k and _number(v) for k, v in metrics.items()):
            raise ValueError(f'{cid}: expected_metrics must contain finite numeric values')
        if 'expected_reward' in case and not _number(case['expected_reward']):
            raise ValueError(f'{cid}: expected_reward must be finite')
        if (kind == 'correct' and 'expected_reward' not in case and not metrics
                and 'reward_bounds' not in case and 'metric_bounds' not in case):
            raise ValueError(f'{cid}: correct control needs an independently determined reward or metric')
        if kind == 'invalid' and ('expected_reward' in case or metrics):
            raise ValueError(f'{cid}: invalid controls must not expect a scientific score')
        for field in ('reward_bounds', 'metric_bounds'):
            if field not in case: continue
            if kind == 'invalid': raise ValueError(f'{cid}: invalid controls cannot have score bounds')
            bounds = {'reward': case[field]} if field == 'reward_bounds' else case[field]
            if not isinstance(bounds, dict) or not bounds: raise ValueError(f'{cid}: empty or malformed score bounds')
            for name, limits in bounds.items():
                if (not isinstance(name, str) or not name or not isinstance(limits, dict) or not limits or
                        set(limits) - {'min', 'max'} or any(not _number(v) for v in limits.values()) or
                        ('min' in limits and 'max' in limits and limits['min'] > limits['max'])):
                    raise ValueError(f'{cid}: score bounds need finite ordered min/max')
        tolerance = case.get('tolerance', 1e-9)
        if not _number(tolerance) or tolerance < 0:
            raise ValueError(f'{cid}: tolerance must be finite and nonnegative')
    if development_case is not None:
        case = cases[0]
        expects_score = bool(case.get('expected_metrics')) or any(
            key in case for key in ('expected_reward', 'reward_bounds', 'metric_bounds'))
        if case['kind'] != 'invalid' and not expects_score:
            raise ValueError('a development case needs an independent expected metric, reward or bound')
        return {**config, 'cases': cases, 'equivalence_groups': {}}, protocol
    equivalence_groups(formal, config, root)
    if development_group is not None:
        return {**config, 'cases': cases}, protocol
    if not {'correct', 'invalid'} <= kinds:
        raise ValueError('scoring smoke needs correct and invalid controls')
    applicability = config.get('weak_valid_applicability')
    if (not isinstance(applicability, dict) or type(applicability.get('applicable')) is not bool
            or not isinstance(applicability.get('reason'), str) or not applicability['reason'].strip()):
        raise ValueError('explain weak_valid_applicability with applicable and reason')
    if applicability['applicable'] != ('weak_valid' in kinds):
        raise ValueError('weak-valid cases must match their applicability declaration')
    return config, protocol


def rejection_errors(case: dict, event: dict) -> list[str]:
    """Check a declared rejection cause, not just any invalid-submission status.

    Older controls may omit the fragment. This does not prove the fixture only
    violates its declared scientific requirement; that still needs review.
    """
    if 'expected_error_contains' not in case:
        return []
    expected = case['expected_error_contains']
    if case.get('kind') != 'invalid' or not isinstance(expected, str) or not expected.strip():
        return ['invalid expected_error_contains declaration']
    response = event.get('response')
    actual = response.get('error') if isinstance(response, dict) else None
    if event.get('status') != 'invalid_submission' or not isinstance(actual, str) or expected not in actual:
        return [f'expected rejection containing {expected!r}, received {actual!r} (status={event.get("status")!r})']
    return []


def outcome_errors(case: dict, event: dict, *, reward_metric=None) -> list[str]:
    """Recheck actual outcomes in both the runner and host receipt validation."""
    errors = []
    if event.get('status') != case['expected_status']:
        errors.append(f"expected {case['expected_status']}, received {event.get('status')}")
    errors.extend(rejection_errors(case, event))
    response = event.get('response') or {}
    observed = event.get('metrics') or {}
    if not isinstance(response, dict) or not isinstance(observed, dict):
        return errors + ['malformed score response/metrics']
    if response.get('status') != event.get('status') or response.get('metrics') != event.get('metrics'):
        errors.append('score event and response disagree')
    if reward_metric and event.get('status') == 'success':
        reward = response.get('reward')
        metric = observed.get(reward_metric)
        if not _number(reward) or not _number(metric) or reward != metric:
            errors.append(f'reward disagrees with the protocol metric {reward_metric}')
    if case['kind'] == 'invalid' and event.get('metrics') not in (None, {}):
        errors.append('invalid submission produced scientific metrics')
    for name, expected in case.get('expected_metrics', {}).items():
        value = observed.get(name)
        if not _number(value) or abs(value - expected) > case.get('tolerance', 1e-9):
            errors.append(f'metric {name}: expected {expected}, received {value}')
    if 'expected_reward' in case:
        reward = response.get('reward')
        if not _number(reward) or abs(reward - case['expected_reward']) > case.get('tolerance', 1e-9):
            errors.append(f"reward: expected {case['expected_reward']}, received {reward}")
    bounds = dict(case.get('metric_bounds', {}))
    for name, limits in bounds.items():
        value = observed.get(name)
        if not _number(value) or ('min' in limits and value < limits['min']) or ('max' in limits and value > limits['max']):
            errors.append(f'metric {name} outside declared bounds: {value}')
    if 'reward_bounds' in case:
        limits, value = case['reward_bounds'], response.get('reward')
        if not _number(value) or ('min' in limits and value < limits['min']) or ('max' in limits and value > limits['max']):
            errors.append(f'reward outside declared bounds: {value}')
    return errors


def resource_evidence_errors(protocol_path: Path, smoke: dict, tested: list[dict]) -> list[str]:
    """Re-resolve current test resources on the host; never trust saved worker paths."""
    from resource_contract import resolve
    protocol = json.loads(protocol_path.read_text())
    if 'resources' not in protocol: return []
    try:
        _, expected = resolve(protocol, protocol_path, mode='authoring',
                              test_bindings=smoke.get('resource_bindings', {}))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return ['cannot verify current scoring resources: ' + str(exc)]
    errors = []
    keys = ('resource_id', 'sha256', 'role')
    expected_resources = {rid: {key: item[key] for key in keys}
                          for rid, item in expected['resources'].items()}
    for row in tested:
        event = row.get('event')
        response = event.get('response') if isinstance(event, dict) else None
        proof = response.get('resource_evidence') if isinstance(response, dict) else None
        if not isinstance(proof, dict) or proof.get('scope') != expected['scope']:
            errors.append(f"{row.get('id')}: missing or mismatched scoring resource evidence"); continue
        if proof.get('instance_sizes', []) != expected.get('instance_sizes', []):
            errors.append(f"{row.get('id')}: test instance size evidence changed; rerun smoke")
        saved = proof.get('resources')
        if (not isinstance(saved, dict) or any(not isinstance(v, dict) for v in saved.values())
                or {rid: {key: item.get(key) for key in keys} for rid, item in saved.items()} != expected_resources):
            errors.append(f"{row.get('id')}: scoring resource content/bindings changed after test; rerun smoke")
    return errors


def binding_errors(package: Path, spec: dict) -> list[str]:
    """Optional integration for old packages; declared/new default files cannot be ignored."""
    from contract import package_path, package_layout
    errors = []
    binding = spec.get('verification', {}).get('scoring_smoke')
    default = package_path(package, package_layout(spec)['score_path']).parent / 'scoring_smoke.json'
    if binding is None:
        return ['scoring_smoke.json requires generated verification.scoring_smoke binding'] if default.exists() else []
    try:
        if not isinstance(binding, dict):
            raise ValueError('verification.scoring_smoke must be an object')
        raw_config = binding.get('config_path')
        if not isinstance(raw_config, str) or not raw_config:
            raise ValueError('verification.scoring_smoke.config_path is required')
        candidates = []
        try:
            candidates.append(package_path(package, raw_config))
        except ValueError:
            pass
        evaluation = package_path(package, package_layout(spec)['score_path']).parent
        if not Path(raw_config).is_absolute() and '..' not in PurePosixPath(raw_config).parts:
            candidates.append(evaluation / raw_config)
        config = next((candidate for candidate in candidates
                       if candidate.is_file() and candidate.resolve().is_relative_to(evaluation.resolve())), None)
        if config is None:
            raise ValueError('missing scoring smoke configuration inside evaluation: ' + raw_config)
        _, protocol = inspect_config(config, require_rejection_reasons=spec.get('assessment_contract_version') == 1)
        smoke = json.loads(config.read_text())
        if spec.get('assessment_contract_version') == 1:
            formal = json.loads(protocol.read_text())
            resources = formal.get('resources', {})
            swaps = smoke.get('resource_bindings', {})
            if not isinstance(swaps, dict): raise ValueError('resource_bindings must map formal resource IDs to fixtures')
            for original, fixture in swaps.items():
                # Self-bindings are harmless declarations for fixture-only
                # adapter context and do not replace a formal runtime input.
                # They are accepted so a smoke config can name its bundled
                # fixture resources explicitly.
                if original == fixture and original in resources and resources[original]['role'] == 'fixture':
                    continue
                # Context aliases (for example ``ground_truth``) can point
                # directly at a bundled fixture without being formal resource
                # IDs.  They are resolved by the adapter context and do not
                # replace a runtime input.
                if original not in resources and fixture in resources and resources[fixture]['role'] == 'fixture':
                    continue
                if original not in resources or resources[original]['role'] == 'fixture' or fixture not in resources or resources[fixture]['role'] != 'fixture':
                    raise ValueError('smoke resources must bind declared formal resources to fixture resources')
                for key in ('format', 'columns', 'id_column', 'items_path', 'forbidden_keys'):
                    if key in resources[original]['content'] and resources[original]['content'][key] != resources[fixture]['content'].get(key):
                        raise ValueError('smoke cannot change formal resource constraint: ' + key)
            for case in smoke['cases']:
                if not case.get('requirement_ids') or not isinstance(case['requirement_ids'], list):
                    raise ValueError('each smoke case must bind requirement_ids')
                declared = {r['id'] for r in formal.get('assessment', {}).get('requirements', [])}
                if any(rid not in declared for rid in case['requirement_ids']): raise ValueError('smoke case references unknown requirement')
                if case['kind'] == 'correct' and not case.get('oracle'):
                    raise ValueError('correct smoke case needs independent oracle description')
            if spec.get('scientific_acceptance_version') == 3:
                for requirement in formal['assessment']['requirements']:
                    if requirement['role'] == 'quality':
                        errors.extend(quality_control_errors(requirement, smoke['cases'], formal['reward_metric']))
        evidence = spec['verification']['score_evidence']
        if protocol.resolve() != package_path(package, evidence['evaluation_protocol']['config_path']).resolve():
            raise ValueError('smoke must use the actual scoring protocol, without a fixture-only replacement')
        tool = package_path(package, binding['runner_path'])
        if sha256(tool) != sha256(Path(__file__)):
            raise ValueError('smoke must use the supplied unmodified scoring_smoke.py')
        for name in ('scoring_entry.py', 'evaluation_protocol.py'):
            if sha256(tool.parent / name) != sha256(Path(__file__).with_name(name)):
                raise ValueError('smoke requires the supplied unmodified ' + name)
        capture = Path(__file__).with_name('score_attempt.py')
        if not capture.exists():
            capture = Path(__file__).resolve().parents[2] / 'stage3-TasksRun&Verify/code/score_attempt.py'
        if sha256(tool.parent / 'score_attempt.py') != sha256(capture):
            raise ValueError('smoke requires the supplied unmodified score_attempt.py')
        command = spec['verification']['commands'][binding['runtime_check']]
        expected = ['python', binding['runner_container'], '--config', binding['config_container']]
        if command['command'] != expected or command['service'] != evidence['service']:
            raise ValueError('smoke runtime command must invoke its bound runner/config in the evaluator')
        from execution_contract import bind_sources
        for path, container in [(config, binding['config_container']), (tool, binding['runner_container'])]:
            if bind_sources(package, spec, command['service'], container) != {path.resolve()}:
                raise ValueError('smoke path must resolve to its bound evaluator file: ' + container)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        errors.append('invalid scoring smoke binding: ' + str(exc))
    return errors


def implementation_hashes(root):
    return {p.relative_to(root).as_posix(): sha256(p) for p in sorted(root.rglob('*.py'))
            if not p.is_relative_to(root / 'checks/evidence') and '__pycache__' not in p.parts}


def run_smoke(config_path: Path, evidence_root: Path, *, resource_mode='runtime', development_case=None, development_group=None) -> dict:
    config_path = config_path.resolve()
    development = development_case is not None or development_group is not None
    if development and resource_mode != 'authoring':
        raise ValueError('individual development cases use authoring fixtures')
    config, protocol = inspect_config(config_path, development_case=development_case, development_group=development_group)
    if development:
        authoring_metric_adapter(protocol, json.loads(protocol.read_bytes()))
    implementations = implementation_hashes(protocol.parent)
    evidence_root = evidence_root.resolve()
    if evidence_root.is_relative_to(config_path.parent) or config_path.parent.is_relative_to(evidence_root):
        raise ValueError('smoke evidence must be separate from the checked evaluator directory')
    evidence_root.mkdir(parents=True, exist_ok=False)
    from scoring_entry import evaluate
    runner = (Path(__file__).with_name('evaluation_protocol.py') if development
              else protocol.parent / 'evaluation_protocol.py')
    # Rapid same-size edits can otherwise reuse timestamp-based .pyc files even
    # while the recorded source hash already points at the repaired adapter.
    command = [sys.executable, '-B', '-X', f'pycache_prefix={evidence_root / "bytecode"}',
               str(runner), '--config', str(protocol), '--input', '{submission}', '--output', '{output}',
               '--resource-mode', resource_mode, '--test-resources', str(config_path)]
    if development and config_path.parent.parts[-2:] == ('common_task', 'evaluation'):
        execution = config_path.parents[2] / 'execution.json'
        if execution.is_file() and json.loads(execution.read_bytes()).get('assessment_contract_version') == 1:
            if protocol != config_path.parent / 'protocol.json':
                raise ValueError('development smoke must use the bound common_task/evaluation/protocol.json')
            command.append('--bind-draft')
    formal = json.loads(protocol.read_text())
    records = []
    for index, case in enumerate(config['cases']):
        submission = _file(config_path.parent, case['submission'])
        before = sha256(submission)
        evidence = evidence_root / f'case-{index:03d}'
        event = evaluate(submission, evidence, command, timeout=config.get('timeout_sec', 120))
        errors = outcome_errors(case, event, reward_metric=formal.get('reward_metric'))
        captured = Path(event['submission_path'])
        if sha256(submission) != before or event.get('submission_sha256') != before or sha256(captured) != before:
            errors.append('submission snapshot/hash mismatch')
        timeline = [json.loads(line) for line in (evidence / 'timeline.jsonl').read_text().splitlines()]
        if timeline != [event]:
            errors.append('score timeline does not contain the actual single attempt')
        records.append({'id': case['id'], 'kind': case['kind'], 'status': 'fail' if errors else 'pass',
                        'errors': errors, 'submission_sha256': before, 'event': event,
                        'timeline': str(evidence / 'timeline.jsonl')})
    result = {'schema_version': 'pipeline-scoring-smoke-result-v1',
              'status': 'pass' if all(r['status'] == 'pass' for r in records) else 'fail',
              'config_sha256': sha256(config_path), 'protocol_sha256': sha256(protocol),
              'implementation_sha256': implementations,
              'cases': records, 'evidence_root': str(evidence_root),
              'resource_mode': resource_mode, 'resource_bindings': config.get('resource_bindings', {}),
              'scope': 'Declared controls through real capture/protocol/evidence; not scientific source certification.'}
    if config.get('equivalence_groups'):
        errors = equivalence_errors(config, formal, records)
        result['equivalence_errors'] = errors
        if errors:
            result['status'] = 'fail'
    if implementations != implementation_hashes(protocol.parent):
        result.update(status='fail', error='scoring implementation changed during testing')
    if development:
        result.update(schema_version='pipeline-scoring-development-result-v1',
                      scope='Selected authoring fixtures through the shared evaluator and current adapter.')
    (evidence_root / 'report.json').write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, help='Omitted: resolve the editable task from /task/input/paths.json')
    parser.add_argument('--evidence-root', type=Path, help='New directory outside the task package; never overwritten')
    parser.add_argument('--resource-mode', choices=['authoring', 'runtime'])
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('--case', dest='development_case', help='Run one named development fixture before generating the full package')
    selection.add_argument('--equivalence-group', dest='development_group', help='Compare the real scored metrics of one set of equivalent submissions')
    args = parser.parse_args(argv)
    package, evidence, archive_allowed = None, None, False
    development = args.development_case is not None or args.development_group is not None
    try:
        if args.config is None:
            from authoring_package import evaluation_context, new_evidence, preserve_evidence
            package, evaluation = evaluation_context(scientific_controls=not development)
            args.config = evaluation / 'scoring_smoke.json'
            args.resource_mode = args.resource_mode or 'authoring'
        evidence = args.evidence_root
        if evidence is None:
            # Retain all evidence; do not delete temporary captures when the command exits.
            evidence = new_evidence(package, 'scoring-smoke') if package else Path(tempfile.mkdtemp(prefix='scoring-smoke-')) / 'evidence'
        if package is not None:
            if evidence.resolve().is_relative_to(package) or package.is_relative_to(evidence.resolve()):
                raise ValueError('smoke evidence must be separate from the editable package')
            if evidence.exists(): raise ValueError('smoke evidence directory must be new')
            archive_allowed = not development
        mode = args.resource_mode or ('authoring' if development else 'runtime')
        result = run_smoke(args.config, evidence, resource_mode=mode, development_case=args.development_case,
                           development_group=args.development_group)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result = {'status': 'fail', 'error': str(exc)}
    if archive_allowed:
        try:
            evidence.mkdir(parents=True, exist_ok=True)
            if not (evidence / 'report.json').exists():
                (evidence / 'report.json').write_text(json.dumps(result) + '\n')
            result['saved_evidence'] = str(preserve_evidence(package, evidence))
        except (OSError, ValueError) as exc:
            result.update(status='fail', archive_error=str(exc))
    print(json.dumps(result, allow_nan=False))
    return 0 if result['status'] == 'pass' else 1


if __name__ == '__main__':
    raise SystemExit(main())
