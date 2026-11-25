#!/usr/bin/env python3
"""Explicit, reviewed task variants and cooperative scripted interactions.

No model rewrites science here. Scenario authors supply public text/assets and
attest to the scope of the change. Private scripts/labels never enter the solver
mount. mailbox-v1 is cooperative file interaction, not native chat injection.
"""
from __future__ import annotations

import copy
import hashlib
import json
import mimetypes
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from score_attempt import now, read_json, sha256, write_json

SAFE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}')
KINDS = {'canonical', 'natural', 'attachment', 'noise', 'ambiguity', 'multiturn'}


def safe_id(value: Any, label: str = 'id') -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ValueError(f'{label} must use 1..120 letters/digits/._-')
    return value


def _file_reference(value: Any, base: Path, label: str) -> Path:
    if not isinstance(value, dict) or set(value) != {'path', 'sha256'}:
        raise ValueError(label + ' must contain exactly path and sha256')
    if not isinstance(value['path'], str) or not value['path']:
        raise ValueError(label + ' path must be nonempty')
    path = Path(value['path'])
    path = path if path.is_absolute() else base / path
    if path.is_symlink() or not path.is_file() or sha256(path) != value['sha256']:
        raise ValueError(label + ' file is missing, symlinked or its sha256 does not match')
    return path.resolve()


def load_scenario(path: Path | None) -> tuple[dict, Path]:
    if path is None:
        return {'schema_version': 'pipeline-scenario-v1', 'scenario_id': 'canonical', 'kind': 'canonical'}, Path.cwd()
    value = read_json(path)
    allowed = {'schema_version', 'scenario_id', 'kind', 'task_id', 'readme', 'data_description',
               'attachments', 'review', 'interaction', 'description', 'transformation'}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError('unknown scenario fields: supported fields are ' + ', '.join(sorted(allowed)))
    if value.get('schema_version') != 'pipeline-scenario-v1' or value.get('kind') not in KINDS:
        raise ValueError('scenario requires pipeline-scenario-v1 and a supported kind')
    safe_id(value.get('scenario_id'), 'scenario_id')
    relocation = value.get('transformation') == 'readme-as-attachment'
    if 'transformation' in value and not relocation:
        raise ValueError('unsupported built-in transformation')
    if relocation and (value['kind'] != 'attachment' or any(value.get(k) for k in ('readme', 'data_description', 'attachments', 'interaction'))):
        raise ValueError('readme-as-attachment only relocates original README; additional changes require a reviewed scenario')
    if value['kind'] == 'canonical' and any(value.get(k) for k in ('readme', 'data_description', 'attachments', 'interaction', 'transformation')):
        raise ValueError('canonical cannot contain input perturbations')
    if value['kind'] in {'natural', 'ambiguity'} and not any(value.get(k) for k in ('readme', 'data_description')):
        raise ValueError('natural/ambiguity cases require an explicit reviewed text replacement')
    if value['kind'] == 'attachment' and not relocation and not any(value.get(k) for k in ('readme', 'attachments')):
        raise ValueError('attachment case must contain an actual attachment/input change')
    if value['kind'] == 'multiturn' and not value.get('interaction'):
        raise ValueError('multiturn case requires an actual interaction script')
    changed = value.get('kind') != 'canonical' or any(value.get(k) for k in ('readme', 'data_description', 'attachments', 'interaction'))
    if changed and not relocation:
        review = value.get('review')
        if not isinstance(review, dict) or review.get('approved') is not True or not all(
                isinstance(review.get(key), str) and review[key].strip() for key in ('reviewer', 'basis')):
            raise ValueError('modified scenarios need explicit review approved=true, reviewer and basis')
        if not isinstance(review.get('information_equivalent'), bool) or review.get('public_exposure_checked') is not True:
            raise ValueError('review must state information_equivalent and public_exposure_checked=true')
        if value['kind'] in {'natural', 'attachment', 'noise'} and not review['information_equivalent']:
            raise ValueError('natural/attachment/noise scenarios must preserve scientific information; use ambiguity for omissions')
    for field in ('readme', 'data_description'):
        if field in value:
            target = _file_reference(value[field], path.parent, field)
            if not target.read_text(encoding='utf-8').strip():
                raise ValueError(field + ' replacement must be nonempty UTF-8 text')
    attachments = value.get('attachments', [])
    if not isinstance(attachments, list):
        raise ValueError('attachments must be a list')
    if value['kind'] == 'noise' and not any(isinstance(item, dict) and item.get('role') == 'noise' for item in attachments):
        raise ValueError('noise case requires at least one explicit noise attachment')
    destinations = set()
    for item in attachments:
        if not isinstance(item, dict) or set(item) != {'source', 'destination', 'role'} or item['role'] not in {'task', 'noise'}:
            raise ValueError('attachment requires source, destination and private role task|noise')
        _file_reference(item['source'], path.parent, 'attachment source')
        dest = Path(item['destination'])
        if dest.is_absolute() or '..' in dest.parts or len(dest.parts) < 2 or dest.parts[0] != 'attachments':
            raise ValueError('attachment destination must stay below public attachments/')
        if dest.as_posix() in destinations:
            raise ValueError('duplicate attachment destination')
        destinations.add(dest.as_posix())
    validate_interaction(value.get('interaction'))
    return value, path.parent.resolve()


