"""Check executable score collection wiring for newly constructed task packages."""
import ast
from pathlib import Path, PurePosixPath
import shlex
import tomllib
import yaml

from pipeline.stage2_construction.contracts.contract import package_layout, package_path, sha256_file


def bind_sources(package: Path, spec: dict, service: str, container_path: str) -> set[Path]:
    """Resolve actual Compose bind mounts for a declared container file."""
    compose_path = package_path(package, package_layout(spec)['compose_file'])
    compose = yaml.safe_load(compose_path.read_text())
    found = set()
    for volume in compose.get('services', {}).get(service, {}).get('volumes', []):
        if isinstance(volume, str):
            host, target, *_ = volume.split(':')
        elif isinstance(volume, dict) and volume.get('type') == 'bind':
            host, target = volume.get('source', ''), volume.get('target', '')
        else:
            continue
        if not isinstance(container_path, str) or not host.startswith(('.', '/')):
            continue
        try:
            relative = PurePosixPath(container_path).relative_to(PurePosixPath(target))
        except ValueError:
            continue
        found.add((compose_path.parent / host / str(relative)).resolve())
    return found


def verifier_script(evidence_root: str) -> str:
    return ('#!/usr/bin/env bash\nset -euo pipefail\n'
            'python /tests/score_verifier.py --evidence-root ' + shlex.quote(evidence_root) +
            ' --output-dir /logs/verifier\n')


def _legacy_verifier_reads(path, *, use_default):
    """Read the invoked verifier as source. Ignore writes, comments and unused files."""
    tree = ast.parse(path.read_text())
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call) or not call.args:
            continue
        first = call.args[0]
        if isinstance(call.func, ast.Name) and call.func.id == 'open':
            mode = next((k.value for k in call.keywords if k.arg == 'mode'),
                        call.args[1] if len(call.args) > 1 else ast.Constant('r'))
            if isinstance(mode, ast.Constant) and str(mode.value).startswith('r'):
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    yield first.value
        if use_default and isinstance(call.func, ast.Attribute) and call.func.attr == 'add_argument':
            if any(isinstance(arg, ast.Constant) and arg.value == '--submission' for arg in call.args):
                for key in call.keywords:
                    if key.arg == 'default' and isinstance(key.value, ast.Constant) and isinstance(key.value.value, str):
                        yield key.value.value


def legacy_output_errors(package, spec):
    submission = PurePosixPath(spec['submission']['path'])
    tests_dir = package / 'common_task/tests'
    hook = tests_dir / 'test.sh'
    if not hook.is_file():
        return ['missing Harbor verifier test.sh for declared required_artifacts'] if spec['submission'].get('required_artifacts') else []
    observed = set()
    shallow_only = True
    try:
        for line in hook.read_text().replace('\\\n', ' ').splitlines():
            args = shlex.split(line, comments=True)
            if not args:
                continue
            command = Path(args[0]).name
            reads = []
            if command in {'test', '[', '[['} and len(args) >= 3 and args[1] in {'-s', '-f', '-e'}:
                reads.append(args[2])
            elif command in {'cmp', 'diff'}:
                operands = [arg for arg in args[1:] if not arg.startswith('-')]
                reads.extend(operands[:2])
                shallow_only = False
            elif command.startswith('python') and args[1:3] == ['-m', 'json.tool']:
                reads.extend(args[3:4])
            elif command.startswith('python') and len(args) >= 2 and Path(args[1]).name == 'verifier.py':
                supplied = [a.split('=', 1)[1] for a in args[2:] if a.startswith('--submission=')]
                supplied.extend(args[i + 1] for i, a in enumerate(args[:-1]) if a == '--submission')
                reads.extend(supplied)
                verifier = tests_dir / 'verifier.py'
                if not verifier.is_file():
                    return ['test.sh invokes missing tests/verifier.py']
                reads.extend(_legacy_verifier_reads(verifier, use_default=not supplied))
                shallow_only = False
            elif command not in {'set', 'echo', 'printf', 'exit', 'true', ':'}:
                shallow_only = False
            for field in reads:
                for prefix in ('$OUTPUT_DIR/', '${OUTPUT_DIR}/'):
                    if field.startswith(prefix):
                        field = str(submission.parent / field[len(prefix):])
                path = PurePosixPath(field)
                if path != submission.parent and path.is_relative_to(submission.parent):
                    observed.add(path)
    except (OSError, SyntaxError, ValueError) as exc:
        return [f'cannot inspect historical Harbor verifier: {exc}']
    errors = [f'Harbor verifier reads undeclared solver output {path}; declared submission is {submission}'
              for path in sorted(observed - {submission})]
    if submission not in observed and (observed or spec['submission'].get('required_artifacts')):
        errors.append(f'declared submission is unchecked by the Harbor verifier: {submission}')
    if shallow_only:
        errors.append('Harbor verifier only checks presence/format or reports a constant; a scientific measurement is required')
    return errors


