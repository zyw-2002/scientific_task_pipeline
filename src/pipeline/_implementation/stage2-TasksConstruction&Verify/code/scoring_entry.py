#!/usr/bin/env python3
"""Freeze a submission, invoke its actual evaluator, and record typed evidence.

Task evaluators write result.json with status, metrics, and (on success) reward.
The framework does not invent scientific metrics or their reward conversion.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import subprocess
import uuid

from score_attempt import capture_submission, record_score_attempt, record_failed_attempt, write_json, read_json

STATUSES = {'success', 'invalid_submission', 'missing_submission', 'unscorable', 'runtime_error'}


def evaluate(submission: Path, evidence_root: Path, command: list[str], *, timeout: int = 300) -> dict:
    if not command or not all(isinstance(v, str) and v for v in command) or not any('{submission}' in v for v in command):
        raise ValueError('evaluator command must use the frozen {submission} placeholder')
    if not any('{output}' in v for v in command):
        raise ValueError('evaluator command must use its fresh {output} directory')
    if type(timeout) is not int or timeout <= 0:
        raise ValueError('timeout must be a positive integer')
    aid = 'attempt-' + uuid.uuid4().hex
    root = evidence_root / 'captures' / aid
    if not submission.is_file():
        event = record_failed_attempt(aid, evidence_root / 'timeline.jsonl', submission_path=str(submission),
                                      reason='Submission file is missing', status='missing_submission')
        write_json(evidence_root / 'last_attempt.json', {'attempt_id': aid})
        return event
    capture = capture_submission(submission, root, aid)
    output = root / 'evaluation'
    output.mkdir()
    argv = [v.replace('{submission}', capture['frozen_submission']).replace('{output}', str(output)) for v in command]
    try:
        process = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        (output / 'stdout.log').write_text(process.stdout)
        (output / 'stderr.log').write_text(process.stderr)
        result = read_json(output / 'result.json')
        if not isinstance(result, dict) or result.get('status') not in STATUSES:
            raise ValueError('evaluator must return a typed scientific status')
        if result['status'] == 'success':
            if process.returncode != 0 or not isinstance(result.get('metrics'), dict) or not result['metrics']:
                raise ValueError('successful scoring needs an actual metric result and exit code zero')
            reward = result.get('reward')
            if type(reward) not in (float, int) or not math.isfinite(reward):
                raise ValueError('evaluator must supply its finite task-defined reward')
        else:
            if result.get('metrics') not in (None, {}):
                raise ValueError('invalid, missing or unscorable submissions cannot claim scientific metrics')
            reward = 0  # Transport compatibility only; no scientific metric is created.
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        result = {'status': 'runtime_error', 'metrics': None, 'error': str(exc)}
        reward = 0
    event = record_score_attempt(capture, evidence_root / 'timeline.jsonl', metrics=result.get('metrics'),
                                 response=result, status=result['status'])
    write_json(evidence_root / 'last_attempt.json', {'attempt_id': aid})
    write_json(output / 'reward.json', {'reward': reward, 'scientific_status': result['status']})
    return event


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--submission', type=Path, required=True)
    parser.add_argument('--evidence-root', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=300)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    result = evaluate(args.submission, args.evidence_root, command, timeout=args.timeout)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] == 'success' else 1


if __name__ == '__main__':
    raise SystemExit(main())