def validate_interaction(interaction: Any) -> None:
    if interaction is None:
        return
    if not isinstance(interaction, dict) or set(interaction) - {'mode', 'events', 'clarifications', 'unmatched_reply'}:
        raise ValueError('unsupported interaction fields')
    if interaction.get('mode') != 'mailbox-v1':
        raise ValueError('only cooperative mailbox-v1 is supported; native chat injection is not implemented')
    ids = set()
    for event in interaction.get('events', []):
        if not isinstance(event, dict) or set(event) != {'id', 'trigger', 'text'}:
            raise ValueError('scripted events require id, trigger and text')
        ident = safe_id(event['id'], 'event id')
        if ident in ids:
            raise ValueError('duplicate scripted event id')
        ids.add(ident)
        trigger = event['trigger']
        if trigger == {'type': 'start'}:
            pass
        elif isinstance(trigger, dict) and set(trigger) == {'type', 'name'} and trigger['type'] == 'milestone':
            safe_id(trigger['name'], 'milestone name')
        else:
            raise ValueError('unsupported trigger: only start or cooperative milestone(name) are supported')
        if not isinstance(event['text'], str) or not event['text'].strip():
            raise ValueError('event text must be nonempty')
    keys = set()
    for reply in interaction.get('clarifications', []):
        if not isinstance(reply, dict) or set(reply) != {'key', 'answer'}:
            raise ValueError('clarification requires exact key and answer')
        key = safe_id(reply['key'], 'clarification key')
        if key in keys or not isinstance(reply['answer'], str) or not reply['answer'].strip():
            raise ValueError('duplicate/empty clarification')
        keys.add(key)
    if 'unmatched_reply' in interaction and not isinstance(interaction['unmatched_reply'], str):
        raise ValueError('unmatched_reply must be text')


MAILBOX_PROTOCOL = """

## Controlled Interaction Interface
This run uses a file mailbox. Reassess computations and conclusions when new information arrives.
Read-only inbox: /task/interaction/inbox/; request outbox: /task/interaction/outbox/.
Write each <request_id>.json atomically using a unique request_id.
Clarification: {"request_id":"q1","kind":"clarification","key":"condition_name","question":"Your question"}.
Milestone: {"request_id":"m1","kind":"milestone","name":"milestone_name"}.
Poll inbox/<request_id>.json and inbox/event-*.json. Unknown conditions receive only the predetermined response.
"""


