#!/usr/bin/env python3
"""Publish the actual evaluator event without recomputing task-specific metrics."""
import argparse
import json
import math
import hashlib
import re
from pathlib import Path

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path):
    return loads_json(path.read_text(encoding='utf-8'))


def loads_json(value: str):
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


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(path)


def check_id(value: str) -> str:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}', value):
        raise ValueError('attempt ID must be 1..200 safe letters, digits or ._:-')
    return value


def publish(root: Path, output: Path, attempt_id: str | None = None) -> dict:
    aid = check_id(attempt_id or read_json(root / 'last_attempt.json')['attempt_id'])
    events = [json.loads(line) for line in (root / 'timeline.jsonl').read_text().splitlines() if line.strip()]
    matches = [event for event in events if event.get('attempt_id') == aid]
    if len(matches) != 1:
        raise ValueError('collected evaluator evidence must contain exactly one selected attempt')
    event = matches[0]
    status = event.get('status')
    if status not in {'success', 'invalid_submission', 'missing_submission', 'unscorable', 'runtime_error'}:
        raise ValueError('unrecognized scientific result status')
    if status != 'missing_submission':
        capture_root = root / 'captures' / aid
        capture = read_json(capture_root / 'capture.json')
        filename = Path(capture['frozen_submission']).name
        frozen = capture_root / 'submission' / filename
        if frozen.is_symlink() or not frozen.is_file():
            raise ValueError('missing collected submission snapshot')
        if capture['attempt_id'] != aid or sha256(frozen) != capture['submission_sha256'] or capture['submission_sha256'] != event.get('submission_sha256'):
            raise ValueError('collected submission identity does not match evaluator evidence')
    if status == 'success':
        result = event.get('response', {})
        resource_evidence = result.get('resource_evidence')
        if resource_evidence is not None and (resource_evidence.get('scope') != 'formal' or resource_evidence.get('mode') != 'runtime'):
            raise ValueError('fixture/authoring evidence cannot be published as a formal task score')
        if result.get('status') != status or result.get('metrics') != event.get('metrics') or not event.get('metrics'):
            raise ValueError('evaluator metrics/status mismatch')
        reward = result.get('reward')
        if type(reward) not in (int, float) or not math.isfinite(reward):
            raise ValueError('missing finite evaluator-defined reward')
    else:
        if event.get('metrics'):
            raise ValueError('failed evaluation must not supply scientific metrics')
        reward = 0
    write_json(output / 'reward.json', {'reward': reward})
    write_json(output / 'results.json', event)
    return event


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence-root', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, default=Path('/logs/verifier'))
    parser.add_argument('--attempt-id')
    args = parser.parse_args()
    event = publish(args.evidence_root, args.output_dir, args.attempt_id)
    print('Scientific status:', event['status'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
