"""Mechanical Compose repair for exec workers; never change scientific tasks."""
from __future__ import annotations
import ast
import copy
from pathlib import PurePosixPath
import re
import shlex


def _argv(value):
    if value is None:
        return []
    if isinstance(value, str):
        return shlex.split(value)
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    raise ValueError('startup command must be a string or an argv list')


def _references(service):
    """Only Compose fields whose values actually name services."""
    depends = service.get('depends_on', [])
    if not isinstance(depends, (list, dict)):
        raise ValueError('depends_on must be a list or object')
    for name in depends:
        yield 'depends_on', name
    for field in ('network_mode', 'ipc', 'pid'):
        value = service.get(field)
        if isinstance(value, str) and value.startswith('service:'):
            yield field, value[8:]
    for field in ('links', 'volumes_from'):
        values = service.get(field, [])
        if not isinstance(values, list):
            raise ValueError(field + ' must be a list')
        for value in values:
            if not isinstance(value, str):
                raise ValueError(field + ' entries must be strings')
            if field == 'volumes_from' and value.startswith('container:'):
                continue
            yield field, value.split(':', 1)[0]
    extends = service.get('extends')
    if isinstance(extends, dict) and not extends.get('file') and extends.get('service'):
        yield 'extends', extends['service']


def service_reference_errors(document):
    services = document.get('services', {})
    errors = []
    for name, service in services.items():
        if not isinstance(service, dict):
            continue
        try:
            for field, target in _references(service):
                if not isinstance(target, str) or target not in services:
                    errors.append(f'service {name}: {field} refers to undefined service {target!r}')
                elif target == name:
                    errors.append(f'service {name}: {field} refers to itself')
        except ValueError as exc:
            errors.append(f'service {name}: {exc}')
    return errors


def rewrite_service_references(document, old, new):
    """Rewrite references only; caller must establish the identity mapping."""
    changed = []
    for name, service in document.get('services', {}).items():
        if not isinstance(service, dict):
            continue
        depends = service.get('depends_on')
        if isinstance(depends, list) and old in depends:
            service['depends_on'] = list(dict.fromkeys(new if v == old else v for v in depends))
            changed.append(name + '.depends_on')
        elif isinstance(depends, dict) and old in depends:
            if new in depends and depends[new] != depends[old]:
                raise ValueError(f'service {name}: conflicting dependency options for {old}/{new}')
            service['depends_on'] = {new if k == old else k: v for k, v in depends.items()}
            changed.append(name + '.depends_on')
        for field in ('network_mode', 'ipc', 'pid'):
            if service.get(field) == 'service:' + old:
                service[field] = 'service:' + new
                changed.append(name + '.' + field)
        for field in ('links', 'volumes_from'):
            values = service.get(field)
            if isinstance(values, list):
                updated = [new + v[len(old):] if isinstance(v, str) and v.split(':', 1)[0] == old else v for v in values]
                if updated != values:
                    service[field] = updated
                    changed.append(name + '.' + field)
        extends = service.get('extends')
        if isinstance(extends, dict) and not extends.get('file') and extends.get('service') == old:
            extends['service'] = new
            changed.append(name + '.extends')
    return changed


def rename_service(document, old, new):
    services = document['services']
    if old == new:
        return []
    if new in services:
        raise ValueError('Conflicting service identity: ' + new)
    services[new] = services.pop(old)
    return rewrite_service_references(document, old, new)


def _idle(argv):
    if argv and argv[0] == 'exec':
        argv = argv[1:]
    return argv in (['sleep', 'infinity'], ['sleep', 'inf'], ['tail', '-f', '/dev/null'])


def _print_only(argv):
    if len(argv) != 3 or PurePosixPath(argv[0]).name not in ('python', 'python3') or argv[1] != '-c':
        return False
    try:
        tree = ast.parse(argv[2])
    except SyntaxError:
        return False
    return bool(tree.body) and all(isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name) and node.value.func.id == 'print'
        and all(isinstance(arg, ast.Constant) for arg in node.value.args) and not node.value.keywords for node in tree.body)


def _finite_shell(script):
    lexer = shlex.shlex(script, posix=True, punctuation_chars=';&|')
    lexer.whitespace_split = True
    commands, current = [], []
    for token in lexer:
        if token in ('&&', ';'):
            if current: commands.append(current)
            current = []
        elif token in ('||', '|', '&'):
            return False
        else:
            current.append(token)
    if current: commands.append(current)
    def finite(command):
        head = PurePosixPath(command[0]).name
        return (head in ('echo', 'printf', 'mkdir', 'touch', 'cd', 'export', 'true')
                or (head in ('pip', 'pip3') and len(command) > 1 and command[1] == 'install')
                or (head in ('python', 'python3') and command[1:4] == ['-m', 'pip', 'install']))
    return bool(commands) and all(finite(c) for c in commands)


