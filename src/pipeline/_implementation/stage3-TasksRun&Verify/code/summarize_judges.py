#!/usr/bin/env python3
"""Summarize local seven-class Judge records without another model call."""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from judge_reporting import FAILURE_ATTRIBUTIONS, CLASSES, DIAGNOSTIC_CLASSES, INVALIDITY_CLASSES, REPORTING_SCHEMA, reporting_dimensions
from score_attempt import read_json, sha256, write_json


def load_record(manifest_path: Path) -> dict:
    manifest = read_json(manifest_path)
    verdict_path = manifest_path.parent / 'judge_verdict.json'
    verdict = read_json(verdict_path)
    if not isinstance(manifest, dict) or not isinstance(verdict, dict):
        raise ValueError('Judge manifest and verdict must be objects')
    category = verdict.get('failure_class')
    if not isinstance(category, str) or category not in CLASSES:
        raise ValueError('invalid failure_class')
    if not isinstance(verdict.get('is_valid'), bool) or verdict['is_valid'] != (category == 'valid'):
        raise ValueError('is_valid/failure_class mismatch')
    for key in ('failure_class', 'is_valid', 'score_attempt_used'):
        if manifest.get(key) != verdict.get(key):
            raise ValueError(f'Judge manifest/verdict disagree on {key}')
    validation = manifest.get('validation_errors', [])
    if not isinstance(validation, list) or not all(isinstance(item, str) for item in validation):
        raise ValueError('validation_errors must be an array of strings')
    new = manifest.get('schema_version') == 'pipeline-judge-manifest-v2'
    provenance = manifest.get('provenance', {})
    if not isinstance(provenance, dict):
        raise ValueError('provenance must be an object')
    fingerprint = provenance.get('request_sha256')
    if fingerprint is not None and (not isinstance(fingerprint, str) or not re.fullmatch(r'[0-9a-f]{64}', fingerprint)):
        raise ValueError('request_sha256 must be a hexadecimal SHA256 string')
    if new:
        if provenance.get('verdict_sha256') != sha256(verdict_path):
            raise ValueError('Judge verdict changed since its manifest was written')
        if fingerprint is None:
            raise ValueError('missing frozen request fingerprint')
    # Old records without quote-check provenance are not promoted into accepted
    # diagnoses merely because they contain failure_class and is_valid.
    if manifest.get('status') == 'review_rejected' and not validation:
        if new:
            raise ValueError('review_rejected manifest lacks validation errors')
        validation = ['legacy review rejected; detailed validation errors unavailable']
    mode = manifest.get('mode', 'unknown')
    dimensions = reporting_dimensions(verdict, mode=mode, validation_errors=validation,
        validation_performed=new and mode not in {'insufficient_evidence', 'rules_failed', 'rules_error'}, legacy=not new)
    if new and manifest.get('reporting') != dimensions:
        raise ValueError('stored reporting dimensions disagree with deterministic mapping')
    attribution = verdict.get('failure_attribution') if new else None
    if attribution is not None and (not isinstance(attribution, str) or attribution not in FAILURE_ATTRIBUTIONS):
        raise ValueError('unrecognized failure_attribution')
    if new and manifest.get('failure_attribution') != attribution:
        raise ValueError('Judge manifest/verdict disagree on failure_attribution')
    task = manifest.get('task', {})
    if not isinstance(task, dict):
        raise ValueError('task metadata must be an object')
    variant = task.get('variant')
    if variant not in ('blind', 'guided'):
        variant = 'unknown'
    return {'judge_manifest': str(manifest_path.resolve()), 'judge_verdict': str(verdict_path.resolve()),
            'task_id': task.get('task_id'), 'variant': variant,
            'score_attempt_used': verdict.get('score_attempt_used'),
            'request_sha256': provenance.get('request_sha256'),
            'failure_class': category, 'is_valid': verdict['is_valid'],
            'failure_attribution': attribution,
            **{key: value for key, value in dimensions.items() if key != 'schema_version'},
            'validation_errors': validation, 'reason': verdict.get('reason', '')}