def render_operational_instruction(template: str, runtime: dict, *, include_resources: bool = True) -> str:
    """Render operational settings without restoring intentionally omitted science."""
    seconds = runtime['time_limit_sec']
    if type(seconds) is not int or seconds < 0:
        raise ValueError('runtime.time_limit_sec must be an integer >= 0 (0 means unlimited)')
    minutes = format(seconds / 60, '.12g')
    if seconds == 0:
        text = re.sub(r'\{\{\s*TIME_LIMIT_MINUTES\s*\}\}(?:[ \t]*(?:minutes?\b))?', 'unlimited', template)
        time_description = 'Total Solver time budget: unlimited (time_limit_sec=0).\n'
    else:
        text = re.sub(r'\{\{\s*TIME_LIMIT_MINUTES\s*\}\}', minutes, template)
        time_description = f'Total Solver time budget: {seconds} seconds ({minutes} minutes).\n'
    if '{{' in text or '}}' in text:
        raise ValueError('solver instruction has unresolved placeholders; only TIME_LIMIT_MINUTES is supported')
    if not include_resources:
        return text
    network = runtime['network_policy']
    networks = {
        'disabled': 'External network access is disabled; authorized local materials and internal task services remain available.',
        'enabled': 'External access is permitted within task authorization; connectivity does not grant additional scientific search rights.',
        'allowlisted': 'External access is limited to the permitted hosts and task resource/search permissions.',
        'gateway_search_only': 'Use only the explicitly provided gateway; direct external access is disabled.',
    }
    search = runtime.get('search_policy', 'none')
    searches = {
        'none': 'External scientific searches are disabled. You may read supplied local materials.',
        'gateway': 'External scientific searches must use the provided search gateway.',
        'direct': 'External scientific searches are allowed within task scope and network restrictions.',
    }
    if network not in networks or search not in searches:
        raise ValueError('unknown runtime network/search policy')
    text += '\n\n## Runtime Resources and Permissions\n'
    text += (f"CPU: {runtime['cpu']}; memory: {runtime['memory_mb']} MiB; GPU: {runtime['gpus']}.\n"
             f'{time_description}'
             f'Network policy network_policy={network}: {networks[network]}\n'
             f'Scientific search policy search_policy={search}: {searches[search]}\n'
             'Authorized data, model and dependency acquisition is separate from scientific searches. '
             'Downloads require both task authorization and permitted network access. '
             'search_policy=none does not itself prohibit authorized downloads or authorize disguised scientific searches.\n')
    if network == 'allowlisted':
        text += 'Allowed hosts: ' + ', '.join(runtime.get('allowed_hosts', [])) + '\n'
    return text


