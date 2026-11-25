#!/usr/bin/env python3
"""Prepare equal Blind/Guided tasks, run isolated Harbor, freeze an attempt, judge it."""
from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

STAGE2_CODE = Path(__file__).resolve().parents[2] / 'stage2-TasksConstruction&Verify' / 'code'
sys.path.append(str(STAGE2_CODE))
from contract import package_fingerprint, package_layout, package_manifest, package_path, execution_fingerprint, validate_execution, read_reference, load_execution, load_metric
from preflight import validate_runtime_evidence
from worker_runtime import run_logged, model_process_env
from network_config import resolve_network, network_for_policy, make_compose_network_overlay
from urllib.parse import urlsplit
from score_attempt import freeze_attempt, now, read_json, sha256, timestamp, write_json
from validity_judge import judge_bundle


def validate_ready(package: Path, *, check_freshness: bool = True) -> tuple[dict, dict]:
    """Validate construction identity; legacy check_freshness no longer gates runtime."""
    spec = load_execution(package)
    errors = validate_execution(spec)
    if errors:
        raise ValueError('; '.join(errors))
    receipt = read_json(package / 'readiness.json')
    if receipt.get('contract_status') in ('REWORK', 'BLOCKED'):
        raise ValueError('stage2 task contract requires repair: ' + receipt['contract_status'])
    if receipt.get('schema_version') != 'pipeline-readiness-v2' or receipt.get('construction_ready', receipt.get('run_ready')) is not True:
        raise ValueError('stage2 did not accept construction for this package')
    package_sha = package_fingerprint(package)
    spec_sha = execution_fingerprint(spec)
    if receipt.get('package_sha256') != package_sha or receipt.get('execution_sha256') != spec_sha:
        raise ValueError('stage2 readiness is stale: package/execution configuration changed after final verification')
    if receipt.get('review_status') != 'complete':
        raise ValueError('final independent construction review is not complete')
    reports = {}
    for key in ('preflight', 'review'):
        reference = receipt.get(key)
        if not isinstance(reference, dict) or not isinstance(reference.get('path'), str):
            raise ValueError(f'readiness missing {key} provenance')
        path = Path(reference['path'])
        if not path.is_absolute():
            path = package / path
        try:
            reports[key] = read_reference(reference,package)
        except (OSError,ValueError,KeyError) as exc:
            raise ValueError(f'{key} provenance hash does not match: {exc}') from exc
    preflight = reports['preflight']
    if not (preflight.get('run_ready') is True or preflight.get('static_pass') is True) or preflight.get('package_sha256') != package_sha or preflight.get('execution_sha256') != spec_sha:
        raise ValueError('preflight does not certify the current package and execution configuration')
    review = reports['review']
    if review.get('status') != 'complete' or review.get('unresolved_blockers'):
        raise ValueError('final review is blocked or has unresolved_blockers')
    return spec, receipt


def _yaml_module():
    try:
        import yaml
    except ImportError as error:
        raise ValueError('preparing Harbor variants requires PyYAML (yaml.safe_load/safe_dump)') from error
    return yaml


def _volume(value: Any) -> tuple[str, str, bool]:
    if isinstance(value, dict):
        return str(value.get('source', '')), str(value.get('target', '')), value.get('read_only') is True
    if isinstance(value, str):
        parts = value.split(':')
        if len(parts) in (2, 3):
            return parts[0], parts[1], len(parts) == 3 and 'ro' in parts[2].split(',')
    return '', '', False


