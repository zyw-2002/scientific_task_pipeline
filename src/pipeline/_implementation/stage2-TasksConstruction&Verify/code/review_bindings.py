#!/usr/bin/env python3
"""Bind a reviewer-approved report to current files and actual test evidence.

Never execute task code, rerun experiments, invent evidence or change scientific
verdicts. Explicit --reviewed acknowledges review of the current input revision.
Host acceptance remains read-only and never calls this authoring helper.
"""
from __future__ import annotations
import argparse
import copy
import json
from pathlib import Path

from contract import atomic_write_json, package_manifest, package_path, sha256_file


def _reference(package, path):
    target = package_path(package, path)
    if not target.is_file():
        raise ValueError('binding requires an existing file: ' + str(path))
    return {'path': target.relative_to(package).as_posix(), 'sha256': sha256_file(target)}


def _evidence(package, spec, check):
    """Select the latest matching run, then validate it; never hide a failed run."""
    from assessment_contract import check_evidence_errors
    verification = spec.get('verification', {})
    name = check['name']
    calibration = {b['runtime_check']: b for b in verification.get('calibration', [])}
    smoke = verification.get('scoring_smoke', {}).get('runtime_check')
    if name not in calibration and name != smoke:
        return None
    candidates = []
    archive = package / 'common_task/evaluation/checks/evidence'
    for path in archive.glob('*/report.json'):
        family = 'scoring-smoke-' if name == smoke else 'scientific-calibration-'
        owned = path.parent.name.startswith(family)
        try:
            saved = json.loads(path.read_text())
        except (ValueError, OSError):
            if owned:
                raise ValueError(name + ': unreadable archived test result: ' + str(path))
            continue
        if not isinstance(saved, dict):
            if owned:
                raise ValueError(name + ': malformed archived test result: ' + str(path))
            continue
        kind = saved.get('schema_version')
        matches = (name == smoke and kind == 'pipeline-scoring-smoke-result-v1')
        if name in calibration:
            matches = kind == 'scientific-calibration-batch-v1' and any(
                r.get('runtime_check') == name for r in saved.get('calibrations', []) if isinstance(r, dict))
            if kind == 'scientific-calibration-guard-v1':
                config = package_path(package, calibration[name]['config_path'])
                # Standalone archived guards are supported by the acceptance gate
                # too. Bind by config identity or the existing explicit reference.
                matches = (saved.get('inputs_sha256', {}).get('config') == sha256_file(config)
                    or any(ref.get('path') == path.relative_to(package).as_posix()
                           for ref in check.get('artifacts', []) if isinstance(ref, dict)))
        # Default runners also archive early failures without a schema. Such a
        # newer failed invocation must not disappear behind an older pass.
        if matches or (owned and kind is None):
            candidates.append(path)
    if not candidates:
        raise ValueError(name + ': no archived structured result; run the declared test first')
    latest = max(candidates, key=lambda p: (p.stat().st_mtime_ns, p.as_posix()))
    binding = _reference(package, latest.relative_to(package).as_posix())
    candidate = dict(check, artifacts=[binding])
    errors = check_evidence_errors(package, spec, {'checks': [candidate]})
    if errors:
        raise ValueError('; '.join(errors))
    return [binding]


def bind_review(package, *, reviewed=False):
    if not reviewed:
        raise ValueError('Review the current scientific inputs first, then use --reviewed to bind that revision')
    package = Path(package)
    if package.is_symlink():
        raise ValueError('review package cannot be a symlink')
    package = package.resolve()
    package_manifest(package)  # Reject symlinks/nonregular files before reading references.
    path = package / 'review_report.json'
    original = json.loads(path.read_text())
    report = copy.deepcopy(original)
    spec = json.loads((package / 'execution.json').read_text())
    if not isinstance(report, dict):
        raise ValueError('review_report must be an object')
    from construction_audit import input_hashes
    initial = input_hashes(package)
    # Bind only existing citations and implementations; never create scientific
    # findings, check statuses, source text, requirement coverage or conclusions.
    def references(value):
        if isinstance(value, list):
            for item in value:
                references(item)
        elif isinstance(value, dict):
            if 'path' in value:
                binding = _reference(package, value['path'])
                if 'quote' in value:
                    quote = value['quote']
                    if not isinstance(quote, str) or not quote.strip() or quote not in (package / binding['path']).read_text():
                        raise ValueError('quoted evidence is absent: ' + binding['path'])
                value['sha256'] = binding['sha256']
            for item in value.values():
                if isinstance(item, (dict, list)):
                    references(item)
    bound_checks = []
    for check in report.get('checks', []):
        if check.get('status') == 'pass':
            artifacts = _evidence(package, spec, check)
            if artifacts is not None:
                check['artifacts'] = artifacts
                bound_checks.append(check['name'])
    references(report)
    scientific = report.get('scientific_checks', {})
    if isinstance(scientific.get('scope_fidelity'), dict):
        scientific['scope_fidelity']['protocol_sha256'] = sha256_file(package / 'common_task/evaluation/protocol.json')
    if isinstance(scientific.get('paper_comparison'), dict):
        from paper_comparison import bindings
        scientific['paper_comparison'].update(bindings(package))
    if isinstance(report.get('construction_audit'), dict):
        report['construction_audit']['input_sha256'] = initial
    # Reuse the acceptance gate, including raw outcomes and input fingerprints.
    from assessment_contract import check_evidence_errors
    errors = check_evidence_errors(package, spec, report)
    if errors:
        raise ValueError('; '.join(errors))
    if input_hashes(package) != initial or json.loads(path.read_text()) != original:
        raise ValueError('inputs/report changed during binding; review the current revision again')
    atomic_write_json(path, report)
    return {'status': 'bound', 'report': str(path), 'test_bindings': bound_checks,
            'input_files': len(initial), 'scientific_verdicts_changed': False,
            'next': 'Run preflight.py; binding does not imply acceptance.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path)
    parser.add_argument('--reviewed', action='store_true', help='Confirm the current inputs have been scientifically reviewed; no verdict is generated')
    args = parser.parse_args(argv)
    try:
        if args.package is None:
            from authoring_package import evaluation_context
            args.package, _ = evaluation_context()
        result = bind_review(args.package, reviewed=args.reviewed)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, SyntaxError) as exc:
        result = {'status': 'failed', 'error': str(exc)}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] == 'bound' else 1


if __name__ == '__main__':
    raise SystemExit(main())