def prepare_effective_task(*, package: Path, output: Path, spec: dict, layout: dict,
                           scenario: Path | None = None, solver_prompt: Path | None = None,
                           private_source: Path | None = None, private_variants: list[Path] | None = None,
                           forbidden_sha256: list[str] | None = None,
                           reference_overlay: Path | None = None, prepared_resources: Path | None = None,
                           verify_resources: bool = False) -> dict:
    """Create a run-owned copy; preserve original task, scorer and scientific data."""
    from contract import package_fingerprint, package_manifest
    definition, base = load_scenario(scenario)
    if definition.get('task_id') not in (None, spec['task_id']):
        raise ValueError('scenario is bound to a different task_id')
    if output.exists():
        raise ValueError('refusing to overwrite effective input')
    if output.resolve().is_relative_to(package.resolve()) or package.resolve().is_relative_to(output.resolve()):
        raise ValueError('effective input must be separate from its immutable source')
    # Forbid accidentally selecting private build/Method files as public attachments.
    boundaries = [(root.resolve(), (root / Path(layout['public_dir']).relative_to('common_task')).resolve())
                  for root in (private_variants or [])]
    if private_source is not None:
        boundaries.append((private_source.resolve(), (private_source / layout['public_dir']).resolve()))
    refs = [definition[key] for key in ('readme', 'data_description') if key in definition]
    refs.extend(item['source'] for item in definition.get('attachments', []))
    for ref in refs:
        origin = _file_reference(ref, base, 'scenario input')
        if any(origin.is_relative_to(root) and not origin.is_relative_to(public_source) for root, public_source in boundaries):
            raise ValueError('scenario inputs cannot expose private source package files, including sibling variants')
        if ref['sha256'] in (forbidden_sha256 or []):
            raise ValueError('scenario input copies protected Method bytes into the public case')
    target = output / 'task'
    shutil.copytree(package, target)
    prepared_references = []
    from formal_resources import declared, inspect_formal
    for delivered_root, references_only in ((prepared_resources, False), (reference_overlay if prepared_resources is None else None, True)):
        if delivered_root is None:
            continue
        overlay = Path(delivered_root)
        if overlay.is_symlink() or not overlay.is_dir():
            raise ValueError('prepared resources must be a regular directory')
        overlay = overlay.resolve()
        for resource in spec.get('data', {}).get('sources', []):
            if references_only and resource.get('role') != 'reference':
                continue
            if resource.get('role') == 'fixture' or resource.get('visibility') == 'build_only':
                continue
            from contract import package_path
            relative = Path(resource['package_path'])
            source = package_path(overlay, relative.as_posix())
            if resource.get('required') is False and not source.exists():
                continue
            from resource_contract import verify_content
            verify_content(source, resource)
            destination = package_path(target, relative.relative_to('common_task').as_posix())
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                if destination.is_dir():
                    shutil.rmtree(destination)
                elif destination.exists():
                    destination.unlink()
                shutil.copytree(source, destination)
                digest = package_fingerprint(source)
            else:
                if destination.is_dir():
                    shutil.rmtree(destination)
                shutil.copy2(source, destination)
                digest = sha256(destination)
            prepared_references.append({'id': resource.get('id'), 'path': str(destination.resolve()),
                                        'sha256': digest, 'source_overlay': str(source)})
    readme = target / Path(layout['readme_path']).relative_to('common_task')
    data_description = target / Path(layout['data_description_path']).relative_to('common_task')
    public = target / Path(layout['public_dir']).relative_to('common_task')
    if definition.get('transformation') == 'readme-as-attachment':
        attachment = public / 'attachments/task_brief.md'
        if attachment.exists():
            raise ValueError('built-in attachment destination already exists')
        attachment.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(readme, attachment)
        readme.write_text('Read `attachments/task_brief.md` and complete its scientific task. Data and documentation remain in the current task directory.\n', encoding='utf-8')
    for key, destination in (('readme', readme), ('data_description', data_description)):
        if key in definition:
            shutil.copyfile(_file_reference(definition[key], base, key), destination)
    for item in definition.get('attachments', []):
        dest = public / item['destination']
        if dest.exists() or dest.is_symlink():
            raise ValueError('attachment would overwrite existing public data')
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_file_reference(item['source'], base, 'attachment'), dest)
    instruction = target / 'instruction.md'
    advice_path = solver_prompt or Path(__file__).resolve().parents[1] / 'prompt/03_solver_instruction.md'
    if advice_path.is_symlink() or not advice_path.is_file() or not advice_path.read_text(encoding='utf-8').strip():
        raise ValueError('solver prompt must be a nonempty regular text file')
    source_bytes = advice_path.read_bytes()
    advice = source_bytes.decode('utf-8')
    execution_bytes = source_bytes
    if '<!-- SOLVER_GUIDANCE_EN_ASSET -->' in advice:
        # Never silently discard edits to the Chinese control prompt. Both
        # reviewed texts are bound, and both are archived outside Solver mounts.
        assets = Path(__file__).resolve().parents[1] / 'assets'
        if (advice_path.parent / 'solver_guidance.mapping.json').is_file():
            assets = advice_path.parent
        binding = read_json(assets / 'solver_guidance.mapping.json')
        execution_path = assets / 'solver_guidance.en.md'
        if execution_path.is_symlink() or not execution_path.is_file():
            raise ValueError('English Solver guidance must be a regular file')
        execution_bytes = execution_path.read_bytes()
        if (binding.get('schema_version') != 'pipeline-solver-guidance-v1'
                or binding.get('control_sha256') != hashlib.sha256(source_bytes).hexdigest()
                or binding.get('execution_sha256') != hashlib.sha256(execution_bytes).hexdigest()):
            raise ValueError('Chinese/English Solver guidance binding is stale; review both prompts and update solver_guidance.mapping.json')
        advice = execution_bytes.decode('utf-8')
    prompt_dir = output / 'prompts'
    prompt_dir.mkdir()
    source_copy = prompt_dir / '03_solver_instruction.md'
    execution_copy = prompt_dir / 'solver_execution.md'
    source_copy.write_bytes(source_bytes)
    execution_copy.write_bytes(execution_bytes)
    guidance_provenance = {
        'source': {'path': str(source_copy.resolve()), 'sha256': sha256(source_copy)},
        'execution': {'path': str(execution_copy.resolve()), 'sha256': sha256(execution_copy)},
    }
    changed_science = definition.get('readme') or definition.get('data_description') or definition.get('transformation')
    if definition['kind'] == 'canonical' or not changed_science:
        # Keep the authored scientific task byte-for-byte; append only the separately
        # configured guidance. Execution configuration never reconstructs science.
        authored = instruction.read_text(encoding='utf-8')
        if not authored.strip():
            raise ValueError('authored instruction.md must be nonempty')
        guidance = render_operational_instruction(advice, spec['runtime'], include_resources=False)
        if not re.search(r'^#{1,6} General Solver Guidance\s*$', guidance, re.M):
            guidance = '# General Solver Guidance\n\n' + guidance
        text = authored + ('\n' if authored.endswith('\n') else '\n\n') + guidance.rstrip() + '\n'
    else:
        # Explicitly reviewed omissions/attachments must not regain original science
        # through the copied instruction. Their actual materials remain authoritative.
        text = render_operational_instruction(advice, spec['runtime'])
    from instruction_checks import public_identity_errors
    exposed = [instruction, readme, data_description]
    exposed.extend(public.rglob('*.md'))
    for path in exposed:
        content = text if path == instruction else path.read_text(encoding='utf-8')
        errors = public_identity_errors(content, spec)
        if errors:
            raise ValueError('; '.join(errors))
    instruction.write_text(text, encoding='utf-8')
    interaction = definition.get('interaction')
    if interaction is not None:
        milestones = sorted({e['trigger']['name'] for e in interaction.get('events', []) if e['trigger']['type'] == 'milestone'})
        clarification_keys = sorted(reply['key'] for reply in interaction.get('clarifications', []))
        # Milestones are instructions, never event payloads or hidden answers.
        protocol = MAILBOX_PROTOCOL + '\nSend the corresponding milestone notification after these stages: ' + ', '.join(milestones) + '\n'
        protocol += 'Available clarification keys (answers are not included): ' + ', '.join(clarification_keys) + '\n'
        instruction.write_text(instruction.read_text(encoding='utf-8') + protocol, encoding='utf-8')
        for name in ('inbox', 'outbox'):
            (output / 'interaction' / name).mkdir(parents=True)
        (output / 'interaction/outbox').chmod(0o777)
    from submission_lifecycle import install
    formal_inputs = inspect_formal(target, declared(spec), common=True) if verify_resources else []
    submission_lifecycle = install(target, spec, layout, prepared=verify_resources and bool(declared(spec)))
    source_hash = package_fingerprint(package)
    manifest = {'schema_version': 'pipeline-effective-input-v1', 'created_at': now(),
                'task_id': spec['task_id'], 'scenario_id': definition['scenario_id'],
                'kind': definition['kind'], 'definition': definition,
                'source_package': str(package.resolve()), 'source_package_sha256': source_hash,
                'effective_task': str(target.resolve()), 'effective_task_sha256': package_fingerprint(target),
                'files': package_manifest(target),
                'solver_prompt_sha256': sha256(instruction),
                'solver_guidance': guidance_provenance,
                'submission_lifecycle': submission_lifecycle,
                'readme': str(readme.resolve()), 'data_description': str(data_description.resolve()),
                'interaction_mode': 'mailbox-v1' if interaction is not None else None,
                'scientific_equivalence': 'exact_readme_relocated' if definition.get('transformation') else 'author_reviewed_not_machine_proven' if definition.get('review') else 'unchanged',
                'delivered_attachments': ['attachments/task_brief.md'] if definition.get('transformation') else [item['destination'] for item in definition.get('attachments', [])],
                'prepared_references': prepared_references, 'formal_resources': formal_inputs,
                'formal_resources_verified': verify_resources}
    actual = [instruction, readme, data_description]
    if (target / 'guidance/method.md').is_file():
        actual.append(target / 'guidance/method.md')
    actual.extend(public / relative for relative in manifest['delivered_attachments'])
    manifest['actual_files'] = [{'path': str(path.resolve()), 'sha256': sha256(path),
                                 'visibility': 'solver' if path == instruction else 'public',
                                 'media_type': mimetypes.guess_type(path.name)[0] or 'application/octet-stream'}
                                for path in actual]
    # Store the exact source definition and final file hashes outside Docker mounts.
    write_json(output / 'scenario_manifest.json', manifest)
    return manifest