def execution_errors(package: Path, spec: dict) -> list[str]:
    if spec.get('scientific_acceptance_version', 0) < 2:
        return legacy_output_errors(package, spec)
    errors = []
    layout = package_layout(spec)
    verification = spec.get('verification', {})
    evidence = verification.get('score_evidence') or {}
    try:
        config = tomllib.loads(package_path(package, layout['task_config_path']).read_text())
        script = package / 'common_task/tests/test.sh'
        if not script.is_file():
            errors.append('missing actual Harbor verifier entry: common_task/tests/test.sh')
        artifacts = config.get('artifacts', [])
        source, service = evidence.get('artifact_source'), evidence.get('service')
        if not source or not service or not isinstance(artifacts, list) or not any(
                isinstance(a, dict) and a.get('source') == source and a.get('service') == service for a in artifacts):
            errors.append('task.toml top-level artifact source/service must match verification.score_evidence')
        if isinstance(source, str) and script.is_file() and script.read_text().strip() != verifier_script(source).strip():
            errors.append('Harbor test.sh must invoke the actual supplied evidence verifier; use verifier_script(artifact_source)')
        verifier = package / 'common_task/tests/score_verifier.py'
        if not verifier.is_file() or sha256_file(verifier) != sha256_file(Path(__file__).with_name('score_verifier.py')):
            errors.append('Harbor verifier must use the supplied unmodified score_verifier.py')
        entry = package_path(package, evidence.get('entrypoint'))
        if not entry.is_file() or sha256_file(entry) != sha256_file(Path(__file__).with_name('scoring_entry.py')):
            errors.append('score collection must use the supplied unmodified scoring_entry.py')
        helper = entry.with_name('score_attempt.py')
        canonical = Path(__file__).resolve().parents[2] / 'stage3-TasksRun&Verify/code/score_attempt.py'
        if not canonical.is_file():
            canonical = Path(__file__).with_name('score_attempt.py')  # Private worker tools.
        if not helper.is_file() or sha256_file(helper) != sha256_file(canonical):
            errors.append('scoring entry needs the supplied score_attempt.py beside it')
        container_entry = evidence.get('entrypoint_container')
        hooks = config.get('verifier', {}).get('collect', [])
        linked = False
        for hook in hooks if isinstance(hooks, list) else []:
            if not isinstance(hook, dict) or hook.get('service') != service:
                continue
            argv = shlex.split(hook.get('command', ''))
            if (container_entry and len(argv) > 1 and argv[1] == container_entry and
                    Path(argv[0]).name.startswith('python') and '--evidence-root' in argv and
                    argv[argv.index('--evidence-root') + 1] == source and '--submission' in argv and
                    argv[argv.index('--submission') + 1] == spec['submission']['path']):
                linked = True
        if not linked:
            errors.append('verifier.collect must invoke the actual scoring entry with the declared submission and evidence root')
        if entry.resolve() not in bind_sources(package, spec, service, container_entry):
            errors.append('declared evaluator mount does not expose the bound scoring entry at entrypoint_container')
        handoff = verification.get('commands', {}).get('scientific_handoff', {})
        if handoff.get('service') != service:
            errors.append('scientific_handoff must test actual submission-to-score-to-evidence flow in the evaluator')
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, yaml.YAMLError) as exc:
        errors.append('invalid score execution contract: ' + str(exc))
    errors.extend(protocol_errors(package, spec))
    return errors


def _storage_locations(package, spec, service, container_path):
    """Map a container file onto its bind or named-volume storage identity."""
    compose_path = package_path(package, package_layout(spec)['compose_file'])
    config = yaml.safe_load(compose_path.read_text())
    found = set()
    for mount in config.get('services', {}).get(service, {}).get('volumes', []):
        if isinstance(mount, str):
            source, target, *options = mount.split(':')
            kind = 'bind' if source.startswith(('.', '/')) else 'volume'
            readonly = bool(options and 'ro' in options[0].split(','))
        elif isinstance(mount, dict):
            source, target = mount.get('source'), mount.get('target')
            kind, readonly = mount.get('type'), mount.get('read_only', False)
        else:
            continue
        if not source or not target:
            continue
        try:
            relative = PurePosixPath(container_path).relative_to(PurePosixPath(target))
        except ValueError:
            continue
        identity = str((compose_path.parent / source).resolve()) if kind == 'bind' else source
        found.add((kind, identity, str(relative), readonly))
    return found