def prepare_variants(package: Path, output: Path, *, diagnostic: bool = False, task_image: str | None = None) -> dict:
    package, output = package.resolve(), output.resolve()
    if output.is_relative_to(package) or package.is_relative_to(output):
        raise ValueError('variant output must be separate from verified source')
    if output.exists():
        raise ValueError(f'refusing to overwrite variants: {output}')
    if diagnostic:
        from diagnostic_support import validate_diagnostic
        spec, receipt = validate_diagnostic(package)
    else:
        spec, receipt = validate_ready(package)
    layout = package_layout(spec)
    common = package / 'common_task'
    public = package_path(package, layout['public_dir'])
    compose = package_path(package, layout['compose_file'])
    method = package_path(package, layout['method_path'])
    if not method.is_file() or not method.read_text(encoding='utf-8').strip():
        raise ValueError('verified package must contain nonempty guidance/method.md')
    if not public.is_relative_to(common) or not compose.is_relative_to(common):
        raise ValueError('public directory and compose must be inside common_task')
    if (public / 'guidance' / 'method.md').exists():
        raise ValueError('common public task already exposes Method; Blind would leak guidance')
    yaml = _yaml_module()
    config = yaml.safe_load(compose.read_text(encoding='utf-8'))
    services = config.get('services', {}) if isinstance(config, dict) else {}
    solver_names = [name for name in layout['solver_services'] if name in services]
    if len(solver_names) != 1:
        raise ValueError('compose must identify exactly one configured solver service')
    solver = solver_names[0]
    mount_ok = False
    for volume in services[solver].get('volumes', []):
        source, target, readonly = _volume(volume)
        if target == '/task/problem':
            mount_ok = readonly and (compose.parent / source).resolve() == public
        if target.startswith('/task/problem/guidance'):
            raise ValueError('common compose must not already expose private method guidance')
    if not mount_ok:
        raise ValueError('common solver compose must bind its actual public directory to /task/problem read-only')
    if spec.get('solver_access') == 'online_search':
        from solver_access import configure_compose
        configure_compose(config, solver, spec['runtime']['network_policy'], solver_only=True)
    for name, service in services.items():
        build = service.get('build')
        context = build if isinstance(build, str) else build.get('context', '.') if isinstance(build, dict) else None
        if context and not (compose.parent / context).resolve().is_relative_to(common / 'environment'):
            raise ValueError(f'{name}: build context must stay inside common_task/environment; Method cannot enter image layers')
    if task_image:
        if not task_image.strip() or any(c.isspace() for c in task_image):
            raise ValueError('task image must be a nonempty image reference without whitespace')
        for service in services.values():
            service.pop('build', None)
            service['image'] = task_image
    source_hash = package_fingerprint(package)
    output.mkdir(parents=True)
    variants = {}
    compose_relative = compose.relative_to(common)
    for mode in ('blind', 'guided'):
        target = output / mode
        shutil.copytree(common, target, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        # The empty mount point exists in both variants; the common image context stays identical.
        (target / public.relative_to(common) / 'guidance').mkdir(parents=True, exist_ok=True)
        # A packaged private Method may live under common_task; remove it from
        # both copies, then deliberately deliver it to Guided only.
        packaged_guidance = target / 'guidance'
        if packaged_guidance.exists():
            shutil.rmtree(packaged_guidance)
        if method.is_relative_to(common):
            packaged_method = target / method.relative_to(common)
            if packaged_method.is_file():
                packaged_method.unlink()
        mode_config = copy.deepcopy(config)
        if mode == 'guided':
            guide_dir = target / 'guidance'
            guide_dir.mkdir()
            shutil.copyfile(method, guide_dir / 'method.md')
            relative_guide = os.path.relpath(guide_dir, (target / compose_relative).parent)
            mode_config['services'][solver].setdefault('volumes', []).append({
                'type': 'bind', 'source': relative_guide,
                'target': '/task/problem/guidance', 'read_only': True})
        import tomllib
        from framework_generator import toml_text, normalize_task_identity
        task_config = tomllib.loads((target / 'task.toml').read_text())
        identity_changed = 'task' in task_config and normalize_task_identity(task_config, spec['task_id'])
        if task_image:
            task_config.setdefault('environment', {})['docker_image'] = task_image
        if identity_changed or task_image:
            (target / 'task.toml').write_text(toml_text(task_config))
        (target / compose_relative).write_text(yaml.safe_dump(mode_config, sort_keys=False), encoding='utf-8')
        variants[mode] = {'path': str(target), 'files': package_manifest(target),
                          'package_sha256': package_fingerprint(target)}
    left, right = [variants[mode]['files'] for mode in ('blind', 'guided')]
    differences = sorted(path for path in left.keys() | right.keys() if left.get(path) != right.get(path))
    if set(differences) != {'guidance/method.md', compose_relative.as_posix()}:
        raise ValueError('unexpected Blind/Guided content differences: ' + repr(differences))
    normalized = yaml.safe_load((output / 'guided' / compose_relative).read_text())
    normalized['services'][solver]['volumes'].pop()
    if normalized != config:
        raise ValueError('Blind/Guided runtime config differs beyond the Method mount')
    if package_fingerprint(package) != source_hash:
        raise ValueError('verified source changed while preparing variants')
    manifest = {'schema_version': 'pipeline-variant-pair-v1', 'created_at': now(),
                'diagnostic_only': diagnostic,
                'task_id': spec['task_id'], 'source_package': str(package),
                'source_package_sha256': source_hash, 'source_readiness': receipt,
                'method_sha256': sha256(method), 'allowed_differences': differences,
                'shared_image_build_context': True, 'task_image': task_image, 'variants': variants}
    write_json(output / 'pair_manifest.json', manifest)
    return manifest


def _load_pair(pair: Path, variant: str, *, for_execution: bool = True, diagnostic: bool = False) -> tuple[dict, dict, Path]:
    manifest = read_json(pair)
    if bool(manifest.get('diagnostic_only')) != diagnostic:
        raise ValueError('Diagnostic pair requires an explicit diagnostic invocation; cannot use it as a verified task')
    if diagnostic:
        from diagnostic_support import validate_diagnostic
        spec, receipt = validate_diagnostic(Path(manifest['source_package']))
    else:
        spec, receipt = validate_ready(Path(manifest['source_package']), check_freshness=for_execution)
    if receipt != manifest.get('source_readiness'):
        raise ValueError('prepared pair readiness provenance was changed')
    if manifest['source_package_sha256'] != package_fingerprint(Path(manifest['source_package'])):
        raise ValueError('pair source is stale')
    record = manifest['variants'][variant]
    package = Path(record['path'])
    if package_manifest(package) != record['files'] or package_fingerprint(package) != record['package_sha256']:
        raise ValueError('prepared variant was modified')
    return manifest, spec, package


def validate_agent_kwargs(agent: str, kwargs: dict | None, *, harbor_adapter: bool = True) -> dict:
    if kwargs is not None and not isinstance(kwargs, dict):
        raise ValueError('agent_kwargs must be a JSON object')
    value = dict(kwargs or {})
    def inspect(item):
        if isinstance(item, dict):
            for key, nested in item.items():
                if not isinstance(key, str):
                    raise ValueError('agent kwarg keys must be text')
                lower = key.lower()
                if any(word in lower for word in ('api_key', 'password', 'secret', 'authorization', 'access_token', 'auth_token')):
                    raise ValueError('credentials must use environment variables, never --agent-kwarg')
                if lower.endswith(('base_url', 'api_base')) and nested is not None:
                    validate_model_endpoint(nested)
                inspect(nested)
        elif isinstance(item, list):
            for nested in item:
                inspect(nested)
    inspect(value)
    json.dumps(value, allow_nan=False)
    for key in value:
        if not key.replace('_', '').isalnum():
            raise ValueError('agent kwarg names must be simple identifiers')
        if key in {'load_trajectory', 'prompt_template_path'}:
            raise ValueError(key + ' would change isolated rollout or frozen prompt semantics; unsupported here')
        if harbor_adapter and agent in {'qwen-coder', 'qwen-code'} and key not in {'base_url', 'version', 'skills_dir', 'mcp_servers', 'extra_env', 'max_tool_calls_per_turn', 'shell_timeout_ms', 'shell_heartbeat_interval_ms', 'skip_loop_detection', 'long_running_tasks'}:
            raise ValueError(f'installed qwen-coder does not expose {key}; choose a supporting adapter rather than silently ignore it')
    return value


def validate_model_endpoint(url: str | None, label: str = 'model endpoint') -> None:
    if url is None:
        return
    parsed = urlsplit(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(label + ' must be HTTP(S) without embedded credentials/query')


def harbor_command(*, package: Path, jobs_dir: Path, job_name: str, agent: str,
                   model: str, harbor: str = 'harbor', gateway_spec: dict | None = None,
                   agent_kwargs: dict | None = None, env_file: Path | None = None,
                   solver_base_url: str | None = None,
                   time_limit_sec: int | None = None,
                   build_timeout_sec: int | None = None) -> list[str]:
    if time_limit_sec is None and gateway_spec is not None:
        time_limit_sec = gateway_spec['runtime'].get('time_limit_sec')
    if build_timeout_sec is None and gateway_spec is not None:
        build_timeout_sec = gateway_spec['runtime'].get('build_timeout_sec')
    for name, value in (('time_limit_sec', time_limit_sec), ('build_timeout_sec', build_timeout_sec)):
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError(f'runtime.{name} must be an integer >= 0 (0 means unlimited)')
    if gateway_spec is not None:
        from gateway_run import build_harbor_command
        configured = copy.deepcopy(gateway_spec)
        if model:
            configured['runtime']['gateway']['model'] = model
        argv = build_harbor_command(spec=configured, package=package, harbor=harbor,
                                   agent=agent, job_name=job_name, jobs_dir=jobs_dir, max_attempts=1)
    else:
        argv = [harbor, 'run', '-p', str(package), '-a', agent, '-m', model,
            '--jobs-dir', str(jobs_dir), '--job-name', job_name,
            '-n', '1', '-k', '1', '--max-retries', '0', '--yes']
    if time_limit_sec == 0:
        argv += ['--agent-timeout-multiplier', '0']
    if build_timeout_sec == 0:
        argv += ['--environment-build-timeout-multiplier', '0']
    if time_limit_sec == 0 or build_timeout_sec == 0:
        # Unlimited environment preparation includes agent installation/setup,
        # which otherwise has a separate hidden 360-second Harbor deadline.
        # Keep a finite Solver execution deadline independent of this choice.
        argv += ['--agent-setup-timeout-multiplier', '0']
    kwargs = validate_agent_kwargs(agent, agent_kwargs)
    # Agent adapters consume different fields. Do not silently put a base URL in
    # an ignored kwarg (installed Qwen uses base_url; Terminus uses api_base).
    if solver_base_url:
        validate_model_endpoint(solver_base_url, 'solver base URL')
        if gateway_spec is not None:
            raise ValueError('solver_base_url and --gateway cannot both select the endpoint')
        key = 'api_base' if agent == 'terminus-2' else 'base_url'
        if key in kwargs and kwargs[key] != solver_base_url:
            raise ValueError('conflicting base URL in agent kwargs')
        kwargs[key] = solver_base_url
    for key, value in sorted(kwargs.items()):
        argv += ['--ak', key + '=' + json.dumps(value, ensure_ascii=False, allow_nan=False)]
    if env_file is not None:
        if not env_file.is_file():
            raise ValueError('solver env file does not exist')
        argv += ['--env-file', str(env_file.resolve())]
    return argv


def run_variant(*, pair: Path, variant: str, output: Path, model: str = '',
                agent: str = 'qwen-coder', harbor: str = 'harbor', gateway: bool = False,
                command: list[str] | None = None, dry_run: bool = False,
                effective_input: Path | None = None, rollout_seed: int | None = None,
                agent_kwargs: dict | None = None, solver_base_url: str | None = None,
                solver_api_key_env: str = 'OPENAI_API_KEY', env_file: Path | None = None, diagnostic: bool = False,
                solver_proxy_url: str | None = None, reference_overlay: Path | None = None) -> dict:
    manifest, spec, package = _load_pair(pair, variant, diagnostic=diagnostic)
    time_limit_sec = spec['runtime']['time_limit_sec']
    build_timeout_sec = spec['runtime'].get('build_timeout_sec')
    process_timeout_sec = (None if time_limit_sec == 0 or build_timeout_sec == 0
                           else time_limit_sec + (build_timeout_sec if build_timeout_sec is not None else 0) + 600)
    agent_kwargs = validate_agent_kwargs(agent, agent_kwargs, harbor_adapter=command is None)
    validate_model_endpoint(solver_base_url, 'solver base URL')
    if reference_overlay is not None:
        reference_overlay = Path(reference_overlay).resolve()
        if reference_overlay.is_symlink() or not reference_overlay.is_dir():
            raise ValueError('reference overlay must be a regular directory')
    immutable_package = package
    effective = None
    from scenarios import prepare_effective_task, validate_effective_input
    if effective_input is not None:
        if reference_overlay is not None:
            raise ValueError('reference overlay must be applied before effective input is frozen')
        effective = validate_effective_input(effective_input)
        if Path(effective['source_package']).resolve() != package.resolve() or effective['task_id'] != spec['task_id']:
            raise ValueError('effective input belongs to a different task/variant')
        package = Path(effective['effective_task'])
    output = output.resolve()
    if output.exists():
        raise ValueError(f'refusing to overwrite run: {output}')
    protected = [package, Path(manifest['source_package']), *(Path(v['path']) for v in manifest['variants'].values())]
    if any(output.is_relative_to(path.resolve()) for path in protected):
        raise ValueError('run output cannot be inside a task package')
    if not model and command is None:
        model = (spec['runtime'].get('gateway') or {}).get('model', '') if gateway else ''
        if not model:
            raise ValueError('provide a solver model (or explicit trusted command adapter)')
    output.mkdir(parents=True)
    if effective is None:
        # All execution entry points must deliver prompt03 and freeze the same
        # authored instruction. Keep the accepted pair immutable.
        effective_input = output / 'input/effective/scenario_manifest.json'
        from formal_resources import preparation
        prepared = preparation(Path(manifest['source_package']), spec, manifest['source_readiness'],
                               manifest.get('task_image'), reference_overlay,
                               solver_proxy_url=solver_proxy_url) if not dry_run and not diagnostic else None
        effective = prepare_effective_task(
            package=package, output=effective_input.parent, spec=spec,
            layout=package_layout(spec), private_source=Path(manifest['source_package']),
            private_variants=[Path(v['path']) for v in manifest['variants'].values()],
            forbidden_sha256=[manifest['method_sha256']], reference_overlay=reference_overlay,
            prepared_resources=prepared, verify_resources=not dry_run and not diagnostic)
        package = Path(effective['effective_task'])
    if not dry_run and not diagnostic:
        from formal_resources import declared, inspect_formal
        resources = declared(spec)
        if resources and (effective.get('formal_resources_verified') is not True or
                          effective.get('formal_resources') != inspect_formal(package, resources, common=True)):
            raise ValueError('formal resource receipt does not match the frozen Solver and evaluator inputs')
    jobs = output / 'jobs'
    workspace = output / 'workspace'
    workspace.mkdir()
    request = {'role': 'solver', 'task_id': spec['task_id'], 'variant': variant,
               'package': str(package), 'instruction': (package / 'instruction.md').read_text(encoding='utf-8'),
               'workspace': str(workspace), 'time_limit_sec': spec['runtime']['time_limit_sec'],
               'model_settings': {'model': model, 'agent': agent, 'agent_kwargs': agent_kwargs or {},
                                  'solver_base_url': solver_base_url, 'rollout_seed': rollout_seed},
               'result_contract': 'Write run_export.json with timeline, trajectory, submission_snapshots [{attempt_id,path,sha256}], workspace_files. Snapshots must be captured at scoring time.'}
    if effective is not None:
        request.update(scenario_id=effective['scenario_id'], interaction_mode=effective['interaction_mode'])
        if effective['interaction_mode']:
            request['interaction'] = {'inbox': str(effective_input.parent / 'interaction/inbox'),
                                      'outbox': str(effective_input.parent / 'interaction/outbox')}
    write_json(output / 'request.json', request)
    if command is not None:
        if not command or not all(isinstance(item, str) for item in command):
            raise ValueError('command must be a nonempty argv array')
        values = {'task': str(package), 'request': str(output / 'request.json'),
                  'workspace': str(workspace), 'role': 'solver'}
        argv = []
        for item in command:
            for key, value in values.items():
                item = item.replace('{' + key + '}', value)
            argv.append(item)
        backend = 'trusted_command_adapter'
    else:
        argv = harbor_command(package=package, jobs_dir=jobs, job_name=spec['task_id'] + '--' + variant,
                              agent=agent, model=model, harbor=harbor, gateway_spec=spec if gateway else None,
                              agent_kwargs=agent_kwargs, solver_base_url=solver_base_url,
                              time_limit_sec=time_limit_sec, build_timeout_sec=build_timeout_sec)
        backend = 'harbor_docker'
    network = network_for_policy(spec['runtime'].get('network'), spec['runtime']['network_policy'],
                                 proxy_url=solver_proxy_url)
    build_network = resolve_network(spec['runtime'].get('build_network', spec['runtime'].get('network')))
    import yaml
    compose_relative = Path(package_layout(spec)['compose_file']).relative_to('common_task')
    compose = yaml.safe_load((package / compose_relative).read_text())
    services = compose.get('services', {})
    network = network.with_bypass([*services, urlsplit(solver_base_url or '').hostname, urlsplit((spec['runtime'].get('gateway') or {}).get('base_url', '')).hostname])
    if command is None:
        overlay = output / 'network-compose.json'
        overlay_config = make_compose_network_overlay({s: 'build' in value for s, value in services.items()}, network)
        from network_config import ENV_NAMES
        for name in set(services) - set(package_layout(spec)['solver_services']):
            overlay_config['services'][name]['environment'] = {key: '' for key in ENV_NAMES}
        write_json(overlay, overlay_config)
        overlays = [str(overlay)]
        if effective is not None and effective['interaction_mode']:
            from scenarios import interaction_overlay
            interaction = output / 'interaction-compose.json'
            solver = [name for name in package_layout(spec)['solver_services'] if name in services]
            if len(solver) != 1:
                raise ValueError('mailbox requires exactly one configured solver service')
            write_json(interaction, interaction_overlay(effective_input.parent, solver[0]))
            overlays.append(str(interaction))
        # Use Harbor's dedicated field: separate verifier environments clear
        # this field. A kwargs override would survive and leak solver mounts.
        for compose_overlay in overlays:
            argv += ['--extra-docker-compose', compose_overlay]
    if command is None and not dry_run:
        from harbor_runtime_validation import validate_task
        validate_task(package, harbor)
    record = {'schema_version': 'pipeline-solver-run-v1', 'task_id': spec['task_id'],
              'diagnostic_only': diagnostic,
              'run_id': uuid.uuid4().hex,
              'variant': variant, 'backend': backend, 'created_at': now(), 'command': argv,
              'model': model, 'agent': agent, 'agent_kwargs': agent_kwargs or {},
              'time_limit_sec': time_limit_sec, 'build_timeout_sec': build_timeout_sec,
              'process_timeout_sec': process_timeout_sec,
              'rollout_seed': rollout_seed, 'seed_scope': 'experiment_identity_only_not_provider_sampling',
              'solver_base_url': solver_base_url, 'solver_api_key_env': solver_api_key_env,
              'env_file': str(env_file.resolve()) if env_file else None,
              'network': network.report,
              'network_env_sha256': network.fingerprint,
              'build_network': build_network.report,
              'build_network_env_sha256': build_network.fingerprint,
              'status': 'dry_run' if dry_run else 'running', 'pair_manifest': str(pair.resolve()),
              'pair_manifest_sha256': sha256(pair),
              'stage2_readiness_sha256': sha256(Path(manifest['source_package']) / 'readiness.json'),
              'package_sha256': package_fingerprint(immutable_package),
              'effective_package_sha256': package_fingerprint(package),
              'effective_input': str(effective_input.resolve()) if effective_input else None,
              'effective_input_sha256': sha256(effective_input) if effective_input else None,
              'isolation': 'adapter_owned_not_enforced_by_this_process' if command else 'Harbor Docker task configuration',
              'score_attempt': None, 'submission_lifecycle': effective.get('submission_lifecycle', {})}
    write_json(output / 'run_manifest.json', record)
    if dry_run:
        return record
    if gateway:
        from gateway_run import health_check
        if not health_check(spec['runtime']['gateway']['base_url']):
            raise ValueError('configured optional gateway is not healthy')
    process_env = model_process_env({'env_file': str(env_file) if env_file else None,
                      'api_key_env': solver_api_key_env if solver_api_key_env != 'OPENAI_API_KEY' else None},
                      network.process_env(build_network=build_network))
    from scenarios import MailboxController
    controller = MailboxController(effective_input.parent, effective['definition']) if effective and effective['interaction_mode'] else None
    started = time.monotonic()
    execution_started = None
    events_path = output / 'events.jsonl'
    def event(kind, **fields):
        with events_path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'timestamp': now(), 'event': kind, **fields}, ensure_ascii=False) + '\n')
    event('RUN_STARTED', model=model, agent=agent)
    try:
        if controller is not None:
            controller.start()
        execution_started = time.monotonic()
        event('SOLVER_STARTED', model=model, agent=agent)
        returncode = run_logged(argv, output / 'execution.log', cwd=output,
                               timeout=process_timeout_sec,
                               env=process_env)
        record['returncode'] = returncode
        record['status'] = 'needs_score_evidence' if returncode == 0 else 'solver_execution_failed'
    except subprocess.TimeoutExpired:
        record.update(status='solver_execution_failed', failure='timeout')
    except OSError as error:
        record.update(status='solver_execution_failed', failure=str(error))
    finally:
        record['wall_time_sec'] = time.monotonic() - started
        record['execution_wall_time_sec'] = time.monotonic() - execution_started if execution_started is not None else None
        if controller is not None:
            controller.stop_event.set()
        if execution_started is not None:
            event('SOLVER_FINISHED', wall_time_sec=record['execution_wall_time_sec'], returncode=record.get('returncode'))
        if controller is not None:
            record['interaction'] = controller.stop()
            if controller.log.is_file():
                record['interaction_log'] = {'path': str(controller.log.resolve()), 'sha256': sha256(controller.log)}
        event('RUN_FINISHED', wall_time_sec=record['wall_time_sec'])
        if record['status'] == 'network_preflight_failed':
            write_json(output / 'run_manifest.json', record)
    record['execution_status'] = 'completed' if record.get('returncode') == 0 else 'failed'
    try:
        if package_fingerprint(package) != record['effective_package_sha256'] or package_fingerprint(immutable_package) != record['package_sha256']:
            record.update(status='task_contract_error', failure='solver adapter modified immutable task package')
        if effective_input is not None:
            validate_effective_input(effective_input, expected_sha256=record['effective_input_sha256'])
    except (OSError, ValueError) as error:
        record.update(status='task_contract_error', failure='task package is no longer intact: ' + str(error))
    if record['status'] != 'task_contract_error':
        try:
            if command is not None:
                raw_export = workspace / 'run_export.json'
                boundary = workspace
            else:
                raw_export = collect_harbor_export(jobs / (spec['task_id'] + '--' + variant), output / 'harbor_export.json',
                                                  score_evidence=spec.get('verification',{}).get('score_evidence'))
                # Harbor can exit zero even when its single agent trial failed.
                # Preserve exported evidence but report the actual trial state.
                from pipeline.stage3_execution.runtime.outcome import trial_outcome
                record.update(trial_outcome(Path(read_json(raw_export)['trial'])))
                boundary = jobs
            exported = bind_export(raw_export, output / 'run_export.json', record,
                                   source_package_sha256=manifest['source_package_sha256'], boundary=boundary)
            record.update(status='evidence_exported', export=str(exported), export_sha256=sha256(exported))
        except (OSError, ValueError, KeyError) as error:
            record['evidence_issue'] = str(error)
    record['finished_at'] = now()
    write_json(output / 'run_manifest.json', record)
    return record