def validate_effective_input(path: Path, *, expected_sha256: str | None = None) -> dict:
    from contract import package_fingerprint, package_manifest
    if expected_sha256 is not None and sha256(path) != expected_sha256:
        raise ValueError('scenario manifest changed since solver launch')
    value = read_json(path)
    for reference in value.get('solver_guidance', {}).values():
        frozen_prompt = Path(reference['path'])
        if frozen_prompt.is_symlink() or not frozen_prompt.is_file() or sha256(frozen_prompt) != reference['sha256']:
            raise ValueError('frozen Solver guidance changed after input preparation')
    task = Path(value['effective_task'])
    if package_fingerprint(task) != value['effective_task_sha256'] or package_manifest(task) != value['files']:
        raise ValueError('effective solver task changed after input preparation')
    if package_fingerprint(Path(value['source_package'])) != value['source_package_sha256']:
        raise ValueError('scenario source task changed')
    return value


def interaction_overlay(input_dir: Path, solver_service: str) -> dict:
    return {'services': {solver_service: {'volumes': [
        {'type': 'bind', 'source': str((input_dir / 'interaction/inbox').resolve()), 'target': '/task/interaction/inbox', 'read_only': True},
        {'type': 'bind', 'source': str((input_dir / 'interaction/outbox').resolve()), 'target': '/task/interaction/outbox', 'read_only': False}]}}}


