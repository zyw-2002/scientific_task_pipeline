"""Check directly authored task instructions without generating or rewriting them.

Only explicit public operating facts and declared deliverables are compared here.
Scientific meaning, sufficiency and conflicts in free prose still need source review.
"""
from __future__ import annotations

import json
import re
import shlex
from typing import Any

FACT_SECTIONS = {'Environment', 'Time Budget', 'Output', 'Task and Deliverables'}
ARTIFACT_SECTIONS = {'Output', 'Task and Deliverables'}
REQUIRED_INSTRUCTION_HEADINGS = (
    'Scientific Background', 'Research Objective', 'Task and Deliverables', 'Input',
    'Output', 'Constraints', 'Environment', 'Evaluation', 'Edge Cases', 'Time Budget',
)

# These are intentionally conservative checks over Solver-visible prose and
# manifests.  Authoring evidence may contain the original locator; the public
# task must describe the method and the data contract without handing the
# Solver a copy/paste route to the source paper or repository.
_LEAKAGE_RULES = (
    (re.compile(r'(?i)\b10\.\d{4,9}/[^\s<>"`]+'),
     'public content exposes a DOI or DOI URL'),
    (re.compile(r'(?i)\b(?:PRJ|ERP|DRP|SRP|ERR|SRR|DRR|ERS|SAMN|SAMEA|GSE|GSM)\d{4,12}\b'),
     'public content exposes a source accession'),
    (re.compile(r'(?i)(?:https?://)?(?:www\.)?github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[^\s<>"`]*)?'),
     'public content exposes a GitHub repository or URL'),
    (re.compile(r'(?i)\b(?:github|commit|revision|提交)\b[^\n]{0,80}\b[0-9a-f]{7,40}\b'),
     'public content exposes a source-code commit or revision'),
    (re.compile(r'(?i)(?:/task/input/source_bundle|/task/evaluation|/u01/[\w./+&-]+|(?:^|[\s`])files/[\w./+&-]+)'),
     'public content exposes an authoring or private filesystem path'),
    (re.compile(r'(?i)(?:论文报告|研究人员(?:使用|发现|成功)|完整恢复|成功恢复|reported that|researchers? (?:used|successfully)|fully recovered|successfully recovered)'),
     'public content exposes a source-paper result claim'),
    (re.compile(r'(?i)\b(?:Science|Nature|Cell|PNAS|Bioinformatics|Nucleic Acids Research)\b\s*[,.;:]?\s*\(?(?:19|20)\d{2}\b'),
     'public content exposes a recognizable paper citation'),
)


def solver_visible_leakage_errors(text: str, *, relative_path: str = '') -> list[str]:
    """Return source-locator/result leaks in content mounted for the Solver.

    This is a gate, rather than a claim that arbitrary scientific prose is
    private.  It catches stable identifiers and direct result statements that
    make the original paper or implementation searchable.  Review evidence is
    never passed to this function.
    """
    if not isinstance(text, str):
        return ['public content is not text']
    errors = []
    for pattern, message in _LEAKAGE_RULES:
        match = pattern.search(text)
        if match:
            sample = re.sub(r'\s+', ' ', match.group(0)).strip()[:100]
            location = f' in {relative_path}' if relative_path else ''
            errors.append(f'{message}{location}: {sample}')
    return errors


def public_identity_errors(markdown: str, config: dict[str, Any]) -> list[str]:
    errors = []
    for key in ('task_id', 'canonical_id'):
        value = config.get(key)
        if isinstance(value, str) and value and re.search(
                r'(?<![A-Za-z0-9_-])' + re.escape(value) + r'(?![A-Za-z0-9_-])', markdown):
            errors.append(f'public content exposes internal {key}')
    if re.search(r'paper-(?:doi|arxiv|title)-[a-f0-9]{16,}', markdown):
        errors.append('public content exposes an internal paper identifier')
    return errors


def _visible_lines(markdown: str):
    """Examples and comments are not authoritative public declarations."""
    # Retain line numbers so framework wiring can be placed under an authored
    # heading without treating headings in examples or HTML comments as real.
    markdown = re.sub(r'<!--.*?-->', lambda m: '\n' * m[0].count('\n'), markdown, flags=re.S)
    if '<!--' in markdown:
        raise ValueError('unclosed HTML comment')
    fence = None
    for index, line in enumerate(markdown.splitlines()):
        token = re.match(r'^\s*(`{3,}|~{3,})', line)
        if token:
            marker = token.group(1)
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            continue
        if fence is None:
            yield index, line
    if fence:
        raise ValueError('unclosed Markdown code fence')


_HEADING = re.compile(r'^(#{1,6})\s+(.+?)\s*#*\s*$')


def section_heading_line(markdown: str, title: str) -> int | None:
    for index, line in _visible_lines(markdown):
        match = _HEADING.match(line)
        if match and match.group(2) == title:
            return index
    return None