def counts(rows: list[dict]) -> dict:
    def tally(key, choices=()):
        values = Counter(row[key] for row in rows if row[key] is not None)
        return {value: values.get(value, 0) for value in sorted(set(choices) | set(values))}
    return {'records': len(rows),
            'accepted_judge_claims': sum(row['decision_status'] == 'accepted' for row in rows),
            'raw_failure_class': tally('failure_class', CLASSES),
            'decision_status': tally('decision_status', ('accepted', 'evidence_insufficient', 'review_rejected', 'legacy_unverified')),
            'statistics_group': tally('statistics_group', CLASSES | {'evidence_insufficient', 'review_rejected', 'legacy_unverified'}),
            'validity_status': tally('validity_status', ('valid', 'invalid_source_or_shortcut', 'undetermined')),
            'invalidity_type': tally('invalidity_type', INVALIDITY_CLASSES),
            'failure_attribution': tally('failure_attribution', FAILURE_ATTRIBUTIONS),
            'failure_diagnosis': tally('failure_diagnosis', DIAGNOSTIC_CLASSES)}


def summarize(judge_roots: list[Path], output: Path) -> dict:
    if output.exists():
        raise ValueError(f'refusing to overwrite summary: {output}')
    paths = set()
    for root in judge_roots:
        if not root.exists():
            raise ValueError(f'Judge root does not exist: {root}')
        if root.is_file():
            if root.name != 'judge_manifest.json':
                raise ValueError('explicit input file must be judge_manifest.json')
            paths.add(root.resolve())
        else:
            paths.update(path.resolve() for path in root.rglob('judge_manifest.json'))
    if not paths:
        raise ValueError('no judge_manifest.json files found; this command expects Stage3 validity Judge outputs')
    rows, errors = [], []
    for path in sorted(paths):
        try:
            rows.append(load_record(path))
        except (OSError, ValueError, KeyError, TypeError) as error:
            errors.append({'judge_manifest': str(path), 'error': str(error)})
    units = defaultdict(list)
    for row in rows:
        if row['request_sha256']:
            units[row['request_sha256']].append(row['judge_manifest'])
    duplicates = [{'request_sha256': key, 'judge_manifests': value}
                  for key, value in sorted(units.items()) if len(value) > 1]
    summary = {'schema_version': 'pipeline-judge-summary-v1', 'reporting_schema': REPORTING_SCHEMA,
        'status': 'input_errors' if errors else 'complete',
        'unit': 'judge_record (one output directory; repeated reviews of an attempt count separately)',
        'discovered_records': len(paths), 'included_records': len(rows), 'excluded_records': len(errors),
        'overall': counts(rows),
        'by_variant': {variant: counts([row for row in rows if row['variant'] == variant])
                       for variant in ('blind', 'guided', 'unknown')},
        'duplicate_frozen_requests': duplicates, 'input_errors': errors,
        'interpretation': [
            'raw_failure_class preserves seven-class output; use statistics_group for diagnosis totals.',
            'Evidence-insufficient/rejected/legacy-unverified records are not task-contract diagnoses.',
            'Accepted means schema/attempt/quotes passed checks, not human-confirmed semantic correctness.',
            'Each of the three invalidity types is counted separately; no intent or cheating rate is inferred.',
            'Engineering/resource/contract diagnoses do not by themselves establish submission provenance validity.',
            'Null diagnosis means not separately classified by the single-label verdict, not proof of absence.',
            'These are Judge records, not scientific accuracy, capability scores, unique trajectories, or training gold.',
            'Transport/authentication failures without a finalized judge_manifest.json are outside these totals.',
            'Duplicates are reported and kept; select the intended batch before comparing Blind/Guided.',
        ]}
    output.mkdir(parents=True)
    write_json(output / 'summary.json', summary)
    with (output / 'records.jsonl').open('w', encoding='utf-8') as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
    columns = ['task_id', 'variant', 'score_attempt_used', 'failure_class', 'is_valid', 'decision_status',
               'validity_status', 'invalidity_type', 'failure_diagnosis', 'failure_attribution', 'statistics_group',
               'evidence_status', 'verdict_origin', 'reason', 'judge_manifest', 'judge_verdict']
    with (output / 'records.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--judge-root', type=Path, action='append', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        summary = summarize(args.judge_root, args.output)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 1 if summary['input_errors'] else 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({'status': 'error', 'error': str(error)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
