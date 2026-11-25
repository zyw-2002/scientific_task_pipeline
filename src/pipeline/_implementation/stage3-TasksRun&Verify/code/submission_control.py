#!/usr/bin/env python3
"""Evaluator-owned mailbox, one-time commit and post-Solver formal checks."""
from contextlib import contextmanager
import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from score_attempt import capture_submission, record_failed_attempt, record_score_attempt, now, read_json, sha256, write_json
from submission_rules import check


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp-' + uuid.uuid4().hex)
    write_json(temporary, value)
    os.replace(temporary, path)


@contextmanager
def locked(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'control.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def process_request(config, request):
    root = Path(config['evidence_root'])
    rid = request.get('request_id')
    if not isinstance(rid, str) or not rid.isalnum() or len(rid) > 64:
        raise ValueError('Invalid request ID')
    action = request.get('action')
    if action not in {'check', 'submit'} or set(request) != {'request_id', 'action'}:
        raise ValueError('Only check/submit actions at the declared submission path are allowed')
    with locked(root):
        receipt = root / 'requests' / (rid + '.json')
        if receipt.exists():
            previous = read_json(receipt)
            if previous['action'] != action:
                return {'request_id': rid, 'status': 'rejected', 'reason': 'Request ID reused'}
            return previous
        response = {'request_id': rid, 'action': action, 'timestamp': now()}
        final_path = root / 'final_submission.json'
        if final_path.exists():
            final = read_json(final_path)
            if action == 'submit' and final.get('request_id') == rid and final.get('accepted_response'):
                atomic(receipt, final['accepted_response'])
                return final['accepted_response']
        if (root / 'closed.json').exists():
            return {**response, 'status': 'rejected', 'reason': 'Solver session has ended'}
        if (root / 'final_submission.json').exists() or (root / 'closed.json').exists():
            response.update(status='rejected', reason='Submission is frozen or the Solver session has ended')
        else:
            try:
                capture = capture_submission(Path(config['submission']), root / 'checks' / rid, rid)
                frozen = Path(capture['frozen_submission'])
                if action == 'check':
                    report = check(frozen, Path(config['protocol']), public=True)
                    response.update(report, submission_sha256=capture['submission_sha256'])
                else:
                    submitted_at = now()
                    response.update(status='accepted', submission_sha256=capture['submission_sha256'],
                                    submitted_at=submitted_at)
                    final = {**capture, 'schema_version': 'pipeline-final-submission-v1', 'status': 'accepted',
                             'request_id': rid, 'submitted_at': submitted_at, 'accepted_response': response}
                    atomic(root / 'final_submission.json', final)
            except (OSError, ValueError) as exc:
                response.update(status='error', reason=str(exc))
        atomic(receipt, response)
        with (root / 'selfchecks.jsonl').open('a') as stream:
            stream.write(json.dumps(response) + '\n')
        return response


def serve(config):
    inbox, outbox = Path(config['inbox']), Path(config['outbox'])
    for folder in (inbox, outbox):
        folder.mkdir(parents=True, exist_ok=True)
        folder.chmod(0o777)
    # Preserve the task-authored evaluator initialization/keepalive command.
    bootstrap = config.get('bootstrap')
    child = subprocess.Popen(bootstrap) if bootstrap else None
    try:
        while True:
            if (Path(config['evidence_root']) / 'closed.json').exists():
                time.sleep(0.1)
                continue  # Keep evaluator alive, but its evidence log is closed.
            for path in sorted(outbox.glob('*.json')):
                response_path = inbox / path.name
                if response_path.exists():
                    continue
                try:
                    if path.is_symlink() or path.stat().st_size > 4096:
                        raise ValueError('Request must be regular JSON <=4 KiB')
                    value = read_json(path)
                    if path.name != value.get('request_id', '') + '.json':
                        raise ValueError('Request filename mismatch')
                    response = process_request(config, value)
                except (OSError, ValueError, TypeError, AttributeError) as exc:
                    response = {'status': 'rejected', 'reason': str(exc)}
                atomic(response_path, response)
            time.sleep(0.1)
    finally:
        if child and child.poll() is None:
            child.terminate()


def finalize(config):
    root = Path(config['evidence_root'])
    with locked(root):
        if (root / 'rule_checks.json').exists():
            return read_json(root / 'rule_checks.json')
        interrupted = (root / 'closed.json').exists()
        atomic(root / 'closed.json', {'closed_at': now()})
        final_path = root / 'final_submission.json'
        final = read_json(final_path) if final_path.exists() else {'status': 'missing', 'submitted_at': None}
        if final['status'] == 'accepted':
            frozen = Path(final['frozen_submission'])
            if sha256(frozen) != final['submission_sha256']:
                raise ValueError('Evaluator-owned final snapshot changed')
            report = check(frozen, Path(config['protocol']))
            if interrupted:
                report = {'status': 'error', 'checks': [{'id': 'finalization', 'status': 'error'}],
                          'reason': 'Previous finalization was interrupted; no scientific evaluation retried.'}
            from score_attempt import timeline_records
            existing = timeline_records(root / 'timeline.jsonl') if (root / 'timeline.jsonl').exists() else []
            if interrupted and existing:
                if len(existing) != 1: raise ValueError('Ambiguous official scoring events')
                event = existing[0]
            elif report['status'] == 'pass':
                from scoring_entry import evaluate
                try:
                    event = evaluate(frozen, root, config['scoring_command'], timeout=config.get('timeout', 300))
                except Exception as exc:
                    # A checker exception must still yield typed evidence, not a
                    # fictitious zero or a second scientific evaluation.
                    from score_attempt import timeline_records
                    existing = timeline_records(root / 'timeline.jsonl') if (root / 'timeline.jsonl').exists() else []
                    if len(existing) == 1:
                        event = existing[0]
                    elif not existing:
                        aid = 'attempt-' + uuid.uuid4().hex
                        capture = capture_submission(frozen, root / 'captures' / aid, aid)
                        event = record_score_attempt(capture, root / 'timeline.jsonl', metrics=None,
                            response={'status': 'runtime_error', 'error': str(exc)}, status='runtime_error')
                    else:
                        raise ValueError('Ambiguous official scoring events') from exc
                    report['reason'] = 'Scoring collector error: ' + str(exc)
                    report['checks'].append({'id': 'scoring_collector', 'status': 'error'})
                status = event.get('status')
                report['checks'].append({'id': 'official_scoring', 'status': 'pass' if status == 'success' else 'fail' if status == 'invalid_submission' else 'error', 'scientific_status': status})
                report['status'] = report['checks'][-1]['status']
                if status == 'success':
                    metrics = event.get('metrics')
                    response = event.get('response') or {}
                    resource = response.get('resource_evidence') or {}
                    verified = (isinstance(metrics, dict) and bool(metrics) and
                        all(type(v) in (int, float) and math.isfinite(v) for v in metrics.values()) and
                        response.get('protocol_sha256') == sha256(Path(config['protocol'])) and
                        resource.get('scope') == 'formal' and resource.get('mode') == 'runtime')
                    report['checks'].append({'id': 'score_protocol_binding', 'status': 'pass' if verified else 'error'})
                if any(c['status'] == 'error' for c in report['checks']):
                    report['status'] = 'error'
            else:
                aid = 'attempt-' + uuid.uuid4().hex
                capture = capture_submission(frozen, root / 'captures' / aid, aid)
                event = record_score_attempt(capture, root / 'timeline.jsonl', metrics=None,
                    response={'status': 'invalid_submission' if report['status'] == 'fail' else 'runtime_error', 'error': report['reason']},
                    status='invalid_submission' if report['status'] == 'fail' else 'runtime_error')
        else:
            atomic(final_path, final)
            report = {'status': 'fail', 'checks': [{'id': 'formal_submission', 'status': 'fail'}],
                      'reason': 'Solver ended without an acknowledged formal submission', 'scientific_validity': 'not_assessed'}
            from score_attempt import timeline_records
            existing = timeline_records(root / 'timeline.jsonl') if (root / 'timeline.jsonl').exists() else []
            if existing:
                if len(existing) != 1: raise ValueError('Ambiguous missing-submission events')
                event = existing[0]
            else:
                event = record_failed_attempt('attempt-' + uuid.uuid4().hex, root / 'timeline.jsonl',
                        submission_path=config['submission'], reason=report['reason'])
        write_json(root / 'last_attempt.json', {'attempt_id': event['attempt_id']})
        report.update(schema_version='pipeline-rule-checks-v1', checked_at=now(),
                      attempt_id=event['attempt_id'], submission_sha256=event.get('submission_sha256'),
                      final_receipt_sha256=sha256(final_path), protocol_sha256=sha256(Path(config['protocol'])))
        atomic(root / 'rule_checks.json', report)
        return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['serve', 'finalize'])
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    config = read_json(args.config)
    sys.path.insert(0, str(Path(config['protocol']).parent))
    if args.action == 'serve':
        serve(config)
    else:
        result = finalize(config)
        print(json.dumps(result))
        raise SystemExit(0 if result['status'] == 'pass' else 1)
