#!/usr/bin/env python3
"""One Stage3 invocation: deliver case -> prompt03 -> freeze selected score -> prompt04."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shlex
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError

CODE = Path(__file__).resolve().parent
if str(CODE) not in sys.path:
    sys.path.insert(0, str(CODE))
_module = importlib.util.spec_from_file_location('_pipeline_stage3_core', CODE / 'pipeline.py')
core = importlib.util.module_from_spec(_module)
_module.loader.exec_module(core)
from score_attempt import now, read_json, sha256, timestamp, timeline_records, write_json
from scenarios import load_scenario, prepare_effective_task
from validity_judge import judge_bundle

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline_runtime import setting


def reference(path: Path) -> dict:
    return {'path': str(path.resolve()), 'sha256': sha256(path)}


def select_attempt(export: Path, *, policy: str = 'latest-scored', attempt_id: str | None = None) -> dict:
    """Choose by prespecified identity/time only. Never inspect metric values."""
    if policy not in {'latest-scored', 'explicit'}:
        raise ValueError('attempt policy must be latest-scored or explicit; best hidden score selection is unsupported')
    if policy == 'explicit' and not attempt_id:
        raise ValueError('explicit policy requires attempt_id')
    if policy != 'explicit' and attempt_id is not None:
        raise ValueError('attempt_id requires explicit policy')
    value = read_json(export)
    path = Path(value['timeline'])
    if not path.is_absolute():
        path = export.parent / path
    events = [e for e in timeline_records(path) if e.get('event') == 'SCORE_ATTEMPT']
    ids = [e.get('attempt_id') for e in events]
    if not events or len(set(ids)) != len(ids):
        raise ValueError('scoring evidence has no unique SCORE_ATTEMPT records')
    if policy == 'explicit':
        candidates = [e for e in events if str(e.get('attempt_id')) == attempt_id]
        if len(candidates) != 1:
            raise ValueError('explicit SCORE_ATTEMPT was not exported')
        chosen = candidates[0]
    else:
        # Equal timestamps use scorer log order. Failed/missing submissions are
        # deliberately eligible: filtering them would cherry-pick successful runs.
        chosen = max(enumerate(events), key=lambda item: (timestamp(item[1].get('scored_at', item[1].get('timestamp'))), item[0]))[1]
    snapshots = [x for x in value.get('submission_snapshots', []) if str(x.get('attempt_id')) == str(chosen['attempt_id'])]
    if len(snapshots) != 1:
        raise ValueError('selected scoring event has no exact exported snapshot/status')
    return chosen


def run_workflow(*, output: Path, package: Path | None = None, pair: Path | None = None,
                 variant: str = 'blind', model: str = '', agent: str = 'qwen-coder',
                 harbor: str = 'harbor', gateway: bool = False, command: list[str] | None = None,
                 dry_run: bool = False, solver_prompt: Path | None = None,
                 judge_prompt: Path | None = None, judge_endpoint: str | None = None,
                 judge_model: str | None = None, judge_api_key_env: str = 'OPENAI_API_KEY',
                 judge_timeout_sec: int = 300, judge_max_input_bytes: int = 8_000_000,
                 judge_temperature: float = 0, judge_max_output_tokens: int | None = None,
                 verdict_file: Path | None = None, scenario: Path | None = None,
                 rollout_seed: int | None = None, agent_kwargs: dict | None = None,
                 solver_base_url: str | None = None, solver_api_key_env: str = 'OPENAI_API_KEY',
                 env_file: Path | None = None, attempt_policy: str = 'latest-scored',
                 attempt_id: str | None = None, diagnostic: bool = False, task_image: str | None = None,
                 solver_proxy_url: str | None = None, reference_overlay: Path | None = None) -> dict:
    if diagnostic and (package is None or not task_image):
        raise ValueError('Diagnostic run requires --package and --task-image')
    if task_image and pair is not None:
        raise ValueError('Bind --task-image when preparing a package/pair; an existing pair is immutable')
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('refusing to overwrite Stage3 workflow: ' + str(output))
    if (package is None) == (pair is None):
        raise ValueError('provide exactly one verified package or pair manifest')
    if variant not in {'blind', 'guided'}:
        raise ValueError('variant must be blind or guided')
    if attempt_policy not in {'latest-scored', 'explicit'} or (attempt_policy == 'explicit') != (attempt_id is not None):
        raise ValueError('use latest-scored without ID, or explicit with attempt_id')
    if verdict_file is not None and (judge_endpoint or judge_model):
        raise ValueError('imported verdict and API judge cannot both be selected')
    if (judge_endpoint is None) != (judge_model is None):
        raise ValueError('judge_endpoint and judge_model must be supplied together')
    # Keep long-running Qwen defaults in the pipeline configuration, while
    # allowing a caller to override them for another agent.
    if not agent_kwargs:
        agent_kwargs = setting('agent_kwargs')
    agent_kwargs = core.validate_agent_kwargs(agent, agent_kwargs, harbor_adapter=command is None)
    core.validate_model_endpoint(solver_base_url, 'solver base URL')
    core.validate_model_endpoint(judge_endpoint, 'judge endpoint')
    if env_file is not None and not env_file.is_file():
        raise ValueError('model env file does not exist')
    if reference_overlay is not None:
        reference_overlay = Path(reference_overlay).resolve()
        if reference_overlay.is_symlink() or not reference_overlay.is_dir():
            raise ValueError('reference overlay must be a regular directory')
    # Validate definition before copying a scientific task or launching a solver.
    definition, _ = load_scenario(scenario)
    solver_prompt = solver_prompt or CODE.parent / 'prompt/03_solver_instruction.md'
    judge_prompt = judge_prompt or CODE.parent / 'prompt/04_run_verify.md'
    for role, prompt in [('solver', solver_prompt), ('judge', judge_prompt)]:
        if not prompt.is_file() or not prompt.read_text(encoding='utf-8').strip():
            raise ValueError(role + ' prompt must be nonempty UTF-8 text')
    if package is not None:
        package = package.resolve()
        if output.is_relative_to(package) or package.is_relative_to(output):
            raise ValueError('workflow output must be separate from verified package')
    else:
        prepared, _, _ = core._load_pair(pair.resolve(), variant)
        protected = [Path(prepared['source_package']), *(Path(v['path']) for v in prepared['variants'].values())]
        if any(output.is_relative_to(path.resolve()) or path.resolve().is_relative_to(output) for path in protected):
            raise ValueError('workflow output must be separate from source and every prepared task variant')
    output.mkdir(parents=True)
    started = time.monotonic()
    result = {'schema_version': 'pipeline-stage3-result-v1', 'status': 'preparing',
              'created_at': now(), 'diagnostic_only': diagnostic, 'variant': variant, 'model': model, 'agent': agent,
              'scenario_id': definition['scenario_id'], 'rollout_seed': rollout_seed,
              'seed_scope': 'experiment_identity_only_not_provider_sampling',
              'attempt_policy': attempt_policy, 'requested_attempt_id': attempt_id,
              'score_attempt_used': None, 'scientific_score': None,
              'paper_comparison': {'status': 'not_run', 'g': None, 'eligible_for_paper_comparison': False},
              'solver': {'status': 'not_started'}, 'judge': {'status': 'not_started'}, 'artifacts': {}}
    if reference_overlay is not None:
        result['artifacts']['reference_overlay'] = {
            'path': str(reference_overlay),
            'sha256': core.package_fingerprint(reference_overlay),
            'visibility': 'evaluator_only_until_effective_input_freeze',
        }
    def finish(status: str) -> dict:
        result['failure_attribution'] = result['judge'].get('failure_attribution')
        result.update(status=status, finished_at=now(), wall_time_sec=time.monotonic() - started)
        from pipeline.stage3_execution.evidence.result_accounting import classify, summarize
        result['result_accounting'] = classify(result)
        result['paper_comparison']['eligible_for_paper_comparison'] = result['result_accounting']['eligible_for_paper_comparison']
        write_json(output/'score_summary.json',summarize([result]))
        write_json(output / 'result.json', result)
        return result
    try:
        if diagnostic:
            from diagnostic_support import prepare_package
            package = prepare_package(package, output / 'input/diagnostic_package', task_image)
            result['artifacts']['diagnostic_adapter'] = reference(package / 'diagnostic.json')
        if pair is None:
            core.prepare_variants(package, output / 'input/base_pair', diagnostic=diagnostic, task_image=task_image)
            pair = output / 'input/base_pair/pair_manifest.json'
        pair = pair.resolve()
        manifest, spec, task = core._load_pair(pair, variant, diagnostic=diagnostic)
        result['task_id'] = spec['task_id']
        result['artifacts']['pair_manifest'] = reference(pair)
        from formal_resources import preparation
        prepared = preparation(Path(manifest['source_package']), spec, receipt=manifest['source_readiness'],
                               image=manifest.get('task_image'), reference_overlay=reference_overlay,
                               solver_proxy_url=solver_proxy_url) if not dry_run and not diagnostic else None
        effective = prepare_effective_task(package=task, output=output / 'input/effective',
                    spec=spec, layout=core.package_layout(spec), scenario=scenario,
                    solver_prompt=solver_prompt, private_source=Path(manifest['source_package']),
                    private_variants=[Path(v['path']) for v in manifest['variants'].values()],
                    forbidden_sha256=[manifest['method_sha256']],
                    reference_overlay=reference_overlay, prepared_resources=prepared, verify_resources=not dry_run and not diagnostic)
        effective_path = output / 'input/effective/scenario_manifest.json'
        result['artifacts']['input_manifest'] = reference(effective_path)
        result['artifacts']['solver_prompt'] = effective['solver_guidance']['source']
        result['artifacts']['solver_execution_prompt'] = effective['solver_guidance']['execution']
        prompt_dir = output / 'input/prompts'
        prompt_dir.mkdir()
        # The exact judge prompt is frozen before any model execution.
        frozen_judge_prompt = prompt_dir / '04_run_verify.md'
        frozen_judge_prompt.write_bytes(judge_prompt.read_bytes())
        result['artifacts']['judge_prompt'] = reference(frozen_judge_prompt)
        config = {'schema_version': 'pipeline-stage3-config-v1', 'pair': str(pair), 'variant': variant,
                  'model': model, 'agent': agent, 'harbor': harbor, 'gateway': gateway,
                  'agent_kwargs': agent_kwargs or {}, 'solver_base_url': solver_base_url,
                  'solver_api_key_env': solver_api_key_env, 'env_file': str(env_file.resolve()) if env_file else None,
                  'solver_prompt_sha256': effective['solver_guidance']['source']['sha256'],
                  'solver_execution_prompt_sha256': effective['solver_guidance']['execution']['sha256'],
                  'judge_prompt_sha256': sha256(frozen_judge_prompt),
                  'judge_endpoint': judge_endpoint, 'judge_model': judge_model,
                  'judge_timeout_sec': judge_timeout_sec, 'judge_max_input_bytes': judge_max_input_bytes,
                  'judge_temperature': judge_temperature, 'judge_max_output_tokens': judge_max_output_tokens,
                  'judge_api_key_env': judge_api_key_env, 'scenario_id': effective['scenario_id'],
                  'scenario_manifest_sha256': sha256(effective_path), 'rollout_seed': rollout_seed,
                  'seed_scope': result['seed_scope'], 'attempt_policy': attempt_policy, 'attempt_id': attempt_id,
                  'command_adapter': command, 'dry_run': dry_run, 'diagnostic_only': diagnostic,
                  'task_image': task_image, 'solver_proxy_url': solver_proxy_url,
                  'reference_overlay': (result['artifacts'].get('reference_overlay')
                                        if reference_overlay is not None else None)}
        write_json(output / 'resolved_config.json', config)
        result['artifacts']['resolved_config'] = reference(output / 'resolved_config.json')
        solver_dir = output / '03_solver'
        run = core.run_variant(pair=pair, variant=variant, output=solver_dir, model=model,
                    agent=agent, harbor=harbor, gateway=gateway, command=command, dry_run=dry_run,
                    effective_input=effective_path, rollout_seed=rollout_seed, agent_kwargs=agent_kwargs,
                    solver_base_url=solver_base_url, solver_api_key_env=solver_api_key_env, env_file=env_file, diagnostic=diagnostic, solver_proxy_url=solver_proxy_url)
        result['run_id'] = run['run_id']
        result['model'] = run.get('model', model)
        result['solver'] = {'status': run['status'], 'execution_status': run.get('execution_status'),
                            'run_manifest': reference(solver_dir / 'run_manifest.json')}
        result['artifacts']['run_manifest'] = reference(solver_dir / 'run_manifest.json')
        if (solver_dir / 'events.jsonl').is_file():
            result['artifacts']['events'] = reference(solver_dir / 'events.jsonl')
        if run.get('interaction_log'):
            result['artifacts']['interaction_events'] = run['interaction_log']
        result['scenario_execution'] = run.get('interaction', {'status': 'not_applicable', 'mode': None})
        (output / 'command.sh').write_text('#!/usr/bin/env bash\nset -euo pipefail\n' + shlex.join(run['command']) + '\n', encoding='utf-8')
        if dry_run:
            result['judge']['status'] = 'planned' if verdict_file or judge_endpoint else 'not_configured'
            return finish('dry_run')
        if run['status'] != 'evidence_exported':
            if diagnostic:
                from validity_judge import insufficient_verdict
                review = output / '04_run_verify/review'
                reason = 'No evaluator-owned scoring event was collected; ' + str(run.get('evidence_issue', run.get('failure', run['status'])))
                verdict = insufficient_verdict({'score_attempt_used':None}, [reason])
                write_json(review / 'judge_verdict.json', verdict)
                write_json(review / 'judge_manifest.json', {'schema_version':'pipeline-diagnostic-judge-v1',
                    'mode':'insufficient_evidence','diagnostic_only':True,'model_called':False,
                    'scientific_score_recalculated':False,'reason':reason,
                    'run_manifest':reference(solver_dir / 'run_manifest.json')})
                result['judge'] = {'status':'insufficient_evidence','model_called':False,
                    'verdict':reference(review / 'judge_verdict.json'),
                    'manifest':reference(review / 'judge_manifest.json')}
                return finish('diagnostic_missing_score_evidence')
            result['judge']['status'] = 'not_run_no_score_evidence'
            return finish('solver_failed' if run.get('execution_status') == 'failed' else 'missing_score_evidence')
        export = Path(run['export'])
        result['artifacts']['run_export'] = reference(export)
        exported = read_json(export)
        for key in ('trajectory', 'timeline'):
            result['artifacts'][key] = reference(Path(exported[key]))
        selected = select_attempt(export, policy=attempt_policy, attempt_id=attempt_id)
        result['score_attempt_used'] = str(selected['attempt_id'])
        result['scientific_score'] = selected.get('metrics')
        result['score_status'] = selected.get('status', 'not_recorded')
        bundle = output / '04_run_verify/evidence'
        core.freeze_export(pair=pair, variant=variant, export=export,
                           attempt_id=result['score_attempt_used'], output=bundle, diagnostic=diagnostic)
        result['artifacts']['bundle'] = {'path': str(bundle.resolve()), 'manifest_sha256': sha256(bundle / 'manifest.json')}
        from score_attempt import load_frozen
        from paper_comparison import calculate
        frozen = load_frozen(bundle)
        result['paper_comparison'] = calculate(frozen.get('paper_reference'), frozen['score_attempt'], diagnostic=diagnostic)
        result['submission_sha256'] = frozen['submission']['sha256']
        from score_attempt import validate_rule_binding
        rules = validate_rule_binding(frozen)
        result['rules'] = rules if rules else {'status': 'legacy_not_recorded'}
        if (not rules or rules['status'] == 'pass') and not verdict_file and not judge_endpoint:
            result['judge']['status'] = 'not_configured'
            return finish('judge_not_configured')
        result['judge']['status'] = 'running'
        judge = judge_bundle(bundle=bundle, output=output / '04_run_verify/review',
                             verdict_file=verdict_file, endpoint=judge_endpoint, model=judge_model,
                             api_key_env=judge_api_key_env, prompt_path=frozen_judge_prompt,
                             timeout_sec=judge_timeout_sec, max_input_bytes=judge_max_input_bytes,
                             temperature=judge_temperature, max_output_tokens=judge_max_output_tokens,
                             env_file=env_file)
        result['judge'] = {'status': judge['status'], 'mode': judge.get('mode'),
                           'model_called': judge.get('model_called'), 'failure_class': judge['failure_class'],
                           'is_valid': judge['is_valid'], 'reporting': judge.get('reporting'),
                           'failure_attribution': judge.get('failure_attribution'),
                           'score_attempt_used': judge.get('score_attempt_used'),
                           'manifest': reference(output / '04_run_verify/review/judge_manifest.json'),
                           'verdict': reference(output / '04_run_verify/review/judge_verdict.json')}
        for field, filename in (('api_usage', 'api_usage.json'), ('raw_response', 'api_response.bin'),
                                ('imported_response', 'imported_response.bin')):
            artifact = output / '04_run_verify/review' / filename
            if artifact.is_file():
                result['judge'][field] = reference(artifact)
        if judge.get('mode') in {'rules_failed', 'rules_error'}:
            return finish(judge['mode'])
        if judge['status'] != 'complete':
            return finish('judge_rejected')
        if judge.get('mode') == 'insufficient_evidence' or judge.get('reporting', {}).get('decision_status') == 'evidence_insufficient':
            return finish('insufficient_judge_evidence')
        if result['scenario_execution']['status'] in {'events_not_triggered', 'controller_failed', 'requests_pending'}:
            return finish('scenario_incomplete')
        if run.get('execution_status') == 'failed':
            return finish('solver_failed')
        # Completion is workflow completion, never an assertion of validity or a
        # high scientific score. The paired verdict retains all seven classes.
        return finish('complete')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result['error'] = str(exc)
        if result['judge']['status'] == 'running' and isinstance(exc, (URLError, TimeoutError, ConnectionError)):
            result['judge']['failure_attribution'] = 'infrastructure_fail'
        if result['solver']['status'] == 'evidence_exported':
            result['judge']['status'] = 'failed'
        return finish('error')


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument('--package', type=Path)
    source.add_argument('--pair', type=Path)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--variant', choices=['blind', 'guided'], default=setting('variant')[0])
    p.add_argument('--model', default=setting('model')[0])
    p.add_argument('--agent', default=setting('agent'))
    p.add_argument('--harbor', default=setting('harbor'))
    p.add_argument('--gateway', action='store_true')
    p.add_argument('--command-config', type=Path)
    p.add_argument('--solver-prompt', type=Path, default=setting('prompt03'))
    p.add_argument('--judge-prompt', type=Path, default=setting('prompt04'))
    p.add_argument('--judge-endpoint')
    p.add_argument('--judge-model')
    p.add_argument('--judge-api-key-env', default=setting('judge_api_key_env'))
    p.add_argument('--judge-timeout-sec', type=int, default=setting('judge_timeout_sec'))
    p.add_argument('--judge-max-input-bytes', type=int, default=setting('judge_max_input_bytes'))
    p.add_argument('--judge-temperature', type=float, default=setting('judge_temperature'))
    p.add_argument('--judge-max-output-tokens', type=int, default=setting('judge_max_output_tokens'))
    p.add_argument('--verdict-file', type=Path)
    p.add_argument('--scenario', type=Path)
    p.add_argument('--rollout-seed', type=int)
    p.add_argument('--agent-kwarg', action='append', default=[], metavar='NAME=JSON')
    p.add_argument('--solver-proxy-url', help='HTTP(S) download proxy for public-network Solver only')
    p.add_argument('--solver-base-url')
    p.add_argument('--solver-api-key-env', default=setting('api_key_env'))
    p.add_argument('--env-file', type=Path)
    p.add_argument('--attempt-policy', choices=['latest-scored', 'explicit'], default=setting('attempt_policy'))
    p.add_argument('--attempt-id')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--diagnostic', action='store_true', help='Explicit unverified package trial; does not grant readiness')
    p.add_argument('--task-image', help='Prebuilt image to bind to run-owned task copies; use with --package')
    p.add_argument('--reference-overlay', type=Path,
                   help='Trusted run-owned directory containing prepared formal references; never passed to Solver directly')
    return p


def main(argv: list[str] | None = None) -> int:
    args = vars(parser().parse_args(argv))
    try:
        kwargs = {}
        for item in args.pop('agent_kwarg'):
            key, sep, raw = item.partition('=')
            if not sep or key in kwargs:
                raise ValueError('agent kwargs must be unique NAME=JSON values')
            kwargs[key] = json.loads(raw)
        args['agent_kwargs'] = kwargs
        if args['judge_endpoint'] and not args['judge_model']:
            args['judge_model'] = setting('judge_model')
        config = args.pop('command_config')
        args['command'] = read_json(config)['command'] if config else None
        result = run_workflow(**args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['status'] in {'complete', 'dry_run'} else 2
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'error', 'error': str(exc)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