def bind_export(raw_export: Path, output: Path, run: dict, *, source_package_sha256: str, boundary: Path) -> Path:
    value = read_json(raw_export)
    hashes = {}
    def resolve(relative: str) -> str:
        path = Path(relative)
        if not path.is_absolute():
            path = raw_export.parent / path
        if path.is_symlink() or not path.resolve().is_relative_to(boundary.resolve()) or not path.is_file():
            raise ValueError('run evidence must be a regular file inside this exact run workspace/artifacts')
        path = path.resolve()
        hashes[str(path)] = sha256(path)
        return str(path)
    value['trajectory'] = resolve(value['trajectory'])
    value['timeline'] = resolve(value['timeline'])
    for snapshot in value['submission_snapshots']:
        if snapshot.get('missing') is True and snapshot.get('path') is None and snapshot.get('sha256') is None:
            continue
        snapshot['path'] = resolve(snapshot['path'])
        if hashes[snapshot['path']] != snapshot['sha256']:
            raise ValueError('exported submission snapshot hash does not match')
    value['workspace_files'] = [resolve(path) for path in value.get('workspace_files', [])]
    value['verification_files'] = {key: resolve(path) for key, path in value.get('verification_files', {}).items()}
    if run.get('submission_lifecycle', {}).get('formal_submission_required'):
        if not {'rule_checks', 'final_submission'} <= value['verification_files'].keys():
            raise ValueError('formal submission requires evaluator-owned final receipt and rule report')
        from score_attempt import timeline_records
        if len([event for event in timeline_records(Path(value['timeline'])) if event.get('event') == 'SCORE_ATTEMPT']) != 1:
            raise ValueError('formal submission requires exactly one official scoring event')
    value['provenance'] = {key: run[key] for key in ('task_id', 'variant', 'package_sha256', 'run_id')}
    value['provenance'].update(source_package_sha256=source_package_sha256,
        run_manifest=str((output.parent / 'run_manifest.json').resolve()), evidence_sha256=hashes)
    write_json(output, value)
    return output


