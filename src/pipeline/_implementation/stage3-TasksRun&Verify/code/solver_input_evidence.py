"""Match ATIF file-tool observations to the public input bytes frozen for a run."""
import hashlib
from pathlib import Path, PurePosixPath
import re
import yaml

from contract import package_layout
from score_attempt import timestamp
from task_contract import public_path


def _read_command(call, result):
    """Only single absolute file reads; stdout must match the frozen artifact."""
    args = call.get('arguments')
    if not isinstance(args, dict) or not isinstance(result, str):
        return None
    if call.get('function_name') == 'read_file' and set(args) == {'file_path'}:
        return args['file_path'], None, result
    if call.get('function_name') != 'run_shell_command' or not isinstance(args.get('command'), str):
        return None
    command = args['command']
    match = re.fullmatch(r'(?:(?:/usr)?/bin/)?(cat|head)(?: -n ([1-9]\d{0,3}))? (?:-- )?(/[\w./+-]+)', command)
    if not match or (match[1] == 'cat' and match[2]):
        return None
    observed = re.fullmatch(r'Command: ([^\n]+)\nDirectory: [^\n]+\nOutput: (.*?)\nError: \(none\)\nExit Code: 0\nSignal: \(none\)\nProcess Group PGID: [^\n]+\n?', result, re.S)
    if not observed or observed[1] != command:
        return None
    return match[3], int(match[2] or 10) if match[1] == 'head' else None, observed[2]


def collect(*, trajectory, effective, spec, cutoff, backend):
    layout = package_layout(spec)
    base = Path(effective['effective_task']).resolve()
    compose = base / Path(layout['compose_file']).relative_to('common_task')
    mounts = []
    if compose.is_file():
        services = (yaml.safe_load(compose.read_text()) or {}).get('services', {})
        for volume in services.get('main', {}).get('volumes', []):
            if isinstance(volume, str):
                parts = volume.split(':')
                if len(parts) != 3 or 'ro' not in parts[2].split(','):
                    continue
                host, target = parts[:2]
            elif isinstance(volume, dict) and volume.get('type') == 'bind' and volume.get('read_only') is True:
                host, target = volume.get('source'), volume.get('target')
            else:
                continue
            if isinstance(host, str) and isinstance(target, str) and host.startswith(('.', '/')):
                mounts.append(((compose.parent / host).resolve(), PurePosixPath(target)))
    sources = list(spec.get('data', {}).get('sources', []))
    manifest = layout['data_sources_path']
    if str(PurePosixPath(manifest).relative_to('common_task')) in effective.get('files', {}) and not any(
            isinstance(s, dict) and s.get('package_path') == manifest for s in sources):
        sources.append({'id':'data_sources_manifest', 'role':'input', 'visibility':'public', 'package_path':manifest})
    items, paths = [], {}
    for source in sources if isinstance(sources, list) else []:
        if not isinstance(source, dict) or source.get('role') != 'input' or source.get('visibility') != 'public':
            continue
        relative = source.get('package_path') or source.get('path')
        if not isinstance(relative, str):
            continue
        path = PurePosixPath(relative)
        if not path.is_relative_to('common_task') or '..' in path.parts:
            continue
        local = str(path.relative_to('common_task'))
        try:
            runtime = public_path(relative, spec)
        except (KeyError, TypeError, ValueError):
            continue
        record = {'resource_id':source.get('id'), 'path':runtime, 'relative':local,
                  'sha256':effective.get('files', {}).get(local), 'declared_split':source.get('split'),
                  'read_status':'unobserved', 'observations':[]}
        items.append(record)
        if record['sha256']:
            paths[runtime] = record
            actual = (base / local).resolve()
            for host, target in mounts:
                if actual.is_relative_to(host):
                    alias = target / actual.relative_to(host)
                    # A more specific Docker mount shadows its parent.
                    matches = [(len(dst.parts), src / alias.relative_to(dst)) for src, dst in mounts if alias.is_relative_to(dst)]
                    if max(matches, key=lambda m: m[0])[1].resolve() == actual:
                        paths[str(alias)] = record
    report = {'scope':'Harbor tool-call/result pairs with output matching the frozen input',
              'process_read_status':'unobserved', 'split_semantics':'declared per file; row selection and algorithm use unobserved',
              'inputs':items}
    if backend != 'harbor_docker' or not isinstance(trajectory, dict) or not paths:
        return report
    steps = trajectory.get('steps')
    if not isinstance(steps, list):
        return report
    # ATIF can assign the assistant-step timestamp before the tool returns.
    # A later agent decision before submission bounds completion of that step.
    for index, step in enumerate(steps[:-1]):
        if not isinstance(step, dict) or step.get('source') != 'agent':
            continue
        try:
            following = steps[index+1]
            if not isinstance(following, dict) or not (timestamp(step['timestamp']) <= timestamp(following['timestamp']) <= cutoff):
                continue
        except (ValueError, KeyError, TypeError):
            continue
        calls = step.get('tool_calls') or []
        observation = step.get('observation')
        results = observation.get('results', []) if isinstance(observation, dict) else []
        if not isinstance(calls, list) or not isinstance(results, list):
            continue
        for call in calls:
            if not isinstance(call, dict) or not call.get('tool_call_id'):
                continue
            rid = call['tool_call_id']
            matches = [r for r in results if isinstance(r, dict) and r.get('source_call_id') == rid]
            if len(matches) != 1 or sum(isinstance(c, dict) and c.get('tool_call_id') == rid for c in calls) != 1:
                continue
            found = _read_command(call, matches[0].get('content'))
            if not found or not isinstance(found[0], str) or found[0] not in paths:
                continue
            runtime, limit, returned = found
            record = paths[runtime]
            if not returned or not returned.strip():
                continue
            payload = returned.encode('utf-8')
            source = Path(effective['effective_task']) / record['relative']
            if not source.is_file() or source.is_symlink():
                continue
            # Use bounded memory. validate_effective_input already checked the
            # full-file digest; here compare what this specific call returned.
            with source.open('rb') as stream:
                prefix = stream.read(len(payload) + 2)
            if limit is None:
                expected = prefix
                if len(expected) >= len(payload)+2:
                    continue
            else:
                expected = b''.join(prefix.splitlines(keepends=True)[:limit])
            if expected != payload and expected != payload + b'\n':
                continue
            record['read_status'] = 'matching_tool_observation'
            record['observations'].append({'tool':call['function_name'], 'step_id':step.get('step_id'), 'call_id':rid,
                      'requested_path':runtime, 'observed_at_or_before':following['timestamp'], 'bytes_matched':len(payload),
                      'observed_sha256':hashlib.sha256(payload).hexdigest()})
    return report
