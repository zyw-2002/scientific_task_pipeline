"""Inspect authored interfaces without importing task code or running commands."""
from __future__ import annotations

import ast
import re
from pathlib import Path, PurePosixPath

import yaml

from calibration_guard import script_arguments
from contract import package_layout, package_path


def adapter_errors(path: Path, protocol: dict | None = None) -> list[str]:
    """Require a directly authored synchronous function callable with two arguments.

    A scorer with a private reference resource must derive its metric from that
    resource.  A common construction error is to use a submitted manifest as a
    fallback when the reference lookup finds no file, which turns a missing or
    partial submission into a self-reported success.  The static check catches
    this narrow pattern without importing or executing task code.
    """
    source = path.read_text(encoding='utf-8')
    tree = ast.parse(source, filename=str(path))
    functions = [node for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == 'measure']
    if len(functions) != 1 or not isinstance(functions[0], ast.FunctionDef):
        return ['metric adapter must define one top-level synchronous measure(path, context) function']
    function = functions[0]
    args = function.args
    positional = len(args.posonlyargs) + len(args.args)
    required = positional - len(args.defaults)
    if required > 2 or (positional < 2 and args.vararg is None) or any(
            default is None for default in args.kw_defaults):
        return ['metric adapter measure must accept two positional arguments without other required arguments']
    body = [node for node in function.body if not (
        isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str))]
    if not body or all(isinstance(node, ast.Pass) or (
            isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            and node.value.value is Ellipsis) for node in body):
        return ['metric adapter measure contains only a docstring/pass/ellipsis placeholder']
    if isinstance(protocol, dict):
        resources = protocol.get('resources', {})
        bindings = protocol.get('resource_bindings', {})
        reference_ids = {
            rid for rid, resource in resources.items()
            if isinstance(resource, dict) and resource.get('role') == 'reference'
        }
        bound_references = reference_ids & {
            rid for rid in bindings.values() if isinstance(rid, str)
        }
        if bound_references:
            lowered = source.lower()
            # A manifest may be used for structural validation.  Treat it as
            # a self-reported score only when the adapter actually reads a
            # score/rate/metric/reward field from that manifest.  This avoids
            # rejecting a legitimate adapter merely because it returns a
            # metrics object after checking manifest IDs.
            self_report = False
            metric_label = re.compile(r'(?:score|rate|metric|reward)', re.IGNORECASE)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    receiver = node.func.value
                    if (isinstance(receiver, ast.Name) and receiver.id == 'manifest'
                            and node.func.attr == 'get' and node.args
                            and isinstance(node.args[0], ast.Constant)
                            and isinstance(node.args[0].value, str)
                            and metric_label.search(node.args[0].value)):
                        self_report = True
                        break
                if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == 'manifest':
                    key = node.slice
                    if isinstance(key, ast.Constant) and isinstance(key.value, str) and metric_label.search(key.value):
                        self_report = True
                        break
            if not self_report and re.search(r'\b(?:manifest|reported[_ ]?(?:rate|score|metric)|self[_ -]?reported)\b', lowered):
                # Keep a narrow fallback heuristic for adapters whose
                # manifest value is first assigned to a reported_* variable.
                self_report = bool(re.search(r'\breported[_ ]?(?:rate|score|metric)\b', lowered)
                                   and re.search(r'\bmanifest\s*\.', lowered))
            if self_report:
                return ['metric adapter must compute scores from bound private reference resources; '
                        'self-reported manifest fallback can turn missing references into a pseudo-success']
    return []


DEPENDENCY_MANIFESTS = (
    'requirements*.txt', 'pyproject.toml', 'Pipfile', 'Pipfile.lock',
    'environment.yml', 'environment.yaml', 'conda-lock.yml',
    'package.json', 'Cargo.toml', 'Project.toml', 'Manifest.toml',
    'DESCRIPTION', 'renv.lock', 'go.mod', 'pom.xml', 'build.gradle',
    'build.gradle.kts', 'CMakeLists.txt',
)


