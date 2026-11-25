"""Deterministic reporting over the unchanged seven-class Judge protocol.

These dimensions describe a validated Judge *claim*, never human ground truth,
intent, scientific capability, or a new decision made by this module.
"""
from __future__ import annotations

INVALIDITY_CLASSES = frozenset({'scientific_invalid', 'placeholder_or_degenerate',
                               'synthetic_substitution'})
DIAGNOSTIC_CLASSES = frozenset({'engineering_failure', 'data_unavailable', 'task_contract_error'})
CLASSES = INVALIDITY_CLASSES | DIAGNOSTIC_CLASSES | {'valid'}
FAILURE_ATTRIBUTIONS = frozenset({'task_contract_fail', 'agent_capability_fail',
                                 'infrastructure_fail', 'scientific_validity_fail'})
REPORTING_SCHEMA = 'pipeline-judge-reporting-v1'

LEGACY_FIELDS = {'cheating_class': {
    'deprecated': True,
    'meaning': 'Historical membership in the three invalidity categories; NOT a cheating finding.',
    'replacement': 'reporting.invalidity_type',
}}


def has_insufficient_marker(value) -> bool:
    return any(marker in str(value) for marker in ('insufficient_evidence', '裁判证据不足'))


def declared_insufficient(verdict: dict) -> bool:
    """Accept the English marker and frozen historical Chinese markers."""
    issues = verdict.get('contract_issues')
    return verdict.get('failure_class') == 'task_contract_error' and (
        has_insufficient_marker(verdict.get('reason', '')) or
        isinstance(issues, list) and any(has_insufficient_marker(item) for item in issues))


def reporting_dimensions(verdict: dict, *, mode: str, validation_errors: list[str],
                         validation_performed: bool, legacy: bool = False) -> dict:
    category = verdict['failure_class']
    if category not in CLASSES:
        raise ValueError('unknown seven-class Judge category')
    if validation_errors:
        decision, group = 'review_rejected', 'review_rejected'
        origin, evidence = 'framework_fallback', 'insufficient'
    elif mode in {'rules_failed', 'rules_error'}:
        decision, group = mode, mode
        origin, evidence = 'executable_rules', 'not_assessed'
    elif mode == 'insufficient_evidence':
        decision, group = 'evidence_insufficient', 'evidence_insufficient'
        origin, evidence = 'framework_fallback', 'insufficient'
    elif declared_insufficient(verdict):
        decision, group = 'evidence_insufficient', 'evidence_insufficient'
        origin, evidence = mode, 'insufficient'
    elif legacy or not validation_performed:
        decision, group = 'legacy_unverified', 'legacy_unverified'
        origin, evidence = mode, 'not_checked'
    else:
        decision, group = 'accepted', category
        origin, evidence = mode, 'exact_quotes_verified'
    substantive = decision == 'accepted'
    return {
        'schema_version': REPORTING_SCHEMA,
        'decision_status': decision,
        'statistics_group': group,
        'validity_status': ('valid' if category == 'valid' else 'invalid_source_or_shortcut'
                            if category in INVALIDITY_CLASSES else 'undetermined')
                           if substantive else 'undetermined',
        'invalidity_type': category if substantive and category in INVALIDITY_CLASSES else None,
        'failure_diagnosis': category if substantive and category in DIAGNOSTIC_CLASSES else None,
        'evidence_status': evidence,
        'verdict_origin': origin,
        'validation_performed': validation_performed,
        'validation_scope': 'JSON schema, locked attempt and exact quotes; not semantic truth or human review',
        'assessment_source': 'judge_claim' if substantive else 'no_substantive_assessment',
        'intent': 'not_assessed',
        'scientific_capability': 'not_assessed',
    }
