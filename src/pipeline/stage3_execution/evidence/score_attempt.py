#!/usr/bin/env python3
"""Capture the bytes used by one scoring attempt, then build a judge evidence bundle.

The evaluator must consume capture_submission()'s returned snapshot path. Importing
an existing run instead requires a timeline record with the actual submission SHA.
No scorer, solver code, or scientific dependency is executed by this module.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    return loads_json(path.read_text(encoding='utf-8'))


def loads_json(value: str) -> Any:
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError('duplicate JSON key: ' + key)
            result[key] = item
        return result
    def invalid(value):
        raise ValueError('nonfinite JSON value: ' + value)
    return json.loads(value, object_pairs_hook=pairs, parse_constant=invalid)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(path)


def check_id(value: str) -> str:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}', value):
        raise ValueError('attempt ID must be 1..200 safe letters, digits or ._:-')
    return value


def timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, AttributeError) as error:
        raise ValueError('timestamps must be ISO-8601 with timezone') from error
    if parsed.tzinfo is None:
        raise ValueError('timestamps must include timezone')
    return parsed


def _copy_file(source: Path, target: Path) -> str:
    if source.is_symlink() or not source.is_file():
        raise ValueError(f'evidence must be a real regular file: {source}')
    before = sha256(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    if sha256(source) != before or sha256(target) != before:
        raise ValueError(f'evidence changed while copying: {source}')
    return before


def capture_submission(submission: Path, output: Path, attempt_id: str) -> dict:
    """Call BEFORE evaluation; score the returned frozen_submission, never source."""
    check_id(attempt_id)
    if output.exists():
        raise ValueError(f'refusing to overwrite capture: {output}')
    output.mkdir(parents=True)
    frozen = output / 'submission' / submission.name
    digest = _copy_file(submission, frozen)
    frozen.chmod(0o444)
    capture = {'schema_version': 'pipeline-score-capture-v1', 'attempt_id': attempt_id,
               'captured_at': now(), 'submission_sha256': digest,
               'frozen_submission': str(frozen.resolve()), 'original_path': str(submission),
               'instruction': 'Evaluate frozen_submission and record its SHA in SCORE_ATTEMPT timeline.'}
    write_json(output / 'capture.json', capture)
    return capture


def record_score_attempt(capture: dict, timeline: Path, *, metrics: dict | None = None,
                         response: Any = None, status: str = 'completed') -> dict:
    """Evaluator-side hook AFTER evaluating capture['frozen_submission'].

    Keep capture directories and timeline under a collected evaluator artifact
    root, normally /logs/artifacts/score_attempts, with each capture beneath
    captures/<attempt_id>/. This function preserves actual
    returned metrics; it does not compute, reinterpret, or normalize any score.
    """
    frozen = Path(capture['frozen_submission'])
    if sha256(frozen) != capture['submission_sha256']:
        raise ValueError('captured submission changed before score recording')
    event = {'event': 'SCORE_ATTEMPT', 'attempt_id': check_id(capture['attempt_id']),
             'scored_at': now(), 'captured_at': capture['captured_at'],
             'submission_path': str(frozen), 'submission_sha256': capture['submission_sha256'],
             'metrics': metrics, 'response': response, 'status': status}
    _append_attempt(timeline, event)
    return event


def record_failed_attempt(attempt_id: str, timeline: Path, *, submission_path: str,
                          reason: str, status: str = 'missing_submission') -> dict:
    """Record a real failed scoring attempt when there were no submission bytes."""
    if status not in {'missing_submission', 'invalid_submission', 'runtime_error', 'failed', 'error', 'unscorable'}:
        raise ValueError('missing-submission event requires an explicit failure status')
    if not isinstance(submission_path, str) or not submission_path.strip() or not isinstance(reason, str) or not reason.strip():
        raise ValueError('failed attempt requires the actual submission path and failure reason')
    event = {'event': 'SCORE_ATTEMPT', 'attempt_id': check_id(attempt_id), 'scored_at': now(),
             'submission_path': submission_path, 'submission_sha256': None,
             'submission_missing': True, 'status': status, 'reason': reason}
    _append_attempt(timeline, event)
    return event


def _append_attempt(timeline: Path, event: dict) -> None:
    serialized = json.dumps(event, ensure_ascii=False, allow_nan=False) + '\n'
    timeline.parent.mkdir(parents=True, exist_ok=True)
    with timeline.open('a+', encoding='utf-8') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        for line in stream:
            if line.strip() and loads_json(line).get('attempt_id') == event['attempt_id']:
                raise ValueError('duplicate SCORE_ATTEMPT ID')
        stream.write(serialized)
        stream.flush()
        import os
        os.fsync(stream.fileno())


def timeline_records(path: Path) -> list[dict]:
    text = path.read_text(encoding='utf-8')
    try:
        value = loads_json(text)
    except json.JSONDecodeError:
        value = [loads_json(line) for line in text.splitlines() if line.strip()]
    if isinstance(value, dict):
        value = [value] if value.get('event') == 'SCORE_ATTEMPT' else value.get('attempts', value.get('events'))
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError('timeline must be a JSON list, {attempts:[...]}, {events:[...]} or JSONL')
    return value


def _material(path: Path, relative: str, kind: str) -> dict:
    size = path.stat().st_size
    text, encoding = None, 'metadata-only'
    metadata_reason = None
    if (kind in {'submission', 'workspace'} or relative.startswith('inputs/')) and size > 2_000_000:
        metadata_reason = 'File exceeds 2 MB text evidence limit; immutable bytes retained, metadata only supplied to text judge.'
    else:
        if size > 256_000_000:
            raise ValueError('text trajectory/timeline exceeds explicit 256 MB evidence limit; no silent truncation')
        try:
            text = path.read_text(encoding='utf-8')
            encoding = 'utf-8'
        except UnicodeDecodeError:
            encoding = 'binary'
            metadata_reason = 'Binary bytes retained; no scientific parsing or scoring is performed by the judge.'
    return {'path': relative, 'kind': kind, 'sha256': sha256(path),
            'size_bytes': size, 'encoding': encoding, 'text': text, 'metadata_reason': metadata_reason,
            'line_count': len(text.splitlines()) if text is not None else None}


def freeze_attempt(*, task: dict, readme: Path, data_description: Path,
                   submission: Path | None, trajectory: Path, timeline: Path,
                   attempt_id: str, output: Path, expected_sha256: str | None = None,
                   workspace_files: list[Path] | None = None, source_paper: Any = None,
                   guided_method: Path | None = None, input_files: list[Path] | None = None,
                   paper_reference: dict | None = None, verification_files: dict | None = None,
                   input_observation_context: dict | None = None) -> dict:
    """Freeze explicit materials; refusal to guess a target prevents latest-file drift."""
    check_id(attempt_id)
    if output.exists():
        raise ValueError(f'refusing to overwrite attempt evidence: {output}')
    records = timeline_records(timeline)
    matches = [event for event in records if str(event.get('attempt_id')) == attempt_id
               and event.get('event', 'SCORE_ATTEMPT') == 'SCORE_ATTEMPT']
    if len(matches) != 1:
        raise ValueError('timeline must identify exactly one target SCORE_ATTEMPT')
    target = matches[0]
    scored_time = timestamp(target.get('scored_at', target.get('timestamp')))
    if target.get('captured_at') is not None and timestamp(target['captured_at']) > scored_time:
        raise ValueError('submission capture occurred after target scoring time')
    declared = target.get('submission_sha256')
    missing = target.get('submission_missing') is True and declared is None and target.get('status') in {
        'missing_submission', 'invalid_submission', 'runtime_error', 'failed', 'error', 'unscorable'}
    if not missing and (not isinstance(declared, str) or not re.fullmatch('[0-9a-f]{64}', declared)):
        raise ValueError('target SCORE_ATTEMPT must record submission_sha256 from the scorer')
    if expected_sha256 is not None and expected_sha256 != declared:
        raise ValueError('expected SHA differs from target timeline SHA')
    if not missing and (submission is None or sha256(submission) != declared):
        raise ValueError('submission does not match target SCORE_ATTEMPT; latest output is not a substitute')
    if not isinstance(target.get('submission_path'), str) or not target['submission_path']:
        raise ValueError('target SCORE_ATTEMPT must record actual submission_path')
    output.mkdir(parents=True)
    materials = {}
    required = [('readme', readme, 'readme.md', 'readme'),
                ('data_description', data_description, 'data_description.md', 'readme'),
                ('timeline', timeline, 'timeline.json', 'timeline'),
                ('trajectory', trajectory, 'trajectory' + (trajectory.suffix or '.txt'), 'trajectory')]
    if missing:
        materials['submission'] = {'missing': True, 'declared_path': target['submission_path'],
            'reason': target.get('reason', target['status']), 'sha256': None}
    else:
        required.append(('submission', submission, 'submission/' + submission.name, 'submission'))
    if guided_method is not None:
        required.append(('method_guidance', guided_method, 'method.md', 'readme'))
    for key, source in (verification_files or {}).items():
        if key not in {'rule_checks', 'final_submission', 'selfchecks'}:
            raise ValueError('unknown verification material')
        required.append((key, source, 'verification/' + source.name, 'timeline'))
    for key, source, relative, kind in required:
        destination = output / relative
        _copy_file(source, destination)
        materials[key] = _material(destination, relative, kind)
    if materials['submission']['sha256'] != declared:
        raise ValueError('submission changed during evidence freeze')
    workspace = []
    for index, source in enumerate(workspace_files or []):
        relative = f'workspace/{index:04d}/{source.name}'
        _copy_file(source, output / relative)
        workspace.append(_material(output / relative, relative, 'workspace'))
    provided_inputs = []
    for index, source in enumerate(input_files or []):
        relative = f'inputs/{index:04d}/{source.name}'
        _copy_file(source, output / relative)
        provided_inputs.append(_material(output / relative, relative, 'readme'))
    request = {'schema_version': 'pipeline-judge-request-v1', 'frozen_at': now(),
               'task': task, 'score_attempt': target, 'score_attempt_used': attempt_id,
               **materials, 'workspace_files': workspace, 'provided_inputs': provided_inputs, 'source_paper': source_paper,
               'evidence_policy': {'primary': 'trajectory', 'workspace': 'supplementary final state only',
                   'locator_format': '<material path>:L<start>[-L<end>]',
                   'binary_submission': 'metadata only; no invented text or scientific recomputation'}}
    if input_observation_context is not None:
        from solver_input_evidence import collect
        cutoff = timestamp(target.get('captured_at') or target.get('scored_at') or target['timestamp'])
        if 'final_submission' in materials:
            final = read_json(output / materials['final_submission']['path'])
            if final.get('submitted_at'):
                cutoff = min(cutoff, timestamp(final['submitted_at']))
        try:
            trace = read_json(output / materials['trajectory']['path'])
        except (ValueError, UnicodeError):
            trace = {}
        observation = collect(trajectory=trace, effective=input_observation_context['effective'],
                              spec=input_observation_context['spec'], cutoff=cutoff, backend=input_observation_context['backend'])
        observation.update(attempt_id=attempt_id, cutoff=cutoff.isoformat(), trajectory_sha256=materials['trajectory']['sha256'],
                           effective_manifest_sha256=input_observation_context['effective_manifest_sha256'])
        write_json(output / 'solver_input_observations.json', observation)
        request['solver_input_observations'] = observation
    if paper_reference is not None:
        write_json(output / 'paper_reference.json', paper_reference)
        request['paper_reference'] = paper_reference
    write_json(output / 'request.json', request)
    manifest = {'schema_version': 'pipeline-attempt-evidence-v1', 'attempt_id': attempt_id,
                'submission_sha256': declared, 'scored_at': target.get('scored_at', target.get('timestamp')),
                'frozen_at': request['frozen_at'], 'files': {str(path.relative_to(output)): sha256(path)
                    for path in sorted(output.rglob('*')) if path.is_file()}}
    write_json(output / 'manifest.json', manifest)
    return request


def load_frozen(bundle: Path) -> dict:
    manifest = read_json(bundle / 'manifest.json')
    expected = manifest.get('files')
    if not isinstance(expected, dict) or 'request.json' not in expected:
        raise ValueError('invalid evidence manifest')
    for relative, digest in expected.items():
        path = bundle / relative
        if not path.resolve().is_relative_to(bundle.resolve()) or path.is_symlink():
            raise ValueError('unsafe evidence path')
        if not path.is_file() or sha256(path) != digest:
            raise ValueError(f'frozen evidence changed: {relative}')
    request = read_json(bundle / 'request.json')
    if 'paper_reference' in request:
        if 'paper_reference.json' not in expected or request['paper_reference'] != read_json(bundle / 'paper_reference.json'):
            raise ValueError('paper reference differs from frozen evidence')
    if request.get('score_attempt_used') != manifest.get('attempt_id'):
        raise ValueError('attempt ID does not match manifest')
    if request['submission']['sha256'] != manifest.get('submission_sha256'):
        raise ValueError('submission hash does not match manifest')
    provided = [request.get(key) for key in ('readme', 'data_description', 'submission', 'timeline', 'trajectory', 'method_guidance', 'rule_checks', 'final_submission', 'selfchecks')]
    provided.extend(request.get('workspace_files', []))
    provided.extend(request.get('provided_inputs', []))
    for material in provided:
        if material is None or material.get('missing') is True:
            continue
        relative = material['path']
        if relative not in expected:
            raise ValueError('request material is absent from frozen manifest')
        actual = _material(bundle / relative, relative, material['kind'])
        if actual != material:
            raise ValueError(f'embedded request material differs from frozen bytes: {relative}')
    targets = [event for event in timeline_records(bundle / request['timeline']['path'])
               if str(event.get('attempt_id')) == request['score_attempt_used']]
    if len(targets) != 1 or targets[0] != request['score_attempt']:
        raise ValueError('embedded target does not match actual frozen timeline')
    validate_rule_binding(request)
    return request


def validate_rule_binding(request):
    required = request.get('task', {}).get('submission_lifecycle', {}).get('formal_submission_required')
    if not required and 'rule_checks' not in request:
        return None  # Explicit historical compatibility.
    if not all(key in request for key in ('rule_checks', 'final_submission')):
        raise ValueError('formal run missing frozen rule report or final receipt')
    rules = loads_json(request['rule_checks']['text'])
    final = loads_json(request['final_submission']['text'])
    event = request['score_attempt']
    if rules.get('schema_version') != 'pipeline-rule-checks-v1' or rules.get('status') not in {'pass', 'fail', 'error'}:
        raise ValueError('invalid formal rule report')
    if rules.get('attempt_id') != request['score_attempt_used'] or rules.get('submission_sha256') != event.get('submission_sha256'):
        raise ValueError('rules refer to a different score or submission')
    if rules.get('final_receipt_sha256') != request['final_submission']['sha256']:
        raise ValueError('rules do not bind the final submission receipt')
    if final.get('status') == 'accepted':
        if final.get('schema_version') != 'pipeline-final-submission-v1':
            raise ValueError('invalid formal submission receipt schema')
        if final.get('submission_sha256') != event.get('submission_sha256') or timestamp(final['submitted_at']) > timestamp(event['captured_at']):
            raise ValueError('scored bytes/time do not match formal submission')
    elif rules['status'] == 'pass' or event.get('submission_missing') is not True:
        raise ValueError('missing final submission cannot have a scored candidate')
    if rules['status'] == 'pass':
        if event.get('status') != 'success' or not rules.get('checks') or any(c.get('status') not in {'pass', 'not_applicable'} for c in rules['checks']):
            raise ValueError('rule pass disagrees with hard checks or official scoring')
    return rules


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--submission', type=Path, required=True)
    parser.add_argument('--attempt-id', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(capture_submission(args.submission, args.output, args.attempt_id), ensure_ascii=False))
        return 0
    except (OSError, ValueError) as error:
        print(json.dumps({'status': 'error', 'error': str(error)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
