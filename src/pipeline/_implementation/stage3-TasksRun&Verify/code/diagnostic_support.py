"""Explicit blocked-package trial adapter; never issues scientific readiness."""
import json
import shlex
import shutil
from pathlib import Path

from contract import package_fingerprint, package_layout, package_path, validate_execution, load_execution, save_execution
from score_attempt import read_json, write_json, sha256, now
from worker_runtime import copy_tree


def validate_diagnostic(package):
    spec = load_execution(package)
    errors = validate_execution(spec)
    if errors:
        raise ValueError('; '.join(errors))
    record = read_json(package / 'diagnostic.json')
    if record.get('schema_version') != 'pipeline-diagnostic-package-v1' or record.get('diagnostic_only') is not True:
        raise ValueError('Missing explicit diagnostic provenance')
    source = Path(record['source_package'])
    if source.resolve() == package.resolve() or package_fingerprint(source) != record['source_package_sha256']:
        raise ValueError('Original diagnostic source was changed')
    receipt = read_json(package / 'readiness.json')
    if receipt != record['original_readiness']:
        raise ValueError('Diagnostic adapter must retain original readiness unchanged')
    return spec, receipt


def prepare_package(source, output, image):
    import yaml
    source, output = source.resolve(), output.resolve()
    if not image or any(c.isspace() for c in image):
        raise ValueError('Specify an existing task image')
    if output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError('Diagnostic copy must be separate from original source')
    original_hash = package_fingerprint(source)
    receipt = read_json(source / 'readiness.json')
    copy_tree(source, output)
    spec = load_execution(output)
    layout = package_layout(spec)
    compose = package_path(output, layout['compose_file'])
    config = yaml.safe_load(compose.read_text())
    services = config['services']
    solvers = [s for s in layout['solver_services'] if s in services]
    if len(solvers) != 1:
        raise ValueError('Diagnostic task needs one identifiable Solver service')
    solver = solvers[0]
    if solver != 'main':
        if 'main' in services:
            raise ValueError('Conflicting main service')
        services['main'] = services.pop(solver)
    evaluator = spec['verification']['score_evidence']['service']
    if evaluator == 'main' or evaluator not in services:
        raise ValueError('Diagnostic task needs a separate evaluator service')
    spec['runtime'].update(image_policy='prebuilt', base_image=image)
    spec.setdefault('framework', {})['solver_services'] = ['main']
    public = package_path(output, layout['public_dir'])
    import os
    public_source = os.path.relpath(public, compose.parent)
    mounts = services['main'].setdefault('volumes', [])
    if not any((v.get('target') if isinstance(v, dict) else v.split(':')[1]) == '/task/problem' for v in mounts):
        mounts.append({'type':'bind','source':public_source,'target':'/task/problem','read_only':True})
    for service in services.values():
        service.pop('build', None)
        service.pop('cpu_count', None)
        service['image'] = image
        service['cpus'] = spec['runtime']['cpu']
    compose.write_text(yaml.safe_dump(config, sort_keys=False))
    # Keep task metric code/config untouched. Restore framework transport helpers
    # only in this explicit diagnostic copy, recording every changed file below.
    stage2 = Path(__file__).resolve().parents[2] / 'stage2-TasksConstruction&Verify/code'
    evaluation = output / 'common_task/evaluation'
    evaluation.mkdir(parents=True, exist_ok=True)
    tests = output / 'common_task/tests'
    tests.mkdir(parents=True, exist_ok=True)
    for name in ('scoring_entry.py',):
        shutil.copyfile(stage2 / name, evaluation / name)
    shutil.copyfile(Path(__file__).with_name('score_attempt.py'), evaluation / 'score_attempt.py')
    obsolete = tests / 'score_attempt.py'
    if obsolete.is_file() and sha256(obsolete) == sha256(Path(__file__).with_name('score_attempt.py')):
        obsolete.unlink()
    shutil.copyfile(stage2 / 'score_verifier.py', tests / 'score_verifier.py')
    evidence = spec['verification']['score_evidence']['artifact_source']
    from execution_contract import verifier_script
    (tests / 'test.sh').write_text(verifier_script(evidence))
    (tests / 'test.sh').chmod(0o755)
    entry = spec['verification']['score_evidence'].get('entrypoint_container', '/task/evaluation/scoring_entry.py')
    # Use the generated task evaluator unchanged, including any genuine faults.
    protocol = spec['verification']['score_evidence'].get('evaluation_protocol')
    evaluation_command = (['python',protocol['container_path'],'--config',protocol['config_container']]
                          if protocol else ['python','/task/evaluation/evaluation_protocol.py'])
    collect = shlex.join(['python',entry,'--submission',spec['submission']['path'],
        '--evidence-root',evidence,'--',*evaluation_command,
        '--input','{submission}','--output','{output}'])
    network_mode = {'enabled':'public','disabled':'no-network','gateway_search_only':'no-network','allowlisted':'allowlist'}[spec['runtime']['network_policy']]
    environment = {'docker_image':image,'cpus':spec['runtime']['cpu'],
        'memory_mb':spec['runtime']['memory_mb'],'gpus':spec['runtime']['gpus'],
        'build_timeout_sec':spec['runtime'].get('build_timeout_sec',0),'network_mode':network_mode}
    if network_mode == 'allowlist':
        environment['allowed_hosts'] = spec['runtime']['allowed_hosts']
    lines = ['schema_version = "1.4"', '', '[task]', 'name = "diagnostic/scientific-task"',
             '', '[agent]', 'timeout_sec = ' + str(spec['runtime']['time_limit_sec']), '', '[environment]']
    lines.extend(k + ' = ' + json.dumps(v) for k,v in environment.items())
    lines += ['', '[verifier]', 'environment_mode = "separate"', 'timeout_sec = 600',
              '', '[verifier.environment]', 'docker_image = ' + json.dumps(image), 'network_mode = "no-network"',
              '', '[[verifier.collect]]', 'service = ' + json.dumps(evaluator),
              'command = ' + json.dumps(collect), 'timeout_sec = 360', '', '[[artifacts]]',
              'source = ' + json.dumps(evidence), 'service = ' + json.dumps(evaluator), '']
    package_path(output, layout['task_config_path']).write_text('\n'.join(lines))
    save_execution(output, spec)
    # Check actual private mounts even though scientific acceptance is bypassed.
    from preflight import _compose_checks
    issues = [check for check in _compose_checks(output, spec, package_layout(spec)) if check['status'] != 'pass']
    if issues:
        raise ValueError('Diagnostic environment isolation/config rejected: ' + str(issues))
    from contract import package_manifest
    before, after = package_manifest(source), package_manifest(output)
    changes = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
    record = {'schema_version':'pipeline-diagnostic-package-v1','created_at':now(),'diagnostic_only':True,
        'source_package':str(source),'source_package_sha256':original_hash,
        'original_readiness':receipt,'task_image':image,'adapter_changes':changes,
        'note':'Unverified task trial. Only runtime/transport wiring adapted; scientific requirements and metric code remain unchanged.'}
    write_json(output / 'diagnostic.json', record)
    if package_fingerprint(source) != original_hash:
        raise ValueError('Original package changed during diagnostic preparation')
    return output