def protocol_errors(package, spec):
    """Require the canonical evaluator on the actual collection command, not as an unused file."""
    if 'evaluation_protocol_version' not in spec:
        return []  # Frozen historical packages retain their original interface.
    if type(spec['evaluation_protocol_version']) is not int or spec['evaluation_protocol_version'] != 1:
        return ['unknown evaluation_protocol_version']
    from pipeline.stage2_construction.contracts.evaluation_protocol import config_errors
    import json
    errors = []
    try:
        layout = package_layout(spec)
        evidence = spec['verification']['score_evidence']
        binding = evidence['evaluation_protocol']
        protocol = package_path(package, binding['path'])
        if sha256_file(protocol) != sha256_file(Path(__file__).with_name('evaluation_protocol.py')):
            errors.append('evaluation must use the supplied unmodified evaluation_protocol.py')
        config_path = package_path(package, binding['config_path'])
        config = json.loads(config_path.read_text())
        errors.extend(config_errors(config))
        if spec.get('framework_generation_version') == 1:
            from pipeline.stage2_construction.contracts.evaluation_protocol import declaration_errors
            from solver_output_audit import protocol_member_errors
            declared = spec.get('submission',{}).get('required_artifacts')
            artifact_errors = declaration_errors(declared)
            errors.extend(artifact_errors)
            if not artifact_errors:
                errors.extend(protocol_member_errors(spec['submission'], config))
        if config.get('reward_direction') not in {'higher_is_better', 'lower_is_better'}:
            errors.append('protocol.reward_direction must declare the actual optimization direction')
        artifact_id = spec.get('verification', {}).get('evaluation_unit_artifact_id')
        if artifact_id:
            artifacts = spec.get('submission', {}).get('required_artifacts', [])
            artifact = next((a for a in artifacts if a.get('id') == artifact_id), None)
            unit_count = len(config.get('units', []))
            if artifact is None:
                available = [a.get('id') for a in artifacts]
                errors.append(f'verification.evaluation_unit_artifact_id {artifact_id!r} must match '
                              f'an ID in submission.required_artifacts; declared IDs: {available!r}')
            elif type(artifact.get('count')) is not int or artifact['count'] != unit_count:
                errors.append(f'evaluation unit count {unit_count} differs from required artifact '
                              f'{artifact_id!r} count {artifact.get("count")!r}')
            if len(config.get('units', [])) > 1 and len({u['path'] for u in config['units']}) != len(config['units']):
                errors.append('fixed-count unit artifacts must reference distinct submission members')
        adapter = (config_path.parent / config['adapter']).resolve()
        if not adapter.is_relative_to(config_path.parent.resolve()) or not adapter.is_file():
            errors.append('metric adapter is missing or outside the evaluator directory')
        evaluator = evidence['service']
        for path, container in [(protocol, binding['container_path']), (config_path, binding['config_container']),
                                (adapter, str(PurePosixPath(binding['config_container']).parent / config['adapter']))]:
            if path.resolve() not in bind_sources(package, spec, evaluator, container):
                errors.append('evaluation protocol/config/adapter is not mounted at its declared evaluator path')
        toml = tomllib.loads(package_path(package, layout['task_config_path']).read_text())
        hooks = toml.get('verifier', {}).get('collect', [])
        expected = ['--config', binding['config_container'], '--input', '{submission}', '--output', '{output}']
        connected = False
        for hook in hooks if isinstance(hooks, list) else []:
            if not isinstance(hook, dict) or hook.get('service') != evaluator:
                continue
            argv = shlex.split(hook.get('command', ''))
            if '--' in argv:
                outer, inner = argv[:argv.index('--')], argv[argv.index('--') + 1:]
                outer_ok = (len(outer) > 1 and outer[1] == evidence.get('entrypoint_container')
                            and '--submission' in outer and '--evidence-root' in outer
                            and outer[outer.index('--submission') + 1] == spec['submission']['path']
                            and outer[outer.index('--evidence-root') + 1] == evidence.get('artifact_source'))
                connected |= (outer_ok and len(inner) == 8 and Path(inner[0]).name.startswith('python')
                              and inner[1] == binding['container_path'] and inner[2:] == expected)
        if not connected:
            errors.append('verifier.collect must invoke the canonical evaluation protocol with frozen submission and fresh output')
        submission = spec['submission']['path']
        readers = _storage_locations(package, spec, evaluator, submission)
        writers = set()
        for solver in layout['solver_services']:
            writers |= {row[:3] for row in _storage_locations(package, spec, solver, submission) if not row[3]}
        if not writers.intersection({row[:3] for row in readers if row[3]}):
            errors.append('evaluator needs read-only access to the same submission storage writable by Solver')
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as error:
        errors.append('invalid evaluation protocol: ' + str(error))
    return errors
