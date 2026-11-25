"""Bind task-specific scientific requirements to executable checks.

Checks evidence structure and file identity, not the completeness or scientific
truth of the author's interpretation. Real container probes remain mandatory.
"""
from __future__ import annotations

from pathlib import Path

from contract import package_path, sha256_file


def acceptance_errors(package: Path, spec: dict, report: dict, *, required: bool = False) -> list[str]:
    rows = report.get('scientific_requirements')
    if rows is None and not required and report.get('schema_version') not in {'pipline-review-report-v3', 'pipline-review-report-v4'}:
        return []  # Historical reports keep their original protocol.
    if not isinstance(rows, list) or not rows:
        return ['scientific_requirements must be a nonempty requirements-to-implementation matrix']
    errors, seen = [], set()
    strict = (type(spec.get('scientific_acceptance_version')) is int and spec['scientific_acceptance_version'] >= 2) or report.get('schema_version') == 'pipline-review-report-v4'
    commands = spec.get('verification', {}).get('commands', {})
    for row in rows:
        if not isinstance(row, dict):
            errors.append('scientific requirement must be an object')
            continue
        rid = row.get('id')
        if not isinstance(rid, str) or not rid.strip() or rid in seen:
            errors.append('scientific requirements need unique nonempty IDs')
            continue
        seen.add(rid)
        if strict:
            for key in ('artifact_ids',):
                if key in row and (not isinstance(row[key], list) or not all(isinstance(v, str) for v in row[key])):
                    errors.append(f'{rid}: {key} must be an array of IDs')
        for key in ('requirement', 'source'):
            if not isinstance(row.get(key), str) or not row[key].strip():
                errors.append(f'{rid}: missing {key}')
        if row.get('kind') == 'trajectory_review':
            if row.get('status') != 'pending_review' or not row.get('evidence_needed'):
                errors.append(f'{rid}: provenance needs pending_review and evidence_needed; declarations do not prove execution')
            continue
        if row.get('kind') != 'program':
            errors.append(f'{rid}: kind must be program or trajectory_review')
            continue
        if row.get('status') != 'implemented':
            errors.append(f'{rid}: scientific requirement is not implemented')
        if not isinstance(row.get('runtime_check'), str) or row['runtime_check'] not in commands:
            errors.append(f'{rid}: missing declared runtime check')
        implementations = row.get('implementation')
        if not isinstance(implementations, list) or not implementations:
            errors.append(f'{rid}: missing actual implementation bindings')
        else:
            for binding in implementations:
                try:
                    if not isinstance(binding, dict) or not isinstance(binding.get('path'), str):
                        raise ValueError('implementation needs a package-relative path')
                    path = package_path(package, binding['path'])
                    if not path.is_file() or sha256_file(path) != binding.get('sha256'):
                        raise ValueError('implementation is missing or changed')
                except (OSError, ValueError, TypeError) as exc:
                    errors.append(f'{rid}: {exc}')
        controls = row.get('controls')
        if not isinstance(controls, list):
            controls = []
        kinds = set()
        for control in controls:
            if not isinstance(control, dict):
                errors.append(f'{rid}: invalid calibration control')
                continue
            kind = control.get('kind')
            if isinstance(kind, str):
                kinds.add(kind)
            if not control.get('case') or not control.get('expected'):
                errors.append(f'{rid}: each control needs case and expected result')
        current_contract = spec.get('assessment_contract_version') == 1
        invalid_applicability = row.get('invalid_applicability', {})
        required_kinds = {'correct', 'invalid'}
        if current_contract and isinstance(invalid_applicability, dict) and invalid_applicability.get('applicable') is False and invalid_applicability.get('reason'):
            required_kinds = {'correct'}
            if 'invalid' in kinds: errors.append(f'{rid}: invalid controls contradict applicability')
        if not required_kinds.issubset(kinds):
            errors.append(f'{rid}: calibration needs correct and invalid controls')
        applicability = row.get('weak_valid_applicability')
        if strict and (not isinstance(applicability, dict) or type(applicability.get('applicable')) is not bool or not applicability.get('reason')):
            errors.append(f'{rid}: explain whether weak-valid outcomes are scientifically meaningful')
        if isinstance(applicability, dict):
            if applicability.get('applicable') and 'weak_valid' not in kinds:
                errors.append(f'{rid}: missing applicable weak-valid control')
            if applicability.get('applicable') is False and 'weak_valid' in kinds:
                errors.append(f'{rid}: do not invent weak-valid outcomes for an inapplicable check')
        if strict:
            for control in controls:
                if not isinstance(control, dict):
                    continue
                expected = 'invalid' if control.get('kind') == 'invalid' else 'valid'
                if control.get('expected_status') != expected:
                    errors.append(f'{rid}: {control.get("kind")} control must expect {expected}')
    if not any(isinstance(row, dict) and row.get('kind') == 'program' for row in rows):
        errors.append('scientific acceptance requires executable checks, not provenance review alone')
    if strict:
        errors.extend(_protocol_errors(package, spec, rows))
    return errors


