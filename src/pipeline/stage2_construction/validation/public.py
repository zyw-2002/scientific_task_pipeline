"""Solver-visible baseline and evaluation bindings used by authoring and the container probe."""
import json
import math
from pathlib import Path, PurePosixPath
import re

from pipeline.stage2_construction.contracts.contract import package_layout, package_path


def required(spec):
    """Return whether an explicitly declared public validation must be checked.

    Formal evaluator scoring and construction smoke are mandatory for generated
    tasks. A separate public validation is optional because many deferred-data
    tasks have no public labels or executable public fixture during construction.
    """
    return 'public_validation' in spec.get('verification', {})


def binding_errors(package, spec, *, deferred_resources=()):
    value = spec.get('verification', {}).get('public_validation')
    if not isinstance(value, dict):
        return ['verification.public_validation must declare the public baseline, validation command, split and metric']
    errors = []
    layout = package_layout(spec)
    public = package_path(package, layout['public_dir'])
    data = package_path(package, layout['data_path'])
    # Only the host's resource_delivery result may defer a missing formal input.
    # Keep its exact package path so aliases cannot name a sibling or a directory member.
    public_inputs = {(s['id'], s['package_path']) for s in spec.get('data', {}).get('sources', [])
                     if isinstance(s, dict) and s.get('visibility') == 'public' and s.get('role') == 'input'
                     and s.get('id') and s.get('package_path')}
    deferred_paths = {package_path(package, row['package_path']) for row in deferred_resources
                      if (row['id'], row['package_path']) in public_inputs}
    mounts = {PurePosixPath('/task/problem'): public, PurePosixPath('/task/input'): public}
    data_dir = PurePosixPath(spec['runtime']['data_dir'])
    if (data.is_dir() or deferred_paths) and data_dir not in mounts:
        mounts[data_dir] = data

    def public_path(arg, *, input_allowed):
        path = PurePosixPath(arg)
        matches = [mount for mount in mounts if path.is_relative_to(mount)]
        if '..' in path.parts or not matches:
            errors.append(f'public validation input or script outside the public task mount: {arg}')
            return
        mount = max(matches, key=lambda p: len(p.parts))
        local = mounts[mount] / path.relative_to(mount)
        if not local.resolve().is_relative_to(mounts[mount].resolve()):
            errors.append(f'missing public validation input or script: {arg}')
        elif not local.exists():
            if (not input_allowed or local not in deferred_paths or not local.is_relative_to(data)
                    or local.resolve() != Path(package).resolve() / local.relative_to(package)):
                errors.append(f'missing public validation input or script: {arg}')
        elif local.is_file() and not local.stat().st_size:
            errors.append(f'empty public validation input or script: {arg}')
        elif local.is_file() and local.suffix.lower() in {'.h5', '.hdf5', '.h5ad'}:
            # A tiny HDF5 signature-only placeholder is not a usable public
            # development input.  Full H5AD schema validation belongs to the
            # task's own scientific loader, while this gate rejects the
            # common 108-byte placeholder produced by deferred-data drafts.
            try:
                with local.open('rb') as stream:
                    signature = stream.read(8)
                if signature != b'\x89HDF\r\n\x1a\n' or local.stat().st_size < 512:
                    errors.append(f'invalid public validation HDF5 input: {arg}')
            except OSError:
                errors.append(f'unreadable public validation input: {arg}')
        elif local.is_dir() and not any(p.is_file() and p.stat().st_size for p in local.rglob('*')):
            errors.append(f'empty public validation directory: {arg}')
    for key in ('baseline_command', 'command'):
        argv = value.get(key)
        if not isinstance(argv, list) or not argv or not all(isinstance(v, str) and v.strip() for v in argv):
            errors.append(f'public_validation.{key} must be a nonempty argv')
            continue
        program_index = 0
        if re.fullmatch(r'python(?:\d+(?:\.\d+)*)?|bash|sh|Rscript|node|perl|ruby|julia', Path(argv[0]).name):
            program_index = next((i for i, arg in enumerate(argv[1:], 1) if not arg.startswith('-')), 0)
        for index, arg in enumerate(argv):
            if any(PurePosixPath(arg).is_relative_to(mount) for mount in mounts):
                public_path(arg, input_allowed=index > program_index)
    inputs = value.get('input_paths')
    if not isinstance(inputs, list) or not inputs or not all(isinstance(p, str) and p for p in inputs):
        errors.append('public_validation.input_paths must name the real public validation inputs')
    else:
        for path in inputs:
            public_path(path, input_allowed=True)
    prediction = value.get('prediction_path')
    writable = (PurePosixPath('/workspace'), PurePosixPath(spec['submission']['path']).parent)
    if (not isinstance(prediction, str) or not PurePosixPath(prediction).is_absolute()
            or '..' in PurePosixPath(prediction).parts or any(PurePosixPath(prediction).is_relative_to(mount) for mount in mounts)
            or not any(p in PurePosixPath(prediction).parents and p != PurePosixPath('/') for p in writable)):
        errors.append('public_validation.prediction_path must be a fixed file in the writable workspace or output directory')
    if not isinstance(value.get('metric'), str) or not value['metric'].strip():
        errors.append('public_validation.metric must name a scientific validation metric')
    if value.get('direction') not in ('higher_is_better', 'lower_is_better'):
        errors.append('public_validation.direction must be higher_is_better or lower_is_better')
    return errors


