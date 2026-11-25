"""Authenticate a previous scientific construction before replaying its full host preflight."""
import hashlib
import json
from pathlib import Path

from contract import execution_fingerprint, package_fingerprint
from orchestrator.interrupted_review import review_start_timeout
from worker_runtime import file_sha256, read_json


def construction_inputs(role_hashes):
    prefix = 'environment/input/source_bundle/'
    sources = {name[len(prefix):]: value for name, value in role_hashes.items() if name.startswith(prefix)}
    inputs = {'sources_sha256': hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest(),
              'instruction_sha256': role_hashes['instruction.md']}
    if 'environment/input/role_reference.md' in role_hashes:
        inputs['role_reference_sha256'] = role_hashes['environment/input/role_reference.md']
    return inputs


def prior_construction(reference, draft, checks, current_role, request):
    build = Path(reference[0]).resolve(strict=True)
    if file_sha256(build) != reference[1]:
        raise ValueError('prior build changed after review recovery was scheduled')
    previous = read_json(build)
    root = build.parent.parent
    if (not review_start_timeout(previous, root) or previous.get('package') != str(draft)
            or not draft.resolve().is_relative_to(root) or not checks.resolve().is_relative_to(root)
            or (previous.get('task_id'), previous.get('canonical_id')) != (request['task_id'], request.get('canonical_id'))
            or previous.get('execution_context') != request):
        return None
    original = next((s for s in reversed(previous['stages']) if s.get('package') == str(draft)), {})
    if (original.get('stage') != '01_construct' or original.get('static_pass') is not True
            or original.get('contract_status') != 'PASS' or original.get('checks') != str(checks)):
        return None
    preflight = read_json(checks)
    if (preflight.get('status') != 'pass' or preflight.get('static_pass') is not True
            or preflight.get('contract_status') != 'PASS' or preflight.get('task_id') != request['task_id']
            or preflight.get('package') != str(draft) or preflight.get('package_sha256') != package_fingerprint(draft)
            or preflight.get('execution_sha256') != execution_fingerprint(read_json(draft/'execution.json'))):
        return None
    evidence_sha = file_sha256(checks)
    inputs = construction_inputs(read_json(current_role/'worker_snapshot.json'))
    if 'checks_sha256' in original:
        if original.get('checks_sha256') != evidence_sha or any(original.get(k) != v for k, v in inputs.items()):
            return None
    else:
        # Older compact runs preserved the original role inputs and the checks
        # actually presented to the independent reviewer. Older legacy runs can
        # use their ordinary repair path if this evidence is unavailable.
        tag = '01' if original['attempt'] == 0 else f"01_{original['attempt']:02d}"
        role = previous.get('role_inputs', {}).get(tag, {})
        hashes = role.get('input_hashes', {})
        review = previous.get('role_inputs', {}).get('02_01', {}).get('input_hashes', {})
        if ('instruction.md' not in hashes or construction_inputs(hashes) != inputs
                or review.get('environment/input/preflight_report.json') != evidence_sha):
            return None
    return dict(build=str(build), build_sha256=reference[1], stage='01_construct', attempt=original['attempt'],
                checks=str(checks), checks_sha256=evidence_sha, package_sha256=preflight['package_sha256'],
                execution_sha256=preflight['execution_sha256'], **inputs)
