"""Keep measurements, validity and infrastructure status separate in reports."""
from __future__ import annotations
from collections import Counter
import math
from statistics import mean

SCORE_FAILURES = {'missing_submission','invalid_submission','unscorable','runtime_error'}


def _metrics(value):
    if type(value) in (int,float) and math.isfinite(value):
        return {'score':value}
    if isinstance(value,dict) and value and all(isinstance(k,str) and type(v) in (int,float) and math.isfinite(v) for k,v in value.items()):
        return dict(value)
    return None


def classify(value):
    judge = value.get('judge') if isinstance(value.get('judge'),dict) else {}
    score = _metrics(value.get('scientific_score'))
    status = value.get('score_status')
    inferred = status in (None,'not_recorded')
    if status in SCORE_FAILURES:
        score = None
        measurement = status
    elif status == 'success':
        measurement = 'scored' if score is not None else 'score_protocol_error'
    elif inferred:
        # Historical exports sometimes contain a metric but no typed status.
        # Preserve it as an observed historical measurement, with inference shown.
        failed = value.get('status') in {'solver_failed','solver_execution_failed','execution_error',
            'network_preflight_failed','missing_score_evidence','diagnostic_missing_score_evidence','error','failed'}
        measurement = 'scored' if score is not None and not failed else 'unscored'
        if failed:
            score = None
    else:
        measurement,score = 'score_protocol_error',None
    valid = judge.get('is_valid',value.get('is_valid'))
    category = judge.get('failure_class',value.get('judge_class'))
    reporting = judge.get('reporting') or value.get('judge_reporting') or {}
    if reporting.get('decision_status') in {'rules_failed', 'rules_error'} or value.get('status') in {'rules_failed', 'rules_error'}:
        validity = 'unreviewed'
    elif reporting.get('decision_status') == 'review_rejected' or judge.get('status',value.get('judge_status')) in {'failed','review_rejected'} or value.get('status') in {'judge_rejected','judge_error'}:
        validity = 'judge_error'
    elif reporting.get('decision_status') == 'evidence_insufficient' or judge.get('status',value.get('judge_status')) == 'insufficient_evidence' or value.get('status') == 'insufficient_judge_evidence' or reporting.get('evidence_sufficient') is False:
        validity = 'insufficient_evidence'
    elif reporting.get('decision_status') == 'legacy_unverified':
        validity = 'unreviewed'
    elif valid is True:
        validity = 'valid'
    elif valid is False:
        validity = 'invalid'
    else:
        validity = 'unreviewed'
    diagnostic = value.get('diagnostic_only') is True
    comparable = measurement == 'scored' and validity == 'valid' and not diagnostic and value.get('rules', {}).get('status', 'pass') in {'pass', 'legacy_not_recorded'}
    paper = value.get('paper_comparison') or {}
    if not isinstance(paper, dict): paper = {}
    reporting_valid = reporting.get('decision_status') == 'accepted' and reporting.get('validity_status') == 'valid'
    judge_attempt = judge.get('score_attempt_used', value.get('judge_score_attempt_used'))
    selected = value.get('score_attempt_used')
    linked = (isinstance(selected, str) and bool(selected) and paper.get('attempt_id') == selected == judge_attempt and
              isinstance(value.get('submission_sha256'), str) and paper.get('submission_sha256') == value['submission_sha256'])
    gap = paper.get('g')
    finite_gap = type(gap) in (int, float) and math.isfinite(gap)
    paper_eligible = (comparable and reporting_valid and linked and paper.get('status') == 'computed' and finite_gap and
                      value.get('status') == 'complete')
    return {'paper_comparison_status': paper.get('status', 'unavailable'), 'paper_gap': gap if finite_gap else None,
        'eligible_for_paper_comparison': paper_eligible,
        'measurement_status':measurement,'metrics':score if measurement == 'scored' else None,
        'score_status_inferred':inferred,'validity_status':validity,'failure_class':category,
        'diagnostic_only':diagnostic,'eligible_for_comparison':comparable,
        'scientific_zero':measurement == 'scored' and score is not None and all(v == 0 for v in score.values())}


def summarize(rows):
    items = [classify(row) for row in rows]
    counts = Counter(item['measurement_status'] for item in items)
    validities = Counter(item['validity_status'] for item in items)
    by_task = {}
    for row,item in zip(rows,items):
        task = str(row.get('task_id','unknown'))
        record = by_task.setdefault(task,{'runs':0,'scored_runs':0,'comparable_runs':0,'metrics':{}})
        record['runs'] += 1
        if item['measurement_status'] != 'scored':
            continue
        record['scored_runs'] += 1
        record['comparable_runs'] += int(item['eligible_for_comparison'])
        for name,value in item['metrics'].items():
            metric = record['metrics'].setdefault(name,{'observed':[],'verified':[]})
            metric['observed'].append(value)
            if item['eligible_for_comparison']:
                metric['verified'].append(value)
    for record in by_task.values():
        record['metrics'] = {name:{'measured_mean':mean(v['observed']),
            'verified_mean':mean(v['verified']) if v['verified'] else None,
            'measured_runs':len(v['observed']),'verified_runs':len(v['verified'])}
            for name,v in record['metrics'].items()}
    paper_tasks = {}
    for row, item in zip(rows, items):
        if item['eligible_for_paper_comparison']:
            # Do not mix task revisions/anchors in one mean.
            key = (str(row.get('task_id', 'unknown')), row['paper_comparison'].get('reference_sha256'))
            paper_tasks.setdefault(key, []).append(item['paper_gap'])
    paper_groups = [{'task_id': task, 'reference_sha256': reference, 'eligible_runs': len(gaps),
                     'mean_g': mean(gaps)} for (task, reference), gaps in sorted(paper_tasks.items(), key=lambda x: str(x[0]))]
    n = len(rows)
    scored = counts['scored']
    return {'schema_version':'pipeline-result-accounting-v1','scheduled_runs':n,
        'scored_runs':scored,'unscored_runs':n-scored,'scoring_coverage':scored/n if n else None,
        'scientific_zero_runs':sum(i['scientific_zero'] for i in items),
        'diagnostic_runs':sum(i['diagnostic_only'] for i in items),
        'comparable_runs':sum(i['eligible_for_comparison'] for i in items),
        'measurement_status_counts':dict(counts),'validity_status_counts':dict(validities),
        'per_task':by_task,
        'paper_comparison': {'eligible_runs': sum(i['eligible_for_paper_comparison'] for i in items),
            'status_counts': dict(Counter(i['paper_comparison_status'] for i in items)),
            'per_task_reference': paper_groups,
            'policy': 'Only complete, accepted-valid, same-attempt formal comparisons. No zero/failure imputation; anchors/revisions remain separate.'},
        'policy':'No zero filling for invalid submissions, missing scores or evaluation failures. '
                 'Measured metrics and validity are separate. Diagnostic runs are excluded from verified means. '
                 'Means are per task and metric; scientific values from different tasks are not pooled.'}
