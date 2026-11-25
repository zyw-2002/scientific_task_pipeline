"""Executable task configuration, package paths and content identities.

Scientific task prose is authored directly in common_task/instruction.md.
This module validates only configuration that programs actually consume.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from urllib.parse import urlsplit
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_VERSION = "pipeline-execution-v1"
RECEIPT_FILES = {"readiness.json", "runtime_evidence.json"}
NETWORK_POLICIES = {"disabled", "allowlisted", "gateway_search_only", "enabled"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def execution_fingerprint(spec: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def package_manifest(root: Path) -> dict[str, str]:
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError(f"package is not a directory: {root}")
    manifest = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_symlink():
            raise ValueError(f"package contains symlink: {relative}")
        if "__pycache__" in relative.parts or path.suffix == ".pyc" or relative.as_posix() in RECEIPT_FILES:
            continue
        if path.is_file():
            manifest[relative.as_posix()] = sha256_file(path)
        elif not path.is_dir():
            raise ValueError(f"package contains nonregular file: {relative}")
    return manifest


def package_fingerprint(root: Path) -> str:
    return hashlib.sha256(json.dumps(package_manifest(root), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def package_layout(spec: dict[str, Any]) -> dict[str, Any]:
    framework = spec.get("framework")
    framework = framework if isinstance(framework, dict) else {}
    public = framework.get("public_dir", "common_task/environment/input")
    defaults = {
        "public_dir": public,
        "instruction_path": "common_task/instruction.md",
        "task_config_path": "common_task/task.toml",
        "readme_path": f"{public}/README.md",
        "data_description_path": f"{public}/data_description.md",
        "data_sources_path": f"{public}/data_sources.json",
        "data_path": f"{public}/data",
        "dockerfile": "common_task/environment/Dockerfile",
        "compose_file": "common_task/environment/docker-compose.yaml",
        "method_path": "common_task/method.md",
        "score_path": "common_task/evaluation/evaluation_protocol.py",
        "validator_path": f"{public}/validate_submission.py",
        "private_paths": ["common_task/environment/sidecar", "common_task/tests", "common_task/evaluation", "guidance", "common_task/guidance", "common_task/method.md", "build_report.json", "review_report.json", "review_report.md", "fixed_task_alignment.json", "execution.json"],
        "solver_services": ["main", "solver", "agent"],
        "evaluator_services": ["eval", "evaluator", "score", "scorer", "naturebench-eval"],
    }
    defaults.update({key: value for key, value in framework.items() if key in defaults})
    return defaults


def package_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError(f"invalid package relative path: {relative!r}")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or pure.as_posix() == ".":
        raise ValueError(f"path must stay inside package: {relative!r}")
    path = Path(root) / relative
    if not path.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError(f"path escapes package: {relative!r}")
    return path


def _absolute(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("/") and value != "/" and ".." not in PurePosixPath(value).parts and not any(x in value for x in ("\\", "\n", "\r", "\x00"))


def resource_estimate_errors(estimate: Any) -> list[str]:
    if not isinstance(estimate, dict):
        return ['resource_estimate must be an object']
    errors = []
    minutes = estimate.get('expected_solver_minutes')
    if type(minutes) is not int or not 30 <= minutes <= 1440:
        errors.append('resource_estimate.expected_solver_minutes must be an integer from 30 to 1440')
    for key in ('gpu_model', 'basis'):
        if not isinstance(estimate.get(key), str) or not estimate[key].strip():
            errors.append(f'resource_estimate.{key} must be nonempty text')
    memory = estimate.get('min_gpu_memory_gb')
    if memory is not None and (type(memory) is not int or memory < 0):
        errors.append('resource_estimate.min_gpu_memory_gb must be nonnegative or null when unknown')
    assumptions = estimate.get('assumptions')
    if not isinstance(assumptions, list) or not all(isinstance(v, str) and v.strip() for v in assumptions):
        errors.append('resource_estimate.assumptions must be an array of nonempty strings')
    return errors


def validate_execution(spec: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(spec, dict):
        return ["execution configuration must be a JSON object"]
    allowed = {'schema_version', 'task_id', 'canonical_id', 'scientific_acceptance_version', 'assessment_contract_version', 'scope_contract_version', 'difficulty_contract_version', 'paper_caption_review_version', 'paper_comparison_version', 'solver_access',
               'evaluation_protocol_version', 'framework_generation_version', 'output_language',
               'submission', 'runtime', 'data', 'framework', 'verification', 'preflight',
               'resource_estimate'}
    for key in sorted(spec.keys() - allowed):
        errors.append(f'execution.{key} is unsupported; write scientific content directly in instruction.md')
    if 'resource_estimate' in spec:
        errors.extend(resource_estimate_errors(spec['resource_estimate']))
    if spec.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if not isinstance(spec.get("task_id"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", spec["task_id"]):
        errors.append("task_id must be a safe nonempty identifier")
    if 'canonical_id' in spec and (not isinstance(spec['canonical_id'], str) or not spec['canonical_id'].strip()):
        errors.append('canonical_id must identify the source paper independently of task_id')

    def obj(parent: dict, key: str, prefix: str = "") -> dict:
        value = parent.get(key)
        if not isinstance(value, dict):
            errors.append(f"{prefix}{key} must be an object")
            return {}
        return value

    def text(parent: dict, key: str, prefix: str) -> None:
        if not isinstance(parent.get(key), str) or not parent[key].strip():
            errors.append(f"{prefix}.{key} must be nonempty text")

    if 'scientific_acceptance_version' in spec and (type(spec['scientific_acceptance_version']) is not int or spec['scientific_acceptance_version'] not in {1, 2, 3}):
        errors.append('scientific_acceptance_version must be 1, 2 or 3')
    if 'assessment_contract_version' in spec and (type(spec['assessment_contract_version']) is not int or spec['assessment_contract_version'] != 1):
        errors.append('assessment_contract_version must be 1')
    if 'paper_caption_review_version' in spec and (type(spec['paper_caption_review_version']) is not int or spec['paper_caption_review_version'] not in {1, 2, 3}):
        errors.append('paper_caption_review_version must be 1, 2 or 3')
    if 'paper_comparison_version' in spec and (type(spec['paper_comparison_version']) is not int or spec['paper_comparison_version'] != 1):
        errors.append('paper_comparison_version must be 1')
    if spec.get('paper_comparison_version') == 1 and spec.get('assessment_contract_version') != 1:
        errors.append('paper_comparison_version requires assessment_contract_version=1')
    if spec.get('scope_contract_version') in (1, 2, 3) and spec.get('assessment_contract_version') != 1:
        errors.append('scope_contract_version requires assessment_contract_version=1')
    if 'scope_contract_version' in spec and (type(spec['scope_contract_version']) is not int or spec['scope_contract_version'] not in (1, 2, 3)):
        errors.append('scope_contract_version must be 1, 2 or 3')
    if 'difficulty_contract_version' in spec and (type(spec['difficulty_contract_version']) is not int or spec['difficulty_contract_version'] != 1):
        errors.append('difficulty_contract_version must be 1')
    for key in ('evaluation_protocol_version', 'framework_generation_version'):
        if key in spec and (type(spec[key]) is not int or spec[key] != 1):
            errors.append(f'{key} must be 1')
    if 'output_language' in spec and spec['output_language'] != 'en':
        errors.append('new task artifacts must use output_language=en')
    submission = obj(spec, "submission")
    if not _absolute(submission.get("path")):
        errors.append("submission.path must be a normalized absolute container path")
    for key in sorted(submission.keys() - {'path', 'required_artifacts', 'validator_path', 'validator_command'}):
        errors.append(f'submission.{key} is unsupported; write submission prose directly in instruction.md')
    if 'required_artifacts' in submission:
        from evaluation_protocol import declaration_errors
        errors.extend(declaration_errors(submission['required_artifacts']))
    if 'validator_command' in submission and (not isinstance(submission['validator_command'], list)
            or not submission['validator_command'] or not all(isinstance(v, str) and v for v in submission['validator_command'])):
        errors.append('submission.validator_command must be a nonempty argv string array')
    if submission.get("validator_path") is not None and not _absolute(submission["validator_path"]):
        errors.append("submission.validator_path must be an absolute container path")
    runtime = obj(spec, "runtime")
    for network_key in ('network', 'build_network'):
        if network_key not in runtime:
            continue
        network = runtime[network_key]
        if not isinstance(network, dict):
            errors.append(f'runtime.{network_key} must be an object')
            continue
        for option in ('enabled', 'inherit_env'):
            if option in network and type(network[option]) is not bool:
                errors.append(f'runtime.{network_key}.{option} must be boolean')
        for option in ('proxy_url', 'proxy_env_file', 'no_proxy'):
            if option in network and not isinstance(network[option], str):
                errors.append(f'runtime.{network_key}.{option} must be a string')
        if isinstance(network.get('proxy_url'), str) and network['proxy_url']:
            try:
                parsed = urlsplit(network['proxy_url'])
                if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {'', '/'} or parsed.query or parsed.fragment:
                    errors.append(f'runtime.{network_key}.proxy_url must be an HTTP(S) proxy origin without credentials')
                parsed.port
            except ValueError:
                errors.append(f'runtime.{network_key}.proxy_url is malformed')
        if 'probe_urls' in network and (not isinstance(network['probe_urls'], list) or not all(isinstance(url, str) and url.startswith(('https://', 'http://')) for url in network['probe_urls'])):
            errors.append(f'runtime.{network_key}.probe_urls must be an array of actual HTTP(S) targets')
    if 'multimodal' in runtime:
        multimodal = runtime['multimodal']
        if not isinstance(multimodal, dict) or type(multimodal.get('enabled')) is not bool:
            errors.append('runtime.multimodal requires a boolean enabled field')
        elif multimodal.get('model_capability', 'text') not in {'text', 'vision', 'unknown'}:
            errors.append('runtime.multimodal.model_capability must be text, vision or unknown')
    if not _absolute(runtime.get("data_dir")):
        errors.append("runtime.data_dir must be a normalized absolute container path")
    # Zero disables the Solver elapsed-time deadline; it is not a zero-second budget.
    for key, minimum in (("cpu", 1), ("memory_mb", 1), ("gpus", 0), ("time_limit_sec", 0)):
        value = runtime.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            errors.append(f"runtime.{key} must be an integer >= {minimum}")
    if 'build_timeout_sec' in runtime:
        value = runtime['build_timeout_sec']
        if type(value) is not int or value < 0:
            errors.append('runtime.build_timeout_sec must be an integer >= 0; 0 means unlimited')
    if 'base_image' in runtime:
        value = runtime['base_image']
        if not isinstance(value, str) or not value or any(c.isspace() for c in value):
            errors.append('runtime.base_image must be a nonempty image reference without whitespace')
    if 'image_policy' in runtime and runtime['image_policy'] not in {'deferred', 'prebuilt'}:
        errors.append('runtime.image_policy must be deferred or prebuilt')
    if runtime.get("network_policy") not in NETWORK_POLICIES:
        errors.append("runtime.network_policy is invalid")
    hosts = runtime.get("allowed_hosts", [])
    if not isinstance(hosts, list) or not all(isinstance(host, str) and host.strip() and not any(c in host for c in "/\n\r ") for host in hosts):
        errors.append("runtime.allowed_hosts must be an array of hostnames")
    elif runtime.get("network_policy") == "allowlisted" and not hosts:
        errors.append("runtime.allowed_hosts is required for an allowlist")
    if 'solver_access' in spec and spec['solver_access'] != 'online_search':
        errors.append('solver_access must be online_search when declared')
    if spec.get('solver_access') == 'online_search' and (
            runtime.get('network_policy') != 'enabled' or runtime.get('search_policy') != 'direct'):
        errors.append('user-selected online_search requires runtime.network_policy=enabled and search_policy=direct')
    search = runtime.get("search_policy", "none")
    if search not in {"none", "gateway", "direct"}:
        errors.append("runtime.search_policy is invalid")
    if search == "gateway" and not isinstance(runtime.get("gateway"), dict):
        errors.append("runtime.gateway must describe the actual gateway when search_policy=gateway")
    for name in ("eval_service_url",):
        value = runtime.get(name)
        if value is not None and (not isinstance(value, str) or not re.match(r"^https?://[^/\s]+", value)):
            errors.append(f"runtime.{name} must be a real configured HTTP(S) URL when supplied")
    data = obj(spec, "data")
    if data.get("mode") not in {"bundled", "referenced", "mixed", "generator", "no_external_data"}:
        errors.append("data.mode is invalid")
    sources = data.get("sources")
    if not isinstance(sources, list):
        errors.append("data.sources must be an array")
    else:
        if data.get("mode") in {"referenced", "mixed", "generator"} and not sources:
            errors.append(f"data.sources must describe acquisition for mode={data.get('mode')}")
        for index, source in enumerate(sources):
            prefix = f"data.sources[{index}]"
            if not isinstance(source, dict):
                errors.append(f"{prefix} must be an object")
                continue
            text(source, "name", prefix)
            if not any(isinstance(source.get(k), str) and source[k].strip() for k in ("materialization", "path", "url", "acquisition")):
                errors.append(f"{prefix} needs an actual source/path/acquisition description")
            if "visibility" in source and source["visibility"] not in {"public", "guided", "private", "build_only"}:
                errors.append(f"{prefix}.visibility is invalid")
            if "availability" in source and source["availability"] not in {"runtime_download", "bundled", "preprovided", "unavailable", "unverified"}:
                errors.append(f"{prefix}.availability is invalid")
            if "required" in source and not isinstance(source["required"], bool):
                errors.append(f"{prefix}.required must be boolean")
            if "package_path" in source:
                try:
                    package_path(Path("/package"), source["package_path"])
                except ValueError as error:
                    errors.append(f"{prefix}.package_path: {error}")
            sha = source.get("sha256")
            if sha is not None and (not isinstance(sha, str) or not re.fullmatch("[a-f0-9]{64}", sha)):
                errors.append(f"{prefix}.sha256 must be a real lowercase SHA256 or omitted")
            size = source.get("size_bytes")
            if size is not None and (isinstance(size, bool) or not isinstance(size, int) or size < 0):
                errors.append(f"{prefix}.size_bytes must be a nonnegative integer or omitted")
    if data.get("mode") == "no_external_data" and not data.get("note"):
        errors.append("data.note must explain why external data are unnecessary")
    framework = spec.get("framework", {})
    if not isinstance(framework, dict):
        errors.append("framework must be an object")
    else:
        for key, value in package_layout(spec).items():
            if isinstance(value, str):
                try:
                    package_path(Path("/package"), value)
                except ValueError as error:
                    errors.append(f"framework.{key}: {error}")
            elif not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
                errors.append(f"framework.{key} must be a string array")
            elif key == "private_paths":
                for item in value:
                    try:
                        package_path(Path("/package"), item)
                    except ValueError as error:
                        errors.append(f"framework.private_paths: {error}")
    verification = spec.get("verification", {})
    if not isinstance(verification, dict):
        errors.append("verification must be an object when supplied")
    else:
        smoke = verification.get('scoring_smoke')
        if smoke is not None:
            if not isinstance(smoke, dict):
                errors.append('verification.scoring_smoke must be an object')
            else:
                try:
                    package_path(Path('/package'), smoke.get('config_path'))
                except (TypeError, ValueError) as exc:
                    errors.append('verification.scoring_smoke.config_path: ' + str(exc))
        artifact_id = verification.get('evaluation_unit_artifact_id')
        if artifact_id is not None and (not isinstance(artifact_id, str) or not artifact_id.strip()):
            errors.append('verification.evaluation_unit_artifact_id must name a required artifact')
        commands = verification.get("commands", {})
        score_evidence=verification.get('score_evidence')
        if score_evidence is not None:
            if not isinstance(score_evidence,dict) or not _absolute(score_evidence.get('artifact_source')):
                errors.append('verification.score_evidence needs an absolute artifact_source')
            else:
                if score_evidence.get('service') not in package_layout(spec)['evaluator_services']:
                    errors.append('score evidence must be produced by a declared evaluator service')
                source=PurePosixPath(score_evidence['artifact_source'])
                convention=PurePosixPath('/logs/artifacts')
                if source==convention or convention in source.parents or source in convention.parents:
                    errors.append('evaluator evidence must not overlap the Solver artifact directory')
        if not isinstance(commands, dict):
            errors.append("verification.commands must be an object")
        else:
            for name, check in commands.items():
                prefix = f"verification.commands.{name}"
                if not isinstance(check, dict):
                    errors.append(f"{prefix} must contain service and command")
                    continue
                service = check.get("service")
                if not isinstance(service, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", service):
                    errors.append(f"{prefix}.service must name an actual Compose service")
                command = check.get("command")
                if not isinstance(command, list) or not command or not all(isinstance(part, str) and part and "\x00" not in part for part in command):
                    errors.append(f"{prefix}.command must be a nonempty argv string array")
        if "hidden_container_paths" in verification:
            paths = verification["hidden_container_paths"]
            if not isinstance(paths, list) or not paths or not all(_absolute(path) for path in paths):
                errors.append("verification.hidden_container_paths must be a nonempty array of normalized absolute paths; omit to use framework defaults")
    preflight = spec.get("preflight", {})
    if not isinstance(preflight, dict):
        errors.append("preflight must be an object when supplied")
    else:
        cases = preflight.get("cases", [])
        if not isinstance(cases, list):
            errors.append("preflight.cases must be an array")
        else:
            ids = set()
            for index, case in enumerate(cases):
                prefix = f"preflight.cases[{index}]"
                if not isinstance(case, dict):
                    errors.append(f"{prefix} must be an object")
                    continue
                if not isinstance(case.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", case["id"]) or case["id"] in ids:
                    errors.append(f"{prefix}.id must be a unique safe identifier")
                ids.add(str(case.get("id")))
                command = case.get("command")
                if not isinstance(command, list) or not command or not all(isinstance(v, str) and v for v in command):
                    errors.append(f"{prefix}.command must be a nonempty argv array")
                if case.get("expected_status") not in {"valid", "invalid_submission", "runtime_error", "unscorable", "success"}:
                    errors.append(f"{prefix}.expected_status must declare the expected outcome")
                if "expected_reward" in case and (isinstance(case["expected_reward"], bool) or not isinstance(case["expected_reward"], (int, float)) or not math.isfinite(case["expected_reward"])):
                    errors.append(f"{prefix}.expected_reward must be finite")
                if "fixture" in case:
                    try:
                        package_path(Path("/package"), case["fixture"])
                    except ValueError as error:
                        errors.append(f"{prefix}.fixture: {error}")
    return errors


def load_execution(package: Path) -> dict[str, Any]:
    """Read executable configuration, without deriving task prose or legacy fallback."""
    path = Path(package) / 'execution.json'
    if path.is_symlink():
        raise ValueError('execution.json must not be a symlink')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('execution configuration must be a JSON object')
    return value


def save_execution(package: Path, config: dict[str, Any]) -> None:
    atomic_write_json(Path(package) / 'execution.json', config)


def load_metric(package: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Describe the metric actually wired to scoring; never infer it from task prose."""
    binding = config.get('verification', {}).get('score_evidence', {}).get('evaluation_protocol', {})
    path = package_path(Path(package), binding.get('config_path', 'common_task/evaluation/protocol.json'))
    protocol = json.loads(path.read_text(encoding='utf-8'))
    from evaluation_protocol import config_errors
    errors = config_errors(protocol)
    if protocol.get('reward_direction') not in {'higher_is_better', 'lower_is_better'}:
        errors.append('protocol.reward_direction must be higher_is_better or lower_is_better')
    if errors:
        raise ValueError('; '.join(errors))
    return {'name': protocol['reward_metric'], 'direction': protocol['reward_direction'],
            'aggregation': protocol['aggregations']}


def atomic_write_text(path: Path, value: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError(f"refusing to write through symlink: {path}")
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def json_reference(path, section=None):
    path=Path(path).resolve()
    if section is None:
        return {'path':str(path),'sha256':sha256_file(path)}
    value=json.loads(path.read_text())[section]
    digest=hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()
    return {'path':str(path),'section':section,'sha256':digest}


def read_reference(reference, base):
    """Read a hash-bound whole JSON file or an immutable section in a merged record."""
    if not isinstance(reference,dict) or not isinstance(reference.get('path'),str):
        raise ValueError('Missing reference path')
    path=Path(reference['path'])
    if not path.is_absolute():path=Path(base)/path
    if path.is_symlink() or not path.is_file():
        raise ValueError('Reference is missing or symlinked')
    section=reference.get('section')
    actual=json_reference(path,section)
    if actual['sha256']!=reference.get('sha256'):
        raise ValueError('Reference hash does not match')
    value=json.loads(path.read_text())
    return value[section] if section is not None else value