_MISSING_CHECK = """import subprocess, sys
from pathlib import Path
path = Path(sys.argv[1])
if path.exists() or path.is_symlink():
    raise SystemExit('public baseline output already exists before the run')
if subprocess.run(sys.argv[2:]).returncode == 0:
    raise SystemExit('public evaluator accepted a missing prediction')
"""
_OUTPUT_CHECK = """import hashlib, json, sys
from pathlib import Path
p = Path(sys.argv[1])
if not p.is_file() or p.is_symlink() or not p.stat().st_size:
    raise SystemExit('public baseline produced no nonempty prediction file')
h = hashlib.sha256()
with p.open('rb') as f:
    for chunk in iter(lambda: f.read(1048576), b''):
        h.update(chunk)
print(json.dumps({'path': str(p), 'sha256': h.hexdigest(), 'bytes': p.stat().st_size}))
"""


def probe_steps(config):
    prediction = config['prediction_path']
    return {
        'public_validation_missing': ['python', '-c', _MISSING_CHECK, prediction, *config['command']],
        'public_baseline': config['baseline_command'],
        'public_baseline_output': ['python', '-c', _OUTPUT_CHECK, prediction],
        'public_validation': config['command'],
    }


def measured_result(config, output, result):
    if (not isinstance(output, dict) or not isinstance(output.get('path'), str)
            or PurePosixPath(output['path']) != PurePosixPath(config['prediction_path'])
            or type(output.get('bytes')) is not int or output['bytes'] <= 0
            or not re.fullmatch('[0-9a-f]{64}', str(output.get('sha256', '')))):
        raise ValueError('public baseline output evidence lacks the declared nonempty hashed prediction')
    metric = config['metric']
    if not isinstance(result, dict) or not isinstance(result.get('metrics'), dict):
        raise ValueError('public evaluator must print a JSON object with metrics and num_samples')
    score = result['metrics'].get(metric)
    if (type(score) not in (int, float) or isinstance(score, float) and not math.isfinite(score)
            or type(result.get('num_samples')) is not int or result['num_samples'] <= 0):
        raise ValueError('public evaluator must return a finite declared metric and positive integer num_samples')
    return {'metric': metric, 'direction': config['direction'], 'value': score,
            'num_samples': result['num_samples'], 'input_paths': config['input_paths'],
            'prediction': output}


def evidence_errors(spec, evidence):
    config = spec.get('verification', {}).get('public_validation')
    if not isinstance(config, dict):
        return ['missing public validation binding']
    try:
        steps = probe_steps(config)
        checks = [item for item in evidence['checks'] if isinstance(item, dict) and item.get('id') in steps]
        if [item['id'] for item in checks] != list(steps):
            return ['public validation receipt lacks the ordered missing-output, baseline, output hash and score checks']
        services = package_layout(spec)['solver_services']
        observed_service = None
        for item in checks:
            cmd = item['command']
            argv = steps[item['id']]
            if (len(cmd) < len(argv) + 3 or cmd[-len(argv):] != argv or cmd[-len(argv)-3:-len(argv)-1] != ['exec', '-T']
                    or cmd[-len(argv)-1] not in services or item.get('scope') != 'container'):
                return ['public validation receipt does not use the declared Solver command: ' + item['id']]
            service = cmd[-len(argv)-1]
            if observed_service is not None and observed_service != service:
                return ['public validation steps must use the same Solver service']
            observed_service = service
        output, scores = [json.loads(Path(item['log_path']).read_text()) for item in checks[-2:]]
        if measured_result(config, output, scores) != evidence.get('public_validation'):
            return ['public baseline metric receipt differs from the observed prediction and score logs']
    except (KeyError, ValueError, TypeError, OSError) as exc:
        return ['invalid public validation receipt: ' + str(exc)]
    return []
