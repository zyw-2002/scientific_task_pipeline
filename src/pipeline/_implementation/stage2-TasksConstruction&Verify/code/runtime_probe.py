"""Run declared smoke checks in the task's disposable Compose environment.

This verifies the interfaces and known small cases, not a full scientific solve.
No evaluator or task dependency is executed/installed on the host.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import uuid
from urllib.parse import urlsplit

from contract import package_fingerprint, execution_fingerprint, package_layout
from worker_runtime import atomic_json, read_json, file_sha256, copy_tree, run_logged
from network_config import (resolve_network, network_for_policy, make_compose_network_overlay,
                            container_probe_command, probe_urls, redact_url)


def _json_command(argv, log, *, cwd, timeout, stderr_log, env=None):
    """Keep Docker warnings out of machine-readable stdout, preserving both."""
    with log.open('wb') as stdout, stderr_log.open('wb') as stderr:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=stdout, stderr=stderr, start_new_session=True)
        try:
            return process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise


def _service_names(config, spec, layout):
    configured = config.get('services')
    if not isinstance(configured, dict) or not configured:
        raise ValueError('Compose resolved services must be a nonempty object')
    framework = spec.get('framework', {})
    groups = []
    for key in ('solver_services', 'evaluator_services'):
        names = layout[key]
        if not isinstance(names, list) or not names or not all(isinstance(n, str) and n for n in names):
            raise ValueError(f'framework.{key} must be a nonempty service-name list')
        if key in framework and any(name not in configured for name in names):
            raise ValueError(f'configured {key} is absent from Compose')
        groups.append([name for name in configured if name in names])
    solvers, evaluators = groups
    if len(solvers) != 1 or not evaluators or set(solvers) & set(evaluators):
        raise ValueError('probe requires exactly one Solver and at least one separate evaluator service')
    if set(configured) != set(solvers + evaluators):
        raise ValueError('probe refuses to start unclassified Compose services; declare actual service roles')
    return solvers[0], evaluators


def probe(package: Path, spec: dict, output: Path, *, docker='docker', timeout=600, runner=None) -> dict:
    output = output.resolve()
    if output.exists():
        raise ValueError(f'probe output already exists: {output}')
    if output.is_relative_to(package.resolve()) or package.resolve().is_relative_to(output):
        raise ValueError('probe output must be outside package')
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError('probe timeout must be positive')
    output.mkdir(parents=True)
    evidence = {'schema_version': 'pipline-runtime-evidence-v1',
                'package_sha256': None,
                'execution_sha256': None, 'verified_at': None,
                'image_id': None, 'checks': [], 'status': 'failed', 'errors': []}
    atomic_json(output / 'runtime_evidence.json', evidence)
    temporary = output / 'package'
    compose_file = temporary / 'common_task/environment/docker-compose.yaml'
    project = 'pipeline-probe-' + uuid.uuid4().hex[:12]
    command = []
    created = False
    network = network_for_policy(spec['runtime'].get('network'), spec['runtime']['network_policy'])
    build_network = resolve_network(spec['runtime'].get('build_network', spec['runtime'].get('network')))
    process_env = network.process_env(build_network=build_network)
    evidence['network'] = network.report
    evidence['build_network'] = build_network.report
    evidence['build_network_env_sha256'] = build_network.fingerprint

    def call(argv, name, *, scope='host', required=True, machine_json=False, command_timeout=timeout):
        log = output / (name + '.log')
        stderr_log = output / (name + '.stderr.log')
        try:
            executor = runner or (_json_command if machine_json else run_logged)
            kwargs = {'cwd': compose_file.parent, 'timeout': command_timeout}
            if runner is None:
                kwargs['env'] = process_env
            if machine_json:
                kwargs['stderr_log'] = stderr_log
            rc = executor(argv, log, **kwargs)
        except (OSError, subprocess.TimeoutExpired) as exc:
            with log.open('a') as stream:
                stream.write('\n' + str(exc) + '\n')
            rc = 124 if isinstance(exc, subprocess.TimeoutExpired) else 127
        # Compose config / docker inspect include resolved environment values.
        # Keep the proof while stripping proxy userinfo before hashing logs.
        for artifact in (log, stderr_log):
            if artifact.is_file():
                content = artifact.read_text(errors='replace')
                for key, value in [*network.env.items(), *build_network.env.items()]:
                    if key != 'NO_PROXY' and key != 'no_proxy' and value:
                        content = content.replace(value, redact_url(value))
                artifact.write_text(content)
        row = {'id': name, 'status': 'pass' if rc == 0 else 'fail', 'scope': scope,
               'command': argv, 'exit_code': rc, 'log_path': str(log), 'log_sha256': file_sha256(log)}
        if stderr_log.is_file():
            row.update(stderr_path=str(stderr_log), stderr_sha256=file_sha256(stderr_log))
        evidence['checks'].append(row)
        if required and rc:
            raise ValueError(f'{name} failed (exit {rc}); see {log}')
        return log

    def execute(service, argv, name):
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
            raise ValueError(f'verification command {name} must be nonempty argv')
        return call(command + ['exec', '-T', service, *argv], name, scope='container')

    try:
        if spec.get("runtime", {}).get("image_policy") == "deferred":
            raise ValueError("Final task image is not bound; aggregate dependencies, build the shared image, and bind it before runtime acceptance")
        if package.is_symlink():
            raise ValueError('probe package cannot be a symlink')
        evidence['package_sha256'] = package_fingerprint(package)
        evidence['execution_sha256'] = execution_fingerprint(spec)
        if execution_fingerprint(read_json(package / 'execution.json')) != evidence['execution_sha256']:
            raise ValueError('provided spec does not match package/execution.json')
        copy_tree(package, temporary)
        layout = package_layout(spec)
        compose_file = temporary / layout['compose_file']
        command = [docker, 'compose', '--project-name', project, '-f', str(compose_file)]
        checks = spec.get('verification', {}).get('commands', {})
        from preflight import inspect_package
        static = inspect_package(temporary, spec)
        atomic_json(output / 'preflight_report.json', static)
        if not static['static_pass']:
            failures = [check['id'] + ': ' + check['reason'] for check in static['checks'] if check['status'] != 'pass']
            raise ValueError('static preflight failed; fix it in stage 02 before starting containers: ' + '; '.join(failures))
        from formal_resources import declared, inspect_formal
        evidence['formal_resources'] = inspect_formal(temporary, declared(spec))
        if spec['runtime']['network_policy'] == 'allowlisted':
            raise ValueError('plain Compose probe cannot enforce Harbor host allowlists; use runtime evidence from the actual Harbor environment')
        config_log = call(command + ['config', '--format', 'json'], 'compose_config', machine_json=True)
        config = read_json(config_log)
        solver, evaluators = _service_names(config, spec, layout)
        services = [solver, *evaluators]
        configured_services = config['services']
        network = network.with_bypass([*services, urlsplit((spec['runtime'].get('gateway') or {}).get('base_url', '')).hostname])
        process_env = network.process_env(build_network=build_network)
        evidence['network'] = network.report
        evidence['network_env_sha256'] = network.fingerprint
        overlay = output / 'network-compose.json'
        atomic_json(overlay, make_compose_network_overlay(
            {s: 'build' in configured_services[s] for s in services}, network))
        command += ['-f', str(overlay)]
        for service in services:
            if service not in configured_services:
                raise ValueError(f'unknown probe service: {service}')
            # Host paths must remain within this disposable package. External datasets
            # must be provided through an explicit runtime integration instead.
            for mount in configured_services[service].get('volumes', []):
                if mount.get('type') == 'bind':
                    source = Path(mount['source']).resolve()
                    if not source.is_relative_to(temporary.resolve()):
                        raise ValueError(f'probe bind escapes disposable package: {source}')
            if configured_services[service].get('container_name'):
                raise ValueError('container_name prevents isolated probe projects')
            definition = configured_services[service]
            if definition.get('privileged') or definition.get('pid') == 'host' or definition.get('ipc') == 'host' or definition.get('volumes_from') or definition.get('cap_add') or definition.get('devices'):
                raise ValueError('probe cannot grant privileged/shared host access to any service')
            if definition.get('network_mode', '') == 'host' or definition.get('network_mode', '').startswith('container:'):
                raise ValueError('probe cannot join host or external container networks')
        for kind in ('volumes', 'networks'):
            for name, definition in config.get(kind, {}).items():
                if definition.get('external') or definition.get('name', project + '_' + name) != project + '_' + name:
                    raise ValueError(f'probe cannot share external or globally named {kind}')
                if definition.get('driver_opts'):
                    raise ValueError(f'probe cannot use host-backed/custom {kind} driver options')
        for kind in ('secrets', 'configs'):
            for definition in config.get(kind, {}).values():
                if definition.get('external') or definition.get('environment'):
                    raise ValueError(f'probe cannot import external/environment {kind}')
                if definition.get('file') and not Path(definition['file']).resolve().is_relative_to(temporary.resolve()):
                    raise ValueError(f'probe {kind} file escapes disposable package')
        if not isinstance(checks, dict):
            raise ValueError('verification.commands must be an object')
        for name in ('data_identity', 'dependencies', 'scoring'):
            if name not in checks:
                raise ValueError(f'missing task-specific verification.commands.{name}; no placeholder check is substituted')
        if spec['runtime'].get('gpus', 0) and 'gpu' not in checks:
            raise ValueError('GPU task needs verification.commands.gpu using the actual scientific framework')
        if spec['runtime'].get('multimodal', {}).get('enabled') and 'multimodal' not in checks:
            raise ValueError('multimodal task needs verification.commands.multimodal in the Solver service')
        hidden_targets = spec.get('verification', {}).get('hidden_container_paths', ['/task/ground_truth', '/task/evaluation'])
        if not isinstance(hidden_targets, list) or not hidden_targets or not all(isinstance(p, str) and p.startswith('/') and p != '/' and '..' not in Path(p).parts for p in hidden_targets):
            raise ValueError('hidden_container_paths must be a nonempty list of absolute protected paths')
        for name, check in checks.items():
            if name not in {'data_identity', 'dependencies', 'scoring', 'gpu', 'multimodal'} and not re.fullmatch(r'scientific_[a-z0-9_]+', name):
                raise ValueError(f'unknown required runtime check: {name}')
            if not isinstance(check, dict) or not isinstance(check.get('command'), list) or not check['command'] or not all(isinstance(v, str) and v for v in check['command']):
                raise ValueError(f'verification command {name} must declare a nonempty argv')
            service = check.get('service', evaluators[0] if name == 'scoring' else solver)
            if service not in services or (name not in {'scoring'} and not name.startswith('scientific_') and service != solver):
                raise ValueError(f'verification command {name} has an invalid service')
        build_timeout = spec['runtime'].get('build_timeout_sec', timeout)
        prebuilt = spec['runtime'].get('image_policy') == 'prebuilt'
        if prebuilt and any('build' in configured_services[s] for s in services):
            raise ValueError('prebuilt image policy forbids task image builds')
        if not prebuilt and any('build' in configured_services[s] for s in services):
            call(command + ['build'], 'image_build', command_timeout=None if build_timeout == 0 else build_timeout)
        created = True
        call(command + ['up', '-d', *(['--no-build', '--pull', 'never'] if prebuilt else []), '--wait', '--wait-timeout', str(min(timeout, 300))], 'service_start')
        image_ids = set()
        runtime_inspections = {}
        for service in services:
            id_log = call(command + ['ps', '-q', service], 'container_id_' + service, machine_json=True)
            ids = id_log.read_text().split()
            if len(ids) != 1 or not re.fullmatch(r'[0-9a-f]{64}', ids[0]):
                raise ValueError('expected one container per scientific service')
            info_log = call([docker, 'inspect', ids[0]], 'inspect_' + service, machine_json=True)
            inspection = json.loads(info_log.read_text())
            if not isinstance(inspection, list) or len(inspection) != 1 or not isinstance(inspection[0], dict):
                raise ValueError('docker inspect must return exactly one container object')
            info = inspection[0]
            if info.get('State', {}).get('Running') is not True or info.get('State', {}).get('Health', {}).get('Status', 'healthy') != 'healthy':
                raise ValueError(f'container is not running/healthy: {service}')
            if not re.fullmatch(r'sha256:[0-9a-f]{64}', str(info.get('Image', ''))):
                raise ValueError('docker inspect did not report an immutable image ID')
            runtime_inspections[service] = info
            image_ids.add(info['Image'])
        if len(image_ids) != 1:
            raise ValueError('solver and evaluator are not running the same image ID')
        evidence['image_id'] = image_ids.pop()
        evidence['services'] = {s: i['Id'] for s, i in runtime_inspections.items()}
        main = runtime_inspections[solver]
        if spec['runtime']['network_policy'] == 'enabled' and (network.report['mode'] == 'proxy' or probe_urls(spec)):
            execute(solver, container_probe_command(probe_urls(spec)), 'network')
        mounts = main.get('Mounts', [])
        problem = spec.get('runtime', {}).get('task_dir', '/task/problem')
        if not any(m.get('Destination') == problem and m.get('RW') is False for m in mounts):
            raise ValueError('actual solver task mount is not read-only')
        private = [temporary / p for p in layout.get('private_paths', [])]
        private.append(temporary / layout['method_path'])
        private.extend(temporary / source['package_path'] for source in spec.get('data', {}).get('sources', [])
                       if source.get('visibility') in {'private', 'build_only', 'guided'} and source.get('package_path'))
        for mount in mounts:
            source = Path(mount.get('Source', '/nonexistent')).resolve()
            if any(source == p.resolve() or source.is_relative_to(p.resolve()) or p.resolve().is_relative_to(source)
                   for p in private):
                raise ValueError('actual solver mount exposes a protected host directory')
        if spec['runtime']['network_policy'] in {'disabled', 'gateway_search_only'}:
            networks = main.get('NetworkSettings', {}).get('Networks', {})
            if main.get('HostConfig', {}).get('NetworkMode') != 'none' and not networks:
                raise ValueError('actual network isolation is missing from Docker inspection')
            for attached_network in networks.values():
                if main['HostConfig'].get('NetworkMode') == 'none':
                    break
                netlog = call([docker, 'network', 'inspect', attached_network['NetworkID']], 'network_' + attached_network['NetworkID'][:12], machine_json=True)
                if not json.loads(netlog.read_text())[0].get('Internal'):
                    raise ValueError('solver network is not disabled/internal as specified')
        # Validate real runtime resources; GPU availability has a task-specific probe below.
        host = main['HostConfig']
        if not isinstance(host.get('Memory'), int) or host['Memory'] <= 0 or host['Memory'] > spec['runtime']['memory_mb'] * 1024 ** 2:
            raise ValueError('solver memory limit missing or exceeds task budget')
        effective_cpu = host.get('NanoCpus', 0) / 1e9
        if not effective_cpu and host.get('CpuPeriod', 0):
            effective_cpu = host.get('CpuQuota', 0) / host['CpuPeriod']
        if effective_cpu <= 0 or effective_cpu > spec['runtime']['cpu']:
            raise ValueError('solver CPU limit missing or exceeds task budget')
        execute(solver, ['python', '-c',
            "from pathlib import Path; p=Path(" + repr(problem) + "); "
            "assert (p/'README.md').read_text().strip(); assert (p/'data_description.md').read_text().strip()"], 'solver_input')
        destination = spec['submission']['path']
        execute(solver, ['python', '-c',
            "from pathlib import Path; import tempfile; p=Path(" + repr(destination) + ").parent; "
            "p.mkdir(parents=True,exist_ok=True); f=tempfile.TemporaryFile(dir=p); f.write(b'probe'); f.close()"], 'submission_writable')
        # Mount/image structural checks above are mandatory, in addition to no obvious
        # protected paths being reachable in the actual solver namespace.
        execute(solver, ['python', '-c', "from pathlib import Path; assert all(not Path(p).exists() for p in "
                                        + repr(hidden_targets) + ")"], 'hidden_isolation')
        for name in [*('data_identity', 'multimodal', 'dependencies', 'scoring', 'gpu'),
                     *sorted(key for key in checks if key.startswith('scientific_'))]:
            if name == 'multimodal' and name not in checks:
                continue
            if name == 'gpu' and name not in checks:
                continue
            check = checks[name]
            service = check.get('service', evaluators[0] if name == 'scoring' else solver)
            if service not in services:
                raise ValueError('check service outside solver/evaluator list')
            if name != 'scoring' and not name.startswith('scientific_') and service != solver:
                raise ValueError(f'{name} must run in the solver service')
            execute(service, check['command'], name)
        from public_validation import measured_result, probe_steps, required as public_validation_required
        if public_validation_required(spec):
            validation = spec['verification']['public_validation']
            logs = {}
            for name, argv in probe_steps(validation).items():
                logs[name] = call(command + ['exec', '-T', solver, *argv], name, scope='container',
                                  machine_json=name in ('public_baseline_output', 'public_validation'))
            evidence['public_validation'] = measured_result(validation, read_json(logs['public_baseline_output']),
                                                          read_json(logs['public_validation']))
        if package_fingerprint(package) != evidence['package_sha256'] or package_fingerprint(temporary) != evidence['package_sha256']:
            raise ValueError('package changed during runtime verification')
        evidence['status'] = 'pass'
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
        evidence['errors'].append(str(exc))
    finally:
        if created:
            cleanup = call(command + ['down', '--volumes', '--remove-orphans'], 'cleanup', required=False)
            if evidence['checks'][-1]['exit_code']:
                evidence['errors'].append('probe cleanup failed; inspect ' + str(cleanup))
                evidence['status'] = 'failed'
        # Containers may alter mounted files during teardown, so bind the final
        # evidence only after cleanup has completed as well.
        if evidence['package_sha256'] is not None:
            try:
                if package_fingerprint(package) != evidence['package_sha256'] or package_fingerprint(temporary) != evidence['package_sha256']:
                    evidence['errors'].append('package changed during runtime verification or cleanup')
                    evidence['status'] = 'failed'
            except (OSError, ValueError) as exc:
                evidence['errors'].append(str(exc))
                evidence['status'] = 'failed'
        evidence['verified_at'] = datetime.now(timezone.utc).isoformat()
        atomic_json(output / 'runtime_evidence.json', evidence)
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--docker', default='docker')
    parser.add_argument('--timeout', type=int, default=600)
    args = parser.parse_args()
    try:
        result = probe(args.package, read_json(args.package / 'execution.json'), args.output,
                       docker=args.docker, timeout=args.timeout)
        print(json.dumps({'status': result['status'], 'errors': result['errors']}, ensure_ascii=False))
        return 0 if result['status'] == 'pass' else 2
    except (OSError, ValueError) as exc:
        print(json.dumps({'status': 'error', 'error': str(exc)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