def parse_sections(markdown: str, *, additional_titles=()) -> list[tuple[str, str]]:
    sections = []
    title, body, level = None, [], None
    main_titles = set(REQUIRED_INSTRUCTION_HEADINGS) | {'General Solver Guidance', *additional_titles}
    for _, line in _visible_lines(markdown):
        heading = _HEADING.match(line)
        if heading and (title is None or len(heading.group(1)) <= level or heading.group(2) in main_titles):
            if title is not None:
                sections.append((title, '\n'.join(body)))
            title, body, level = heading.group(2), [], len(heading.group(1))
        elif title is not None:
            body.append(line)
    if title is not None:
        sections.append((title, '\n'.join(body)))
    return sections


def _plain(value: str) -> str:
    return value.strip().strip('`').strip().removeprefix('**').removesuffix('**').strip()


def _cells(line: str) -> list[str] | None:
    line = line.strip()
    if not line.startswith('|') or not line.endswith('|'):
        return None
    cells = [_plain(cell) for cell in re.split(r'(?<!\\)\|', line)[1:-1]]
    if cells and all(re.fullmatch(r':?-+:?', value) for value in cells):
        return None
    return [cell.replace(r'\|', '|') for cell in cells]


def expected_public_facts(config: dict[str, Any]) -> dict[str, str]:
    """Machine-readable values only; never write scientific prose or Markdown."""
    runtime, submission = config.get('runtime', {}), config.get('submission', {})
    estimate = config.get('resource_estimate', {})
    facts = {}
    for label, source, key in (
            ('CPU cores', runtime, 'cpu'), ('Memory (MiB)', runtime, 'memory_mb'),
            ('GPUs', runtime, 'gpus'), ('GPU model', estimate, 'gpu_model'),
            ('Data directory', runtime, 'data_dir'), ('Network', runtime, 'network_policy'),
            ('Time limit (seconds)', runtime, 'time_limit_sec'),
            ('Expected time (minutes)', estimate, 'expected_solver_minutes'),
            ('Submission path', submission, 'path')):
        if key in source:
            facts[label] = str(source[key])
    facts['Search'] = str(runtime.get('search_policy', 'none'))
    if runtime.get('network_policy') == 'allowlisted':
        facts['Allowed hosts'] = ', '.join(runtime.get('allowed_hosts', []))
    return facts


def _artifact_rows(sections: list[tuple[str, str]]) -> list[dict[str, str]]:
    result = []
    for title, body in sections:
        if title not in ARTIFACT_SECTIONS:
            continue
        header = None
        for line in body.splitlines():
            cells = _cells(line)
            if cells is None:
                if line.strip() and not line.strip().startswith('|'):
                    header = None
                continue
            lowered = [value.lower() for value in cells]
            if lowered == ['file or pattern', 'count', 'format', 'fields']:
                header = lowered
                continue
            if header and len(cells) == len(header):
                result.append(dict(zip(header, cells)))
    return result


def _artifact_errors(sections: list[tuple[str, str]], config: dict[str, Any]) -> list[str]:
    declarations = config.get('submission', {}).get('required_artifacts', [])
    rows = _artifact_rows(sections)
    by_path = {}
    for row in rows:
        by_path.setdefault(row['file or pattern'], []).append(row)
    expected_paths = set()
    errors = []
    for item in declarations if isinstance(declarations, list) else []:
        if not isinstance(item, dict):
            continue  # Configuration validation explains malformed declarations.
        paths = item.get('paths')
        pattern = item.get('pattern')
        names = paths if isinstance(paths, list) else ([pattern] if isinstance(pattern, str) else [])
        for name in names:
            if not isinstance(name, str):
                continue
            public_name = config.get('submission', {}).get('path', '') if name == '{submission}' else name
            expected_paths.add(public_name)
            matches = by_path.get(public_name, [])
            if len(matches) != 1:
                errors.append(f'instruction deliverables must declare {public_name} exactly once in the File or pattern table')
                continue
            row = matches[0]
            count = '1' if isinstance(paths, list) else str(item.get('count', 'at least one'))
            if row['count'] != count:
                errors.append(f'instruction artifact {public_name}: Count must be {count}')
            expected_format = str(item.get('format', 'any'))
            if row['format'].lower() != expected_format.lower():
                errors.append(f'instruction artifact {public_name}: Format must be {expected_format}')
            expected_fields = item.get('columns', item.get('json_required_keys', []))
            if row['fields'].startswith('['):
                try:
                    fields = json.loads(row['fields'])
                    if not isinstance(fields, list) or not all(isinstance(value, str) for value in fields):
                        raise ValueError('expected a string array')
                except (ValueError, TypeError):
                    errors.append(f'instruction artifact {public_name}: Fields JSON must be an array of strings')
                    continue
            else:
                fields = [_plain(value) for value in row['fields'].split(',')] if row['fields'] not in {'-', 'none'} else []
            if 'columns' in item:
                equal = fields == expected_fields
            else:
                equal = len(fields) == len(set(fields)) and set(fields) == set(expected_fields)
            if not equal:
                errors.append(f'instruction artifact {public_name}: Fields differ from required columns or JSON keys '
                              f'(declared {len(fields)}, first fields {fields[:4]!r}; '
                              f'contract {len(expected_fields)}, first fields {expected_fields[:4]!r})')
    for path in sorted(by_path.keys() - expected_paths):
        errors.append(f'instruction declares an output that the evaluator does not check: {path}')
    return errors