def _protocol_errors(package: Path, spec: dict, rows: list) -> list[str]:
    errors = []
    artifacts = spec.get('submission', {}).get('required_artifacts')
    if not isinstance(artifacts, list) or not artifacts:
        errors.append('declare task-specific submission.required_artifacts, including every required result and provenance artifact')
        artifacts = []
    indexed = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict) or not isinstance(artifact.get('id'), str) or not artifact['id'] or artifact['id'] in indexed or not artifact.get('description'):
            errors.append('required artifacts need unique IDs and English descriptions')
            continue
        indexed[artifact['id']] = artifact
    covered = {aid for row in rows if isinstance(row, dict) and row.get('kind') == 'program'
               for aid in (row.get('artifact_ids') if isinstance(row.get('artifact_ids'), list) else []) if isinstance(aid, str)}
    if indexed.keys() - covered:
        errors.append('required artifacts have no executable validation binding: ' + ', '.join(sorted(indexed.keys() - covered)))
    if covered - indexed.keys():
        errors.append('artifact validation references undeclared artifacts')
    errors.extend(calibration_binding_errors(package, spec))
    from scoring_smoke import binding_errors
    errors.extend(binding_errors(package, spec))
    return errors


def calibration_binding_errors(package: Path, spec: dict) -> list[str]:
    """Shared construction/review validation; no suites or task imports are executed."""
    from calibration_guard import inspect_config
    errors = []
    calibration = spec.get('verification', {}).get('calibration')
    if not isinstance(calibration, list) or not calibration:
        errors.append('declare verification.calibration with real suites and known faulty implementations')
        calibration = []
    for binding in calibration:
        try:
            config_path = package_path(package, binding['config_path'])
            inspect_config(config_path)
            guard = package_path(package, binding['guard_path'])
            if sha256_file(guard) != sha256_file(Path(__file__).with_name('calibration_guard.py')):
                errors.append('calibration must use the supplied unmodified calibration_guard.py')
            command = spec['verification']['commands'][binding['runtime_check']]['command']
            at = command.index('--config')
            if len(command) < 2 or not Path(command[0]).name.startswith('python') or command[1] != binding['guard_container'] or command[at + 1] != binding['config_container']:
                errors.append('calibration runtime command does not invoke its bound guard and config')
            from execution_contract import bind_sources
            service = spec['verification']['commands'][binding['runtime_check']]['service']
            if guard.resolve() not in bind_sources(package, spec, service, binding['guard_container']):
                errors.append('calibration guard container path does not resolve to its bound implementation')
            if config_path.resolve() not in bind_sources(package, spec, service, binding['config_container']):
                errors.append('calibration config container path does not resolve to its bound file')
        except (OSError, ValueError, KeyError, TypeError, SyntaxError, IndexError, AttributeError) as exc:
            errors.append('invalid calibration binding: ' + str(exc))
    return errors