def collect_harbor_export(job: Path, output: Path, *, score_evidence: dict | None = None) -> Path:
    """Collect one exact job's one trial, never a newest global gateway session.

    Artifact paths follow the installed Harbor source-derived mirror convention.
    The scorer owns capture.json and timeline.jsonl; the solver trajectory comes
    from Harbor's actual agent log. Ambiguous retries/trials are rejected.
    """
    if not job.is_dir():
        raise ValueError('Harbor job directory was not produced')
    trials = [path for path in job.iterdir() if path.is_dir() and
              (path / 'agent').is_dir() and (path / 'artifacts').is_dir()]
    if len(trials) != 1:
        raise ValueError('expected exactly one Harbor trial; select an explicit exported attempt for multi-step/retry runs')
    trial = trials[0]
    trajectory = trial / 'agent/trajectory.json'
    if not trajectory.is_file():
        raise ValueError('Harbor trial has no actual agent/trajectory.json')
    manifest_path = trial / 'artifacts/manifest.json'
    entries = []
    if manifest_path.is_file():
        collected = read_json(manifest_path)
        entries = collected if isinstance(collected, list) else collected.get('entries', [])

    def artifact_path(entry):
        name = entry.get('destination')
        if not isinstance(name, str) or not name:
            raise ValueError('invalid collected artifact destination')
        relative = Path(name)
        if relative.is_absolute() or not relative.parts or '..' in relative.parts:
            raise ValueError('invalid collected artifact destination')
        root = trial / 'artifacts'
        path = trial / relative if relative.parts[0] == 'artifacts' else root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError('collected artifact escapes the trial artifact directory')
        return path

    if score_evidence:
        source = score_evidence['artifact_source']
        selected = [entry for entry in entries if entry.get('source') == source]
        if len(selected) != 1 or selected[0].get('service') != score_evidence['service'] or selected[0].get('status') != 'ok':
            raise ValueError('score evidence was not collected from the designated evaluator service')
        artifacts = artifact_path(selected[0])
    else:
        artifacts = trial / 'artifacts/logs/artifacts/score_attempts'

    from submission_lifecycle import EXPERIMENT_LOG_PATHS
    logs = []
    for entry in entries:
        if (entry.get('source') in EXPERIMENT_LOG_PATHS and entry.get('service') in (None, 'main')
                and entry.get('status') == 'ok' and entry.get('type') == 'file'):
            path = artifact_path(entry)
            if not path.is_file():
                raise ValueError('collected experiment log missing from Harbor export')
            logs.append(str(path.resolve()))
    timeline = artifacts / 'timeline.jsonl'
    if not timeline.is_file():
        raise ValueError('scorer did not export the configured score-attempt timeline.jsonl')
    from score_attempt import timeline_records
    events = timeline_records(timeline)
    snapshots = []
    for event in events:
        if event.get('event') != 'SCORE_ATTEMPT':
            continue
        attempt_id = event.get('attempt_id')
        from score_attempt import check_id
        check_id(attempt_id)
        if event.get('submission_missing') is True and event.get('submission_sha256') is None:
            snapshots.append({'attempt_id': attempt_id, 'path': None, 'sha256': None, 'missing': True})
            continue
        capture_dir = artifacts / 'captures' / attempt_id
        capture = read_json(capture_dir / 'capture.json')
        source = Path(capture['frozen_submission'])
        # Capture's basename is preserved when Harbor mirrors the artifact tree.
        snapshot = capture_dir / 'submission' / source.name
        digest = sha256(snapshot)
        if capture.get('attempt_id') != attempt_id or digest != capture.get('submission_sha256') or digest != event.get('submission_sha256'):
            raise ValueError(f'captured attempt provenance mismatch: {attempt_id}')
        snapshots.append({'attempt_id': attempt_id, 'path': str(snapshot.resolve()), 'sha256': digest})
    if not snapshots or len({item['attempt_id'] for item in snapshots}) != len(snapshots):
        raise ValueError('no unique scorer snapshots were exported')
    write_json(output, {'schema_version': 'pipeline-run-export-v1', 'source': 'harbor_exact_trial',
                        'trial': str(trial.resolve()), 'trajectory': str(trajectory.resolve()),
                        'timeline': str(timeline.resolve()), 'submission_snapshots': snapshots,
                        'workspace_files': logs, 'verification_files': {key: str((artifacts / filename).resolve())
                            for key, filename in [('rule_checks', 'rule_checks.json'), ('final_submission', 'final_submission.json'), ('selfchecks', 'selfchecks.jsonl')]
                            if (artifacts / filename).is_file()}})
    return output