def _declared_command_errors(lines, runtime, evaluation):
    """Check explicitly labelled executable examples even if correct wiring is also supplied."""
    errors = []
    for line in lines:
        declaration = re.match(r'^\s*[-*|\s]*(?P<label>(?:solver |runtime )?entrypoint|'
                               r'(?:formal |public )?evaluator command|public validation command|'
                               r'how to invoke the evaluator)\s*[*`]*(?:[:=|])\s*[* ]*(?P<value>.*)',
                               line, re.I)
        if not declaration:
            continue
        label = declaration['label'].lower()
        value = declaration['value'].strip().strip('|').strip()
        commands = re.findall(r'`([^`]+)`', value) or [value]
        for command in commands:
            if not re.match(r'^(?:python[\d.]*|bash|sh|/\S+)\s+', command):
                continue
            try:
                argv = shlex.split(command)
            except ValueError:
                errors.append(f'instruction has invalid command quoting for {label}')
                continue
            if 'entrypoint' in label:
                expected = shlex.split(runtime['entrypoint'])
            elif 'public' in label or (label == 'how to invoke the evaluator' and 'public' in value.lower()):
                expected = evaluation.get('public_validation', {}).get('command', [])
                # A prose example can name the script and describe its arguments separately.
                if expected and len(argv) == 2 and argv[1:] == expected[1:2]:
                    continue
            else:
                expected = evaluation.get('command', [])
            if argv != expected:
                errors.append(f'instruction has conflicting {label}: {command}')
    return errors


def solver_contract_errors(markdown: str, contract: dict[str, Any] | None) -> list[str]:
    if not isinstance(contract, dict):
        return ['task_contract.json is missing or not an object']
    errors = []
    # Markdown code blocks can contain the actual invocation. Comments cannot
    # stand in for public instructions.
    visible = re.sub(r'<!--.*?-->', '', markdown, flags=re.S)
    lines = visible.splitlines()
    for item in contract.get('inputs', []):
        path = item['path']
        rows = [row for row in lines if path in row]
        if not rows:
            errors.append(f'instruction missing input path: {path}')
        elif item.get('package_path'):
            required = 'required' if item['required'] else 'optional'
            opposite = 'optional' if item['required'] else 'required'
            if any(re.search(r'\b' + opposite + r'\b', row, re.I)
                   and not re.search(r'\b' + required + r'\b', row, re.I) for row in rows):
                errors.append(f'instruction has conflicting input status for {path}; expected {required}')
            if not any(re.search(r'\b' + required + r'\b', row, re.I)
                       and not re.search(r'\b' + opposite + r'\b', row, re.I)
                       for row in rows):
                errors.append(f'instruction must mark {path} {required} on its input row')
    runtime = contract['runtime']
    for variable in ('DATA_DIR', 'OUTPUT_DIR'):
        # Keep the binding on one line. Authors use shell assignments, Markdown
        # definition lists and two-column operating facts tables.
        binding = re.compile(r'\b' + variable + r'\b[ `*)]*[=:|][ `*]*(/[^\s`*,;|)]+)')
        paths = [value.rstrip(':') for line in lines for value in binding.findall(line)]
        if runtime[variable] not in paths:
            errors.append(f'instruction must bind {variable}={runtime[variable]}')
        if any(value != runtime[variable] for value in paths):
            errors.append(f'instruction has conflicting {variable} bindings; expected {runtime[variable]}')
    if runtime['entrypoint'] not in visible or runtime['contract_path'] not in visible:
        errors.append('instruction must identify the solver entrypoint and the solver contract path')
    evaluation = contract['evaluation']
    errors.extend(_declared_command_errors(lines, runtime, evaluation))
    outputs = {item['path'] for item in contract.get('outputs', [])}
    for line in lines:
        binding = re.match(r'^\s*[-*|\s]*(?:submission path|submit(?: to)?)\s*[*:|= `]*(/[^`\s|)]+)', line, re.I)
        if binding and outputs and binding[1].rstrip('.,:') not in outputs:
            errors.append(f'instruction has conflicting submission path: {binding[1]}')
    if evaluation.get('mode') == 'automatic_private_verifier':
        if not re.search(r'(?i)automatic\s+(?:private\s+)?verif(?:ication|ier)', visible):
            errors.append('instruction must describe automatic private verification after submission')
    elif 'evaluation.command' not in visible:
        argv = evaluation['command']
        if not any(all(word in row for word in argv) for row in lines):
            errors.append('instruction must give the formal argv or reference task_contract.json evaluation.command')
    known_paths = {item['path'] for item in contract['inputs']}
    public_validation = evaluation.get('public_validation', {})
    known_paths.update(arg for key in ('command', 'baseline_command', 'input_paths')
                       for arg in public_validation.get(key, []) if arg.startswith('/'))
    for path in re.findall(r'(?<![\w./-])(/[^\s\x60\x22\x27<>,;|)]+)', visible):
        path = path.rstrip('.:')
        data_dir = runtime['data_dir'].rstrip('/')
        if path.startswith(data_dir + '/') and path not in known_paths and not any(
                item['format'] == 'directory' and path.startswith(item['path'].rstrip('/') + '/')
                for item in contract['inputs']):
            errors.append(f'instruction references undeclared data path: {path}')
    return list(dict.fromkeys(errors))