def _bootstrap(argv):
    """Return finite init argv, [] for idle, None for an unknown server."""
    if _idle(argv):
        return []
    if _print_only(argv) or (argv and PurePosixPath(argv[0]).name in ('echo', 'printf')):
        return argv
    if argv and PurePosixPath(argv[0]).name in ('sh', 'bash', 'dash'):
        index = next((i for i, token in enumerate(argv[1:], 1) if token.startswith('-') and not token.startswith('--') and 'c' in token[1:]), None)
        if index is not None and index + 1 == len(argv) - 1:
            script = argv[index + 1].strip()
            if _idle(shlex.split(script)):
                return []
            match = re.search(r'(?:&&|;)\s*(?:exec\s+)?(?:sleep\s+(?:infinity|inf)|tail\s+-f\s+/dev/null)\s*;?\s*$', script)
            init = script[:match.start()].strip() if match else script
            if _finite_shell(init):
                # Preserve shell/login options, but fail on an initialization
                # error instead of allowing a later echo to hide its status.
                result = list(argv)
                result[index] = result[index] if 'e' in result[index][1:] else result[index] + 'e'
                result[index + 1] = init
                return result
    return None


def lifecycle_declaration_errors(name, service):
    try:
        entry, cmd = _argv(service.get('entrypoint')), _argv(service.get('command'))
        if entry and cmd and entry == cmd and _idle(entry):
            return [f'service {name}: duplicate keepalive entrypoint and command form an invalid combined process']
        argv = entry + cmd
        init = _bootstrap(argv)
        if init:
            return [f'service {name}: finite initialization exits before docker compose exec; preserve it and add a persistent worker']
    except ValueError as exc:
        return [f'service {name}: invalid startup declaration: {exc}']
    return []


def normalize_exec_worker(service, name='main'):
    """Keep declared finite initialization, then keep the exec container alive.

    Unknown server commands and explicit health checks are preserved. This is
    transport/lifecycle wiring, not permission to replace arbitrary task code.
    """
    if service.get('x-pipeline-exec-worker') == 1:
        return False
    entry, cmd = _argv(service.get('entrypoint')), _argv(service.get('command'))
    argv = entry if entry and entry == cmd and _idle(entry) else entry + cmd
    init = _bootstrap(argv)
    if init is None:
        return False
    marker = '/tmp/pipeline-' + re.sub('[^a-zA-Z0-9_.-]', '-', name) + '-ready'
    script = 'rm -f ' + shlex.quote(marker) + '\n'
    if init:
        script += shlex.join(init) + '\n'
    script += 'touch ' + shlex.quote(marker) + '\nexec sleep infinity'
    service['entrypoint'] = []
    service['command'] = ['sh', '-ec', script]
    if 'healthcheck' not in service:
        service['healthcheck'] = {'test': ['CMD-SHELL', 'test -f ' + shlex.quote(marker)],
                                  'interval': '2s', 'timeout': '2s', 'retries': 900}
    service['x-pipeline-exec-worker'] = 1
    return True


def normalize_runtime_compose(document, solver='main', legacy_solver=None):
    changes = []
    services = document.get('services', {})
    if legacy_solver and legacy_solver != solver and legacy_solver not in services and solver in services:
        changes.extend(rewrite_service_references(document, legacy_solver, solver))
    errors = service_reference_errors(document)
    if errors:
        raise ValueError('; '.join(errors))
    if solver not in services:
        raise ValueError('Missing Solver service: ' + solver)
    if normalize_exec_worker(services[solver], solver):
        changes.append(solver + '.exec_worker')
    return changes


def verifier_compose_document(document=None):
    """Bind framework tests in a separate verifier using a shared base image."""
    result = copy.deepcopy(document) if document is not None else {}
    service = result.setdefault('services', {}).setdefault('main', {})
    service.setdefault('entrypoint', [])
    service.setdefault('command', ['sleep', 'infinity'])
    volumes = service.setdefault('volumes', [])
    for v in volumes:
        target = v.get('target') if isinstance(v, dict) else v.split(':')[1] if isinstance(v, str) and ':' in v else None
        if target == '/tests':
            expected = isinstance(v, dict) and v.get('type') == 'bind' and v.get('source') in ('.', './') and v.get('read_only') is True
            if not expected:
                raise ValueError('Verifier /tests must bind its own task tests directory read-only')
            return result
    volumes.append({'type': 'bind', 'source': './', 'target': '/tests', 'read_only': True})
    return result