def dependency_errors(package: Path) -> list[str]:
    environment = package / 'common_task/environment'
    for pattern in DEPENDENCY_MANIFESTS:
        for path in environment.glob(pattern):
            if path.is_file() and not path.is_symlink() and path.read_bytes().strip():
                return []
    return ['provide a nonempty dependency manifest in common_task/environment '
            '(e.g. requirements.txt, pyproject.toml, environment.yml, package.json or Cargo.toml); '
            'a standard-library-only Python task may state that explicitly in requirements.txt']


def _script_sources(compose_path: Path, service: dict, reference: str) -> set[Path]:
    """Use the most specific mount; a child volume can shadow a parent bind."""
    matches = []
    for mount in service.get('volumes', []):
        if isinstance(mount, str):
            source, target, *_ = mount.split(':')
            kind = 'bind' if source.startswith(('.', '/')) else 'volume'
        elif isinstance(mount, dict):
            source, target, kind = mount.get('source', ''), mount.get('target', ''), mount.get('type')
        else:
            continue
        try:
            relative = PurePosixPath(reference).relative_to(PurePosixPath(target))
        except ValueError:
            continue
        path = (compose_path.parent / source / str(relative)).resolve() if kind == 'bind' else None
        matches.append((len(PurePosixPath(target).parts), path))
    if not matches:
        return set()
    depth = max(item[0] for item in matches)
    return {path for size, path in matches if size == depth and path is not None}


def command_errors(package: Path, spec: dict) -> list[str]:
    """Resolve declared package scripts through the actual service bind mounts."""
    errors = []
    commands = spec.get('verification', {}).get('commands', {})
    try:
        compose_path = package_path(package, package_layout(spec)['compose_file'])
        compose = yaml.safe_load(compose_path.read_text())
        services = compose.get('services', {})
        for name, check in commands.items():
            service, argv = check['service'], check['command']
            if service not in services:
                errors.append(f'{name}: unknown Compose service {service}')
                continue
            entry, arguments = script_arguments(argv)
            if (name != 'scientific_handoff' and entry == argv[0]
                    and Path(entry).suffix.lower() not in {'.py', '.sh', '.r', '.jl', '.js', '.rb', '.pl'}
                    and not _script_sources(compose_path, services[service], entry)):
                # Installed binaries (e.g. /usr/bin/nvidia-smi) need runtime validation.
                entry = None
            if name == 'scientific_handoff' and entry is None:
                errors.append('scientific_handoff must invoke a real package script by its absolute container path')
            references = [entry] if entry else []
            for value in arguments:
                value = value.split('=', 1)[1] if value.startswith('--') and '=' in value else value
                if Path(value).suffix.lower() in {'.py', '.sh', '.r', '.jl', '.js', '.rb', '.pl'}:
                    references.append(value)
            for reference in dict.fromkeys(references):
                if not reference.startswith('/') or '..' in PurePosixPath(reference).parts:
                    errors.append(f'{name}: package script needs an absolute container path: {reference}')
                    continue
                paths = _script_sources(compose_path, services[service], reference)
                # An overlapping parent mount must not hide a missing child mount.
                if len(paths) != 1:
                    errors.append(f'{name}: script must resolve to one package bind mount: {reference}')
                    continue
                path = next(iter(paths))
                if (not path.is_relative_to(package.resolve()) or path.is_symlink()
                        or not path.is_file() or not path.stat().st_size):
                    errors.append(f'{name}: missing or invalid mounted package script: {reference}')
                elif path.suffix == '.py':
                    compile(path.read_text(encoding='utf-8'), str(path), 'exec')
    except (OSError, ValueError, TypeError, KeyError, AttributeError, SyntaxError, yaml.YAMLError) as error:
        errors.append('invalid verification script declaration: ' + str(error))
    return errors
