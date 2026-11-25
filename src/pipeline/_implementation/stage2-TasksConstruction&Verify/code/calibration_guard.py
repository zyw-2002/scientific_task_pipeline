#!/usr/bin/env python3
"""Run a calibration suite against real code and declared faulty implementations.

Executed inside the evaluator container, never as host scientific validation.
Every run uses its own writable copy; mounted task files remain unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
from pathlib import Path
import shutil
import subprocess
import tempfile
import time


def _file(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise ValueError('calibration paths must stay inside their declared root')
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink() or not path.is_file():
        raise ValueError('missing or unsafe calibration file: ' + relative)
    return path


def script_arguments(command: list[str]) -> tuple[str | None, list[str]]:
    """Identify explicit script arguments; inline/module probes are allowed separately."""
    executable = Path(command[0]).name
    interpreter = bool(re.fullmatch(r'python(?:\d+(?:\.\d+)*)?', executable)) or executable in {
        'sh', 'bash', 'zsh', 'node', 'nodejs', 'Rscript', 'julia', 'ruby', 'perl'}
    if not interpreter:
        entry = command[0] if '/' in command[0] or Path(command[0]).suffix in {'.py', '.sh'} else None
        return entry, command[1:]
    i = 1
    while i < len(command):
        value = command[i]
        if value in {'-c', '-e', '--eval'} and (executable not in {'sh', 'bash', 'zsh'} or value == '-c'):
            return None, []  # Inline code is not a filesystem path.
        if executable in {'sh', 'bash', 'zsh'} and value.startswith('-') and 'c' in value[1:]:
            return None, []
        if value == '-m':
            return None, command[i + 2:]
        if value in {'-W', '-X'}:
            i += 2
            continue
        if value == '--':
            i += 1
            break
        if not value.startswith('-'):
            break
        i += 1
    return (command[i], command[i + 1:]) if i < len(command) else (None, [])


def inspect_config(config_path: Path) -> tuple[dict, Path]:
    config = json.loads(config_path.read_text())
    if not isinstance(config, dict):
        raise ValueError('calibration config must be a JSON object')
    source = config.get('source_dir', '.')
    if not isinstance(source, str) or Path(source).is_absolute() or '..' in Path(source).parts:
        raise ValueError('source_dir must be relative to the calibration config')
    root = (config_path.parent / source).resolve()
    if not root.is_dir() or not root.is_relative_to(config_path.parent.resolve()):
        raise ValueError('missing or unsafe calibration source directory')
    if any(path.is_symlink() for path in root.rglob('*')):
        raise ValueError('calibration sources must not contain symlinks')
    command = config.get('command')
    if not isinstance(command, list) or not command or not all(isinstance(v, str) and v for v in command):
        raise ValueError('calibration command must be an argv array')
    if not any('{root}' in part for part in command):
        raise ValueError('suite command must run from its disposable {root}')
    entry, _ = script_arguments(command)
    if entry is not None and not entry.startswith('{root}/'):
        raise ValueError('calibration suite script must run from its disposable {root}/ path')
    # Check declared suite/input paths without importing scientific dependencies.
    # The default source directory includes the adapter and the generated evaluator.
    for argument in command:
        value = argument.split('=', 1)[1] if argument.startswith('--') and '=' in argument else argument
        if value == '{root}':
            continue  # E.g. a test runner's discovery directory.
        if not value.startswith('{root}/'):
            continue
        relative = value[len('{root}/'):]
        path = root / relative
        if ('..' in Path(relative).parts or not path.resolve().is_relative_to(root.resolve())
                or path.is_symlink() or not path.exists()):
            raise ValueError('missing or unsafe calibration command path: ' + value)
        if value == entry and (not path.is_file() or not path.read_bytes().strip()):
            raise ValueError('calibration suite script must be a nonempty file: ' + value)
        if path.is_file() and path.suffix == '.py':
            compile(path.read_text(encoding='utf-8'), str(path), 'exec')
    mutations = config.get('mutations')
    if not isinstance(mutations, list) or not mutations:
        raise ValueError('declare at least one meaningful faulty implementation')
    seen = set()
    for mutation in mutations:
        mid = mutation.get('id') if isinstance(mutation, dict) else None
        if not isinstance(mid, str) or not mid or mid in seen or mid == 'baseline':
            raise ValueError('mutations need unique nonempty IDs')
        seen.add(mid)
        if not mutation.get('fault'):
            raise ValueError('each mutation must explain the scientific or acceptance fault')
        target = _file(root, mutation.get('target'))
        replacement = _file(config_path.parent, mutation.get('replacement'))
        if target.suffix == '.py':
            compile(target.read_text(), str(target), 'exec')
            compile(replacement.read_text(), str(replacement), 'exec')
        if target.read_bytes() == replacement.read_bytes():
            raise ValueError('mutation must differ from the actual implementation')
    return config, root


def _digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''): result.update(block)
    return result.hexdigest()


def input_hashes(config_path):
    """Bind the disposable source, suite data and mutants, excluding prior evidence."""
    config, source = inspect_config(config_path)
    archive = config_path.parent / 'checks/evidence'
    result = {'config': _digest(config_path)}
    for path in sorted(source.rglob('*')):
        if (path.is_file() and not path.is_relative_to(archive)
                and '__pycache__' not in path.parts and path.suffix != '.pyc'):
            result['source/' + path.relative_to(source).as_posix()] = _digest(path)
    for mutant in config['mutations']:
        result['mutant/' + mutant['id']] = _digest(_file(config_path.parent, mutant['replacement']))
    return result


def run_passed(record, *, baseline):
    """Identical success criteria during execution and saved-evidence validation."""
    if type(record.get('exit_code')) is not int: return False
    if baseline: return record['exit_code'] == 0
    stdout, stderr = record.get('stdout'), record.get('stderr')
    if not isinstance(stdout, str) or not isinstance(stderr, str): return False
    diagnostic = stdout + '\n' + stderr
    broken = re.search(r'(?:ImportError|ModuleNotFoundError|SyntaxError|IndentationError|NameError|AttributeError):', diagnostic)
    test_failure = any(marker in diagnostic for marker in ('AssertionError', 'FAIL:', 'FAILED'))
    return record['exit_code'] == 1 and test_failure and not broken


def evidence_errors(config_path, result):
    """Validate a preserved guard result without running the scientific suite."""
    config, _ = inspect_config(config_path)
    if not isinstance(result, dict) or result.get('schema_version') != 'scientific-calibration-guard-v1':
        return ['calibration needs a structured guard report']
    errors = []
    if result.get('status') != 'pass': errors.append('calibration report did not pass')
    if result.get('inputs_sha256') != input_hashes(config_path):
        errors.append('calibration evidence is stale or lacks input hashes; rerun current calibration')
    runs = result.get('runs')
    ids = ['baseline', *[m['id'] for m in config['mutations']]]
    if (not isinstance(runs, list) or len(runs) != len(ids)
            or any(not isinstance(r, dict) for r in runs)
            or [r.get('id') for r in runs] != ids):
        return errors + ['calibration evidence must cover baseline and every declared mutant exactly once']
    for record in runs:
        if record.get('status') != 'pass' or not run_passed(record, baseline=record['id'] == 'baseline'):
            errors.append('calibration run did not pass: ' + record['id'])
    return errors


def run_guard(config_path: Path, *, timeout: int = 120) -> dict:
    if type(timeout) is not int or timeout <= 0:
        raise ValueError('timeout must be a positive integer')
    config_path = config_path.resolve()
    config, source = inspect_config(config_path)
    inputs = input_hashes(config_path)
    records = []
    for mutation in [None, *config['mutations']]:
        mid = mutation['id'] if mutation else 'baseline'
        with tempfile.TemporaryDirectory(prefix='scientific-calibration-') as temp:
            root = Path(temp) / 'source'
            def ignore(directory, names):
                skipped = set(shutil.ignore_patterns('__pycache__', '*.pyc')(directory, names))
                if Path(directory).resolve() == (config_path.parent / 'checks').resolve():
                    skipped.add('evidence')  # Archived prior runs are not calibration inputs.
                return skipped
            shutil.copytree(source, root, ignore=ignore)
            if mutation:
                shutil.copyfile(_file(config_path.parent, mutation['replacement']), root / mutation['target'])
            argv = [value.replace('{root}', str(root)) for value in config['command']]
            started = time.monotonic()
            try:
                process = subprocess.Popen(argv, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           text=True, start_new_session=True,
                                           env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
                                                'PYTHONPATH': str(root)})
                try:
                    stdout, stderr = process.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()
                    raise
                # Require a real assertion/test failure, not a broken import, tool or process.
                detected = run_passed({'exit_code': process.returncode, 'stdout': stdout, 'stderr': stderr},
                                      baseline=mutation is None)
                records.append({'id': mid, 'status': 'pass' if detected else 'fail',
                                'exit_code': process.returncode, 'stdout': stdout,
                                'stderr': stderr, 'duration_sec': time.monotonic() - started})
            except (OSError, subprocess.TimeoutExpired) as exc:
                records.append({'id': mid, 'status': 'fail', 'error': str(exc)})
        if mutation is None and records[-1]['status'] != 'pass':
            break
    result = {'schema_version': 'scientific-calibration-guard-v1',
            'status': 'pass' if len(records) == len(config['mutations']) + 1 and
                       all(r['status'] == 'pass' for r in records) else 'fail',
            'inputs_sha256': inputs,
            'runs': records,
            'scope': 'Known faulty implementations detected; not a proof of complete scientific correctness'}
    if input_hashes(config_path) != inputs:
        result.update(status='fail', error='calibration inputs changed during execution')
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, help='Omitted: run every calibration bound in the editable task')
    parser.add_argument('--timeout', type=int, default=120)
    args = parser.parse_args(argv)
    package, evidence = None, None
    try:
        if args.config is not None:
            report = run_guard(args.config, timeout=args.timeout)
        else:
            from authoring_package import evaluation_context, new_evidence, preserve_evidence
            from contract import package_path
            package, evaluation = evaluation_context(scientific_controls=True)
            evidence = new_evidence(package, 'scientific-calibration')
            evidence.mkdir()
            spec = json.loads((package / 'execution.json').read_text())
            bindings = spec.get('verification', {}).get('calibration')
            if not isinstance(bindings, list) or not bindings:
                raise ValueError('no generated calibration bindings; run preflight first')
            reports = []
            for binding in bindings:
                config = package_path(package, binding['config_path'])
                if not config.resolve().is_relative_to(evaluation.resolve()):
                    raise ValueError('calibration config must stay in private evaluation')
                try:
                    result = run_guard(config, timeout=args.timeout)
                except (OSError, ValueError, SyntaxError) as exc:
                    result = {'status': 'fail', 'error': str(exc)}
                reports.append({'runtime_check': binding['runtime_check'], 'config': binding['config_path'], 'result': result})
            report = {'schema_version': 'scientific-calibration-batch-v1',
                      'status': 'pass' if all(r['result']['status'] == 'pass' for r in reports) else 'fail',
                      'calibrations': reports}
    except (OSError, ValueError, SyntaxError, KeyError, TypeError) as exc:
        report = {'status': 'fail', 'error': str(exc)}
    if package is not None and evidence is not None:
        try:
            (evidence / 'report.json').write_text(json.dumps(report, ensure_ascii=False) + '\n')
            report['saved_evidence'] = str(preserve_evidence(package, evidence))
        except (OSError, ValueError) as exc:
            report.update(status='fail', archive_error=str(exc))
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['status'] == 'pass' else 1


if __name__ == '__main__':
    raise SystemExit(main())