def freeze_export(*, pair: Path, variant: str, export: Path, attempt_id: str, output: Path, diagnostic: bool = False) -> dict:
    manifest, spec, package = _load_pair(pair, variant, for_execution=False, diagnostic=diagnostic)
    value = read_json(export)
    provenance = value.get('provenance')
    if not isinstance(provenance, dict):
        raise ValueError('run export lacks runner provenance; use the runner-produced run_export.json')
    expected = {'task_id': spec['task_id'], 'variant': variant,
                'package_sha256': package_fingerprint(package),
                'source_package_sha256': manifest['source_package_sha256']}
    if any(provenance.get(key) != item for key, item in expected.items()):
        raise ValueError('run export belongs to a different task, variant, or package revision')
    run = read_json(Path(provenance['run_manifest']))
    if run.get('run_id') != provenance.get('run_id') or run.get('status') != 'evidence_exported' or run.get('export_sha256') != sha256(export):
        raise ValueError('run export is not bound to its completed runner manifest')
    if run.get('pair_manifest_sha256') != sha256(pair) or run.get('stage2_readiness_sha256') != sha256(Path(manifest['source_package']) / 'readiness.json'):
        raise ValueError('prepared pair or stage2 receipt changed since the solver run')
    for key in ('task_id', 'variant', 'package_sha256'):
        if run.get(key) != expected[key]:
            raise ValueError('run manifest provenance disagrees with selected task')
    for path, digest in provenance.get('evidence_sha256', {}).items():
        if sha256(Path(path)) != digest:
            raise ValueError('run material changed after export: ' + path)
    snapshots = [item for item in value.get('submission_snapshots', []) if str(item.get('attempt_id')) == attempt_id]
    if len(snapshots) != 1:
        raise ValueError('run export must have exactly one snapshot for the explicitly selected attempt')
    def resolve(value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else export.parent / path
    layout = package_layout(spec)
    task = {'task_id': spec['task_id'], 'metric': load_metric(Path(manifest['source_package']), spec), 'variant': variant}
    if diagnostic:
        task['diagnostic_only'] = True
        task['original_run_ready'] = manifest['source_readiness'].get('run_ready')
    extras = [resolve(path) for path in value.get('workspace_files', [])]
    task['submission_lifecycle'] = run.get('submission_lifecycle', {})
    verification_files = {key: resolve(path) for key, path in value.get('verification_files', {}).items()}
    # Older run exports may predate effective-input manifests. Their authored
    # instruction was still delivered and must be visible to the Judge.
    supplied_inputs = [package / 'instruction.md']
    input_observation_context = None
    if run.get('effective_input'):
        from scenarios import validate_effective_input
        effective = validate_effective_input(Path(run['effective_input']), expected_sha256=run['effective_input_sha256'])
        if effective['source_package_sha256'] != expected['package_sha256']:
            raise ValueError('effective input does not derive from this frozen task variant')
        package = Path(effective['effective_task'])
        input_observation_context = {'effective':effective, 'spec':spec,
            'backend':run['backend'] if value.get('source') == 'harbor_exact_trial' else None,
            'effective_manifest_sha256':run['effective_input_sha256']}
        task.update(scenario_id=effective['scenario_id'], scenario_kind=effective['kind'],
                    review_basis='Judge only actual delivered input; private scenario intentions are not solver requirements.')
        supplied_inputs = [package / 'instruction.md']
        public = package / Path(layout['public_dir']).relative_to('common_task')
        supplied_inputs.extend(public / destination for destination in effective.get('delivered_attachments', []))
        interaction_log = Path(run['effective_input']).parent / 'interaction/events.jsonl'
        if interaction_log.is_file():
            reference = run.get('interaction_log', {})
            if reference.get('path') != str(interaction_log.resolve()) or reference.get('sha256') != sha256(interaction_log):
                raise ValueError('interaction evidence changed after solver execution')
            from score_attempt import timeline_records
            selected = [e for e in timeline_records(resolve(value['timeline'])) if e.get('event') == 'SCORE_ATTEMPT' and str(e.get('attempt_id')) == attempt_id]
            if len(selected) != 1:
                raise ValueError('target scoring event is ambiguous')
            cutoff = timestamp(selected[0].get('scored_at', selected[0].get('timestamp')))
            if verification_files.get('final_submission'):
                submitted_at = read_json(verification_files['final_submission']).get('submitted_at')
                if submitted_at: cutoff = timestamp(submitted_at)
            delivered = [json.loads(line) for line in interaction_log.read_text().splitlines() if line.strip()]
            delivered = [e for e in delivered if timestamp(e['timestamp']) <= cutoff]
            # Preserve actual requests/replies, not undelivered future script facts.
            selected_log = export.parent / 'interaction_at_score.json'
            write_json(selected_log, {'score_attempt_used': attempt_id, 'events': delivered,
                                     'delivery_does_not_prove_reading': True})
            supplied_inputs.append(selected_log)
    public = package / Path(layout['public_dir']).relative_to('common_task')
    for public_doc in (public / 'task_contract.json', package / Path(layout['data_sources_path']).relative_to('common_task')):
        if public_doc.is_file() and public_doc.is_relative_to(public) and public_doc not in supplied_inputs:
            supplied_inputs.append(public_doc)
    from paper_comparison import reference_snapshot
    paper_reference = reference_snapshot(Path(manifest['source_package']), spec)
    return freeze_attempt(task=task, paper_reference=paper_reference,
        readme=package / Path(layout['readme_path']).relative_to('common_task'),
        data_description=package / Path(layout['data_description_path']).relative_to('common_task'),
        submission=resolve(snapshots[0]['path']) if snapshots[0].get('path') else None, trajectory=resolve(value['trajectory']),
        timeline=resolve(value['timeline']), attempt_id=attempt_id, output=output,
        expected_sha256=snapshots[0]['sha256'],
        workspace_files=extras, verification_files=verification_files,
        input_files=supplied_inputs, input_observation_context=input_observation_context,
        source_paper=None, guided_method=package / 'guidance/method.md' if variant == 'guided' else None)


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    prepare = commands.add_parser('prepare')
    prepare.add_argument('--package', type=Path, required=True)
    prepare.add_argument('--output', type=Path, required=True)
    prepare.add_argument('--task-image')
    run = commands.add_parser('run')
    run.add_argument('--pair', type=Path, required=True)
    run.add_argument('--variant', choices=['blind', 'guided'], required=True)
    run.add_argument('--output', type=Path, required=True)
    run.add_argument('--model', default='')
    run.add_argument('--agent', default='qwen-coder')
    run.add_argument('--harbor', default='harbor')
    run.add_argument('--solver-proxy-url', help='HTTP(S) download proxy for public-network Solver only')
    run.add_argument('--gateway', action='store_true', help='optional configured model/search gateway')
    run.add_argument('--command-config', type=Path, help='explicit trusted adapter JSON with command argv; owns isolation')
    run.add_argument('--dry-run', action='store_true')
    freeze = commands.add_parser('freeze')
    freeze.add_argument('--pair', type=Path, required=True)
    freeze.add_argument('--variant', choices=['blind', 'guided'], required=True)
    freeze.add_argument('--export', type=Path, required=True)
    freeze.add_argument('--attempt-id', required=True)
    freeze.add_argument('--output', type=Path, required=True)
    judge = commands.add_parser('judge')
    judge.add_argument('--bundle', type=Path, required=True)
    judge.add_argument('--output', type=Path, required=True)
    choice = judge.add_mutually_exclusive_group()
    choice.add_argument('--verdict', dest='verdict_file', type=Path)
    choice.add_argument('--endpoint', help='text-only chat-completions endpoint; no hosted agent/search tools')
    judge.add_argument('--model')
    judge.add_argument('--api-key-env', default='OPENAI_API_KEY')
    summarize = commands.add_parser('summarize', help='local reporting; no model calls')
    summarize.add_argument('--judge-root', type=Path, action='append', required=True)
    summarize.add_argument('--output', type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.action == 'prepare':
            value = prepare_variants(args.package, args.output, task_image=args.task_image)
        elif args.action == 'run':
            command = read_json(args.command_config)['command'] if args.command_config else None
            value = run_variant(pair=args.pair, variant=args.variant, output=args.output,
                                model=args.model, agent=args.agent, harbor=args.harbor,
                                gateway=args.gateway, command=command, dry_run=args.dry_run, solver_proxy_url=args.solver_proxy_url)
        elif args.action == 'freeze':
            request = freeze_export(pair=args.pair, variant=args.variant, export=args.export,
                                    attempt_id=args.attempt_id, output=args.output)
            value = {'status': 'frozen', 'score_attempt_used': request['score_attempt_used']}
        elif args.action == 'summarize':
            from summarize_judges import summarize
            value = summarize(args.judge_root, args.output)
        else:
            value = judge_bundle(bundle=args.bundle, output=args.output, verdict_file=args.verdict_file,
                                 endpoint=args.endpoint, model=args.model, api_key_env=args.api_key_env)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        if value.get('mode') in {'rules_failed', 'rules_error'}:
            return 2
        if value.get('execution_status') == 'failed':
            return 1
        if value.get('status') == 'needs_score_evidence':
            return 2
        return 1 if value.get('status') in {'solver_execution_failed', 'task_contract_error', 'review_rejected', 'input_errors', 'network_preflight_failed'} else 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({'status': 'error', 'error': str(error)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
