"""Verify a completed host receipt for an independent reviewer startup failure."""
import json
from pathlib import Path


def review_start_timeout(manifest: dict, build: Path) -> bool:
    """Recognize a terminal review startup failure with no new worker workspace."""
    stages = manifest.get('stages')
    if manifest.get('status') != 'failed' or not isinstance(stages, list) or not stages:
        return False
    stage = stages[-1]
    if not isinstance(stage, dict) or stage.get('stage') != '02_construct_verify':
        return False
    attempt = stage.get('attempt')
    if (stage.get('status') != 'worker_failed' or stage.get('retry_scheduled') is not False
            or type(attempt) is not int or attempt < 0):
        return False
    failure_path = build.resolve()/'metadata'/'worker_failures'/f'02_{attempt + 1:02d}.json'
    failures = manifest.get('worker_failures')
    if (not isinstance(failures, list) or failures[-1:] != [str(failure_path)]
            or stage.get('failure') != str(failure_path)):
        return False
    try:
        if failure_path.resolve(strict=True) != failure_path:
            return False
        record = json.loads(failure_path.read_bytes())
    except (OSError, ValueError, RuntimeError):
        return False
    if not isinstance(record, dict):
        return False
    details = record.get('details')
    return (record.get('schema_version') == 'pipeline-worker-failure-v1'
            and record.get('status') == 'failed' and record.get('code') == 'harbor_trial_failed'
            and record.get('frozen_inputs_intact') is True and record.get('retry_scheduled') is False
            and type(record.get('candidate_count')) is int and record['candidate_count'] == 0
            and record.get('packages') == [] and record.get('copy_errors') == []
            and isinstance(details, dict) and details.get('job_finished') is True
            and details.get('trial_count') == 1 and details.get('errored_trials') == 1
            and details.get('exception_types') == ['EnvironmentStartTimeoutError'])
