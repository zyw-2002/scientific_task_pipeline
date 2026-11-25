"""Attach a completed Judge review to an already scored workflow."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pipeline.shared.io.jsonio import read_json
from pipeline.stage3_execution.evidence.result_accounting import classify, summarize


def _reference(path: Path) -> dict:
    return {'path': str(path.resolve()),
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def complete_review(output: Path) -> dict:
    """Finish reporting without starting Solver again; require frozen provenance."""
    from pipeline._legacy_loader import implementation_paths
    output = Path(output).resolve()
    result = read_json(output / 'result.json')
    review = output / '04_run_verify/review'
    manifest = read_json(review / 'judge_manifest.json')
    verdict = read_json(review / 'judge_verdict.json')
    bundle = output / '04_run_verify/evidence'
    with implementation_paths(stage=3):
        from score_attempt import load_frozen, validate_rule_binding
        frozen = load_frozen(bundle)
        rules = validate_rule_binding(frozen)
    if (manifest.get('score_attempt_used') != result.get('score_attempt_used')
            or frozen.get('score_attempt_used') != result.get('score_attempt_used')):
        raise ValueError('Judge and workflow must refer to the same frozen score attempt')
    if frozen['submission']['sha256'] != result.get('submission_sha256'):
        raise ValueError('Judge submission differs from the scored workflow submission')
    provenance = manifest.get('provenance', {})
    if (provenance.get('request_sha256') != _reference(bundle / 'request.json')['sha256']
            or provenance.get('verdict_sha256') != _reference(review / 'judge_verdict.json')['sha256']):
        raise ValueError('Judge provenance does not match the frozen request and verdict')
    if verdict.get('score_attempt_used') != result['score_attempt_used']:
        raise ValueError('Judge verdict refers to another attempt')
    for key in ('run_manifest', 'run_export', 'trajectory', 'timeline'):
        reference = result['artifacts'][key]
        if _reference(Path(reference['path']))['sha256'] != reference['sha256']:
            raise ValueError('Workflow artifact changed: ' + key)
    result['judge'] = {key: manifest.get(key) for key in (
        'status', 'mode', 'model_called', 'failure_class', 'is_valid', 'reporting',
        'failure_attribution', 'score_attempt_used')}
    result['judge'].update(manifest=_reference(review / 'judge_manifest.json'),
                           verdict=_reference(review / 'judge_verdict.json'))
    for key, name in (('api_usage', 'api_usage.json'), ('raw_response', 'api_response.bin')):
        if (review / name).is_file():
            result['judge'][key] = _reference(review / name)
    mode = manifest.get('mode')
    if rules and rules.get('status') != 'pass':
        result['status'] = 'rules_failed' if rules['status'] == 'fail' else 'rules_error'
    elif manifest.get('status') != 'complete':
        result['status'] = 'judge_rejected'
    elif mode == 'insufficient_evidence' or manifest.get('reporting', {}).get('decision_status') == 'evidence_insufficient':
        result['status'] = 'insufficient_judge_evidence'
    elif result.get('scenario_execution', {}).get('status') in {'events_not_triggered', 'controller_failed', 'requests_pending'}:
        result['status'] = 'scenario_incomplete'
    elif result.get('solver', {}).get('execution_status') == 'failed':
        result['status'] = 'solver_failed'
    else:
        result['status'] = 'complete'
    result['failure_attribution'] = manifest.get('failure_attribution')
    result['result_accounting'] = classify(result)
    result.setdefault('paper_comparison', {})['eligible_for_paper_comparison'] = result['result_accounting']['eligible_for_paper_comparison']
    (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    (output / 'score_summary.json').write_text(json.dumps(summarize([result]), ensure_ascii=False, indent=2) + '\n')
    return result
