#!/usr/bin/env python3
"""Diagnostic package checks; only bound container evidence can make run_ready.

A failed construction inspection stays in stage 01 until all rules pass. Host evaluator
smoke tests are explicitly separate from actual container readiness evidence.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from contract import (atomic_write_json, package_fingerprint, package_layout,
                      package_manifest, package_path, sha256_file,
                      execution_fingerprint, validate_execution)
from instruction_checks import (validate_instruction, public_identity_errors,
                                solver_visible_leakage_errors, REQUIRED_INSTRUCTION_HEADINGS)

REPORT_SCHEMA_VERSION = "pipline-preflight-report-v2"
DEFAULT_AUTHORING_PATHS = Path('/task/input/paths.json')
RUNTIME_CHECKS = {"solver_input", "data_identity", "submission_writable", "dependencies", "scoring", "hidden_isolation"}


def _check(check_id: str, errors: list[str], detail: str = "") -> dict:
    return {"id": check_id, "status": "fail" if errors else "pass", "reason": "; ".join(errors) if errors else (detail or "ok")}


def task_resource_errors(spec: dict) -> list[str]:
    """Check the fixed authoring boundaries, independent of launcher budgets."""
    from contract import resource_estimate_errors
    estimate = spec.get('resource_estimate')
    errors = resource_estimate_errors(estimate)
    runtime = spec.get('runtime')
    gpus = runtime.get('gpus') if isinstance(runtime, dict) else None
    if type(gpus) is not int or not 0 <= gpus <= 8:
        errors.append('runtime.gpus must be an integer from 0 to 8')
    if isinstance(estimate, dict) and estimate.get('gpu_model') != 'NVIDIA A100':
        errors.append('resource_estimate.gpu_model must be NVIDIA A100')
    return errors


def request_constraint_errors(spec: dict, request: dict) -> list[str]:
    """Host metadata and requested Solver access policy; preserve scientific and resource choices.

    The name remains for host callers. Authoring input is paths.json and contains
    none of these metadata or resource fields.
    """
    return task_resource_errors(spec) + pipeline_metadata_errors(spec, request)


def pipeline_metadata_errors(spec: dict, metadata: dict) -> list[str]:
    errors = []
    for key in ('task_id', 'canonical_id',
                'assessment_contract_version', 'scope_contract_version', 'difficulty_contract_version', 'paper_caption_review_version', 'paper_comparison_version', 'solver_access',
                'scientific_acceptance_version', 'output_language',
                'evaluation_protocol_version',
                'framework_generation_version'):
        if key in metadata and spec.get(key) != metadata[key]:
            errors.append(f'final task dropped or changed pipeline metadata {key}')
    return errors


def buildtasks_gate(report: dict, construction_errors: list[str], *,
                    review_status: str, require_runtime: bool = False) -> dict:
    """Attach the complete repair feedback without changing static-pass semantics.

    The caller supplies scientific-review and framework-generation validation.
    Harbor's construction loop must use this gate, not static status alone.
    """
    failures = [dict(check) for check in report['checks'] if check['status'] != 'pass']
    failures.extend(_check('CONSTRUCTION-ACCEPTANCE', [error]) for error in construction_errors)
    if review_status != 'complete':
        failures.append(_check('SCIENTIFIC-REVIEW', [f'review status is {review_status}']))
    failures.extend(_check('INSPECTION', [error]) for error in report['errors'])
    if report['smoke']['status'] == 'fail':
        failures.append(_check('EVALUATOR-SMOKE', ['host evaluator smoke failed; inspect smoke.cases']))
    contract_complete = bool(report['static_pass'] and report['status'] == 'pass' and not failures)
    if require_runtime and not report['run_ready']:
        readiness = report.get('runtime_readiness', {})
        failures.append(_check('RUNTIME-READINESS', readiness.get('errors') or
                               ['fresh passing container evidence is required']))
    ready = bool(report['static_pass'] and report['status'] == 'pass' and not failures)
    gate = {'status': 'pass' if ready else 'fail',
            'scope': 'execution' if require_runtime else 'construction',
            'action': 'accept' if ready else 'repair',
            'failures': failures}
    gate['contract_status'] = ('BLOCKED' if report.get('contract_status') == 'BLOCKED' else
                               'PASS' if contract_complete else 'REWORK')
    if gate['contract_status'] == 'BLOCKED':
        gate['action'] = 'supply_resources'
    report['buildtasks'] = gate
    return gate


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _overlap(a: Path, b: Path) -> bool:
    a, b = a.resolve(), b.resolve()
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def _docker_instructions(text: str) -> list[tuple[str, str]]:
    result, pending = [], ""
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        stripped = line.rstrip()
        if not stripped:
            continue
        pending += stripped[:-1] + " " if stripped.endswith("\\") else stripped
        if stripped.endswith("\\"):
            continue
        parts = pending.strip().split(None, 1)
        if parts:
            result.append((parts[0].upper(), parts[1] if len(parts) == 2 else ""))
        pending = ""
    if pending:
        raise ValueError("Dockerfile ends in an unfinished continuation")
    return result


def _copy_sources(value: str) -> tuple[list[str], bool]:
    from_stage = False
    while value.startswith("--"):
        flag_parts = value.split(None, 1)
        flag, value = flag_parts[0], flag_parts[1] if len(flag_parts) > 1 else ""
        if flag.startswith("--from="):
            from_stage = True
        if "=" not in flag and flag in {"--from", "--chown", "--chmod", "--exclude"}:
            flag_parts = value.lstrip().split(None, 1)
            value = flag_parts[1] if len(flag_parts) > 1 else ""
            from_stage |= flag == "--from"
        value = value.lstrip()
    parts = json.loads(value) if value.startswith("[") else shlex.split(value)
    if not isinstance(parts, list) or len(parts) < 2 or not all(isinstance(v, str) for v in parts):
        raise ValueError("invalid COPY/ADD arguments")
    return parts[:-1], from_stage


def _docker_copy_errors(root: Path, context: Path, dockerfile: Path, private: list[Path]) -> list[str]:
    errors = []
    try:
        for instruction, value in _docker_instructions(_read(dockerfile)):
            if instruction not in {"COPY", "ADD"}:
                continue
            sources, from_stage = _copy_sources(value)
            if from_stage:
                # Every stage's local inputs are checked; external stage content
                # still needs the actual image-isolation probe at readiness.
                continue
            for source in sources:
                if "${" in source or "$" in source:
                    errors.append(f"cannot resolve Docker {instruction} source: {source}")
                    continue
                if re.match(r"^[a-z]+://", source):
                    if instruction == "ADD":
                        errors.append(f"remote ADD requires runtime isolation evidence and explicit acquisition: {source}")
                    continue
                clean = source.lstrip("/")
                if ".." in Path(clean).parts:
                    errors.append(f"Docker source traverses build context: {source}")
                    continue
                if any(ch in clean for ch in "*?["):
                    candidates = list(context.glob(clean))
                    if not candidates:
                        errors.append(f"Docker source matches no build files: {source}")
                else:
                    candidates = [context / clean]
                for candidate in candidates:
                    if not candidate.resolve().is_relative_to(root):
                        errors.append(f"Docker source escapes package: {source}")
                    if any(_overlap(candidate, protected) for protected in private):
                        errors.append(f"shared image COPY/ADD includes protected material: {source}")
                    if candidate.is_file() and candidate.suffix.lower() in {".tar", ".tgz", ".gz", ".zip", ".xz", ".bz2"}:
                        # Archive contents cannot be inferred from their basename.
                        # Runtime inspection remains mandatory; do not certify them here.
                        pass
    except (ValueError, OSError, TypeError) as error:
        errors.append(f"cannot inspect Dockerfile {dockerfile}: {error}")
    return errors


def _resolve_bind(source: str, compose_dir: Path) -> Path:
    if "$" in source:
        raise ValueError(f"unresolved interpolation in bind mount: {source}")
    return (compose_dir / source).resolve() if not source.startswith("/") else Path(source).resolve()


def _service_lifecycle_errors(name: str, service: dict) -> list[str]:
    """Check declarations for up/exec workers; runtime still proves liveness."""
    from compose_runtime import lifecycle_declaration_errors
    mechanical_errors = lifecycle_declaration_errors(name, service)
    if mechanical_errors:
        return mechanical_errors

    def argv(value, field):
        if value is None:
            return []
        if isinstance(value, str):
            return shlex.split(value)
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return value
        raise ValueError(f'{field} must be a string or an argv list')

    try:
        process = argv(service.get('entrypoint'), 'entrypoint') + argv(service.get('command'), 'command')
        if not process or not process[0].strip():
            return [f'service {name}: declare an explicit non-interactive, long-running command or entrypoint; '
                    'the probe needs a running container for docker compose exec. For an exec worker use '
                    'entrypoint: [] and command: ["sleep", "infinity"]; inherited image CMD is not verified statically']
        for _ in range(4):
            if process and process[0] == 'exec':
                process = process[1:]
            if not process:
                return [f'service {name}: empty startup command']
            program = Path(process[0]).name
            if program in {'true', 'false', 'exit', 'echo', 'printf'}:
                return [f'service {name}: {program} is a one-shot command, not a persistent exec worker']
            if program in {'sh', 'bash', 'dash', 'zsh', 'ksh'}:
                option = next((i for i, value in enumerate(process[1:], 1)
                               if value.startswith('-') and not value.startswith('--') and 'c' in value[1:]), None)
                if option is not None and option + 1 < len(process):
                    process = shlex.split(process[option + 1])
                    if not process:
                        return [f'service {name}: empty shell command exits before exec checks']
                    continue
                if not any(value and not value.startswith('-') for value in process[1:]):
                    return [f'service {name}: bare {program} relies on interactive input and can exit 0 in detached mode; '
                            'declare a persistent process instead']
            break
    except ValueError as error:
        return [f'service {name}: invalid startup declaration: {error}']
    return []


def _compose_checks(root: Path, spec: dict, layout: dict) -> list[dict]:
    errors, image_errors, isolation_errors, lifecycle_errors = [], [], [], []
    compose = package_path(root, layout["compose_file"])
    if not compose.is_file():
        return [_check("DOCKER-COMPOSE", [f"missing {layout['compose_file']}"])]
    try:
        import yaml
    except ImportError:
        return [_check("DOCKER-COMPOSE", ["PyYAML is required for structural Compose checks; install it in the pipeline environment"])]
    try:
        document = yaml.safe_load(compose.read_text(encoding="utf-8"))
        services = document.get("services") if isinstance(document, dict) else None
        if not isinstance(services, dict) or not services:
            raise ValueError("Compose must define services")
        if document.get("include"):
            errors.append("Compose includes must be resolved into the reviewed package configuration")
        from compose_runtime import service_reference_errors
        errors.extend(service_reference_errors(document))
        solver_names = [name for name in layout['solver_services'] if name in services]
        if spec.get('solver_access') == 'online_search' and len(solver_names) == 1:
            from copy import deepcopy
            from solver_access import configure_compose
            isolated = deepcopy(document)
            configure_compose(isolated, solver_names[0], 'enabled', solver_only=True)
            if isolated != document:
                isolation_errors.append('regenerate Compose with an external Solver bridge and only internal task-service networks')
        private = [package_path(root, path) for path in layout["private_paths"]]
        private.append(package_path(root, layout["method_path"]))
        for source in spec.get("data", {}).get("sources", []):
            if source.get("visibility") in {"private", "build_only", "guided"} and source.get("package_path"):
                private.append(package_path(root, source["package_path"]))
        image_identities = set()
        inspected = set()
        solver_seen = False
        for name, service in services.items():
            if not isinstance(service, dict):
                errors.append(f"service {name} must be an object")
                continue
            if service.get("extends"):
                errors.append(f"service {name}: extends must be resolved before inspection")
            if name in {*layout['solver_services'], *layout['evaluator_services']}:
                lifecycle_errors.extend(_service_lifecycle_errors(name, service))
            # Read-only inputs must exist before Docker starts; short bind syntax
            # otherwise silently creates an empty host directory for a typo.
            for mount in service.get('volumes', []):
                if isinstance(mount, str):
                    parts = mount.split(':')
                    if len(parts) < 2:
                        continue
                    source = parts[0]
                    is_bind = source.startswith(('.', '/', '~', '$'))
                    read_only = len(parts) > 2 and 'ro' in parts[2].split(',')
                elif isinstance(mount, dict):
                    source = mount.get('source', '')
                    is_bind = mount.get('type', 'volume') == 'bind'
                    read_only = mount.get('read_only', False) is True
                else:
                    continue
                if is_bind and read_only and not _resolve_bind(source, compose.parent).exists():
                    errors.append(f'service {name}: read-only bind source does not exist: {source}; '
                                  'resolve paths relative to docker-compose.yaml')
            is_solver = name not in layout["evaluator_services"]
            if name in layout["solver_services"]:
                is_solver = True
            if name in layout['solver_services'] and spec.get('framework_generation_version') == 1:
                expected_env = {'DATA_DIR': spec['runtime']['data_dir'],
                                'OUTPUT_DIR': str(PurePosixPath(spec['submission']['path']).parent)}
                env = service.get('environment', {})
                if isinstance(env, list):
                    env = dict(entry.split('=', 1) for entry in env if isinstance(entry, str) and '=' in entry)
                if not isinstance(env, dict) or any(env.get(k) != v for k, v in expected_env.items()):
                    errors.append(f'{name}: DATA_DIR or OUTPUT_DIR differs from the solver contract')
                if service.get('working_dir') != '/workspace':
                    errors.append(f'{name}: working_dir differs from the solver contract')
            solver_seen |= is_solver
            image = service.get("image")
            if spec['runtime'].get('image_policy') == 'deferred':
                if 'build' in service or image:
                    image_errors.append(f'service {name}: deferred image must omit image and build until binding')
            if spec['runtime'].get('image_policy') == 'prebuilt':
                if 'build' in service:
                    image_errors.append(f'service {name}: prebuilt policy forbids build')
                if image != spec['runtime'].get('base_image'):
                    image_errors.append(f'service {name}: must use the supplied prebuilt base_image directly')
            build = service.get("build")
            build_identity = None
            if build:
                config = {"context": build} if isinstance(build, str) else build
                if not isinstance(config, dict) or not isinstance(config.get("context", "."), str):
                    raise ValueError(f"service {name}: invalid build context")
                raw_context = config.get("context", ".")
                if "$" in raw_context or re.match(r"^[a-z]+://", raw_context):
                    raise ValueError(f"service {name}: build context must be resolved and local")
                context = (compose.parent / raw_context).resolve()
                if not context.is_relative_to(root):
                    raise ValueError(f"service {name}: build context escapes reviewed package")
                dockerfile = (context / config.get("dockerfile", "Dockerfile")).resolve()
                if not dockerfile.is_relative_to(root) or not dockerfile.is_file():
                    raise ValueError(f"service {name}: Dockerfile missing or outside package")
                build_identity = json.dumps({"context": str(context), "dockerfile": str(dockerfile), "target": config.get("target"), "args": config.get("args")}, sort_keys=True)
                if build_identity not in inspected:
                    inspected.add(build_identity)
                    isolation_errors.extend(_docker_copy_errors(root, context, dockerfile, private))
                if config.get("additional_contexts"):
                    isolation_errors.append(f"service {name}: additional build contexts need explicit package isolation review")
            if image:
                if not isinstance(image, str) or "$" in image:
                    errors.append(f"service {name}: image must be resolved")
                image_identities.add((str(image), build_identity))
            elif build_identity:
                image_identities.add((None, build_identity))
            elif spec["runtime"].get("image_policy") != "deferred":
                errors.append(f"service {name}: missing shared image/build")
            if not is_solver:
                continue
            if service.get("privileged") or service.get("pid") == "host" or service.get("volumes_from"):
                isolation_errors.append(f"solver service {name} has privileged or inherited filesystem access")
            for entry in service.get("volumes", []):
                if isinstance(entry, str):
                    parts = entry.split(":")
                    if len(parts) < 2:
                        continue
                    source = parts[0]
                    target = parts[1]
                    kind = "bind" if source.startswith((".", "/", "~", "$")) else "volume"
                elif isinstance(entry, dict):
                    source, kind = entry.get("source", ""), entry.get("type", "volume")
                    target = entry.get("target", "")
                else:
                    raise ValueError(f"service {name}: malformed volume")
                if kind == "bind":
                    mounted = _resolve_bind(source, compose.parent)
                    if str(mounted) in {"/", "/var/run/docker.sock", "/run/docker.sock"} or any(_overlap(mounted, protected) for protected in private):
                        isolation_errors.append(f"solver service {name} mounts protected material: {source}")
                    if not mounted.is_relative_to(root):
                        isolation_errors.append(f"solver service {name} bind source is outside reviewed package: {source}")
                else:
                    # A named volume shared with a scorer can expose references.
                    for other_name, other in services.items():
                        if other_name not in layout["evaluator_services"] or not isinstance(other, dict):
                            continue
                        for other_entry in other.get("volumes", []):
                            other_source = other_entry.split(":")[0] if isinstance(other_entry, str) else other_entry.get("source") if isinstance(other_entry, dict) else None
                            if source and source == other_source and not (target not in {"", "/", "/task"} and target.startswith("/") and Path(spec["submission"]["path"]).is_relative_to(Path(target))):
                                isolation_errors.append(f"solver and evaluator share named volume {source}; use explicit isolated mounts")
            for kind in ("secrets", "configs"):
                for entry in service.get(kind, []):
                    key = entry if isinstance(entry, str) else entry.get("source")
                    definition = document.get(kind, {}).get(key, {})
                    if isinstance(definition, dict) and definition.get("file"):
                        source = _resolve_bind(definition["file"], compose.parent)
                        if any(_overlap(source, protected) for protected in private):
                            isolation_errors.append(f"solver {kind} exposes protected material: {key}")
                    else:
                        isolation_errors.append(f"solver external {kind} {key} cannot be inspected statically")
        if not solver_seen:
            errors.append("Compose has no Solver service")
        # Different explicit build definitions are different environments even if
        # they reuse a tag; image-only references to the same shared tag are okay.
        names = {identity[0] for identity in image_identities if identity[0] is not None}
        builds = {identity[1] for identity in image_identities if identity[1] is not None}
        if len(names) > 1 or len(builds) > 1 or (None in {item[0] for item in image_identities} and len(image_identities) > 1):
            image_errors.append("Solver and evaluator must use one shared image and build definition")
    except (ValueError, OSError, TypeError, AttributeError, yaml.YAMLError) as error:
        errors.append(str(error))
    return [_check("DOCKER-COMPOSE", errors),
            _check("SERVICE-LIFECYCLE", lifecycle_errors, "Explicit startup declarations checked; actual service liveness still requires container evidence"),
            _check("SHARED-IMAGE", image_errors), _check("HIDDEN-ISOLATION-STATIC", isolation_errors, "Static paths checked; actual image/mount isolation still requires container evidence")]


def check_structure(root: Path) -> list[dict]:
    """First review: deterministic filesystem and heading checks, no model call."""
    checks = []
    checks.append(_check('NO-LEGACY-TASK-SPEC',
                        ['obsolete task_spec.json: author instruction.md directly and supply execution.json']
                        if (root / 'task_spec.json').exists() else []))
    common = root / 'common_task'
    for name in ('environment', 'tests', 'evaluation'):
        checks.append(_check('DIRECTORY-' + name.upper(),
            [] if (common / name).is_dir() else [f'missing directory: common_task/{name}']))
    for name in ('instruction.md', 'task.toml', 'method.md'):
        path = common / name
        checks.append(_check('FILE-' + name.upper(),
            [] if path.is_file() and path.stat().st_size else [f'missing or empty: common_task/{name}']))
    for name in ('guidance', 'environment/sidecar', 'valuation'):
        checks.append(_check('NO-' + name.upper().replace('/', '-'),
            [] if not (common / name).exists() else [f'obsolete directory: common_task/{name}']))
    # Ignore fenced examples; accept Markdown headings at any level.
    headings = []
    fence = None
    for line in _read(common / 'instruction.md').splitlines():
        token = re.match(r'^\s*(`{3,}|~{3,})', line)
        if token:
            if fence is None:
                fence = token.group(1)
            elif token.group(1)[0] == fence[0] and len(token.group(1)) >= len(fence):
                fence = None
            continue
        match = re.match(r'^#{1,6}\s+(.+?)\s*#*\s*$', line) if fence is None else None
        if match:
            headings.append(match.group(1))
    for title in REQUIRED_INSTRUCTION_HEADINGS:
        checks.append(_check('HEADING-' + title.upper().replace(' ', '-'),
            [] if title in headings else [f'missing instruction heading: {title}']))
    return checks


def protocol_config(root: Path, config: dict) -> dict | None:
    relative = config.get('verification', {}).get('score_evidence', {}).get('evaluation_protocol', {}).get(
        'config_path', 'common_task/evaluation/protocol.json')
    try:
        value = json.loads(package_path(root, relative).read_text())
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def check_static(root: Path, spec: dict[str, Any], *, deferred_resources=None, construction_only=False) -> list[dict]:
    layout = package_layout(spec)
    checks = []
    # A generated machine-readable contract is the shared source of truth for
    # solver paths, artifacts and evaluation.  Validate it before allowing a
    # package to proceed to scientific review or Stage3.
    contract_path = package_path(root, layout['public_dir']) / 'task_contract.json'
    if spec.get('framework_generation_version') == 1:
        from task_contract import submission_location_errors
        checks.append(_check('SUBMISSION-LOCATION', submission_location_errors(root, spec)))
        try:
            from task_contract import binding_errors
            contract = json.loads(contract_path.read_text(encoding='utf-8'))
            protocol = protocol_config(root, spec) or {}
            contract_failures = binding_errors(root, spec, contract, protocol)
            checks.append(_check('SOLVER-TASK-CONTRACT', contract_failures,
                                 'Generated task_contract.json binds the solver-facing paths and evaluation entrypoint'))
        except (OSError, ValueError, TypeError, KeyError) as error:
            checks.append(_check('SOLVER-TASK-CONTRACT', [str(error)]))
    required = ("instruction_path", "task_config_path", "readme_path", "data_description_path", "dockerfile", "compose_file", "method_path", "score_path")
    if spec['runtime'].get('image_policy') in {'prebuilt', 'deferred'}:
        required = tuple(key for key in required if key != 'dockerfile')
    errors = [f"missing or empty: {layout[key]}" for key in required if not package_path(root, layout[key]).is_file() or not package_path(root, layout[key]).stat().st_size]
    checks.append(_check("PACKAGE-FILES", errors))
    instruction = _read(package_path(root, layout["instruction_path"]))
    checks.append(_check("PUBLIC-INSTRUCTION", validate_instruction(instruction, spec, protocol_config(root, spec))))
    if spec.get('framework_generation_version') == 1:
        try:
            from instruction_checks import solver_contract_errors
            from task_contract import contract_errors
            contract = json.loads(contract_path.read_text(encoding='utf-8'))
            checks.append(_check('SOLVER-CONTRACT-INSTRUCTION',
                                 contract_errors(contract) + solver_contract_errors(instruction, contract),
                                 'Every solver-facing path and evaluator binding is named in instruction.md'))
        except (OSError, ValueError, TypeError, KeyError) as error:
            checks.append(_check('SOLVER-CONTRACT-INSTRUCTION', [str(error)]))
    readme = _read(package_path(root, layout["readme_path"]))
    readme_errors = [] if spec["submission"]["path"] in readme else ["public README does not state submission.path"]
    authored_readme = re.sub(r'<!-- submission path -->.*?<!-- end submission path -->', '', readme, flags=re.S)
    authored_readme = re.sub(r'<!--.*?-->|^\s*#{1,6}[^\n]*', '', authored_readme, flags=re.S | re.M)
    if '{{MATERIALS_AND_USAGE}}' in readme or ('<!-- submission path -->' in readme and not authored_readme.strip()):
        readme_errors.append('public README needs task-specific materials and usage')
    checks.append(_check("SUBMISSION-CONTRACT", readme_errors))
    errors = []
    try:
        import tomllib
        configuration = tomllib.loads(_read(package_path(root, layout["task_config_path"])))
        from framework_generator import valid_harbor_name
        if configuration.get('task') is not None and not valid_harbor_name(configuration['task'].get('name')):
            errors.append('task.toml task.name must be a Harbor org/name identifier, not a paper title')
        timeout = configuration.get("agent", {}).get("timeout_sec")
        if isinstance(timeout, bool) or timeout != spec["runtime"]["time_limit_sec"]:
            errors.append("task.toml agent.timeout_sec must equal runtime.time_limit_sec")
        environment = configuration.get("environment", {})
        if spec["runtime"].get("image_policy") == "deferred" and environment.get("docker_image"):
            errors.append("deferred task image must not be declared as bound in task.toml")
        if spec['runtime'].get('image_policy') == 'prebuilt' and environment.get('docker_image') != spec['runtime'].get('base_image'):
            errors.append('task.toml environment.docker_image must equal the supplied prebuilt base_image')
        if 'build_timeout_sec' in spec['runtime']:
            build_timeout = environment.get('build_timeout_sec')
            if isinstance(build_timeout, bool) or build_timeout != spec['runtime']['build_timeout_sec']:
                errors.append('task.toml environment.build_timeout_sec must equal runtime.build_timeout_sec')
        for key, runtime_key in (("cpus", "cpu"), ("memory_mb", "memory_mb"), ("gpus", "gpus")):
            if key in environment and environment[key] != spec["runtime"][runtime_key]:
                errors.append(f"task.toml environment.{key} differs from runtime.{runtime_key}")
        mode = environment.get("network_mode")
        allowed_modes = {"disabled": {"none", "no-network", "disabled"}, "gateway_search_only": {"none", "no-network", "disabled"}, "allowlisted": {"allowlist", "allowlisted"}, "enabled": {"public", "bridge", "enabled", "internet"}}
        if mode is not None and mode not in allowed_modes[spec["runtime"]["network_policy"]]:
            errors.append("task.toml network_mode contradicts runtime.network_policy")
    except (ImportError, ValueError, OSError, TypeError) as error:
        errors.append(f"cannot parse task.toml: {error}")
    checks.append(_check("RUNTIME-CONTRACT", errors, "Task configuration checked; actual cgroups and network require container verification"))
    private = [package_path(root, p) for p in layout["private_paths"]] + [package_path(root, layout["method_path"])]
    public = package_path(root, layout["public_dir"])
    errors = [f"protected path is inside public task tree: {path.relative_to(root)}" for path in private if path.is_relative_to(public)]
    for item in public.rglob("*") if public.is_dir() else []:
        if item.name in {"build_report.json", "review_report.json", "review_report.md", "readiness.json", "audit_context.json"}:
            errors.append(f"internal construction evidence is solver-visible: {item.relative_to(root)}")
    # Scan public documentation, manifests and scripts, not potentially huge raw datasets.
    formal_paths = {package_path(root, s['package_path']) for s in spec.get('data', {}).get('sources', [])
                    if construction_only and spec.get('scope_contract_version') == 3
                    and s.get('role') in {'input', 'reference'} and s.get('package_path')}
    # instruction.md is mounted/read by the Solver even though it lives beside
    # the public input directory; include it in the same leakage boundary.
    identity_paths = [package_path(root, layout["method_path"]),
                      package_path(root, layout["instruction_path"])]
    identity_paths.extend(item for item in public.rglob("*") if item.is_file() and
                          item.suffix.lower() in {".md", ".json", ".yaml", ".yml", ".toml", ".py", ".sh"}
                          and not (item.is_relative_to(package_path(root, layout['data_path']))
                                   and any(item.is_relative_to(path) for path in formal_paths)))
    for item in identity_paths:
        for error in public_identity_errors(_read(item), spec):
            errors.append(f"{item.relative_to(root)}: {error}")
    # Public documentation and manifests are Solver inputs.  Keep source
    # locators/result claims in private authoring evidence only; otherwise the
    # task can be solved by searching the cited paper or repository.
    public_paths = [item for item in identity_paths if item.is_relative_to(public)]
    solver_visible_paths = public_paths + [package_path(root, layout["instruction_path"])]
    for item in solver_visible_paths:
        relative = item.relative_to(root).as_posix()
        for error in solver_visible_leakage_errors(_read(item), relative_path=relative):
            errors.append(error)
    checks.append(_check("PUBLIC-PRIVATE-BOUNDARY", errors))
    if spec.get('framework_generation_version') == 1:
        from evaluation_protocol import declaration_errors
        from construction_checks import dependency_errors, command_errors
        from scientific_acceptance import calibration_binding_errors
        checks.append(_check('REQUIRED-ARTIFACT-DECLARATIONS',
                             declaration_errors(spec.get('submission', {}).get('required_artifacts'))))
        requirements = root / 'common_task/environment/environment_requirements.md'
        errors = [] if requirements.is_file() and requirements.stat().st_size else [
            'missing environment requirements: common_task/environment/environment_requirements.md']
        checks.append(_check('ENVIRONMENT-REQUIREMENTS', errors))
        checks.append(_check('DEPENDENCY-MANIFEST', dependency_errors(root)))
        checks.append(_check('VERIFICATION-SCRIPTS', command_errors(root, spec)))
        checks.append(_check('SCIENTIFIC-CALIBRATION-CONFIG', calibration_binding_errors(root, spec)))
        from scoring_smoke import binding_errors
        smoke_errors = binding_errors(root, spec)
        if spec.get('verification', {}).get('scoring_smoke') is not None or smoke_errors:
            checks.append(_check('SCORING-SMOKE-CONFIG', smoke_errors,
                                 'Fixture and actual protocol bindings only; controls must execute in an isolated container'))
    from execution_contract import execution_errors
    from artifact_language import inspect_language
    checks.append(_check('SCORE-EXECUTION-CONTRACT', execution_errors(root, spec)))
    if spec.get('output_language') == 'en':
        checks.append(_check('ARTIFACT-LANGUAGE', inspect_language(root, spec)))
    checks.extend(_compose_checks(root, spec, layout))
    data, runtime = spec["data"], spec["runtime"]
    errors = []
    if deferred_resources is None and spec.get('assessment_contract_version') == 1:
        from task_contract import resource_delivery
        _, deferred_resources = resource_delivery(root, spec)
    deferred_ids = {entry['id'] for entry in deferred_resources or []}
    if data["mode"] in {"bundled", "mixed"}:
        declared = [source for source in data['sources'] if source.get('availability') in {'bundled', 'preprovided'}
                    and source.get('required', True) and source.get('id') not in deferred_ids]
        if declared:
            for source in declared:
                try:
                    relative = source.get('package_path') or source.get('path')
                    if source.get('package_path') and source.get('path') and source['package_path'] != source['path']:
                        raise ValueError('path and package_path disagree')
                    path = package_path(root, relative)
                    directory_resource = spec.get('assessment_contract_version') == 1 and source.get('content', {}).get('format') == 'directory'
                    if not ((path.is_dir() and any(path.iterdir())) if directory_resource else (path.is_file() and path.stat().st_size)):
                        raise ValueError('declared bundled file is missing or empty')
                    from resource_contract import digest
                    formal_at_runtime = (construction_only and spec.get('scope_contract_version') == 3
                                         and source.get('role') in {'input', 'reference'})
                    if source.get('sha256') and not formal_at_runtime and (digest(path) if directory_resource else sha256_file(path)) != source['sha256']:
                        raise ValueError('declared bundled data hash differs from actual bytes')
                except (OSError, ValueError, TypeError) as error:
                    errors.append(source.get('name', 'data') + ': ' + str(error))
        elif not deferred_ids:
            data_path = package_path(root, layout["data_path"])
            if not data_path.is_dir() or not any(p.is_file() for p in data_path.rglob("*")):
                errors.append("bundled data directory contains no files")
    if data["mode"] in {"referenced", "mixed"}:
        path = package_path(root, layout["data_sources_path"])
        try:
            sources = json.loads(_read(path))
            if not isinstance(sources, (list, dict)) or not sources:
                raise ValueError("manifest must be a nonempty object or array")
            serialized = json.dumps(sources, ensure_ascii=False)
            for source in data["sources"]:
                if source.get("visibility") in {"private", "build_only"}:
                    for key in ("url", "package_path"):
                        if source.get(key) and source[key] in serialized:
                            errors.append(f"public acquisition manifest exposes protected source {source['name']}")
            if runtime["network_policy"] in {"disabled", "gateway_search_only"} and any(s.get("required", True) and s.get("availability", "runtime_download") == "runtime_download" and s.get("visibility", "public") == "public" and not (spec.get('assessment_contract_version') == 1 and s.get('prepare')) for s in data["sources"]):
                errors.append("required runtime downloads contradict Solver direct network policy; preprovide the resource")
        except (ValueError, OSError) as error:
            errors.append(f"data_sources.json is missing/invalid: {error}")
    checks.append(_check("DATA-ACQUISITION", errors, "File/source declarations inspected; actual identity and availability require container checks"))
    if spec.get('assessment_contract_version') == 1:
        from assessment_contract import inspect as inspect_assessment
        try:
            protocol = json.loads((root / 'common_task/evaluation/protocol.json').read_text())
            for check_id, failures in inspect_assessment(root, spec, protocol, construction_only=construction_only).items():
                checks.append(_check(check_id, failures, 'Declared contract consistent; not scientific or runtime certification'))
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            checks.append(_check('ASSESSMENT-DEFINITION', [str(exc)]))
    errors = []
    for key in ("score_path", "validator_path"):
        path = package_path(root, layout[key])
        if key == "validator_path" and not path.exists() and not spec["submission"].get("validator_path"):
            continue
        if not path.is_file():
            errors.append(f"missing configured program: {layout[key]}")
        elif path.suffix == ".py":
            try:
                compile(path.read_text(encoding="utf-8"), str(path), "exec")
            except (SyntaxError, ValueError) as error:
                errors.append(f"{layout[key]}: {error}")
    # Parse the task's actual adapter too; compiling only the generated wrapper
    # would miss a syntax error in the scientific implementation. No imports run.
    binding = spec.get('verification', {}).get('score_evidence', {}).get('evaluation_protocol', {})
    if binding.get('config_path'):
        try:
            config_path = package_path(root, binding['config_path'])
            config = json.loads(config_path.read_text())
            path_errors = []
            for rid, resource in config.get('resources', {}).items():
                if (resource.get('visibility') == 'public' and resource.get('availability') != 'bundled'
                        and resource.get('content', {}).get('format') != 'directory'
                        and (root / resource['package_path']).is_dir()):
                    path_errors.append(rid + ': declared file format conflicts with a package directory')
            checks.append(_check('RESOURCE-PATH-KINDS', path_errors,
                                 'Checks file/directory declarations only; no full data download required'))
            if spec.get('framework_generation_version') == 1:
                from scoring_smoke import authoring_metric_adapter
                adapter = authoring_metric_adapter(config_path, config)
            else:
                adapter = (config_path.parent / config['adapter']).resolve()
                if not adapter.is_relative_to(config_path.parent.resolve()):
                    raise ValueError('metric adapter escapes evaluator directory')
            if adapter.suffix == '.py':
                compile(adapter.read_text(encoding='utf-8'), str(adapter), 'exec')
                if spec.get('framework_generation_version') == 1:
                    from construction_checks import adapter_errors
                    errors.extend(adapter_errors(adapter, config))
        except (OSError, ValueError, TypeError, KeyError, SyntaxError) as error:
            errors.append('metric adapter: ' + str(error))
    checks.append(_check("PROGRAM-SYNTAX", errors))
    from public_validation import binding_errors as validation_binding_errors, required as public_validation_required
    if public_validation_required(spec):
        checks.append(_check('PUBLIC-BASELINE-VALIDATION',
                             validation_binding_errors(root, spec, deferred_resources=deferred_resources or ()),
                             'Public validation files and metric bindings checked. Execution requires container evidence.'))
    return checks


def validate_runtime_evidence(evidence: Any, package_sha256: str | None, execution_sha256: str | None, spec: dict, *, now: datetime | None = None, max_age_sec: int = 86400) -> dict:
    configuration_errors = validate_execution(spec)
    if configuration_errors:
        return {"status": "unverified", "errors": configuration_errors}
    errors = []
    if spec.get("runtime", {}).get("image_policy") == "deferred":
        return {"status": "unverified", "errors": ["final task image is not bound"]}
    if evidence is None:
        return {"status": "unverified", "errors": ["no actual container runtime evidence supplied"]}
    if not isinstance(evidence, dict):
        return {"status": "fail", "errors": ["runtime evidence must be an object"]}
    if evidence.get("status") != "pass":
        errors.append("runtime evidence does not report a complete successful probe")
    if evidence.get("schema_version") != "pipline-runtime-evidence-v1":
        errors.append("runtime evidence schema is unsupported")
    if not package_sha256 or evidence.get("package_sha256") != package_sha256:
        errors.append("runtime evidence does not bind the current package content")
    if not execution_sha256 or evidence.get("execution_sha256") != execution_sha256:
        errors.append("runtime evidence does not bind the current task spec")
    try:
        timestamp = datetime.fromisoformat(str(evidence.get("verified_at", "")).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("verified_at needs a timezone")
        age = ((now or datetime.now(timezone.utc)) - timestamp).total_seconds()
        if age < -300 or age > max_age_sec:
            errors.append("runtime evidence is expired or dated in the future")
    except (ValueError, TypeError):
        errors.append("runtime evidence verified_at must be an ISO timestamp with timezone")
    if not isinstance(evidence.get("image_id"), str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", evidence["image_id"]):
        errors.append("runtime evidence lacks actual Docker image identity")
    required = set(RUNTIME_CHECKS)
    # Custom scientific checks are mandatory too, not just the built-in probe IDs.
    required.update(spec.get("verification", {}).get("commands", {}))
    if spec.get("runtime", {}).get("gpus", 0):
        required.add("gpu")
    if spec.get("runtime", {}).get("multimodal", {}).get("enabled"):
        required.add("multimodal")
    if spec.get('runtime', {}).get('network_policy') == 'enabled':
        if not re.fullmatch(r'[0-9a-f]{64}', str(evidence.get('network_env_sha256', ''))):
            errors.append('runtime evidence lacks resolved download network fingerprint')
        network = spec.get('runtime', {}).get('network', {})
        if (network.get('proxy_url') or network.get('proxy_env_file') or network.get('probe_urls')
                or evidence.get('network', {}).get('mode') == 'proxy'
                or any(s.get('availability', 'runtime_download' if spec.get('data', {}).get('mode') in {'referenced', 'mixed'} else 'bundled') == 'runtime_download'
                       and s.get('visibility', 'public') == 'public' and s.get('required', True) and s.get('url')
                       for s in spec.get('data', {}).get('sources', []))):
            required.add('network')
    checks = evidence.get("checks")
    if not isinstance(checks, list):
        checks = []
        errors.append("runtime evidence checks must be an array")
    seen = set()
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get("id"), str):
            errors.append("invalid runtime check record")
            continue
        cid = check["id"]
        if cid in seen:
            errors.append(f"duplicate runtime check {cid}")
        seen.add(cid)
        if check.get("status") != "pass" or (cid in required and check.get("scope") != "container") or check.get("exit_code") != 0:
            errors.append(f"runtime check {cid} did not pass in a container")
        command = check.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(v, str) and v for v in command):
            errors.append(f"runtime check {cid} lacks executed argv")
        try:
            log = Path(check.get("log_path", ""))
            if not log.is_file() or log.is_symlink() or not check.get("log_sha256") or sha256_file(log) != check["log_sha256"]:
                errors.append(f"runtime check {cid} log is missing or changed")
        except (OSError, ValueError, TypeError):
            errors.append(f"runtime check {cid} has invalid log evidence")
    if required - seen:
        errors.append("missing runtime checks: " + ", ".join(sorted(required - seen)))
    from public_validation import evidence_errors as public_evidence_errors, required as public_validation_required
    if public_validation_required(spec):
        errors.extend(public_evidence_errors(spec, evidence))
    return {"status": "pass" if not errors else "fail", "errors": errors, "required_checks": sorted(required)}


def _expand(argv: list[str], mapping: dict[str, str]) -> list[str]:
    result = []
    for arg in argv:
        for key, value in mapping.items():
            arg = arg.replace("{" + key + "}", value)
        result.append(arg)
    return result


def run_smoke(package: Path, spec: dict, *, timeout_sec: int = 300) -> dict:
    cases = spec.get("preflight", {}).get("cases", [])
    if not cases:
        return {"status": "unverified", "scope": "host", "cases": [], "reason": "no task-specific known-outcome smoke cases configured"}
    results = []
    before = package_fingerprint(package)
    with tempfile.TemporaryDirectory(prefix="pipline-host-smoke-") as temporary:
        scratch = Path(temporary)
        for case in cases:
            output = scratch / case["id"]
            output.mkdir()
            mapping = {"python": sys.executable, "package": str(package), "output_dir": str(output)}
            if case.get("fixture"):
                fixture = package_path(package, case["fixture"])
                if not fixture.is_file():
                    results.append({"id": case["id"], "status": "fail", "reason": "fixture missing"})
                    continue
                submission = output / fixture.name
                submission.write_bytes(fixture.read_bytes())
                mapping["submission"] = str(submission)
            argv = _expand(case["command"], mapping)
            record = {"id": case["id"], "status": "fail", "command": argv}
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            try:
                process = subprocess.Popen(argv, cwd=package, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
                try:
                    stdout, stderr = process.communicate(timeout=timeout_sec)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    stdout, stderr = process.communicate()
                    raise ValueError("host smoke command timed out")
                record.update(exit_code=process.returncode, stdout_tail=stdout[-2000:], stderr_tail=stderr[-2000:])
                result_path = output / case.get("result_file", "result.json")
                if not result_path.resolve().is_relative_to(output):
                    raise ValueError("smoke result_file escapes fresh output directory")
                result = json.loads(result_path.read_text()) if result_path.is_file() else None
                expected = case["expected_status"]
                expected_exit = case.get("expected_exit_code", 0 if expected in {"valid", "success"} else None)
                if expected_exit is not None and process.returncode != expected_exit:
                    raise ValueError(f"exit {process.returncode}, expected {expected_exit}")
                if expected != "success":
                    if not isinstance(result, dict) or result.get("status") != expected:
                        raise ValueError(f"fresh result must explicitly report status={expected}; a numeric zero is not an invalidity status")
                if "expected_reward" in case:
                    reward = result.get("reward") if isinstance(result, dict) else None
                    if isinstance(reward, bool) or not isinstance(reward, (int, float)) or not math.isfinite(reward):
                        raise ValueError("fresh result lacks a finite reward")
                    if not math.isclose(reward, case["expected_reward"], rel_tol=0, abs_tol=case.get("reward_tolerance", 1e-9)):
                        raise ValueError(f"reward {reward} differs from task-specific expected {case['expected_reward']}")
                record.update(status="pass", reason="task-specific known outcome matched")
            except (OSError, ValueError, TypeError) as error:
                record["reason"] = str(error)
            results.append(record)
    if before != package_fingerprint(package):
        results.append({"id": "package_unchanged", "status": "fail", "reason": "host smoke modified package contents"})
    return {"status": "pass" if all(v["status"] == "pass" for v in results) else "fail", "scope": "host", "cases": results, "reason": "Host smoke never proves container readiness"}


def inspect_package(package: Path, spec: dict[str, Any], *, run_evaluator: bool = False, timeout_sec: int = 300, runtime_evidence: dict | Path | None = None, construction_only=False) -> dict[str, Any]:
    package = Path(package).resolve()
    spec = spec if isinstance(spec, dict) else {}
    report: dict[str, Any] = {"schema_version": REPORT_SCHEMA_VERSION, "package": str(package), "task_id": spec.get("task_id") if isinstance(spec, dict) else None, "status": "fail", "static_pass": False, "run_ready": False, "package_sha256": None, "execution_sha256": None, "checks": check_structure(package) if spec.get("framework_generation_version") == 1 else [], "errors": [], "warnings": [], "smoke": {"status": "skipped", "scope": "host", "cases": []}}
    try:
        report["package_sha256"] = package_fingerprint(package)
        report["execution_sha256"] = execution_fingerprint(spec)
        errors = validate_execution(spec)
        report["checks"].append(_check("EXECUTION-CONFIG", errors))
        if spec.get('framework_generation_version') == 1:
            report['checks'].append(_check('RESOURCE-LIMITS', task_resource_errors(spec)))
        if spec.get('assessment_contract_version') == 1 and isinstance(spec.get('data', {}).get('sources'), list):
            from task_contract import resource_delivery
            missing, deferred = resource_delivery(package, spec)
            report['missing_resources'] = missing
            report['deferred_resources'] = deferred
            report['checks'].append(_check('REQUIRED-RESOURCE-BYTES', missing,
                'Required resources are present or have a declared runtime preparation plan; see deferred_resources.'))
        if not errors:
            report["checks"].extend(check_static(package, spec, deferred_resources=report.get('deferred_resources'),
                                                 construction_only=construction_only))
            if run_evaluator:
                report["smoke"] = run_smoke(package, spec, timeout_sec=timeout_sec)
        report["static_pass"] = all(check["status"] == "pass" for check in report["checks"])
        if isinstance(runtime_evidence, (str, Path)):
            runtime_evidence = json.loads(Path(runtime_evidence).read_text(encoding="utf-8"))
        readiness = ({"status": "unverified", "errors": errors} if errors else
                     validate_runtime_evidence(runtime_evidence, report["package_sha256"], report["execution_sha256"], spec))
        if report.get('deferred_resources'):
            pending = ', '.join(r['id'] for r in report['deferred_resources'])
            readiness = {'status': 'unverified', 'errors': [*readiness.get('errors', []),
                         'Prepare and validate required formal resources before execution: ' + pending]}
        report["runtime_readiness"] = readiness
        # Rehash after optional commands; evidence predating their mutation is stale.
        if report["package_sha256"] != package_fingerprint(package):
            report["errors"].append("package changed during inspection")
        report["run_ready"] = report["static_pass"] and readiness["status"] == "pass" and report["smoke"]["status"] != "fail" and not report["errors"]
        report["status"] = "pass" if report["static_pass"] and report["smoke"]["status"] != "fail" and not report["errors"] else "fail"
    except (OSError, ValueError, TypeError, KeyError) as error:
        report["errors"].append(str(error))
        report["runtime_readiness"] = {"status": "unverified", "errors": ["package inspection did not complete"]}
    report['contract_status'] = ('BLOCKED' if report.get('missing_resources') else
                                 'PASS' if report['status'] == 'pass' else 'REWORK')
    report["failure_class"] = "pass" if report["status"] == "pass" else "contract_error"
    return report


def inspect_construction(package: Path, *, expected_request: dict | None = None,
                         generation: dict | None = None, review_report: bool = False,
                         previous_package: Path | None = None, source_bundle: Path | None = None,
                         require_scoring_evidence: bool = False) -> dict:
    """Single rule gate used by the authoring worker, host and initial 02 entry.

    Does not require a review report or final image. Scientific meaning and actual
    execution remain separate checks; neither prose nor a saved pass is trusted.
    """
    package = Path(package)
    try:
        spec = json.loads((package / 'execution.json').read_text(encoding='utf-8'))
        if not isinstance(spec, dict):
            raise ValueError('execution.json must be an object')
        report = inspect_package(package, spec, construction_only=True)
        if source_bundle is not None:
            from paper_comparison import source_claim_errors
            report['checks'].append(_check('PAPER-SOURCE-CLAIMS',
                source_claim_errors(package, spec, source_bundle, review=review_report)))
            fixed = Path(source_bundle) / 'fixed_task_instruction.md'
            from solver_access import access_disclosure_errors
            report['checks'].append(_check('SOLVER-ACCESS-DISCLOSURE', access_disclosure_errors(package, spec, fixed)))
            if review_report and fixed.is_file():
                from fixed_task_alignment import alignment_errors
                report['checks'].append(_check('FIXED-TASK-ALIGNMENT', alignment_errors(package, spec, fixed, construction_only=True)))
        if not any(c['id'] == 'DIRECTORY-ENVIRONMENT' for c in report['checks']):
            report['checks'] = check_structure(package) + report['checks']
        if not any(c['id'] == 'RESOURCE-LIMITS' for c in report['checks']):
            report['checks'].append(_check('RESOURCE-LIMITS', task_resource_errors(spec)))
        if expected_request is not None:
            report['checks'].append(_check('PIPELINE-METADATA', pipeline_metadata_errors(spec, expected_request)))
        if generation is not None:
            errors = generation.get('errors', [])
            if generation.get('status') != 'generated' and not errors:
                errors = ['framework generation did not complete']
            report['checks'].append(_check('FRAMEWORK-GENERATION', errors))
            if errors:
                # Generated bindings do not exist until generation succeeds. Keep
                # the observations for audit, but do not ask agents to invent them.
                dependent = {'SCIENTIFIC-CALIBRATION-CONFIG', 'SCORING-SMOKE-CONFIG',
                             'SCORE-EXECUTION-CONTRACT', 'FORMAL-RESOURCE-BINDINGS',
                             'SOLVER-TASK-CONTRACT', 'SOLVER-CONTRACT-INSTRUCTION',
                             # task.toml and method.md are generated only after
                             # the framework plan validates.  Do not report
                             # their absence as an independent authoring error
                             # when generation itself is already blocked.
                             'FILE-TASK.TOML', 'FILE-METHOD.MD'}
                for check in report['checks']:
                    if check['id'] in dependent and check['status'] != 'pass':
                        check['observed_reason'] = check['reason']
                        check.update(status='blocked', blocked_by='FRAMEWORK-GENERATION',
                            reason='Repair the FRAMEWORK-GENERATION finding in the authoring files and rerun preflight.py. '
                                   'The generator writes task_contract.json, the Solver instruction bindings, '
                                   'the evaluation entrypoint and the test bindings. This check will run again.')
                report['repair_first'] = ['FRAMEWORK-GENERATION']
                report['checks'].sort(key=lambda c: c['id'] != 'FRAMEWORK-GENERATION')
        if review_report:
            from review_report import validate_review
            try:
                review = json.loads((package / 'review_report.json').read_text())
                errors = validate_review(package, spec, review,
                                         required=bool(spec.get('scientific_acceptance_version')))
                if spec.get('assessment_contract_version') == 1:
                    from assessment_contract import history_errors
                    errors.extend(history_errors(previous_package, review))
            except (OSError, ValueError, TypeError, KeyError) as exc:
                errors = ['review_report.json: ' + str(exc)]
            report['checks'].append(_check('REVIEW-REPORT', errors))
        report['static_pass'] = not report['errors'] and all(c['status'] == 'pass' for c in report['checks'])
        report['status'] = 'pass' if report['static_pass'] else 'fail'
        report['run_ready'] = bool(report['run_ready'] and report['static_pass'])
        if require_scoring_evidence and (spec.get('assessment_contract_version') == 1 or
                                         (expected_request or {}).get('assessment_contract_version') == 1):
            from assessment_contract import construction_smoke_check
            proof = construction_smoke_check(package, spec)
            report['checks'].append(proof)
            if proof['status'] != 'pass':
                report.update(status='fail', run_ready=False)
    except (OSError, ValueError, TypeError, KeyError) as error:
        report = {'status': 'fail', 'static_pass': False, 'run_ready': False,
                  'checks': check_structure(package) + [_check('DRAFT-PARSE', [str(error)])],
                  'errors': [str(error)]}
    if generation is not None:
        report['framework_generation'] = generation
    report['contract_status'] = ('BLOCKED' if report.get('missing_resources') else
                                 'PASS' if report['status'] == 'pass' else 'REWORK')
    report['failure_class'] = 'pass' if report['status'] == 'pass' else 'contract_error'
    report.update(review_round=1, review_type='rules',
                  action=('review_content' if report['status'] == 'pass' else
                          'supply_resources' if report['contract_status'] == 'BLOCKED' else 'repair_in_01'))
    return report


def print_worker_summary(package: Path, report_path: Path, report: dict) -> None:
    failed = [check for check in report['checks'] if check['status'] == 'fail']
    print(f"Current draft {package}: {report['contract_status']} ({len(failed)} failed checks).")
    print(f"Full current report: {report_path}")
    if failed:
        print('Failed checks: ' + ', '.join(check['id'] for check in failed))
    for check in failed[:4]:
        reason = ' '.join(check['reason'].splitlines()[0].split()) if check.get('repair_commands') else ' '.join(check['reason'].split())
        if len(reason) > 240:
            reason = reason[:237] + '...'
        print(f"{check['id']} — {reason}")
    for check in failed:
        for command in check.get('repair_commands', ()):
            print(command)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        'With no --package, read /task/input/paths.json (or --paths), generate the framework, '
        'and run construction checks. Review workers also validate review_report.json automatically.'))
    parser.add_argument("--execution", type=Path, help="Defaults to PACKAGE/execution.json")
    parser.add_argument("--construction", action="store_true", help="Check construction rules. 01 worker paths also require saved scoring evidence.")
    parser.add_argument("--paths", type=Path, help="File locations for the editable package and frozen materials")
    parser.add_argument("--generate-framework", action="store_true", help="Generate framework-owned wiring before construction checks")
    parser.add_argument('--review-report', action='store_true', help='Also validate package/review_report.json with the same interface as host acceptance; no task execution')
    parser.add_argument("--package", type=Path, help="full package root; omitted: use authoring paths.output_dir and automatic static checks")
    parser.add_argument("--report", type=Path)
    parser.add_argument('--explain-check', metavar='ID',
                        help='Print the full current finding for one construction check. The full gate still runs.')
    parser.add_argument("--run-evaluator", action="store_true", help="optional host smoke only; never proves run_ready")
    parser.add_argument("--require-evaluator", action="store_true", help="require configured host smoke to pass")
    parser.add_argument("--runtime-evidence", type=Path)
    parser.add_argument("--require-run-ready", action="store_true")
    parser.add_argument("--timeout-sec", type=int, default=300)
    args = parser.parse_args(argv)
    if args.package is None:
        from authoring_package import read_paths
        args.paths = args.paths or DEFAULT_AUTHORING_PATHS
        try:
            defaults = read_paths(args.paths)
        except (OSError, ValueError) as error:
            parser.error(f'cannot read authoring paths {args.paths}: {error}; outside a worker, provide --package')
        args.package = Path(defaults['output_dir'])
        args.construction = True
        args.generate_framework = True
        # These paths are supplied by prepare_verify, not inferred from whether
        # the editable report exists. Deleting the report must not bypass review.
        review_worker = bool(defaults.get('review_report_schema') or defaults.get('construction_instructions'))
        args.review_report = args.review_report or review_worker
        if args.report is None:
            args.report = args.package.parent / ('review_preflight.json' if args.review_report else 'construction_preflight.json')
    if args.explain_check and not args.construction:
        parser.error('--explain-check requires construction checks')
    if args.generate_framework and not args.construction:
        parser.error('--generate-framework requires --construction')
    if args.review_report and not args.construction:
        parser.error('--review-report requires --construction')
    if args.paths and not args.construction:
        parser.error('--paths requires --construction')
    if args.construction and (args.require_run_ready or args.runtime_evidence or args.run_evaluator or args.require_evaluator):
        parser.error('--construction is a static gate; run evaluator/runtime checks separately')
    if args.construction:
        require_scoring_evidence = False
        if args.report and args.report.resolve().is_relative_to(args.package.resolve()):
            parser.error('construction report must be outside the checked package')
        if args.paths:
            from authoring_package import bind_framework_fields, framework_metadata, read_paths
            try:
                paths = read_paths(args.paths)
                if Path(paths['output_dir']).resolve() != args.package.resolve():
                    raise ValueError('--package must match paths.output_dir')
                for key in ('source_bundle', 'draft'):
                    if paths.get(key) and _overlap(args.package, Path(paths[key])):
                        raise ValueError('checked package must be separate from frozen inputs')
            except (OSError, ValueError) as error:
                parser.error(str(error))
            bind_framework_fields(args.package, framework_metadata(args.package, paths))
            require_scoring_evidence = not (paths.get('review_report_schema') or paths.get('construction_instructions'))
        generation = None
        if args.generate_framework:
            from framework_generator import generate
            generation = generate(args.package, enabled=True)
        report = inspect_construction(args.package, generation=generation, review_report=args.review_report,
                                     require_scoring_evidence=require_scoring_evidence,
                                     source_bundle=Path(paths['source_bundle']) if args.paths else None,
                                     previous_package=Path(paths['draft']) if args.paths and paths.get('draft') else None)
        report_path = args.report or args.package.parent / 'preflight_report.json'
        atomic_write_json(report_path, report)
        if args.explain_check:
            check = next((row for row in report['checks'] if row['id'] == args.explain_check), None)
            if check is None:
                parser.error(f'{args.explain_check} is absent from the current report {report_path}')
            print(f"{check['id']} ({check['status']}) — {check['reason']}")
            print(f"Full gate: {report['contract_status']}. Report: {report_path}")
        elif args.paths:
            print_worker_summary(args.package, report_path, report)
        else:
            for check in report['checks']:
                print(f"{check['id']}: {check['status']} — {check['reason']}")
        return 0 if report['status'] == 'pass' else 1
    try:
        spec = json.loads((args.execution or args.package / 'execution.json').read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        spec = {}
        load_error = str(error)
    else:
        load_error = None
    report = inspect_package(args.package, spec, run_evaluator=args.run_evaluator or args.require_evaluator, timeout_sec=args.timeout_sec, runtime_evidence=args.runtime_evidence)
    if load_error:
        report["errors"].append(load_error)
    atomic_write_json(args.report or args.package.parent / "preflight_report.json", report)
    print(json.dumps({"status": report["status"], "static_pass": report["static_pass"], "run_ready": report["run_ready"]}, ensure_ascii=False))
    if report["status"] != "pass":
        return 1
    if args.require_run_ready and not report["run_ready"]:
        return 2
    if args.require_evaluator and report["smoke"]["status"] != "pass":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