def validate_instruction(markdown: str, config: dict[str, Any], protocol: dict | None = None) -> list[str]:
    if not isinstance(markdown, str) or not markdown.strip():
        return ['instruction.md is empty']
    errors = []
    if re.search(r'\{\{\s*[A-Z][A-Z0-9_]*', markdown):
        errors.append('instruction.md contains unresolved placeholders')
    try:
        sections = parse_sections(markdown)
    except ValueError as error:
        return [str(error)] + public_identity_errors(markdown, config)
    values = {}
    for title in ('Scientific Background', 'Research Objective'):
        if any(heading == title and not body.strip() for heading, body in sections):
            errors.append(f'instruction {title} needs task-specific scientific text')
    expected = expected_public_facts(config)
    for title, body in sections:
        if title not in FACT_SECTIONS:
            continue
        for line in body.splitlines():
            cells = _cells(line)
            if cells and len(cells) == 2 and cells[0] in expected:
                values.setdefault(cells[0], []).append(cells[1])
    for label, value in expected.items():
        found = values.get(label, [])
        if len(found) != 1:
            errors.append(f'instruction must declare {label} exactly once in its operating facts table')
        elif found[0] != value:
            errors.append(f'instruction {label} differs from execution configuration: expected {value}')
    errors.extend(_artifact_errors(sections, config))
    if config.get('assessment_contract_version') == 1 and config.get('framework_generation_version') == 1:
        required_contract_tokens = {
            'task_contract.json': r'(?i)task_contract\.json',
            'DATA_DIR': r'(?i)\bDATA_DIR\b',
            'OUTPUT_DIR': r'(?i)\bOUTPUT_DIR\b',
            'runtime entrypoint': r'(?i)python\s+/workspace/run\.py',
            'evaluator command': r'(?i)(?:automatic\s+(?:private\s+)?verif(?:ication|ier)|evaluation\.command|scoring_entry\.py|evaluation_protocol\.py|test\.sh|verifier\.py)',
            'success condition': r'(?i)(?:success condition|status\s*=\s*success|successful evaluation)',
            'required/optional': r'(?i)(?:required|optional)',
        }
        for label, pattern in required_contract_tokens.items():
            if not re.search(pattern, markdown):
                errors.append(f'instruction must include solver contract field: {label}')
        input_section = '\n'.join(body for title, body in sections if title == 'Input')
        if not input_section.strip() or not re.search(r'(?i)(path|purpose|format|field|schema)', input_section):
            errors.append('instruction Input must state each input path, purpose, format and fields')
        evaluation_section = '\n'.join(body for title, body in sections if title == 'Evaluation')
        if not re.search(r'(?i)(automatic\s+(?:private\s+)?verif(?:ication|ier)|command|invoke|run|python)', evaluation_section):
            errors.append('instruction Evaluation must state how to invoke the evaluator')
    if protocol is not None:
        evaluation = '\n'.join(body for title, body in sections if title == 'Evaluation')
        for key in ('reward_metric', 'reward_direction'):
            value = protocol.get(key)
            if isinstance(value, str) and value:
                accepted = [value]
                if key == 'reward_direction' and value in {'lower_is_better', 'higher_is_better'}:
                    accepted.append(value.replace('_', ' '))
                if not any(re.search(r'(?<![\w-])' + re.escape(text) + r'(?![\w-])', evaluation,
                                     flags=re.I if key == 'reward_direction' else 0) for text in accepted):
                    errors.append(f'instruction Evaluation must state {key}: {value}')
    errors.extend(public_identity_errors(markdown, config))
    return errors