class MailboxController:
    """Host-owned deterministic responder. Scripts/events remain outside mounts."""
    def __init__(self, input_dir: Path, definition: dict):
        self.root = input_dir / 'interaction'
        self.script = definition.get('interaction')
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.seen: set[str] = set()
        self.delivered: set[str] = set()
        self.log = self.root / 'events.jsonl'
        self.error: str | None = None

    def _record(self, value: dict) -> None:
        with self.log.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'timestamp': now(), **value}, ensure_ascii=False) + '\n')

    def _deliver(self, trigger: dict) -> None:
        for event in self.script.get('events', []):
            if self.stop_event.is_set():
                return
            if event['trigger'] == trigger and event['id'] not in self.delivered:
                item = {'event_id': event['id'], 'text': event['text'], 'delivered_at': now()}
                write_json(self.root / 'inbox' / ('event-' + event['id'] + '.json'), item)
                self.delivered.add(event['id'])
                self._record({'event': 'SCENARIO_MESSAGE_DELIVERED', **item, 'trigger': trigger})

    def poll(self) -> None:
        if self.script is None:
            return
        self._deliver({'type': 'start'})
        for path in sorted((self.root / 'outbox').glob('*.json')):
            if self.stop_event.is_set():
                return
            if path.name in self.seen:
                continue
            self.seen.add(path.name)
            try:
                if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
                    raise ValueError('request must be a regular JSON file <=64 KiB')
                request = read_json(path)
                rid = safe_id(request.get('request_id'), 'request_id')
                if path.name != rid + '.json' or rid.startswith('event-'):
                    raise ValueError('request filename must match request_id and not use reserved event- prefix')
                kind = request.get('kind')
                self._record({'event': 'SCENARIO_REQUEST', 'request': request})
                if kind == 'clarification':
                    key = request.get('key')
                    replies = [x['answer'] for x in self.script.get('clarifications', []) if x['key'] == key]
                    response = {'request_id': rid, 'kind': kind, 'matched': bool(replies),
                                'text': replies[0] if replies else self.script.get('unmatched_reply', 'No additional information is available. Record necessary assumptions or blockers explicitly.')}
                elif kind == 'milestone':
                    name = safe_id(request.get('name'), 'milestone name')
                    self._deliver({'type': 'milestone', 'name': name})
                    response = {'request_id': rid, 'kind': kind, 'text': 'Milestone recorded. Check inbox/event-*.json.'}
                else:
                    raise ValueError('unsupported mailbox request kind')
                if self.stop_event.is_set():
                    self._record({'event': 'SCENARIO_RESPONSE_NOT_DELIVERED', 'request_id': rid, 'reason': 'solver command finished'})
                    continue
                write_json(self.root / 'inbox' / (rid + '.json'), response)
                self._record({'event': 'SCENARIO_RESPONSE', 'response': response})
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                self._record({'event': 'SCENARIO_REQUEST_REJECTED', 'file': path.name, 'reason': str(exc)})

    def start(self) -> None:
        if self.script is None:
            return
        self.poll()
        def loop():
            try:
                while not self.stop_event.wait(0.1):
                    self.poll()
            except Exception as exc:
                self.error = str(exc)
        self.thread = threading.Thread(target=loop, name='scenario-mailbox', daemon=True)
        self.thread.start()

    def stop(self) -> dict:
        if self.script is None:
            return {'mode': None, 'status': 'not_applicable'}
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
        missing = [e['id'] for e in self.script.get('events', []) if e['id'] not in self.delivered]
        pending = sorted(path.name for path in (self.root / 'outbox').glob('*.json') if path.name not in self.seen)
        value = {'mode': 'mailbox-v1', 'status': 'controller_failed' if self.error else 'events_not_triggered' if missing else 'requests_pending' if pending else 'complete',
                 'delivered_events': sorted(self.delivered), 'untriggered_events': missing,
                 'pending_requests': pending,
                 'native_chat_injection': False, 'delivery_proves_model_read': False, 'error': self.error}
        write_json(self.root / 'summary.json', value)
        return value
