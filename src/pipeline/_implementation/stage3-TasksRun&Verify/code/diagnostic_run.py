#!/usr/bin/env python3
"""Use saved launcher settings to run one explicit 03 -> 04 diagnostic trial."""
import argparse
import json
import os
from pathlib import Path
from workflow import run_workflow
from pipeline_runtime import setting
from worker_runtime import model_process_env


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--package', type=Path, required=True)
    p.add_argument('--image', required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--variant', choices=['guided','blind'], default='guided')
    p.add_argument('--dry-run', action='store_true')
    a = p.parse_args()
    env_file = Path(setting('model_env_file'))
    env = model_process_env({'env_file':str(env_file)}, dict(os.environ))
    base = setting('model_base_url') or env.get('BASE_URL') or env.get('OPENAI_BASE_URL')
    if not base:
        p.error('No actual model endpoint configured')
    base = base.rstrip('/')
    if base.endswith('/chat/completions'):
        base = base[:-len('/chat/completions')]
    judge_endpoint = setting('judge_endpoint') or base + '/chat/completions'
    result = run_workflow(package=a.package, output=a.output, variant=a.variant,
        model=setting('model')[0], agent=setting('agent'), harbor=setting('harbor'),
        solver_prompt=Path(setting('prompt03')), judge_prompt=Path(setting('prompt04')),
        judge_endpoint=judge_endpoint, judge_model=setting('judge_model'),
        judge_api_key_env=setting('judge_api_key_env'), env_file=env_file,
        solver_base_url=base, solver_api_key_env=setting('api_key_env'),
        judge_timeout_sec=setting('judge_timeout_sec'),
        judge_max_output_tokens=setting('judge_max_output_tokens'),
        diagnostic=True, task_image=a.image, dry_run=a.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] in {'complete','dry_run'} else 2


if __name__ == '__main__':
    raise SystemExit(main())
