"""Check fixed-source deliverables and explicit public final-score formulas."""
from fnmatch import fnmatchcase
from math import isclose
from pathlib import PurePosixPath
import re

from instruction_checks import _visible_lines


def fixed_deliverables(markdown):
    """Read top-level list items under an explicitly labelled deliverables heading."""
    result = []
    section = None
    optional_section = None
    list_indent = None
    for index, line in _visible_lines(markdown):
        heading = re.match(r'^\s*(#{1,6})\s+(.+?)\s*#*\s*$', line)
        if heading:
            depth = len(heading[1])
            if optional_section is not None and depth <= optional_section:
                optional_section = None
            if section is not None and len(heading[1]) <= section:
                section = None
            if re.fullmatch(r'(?:required\s+)?(?:deliverables|submission files|required outputs)|'
                            r'(?:必需|必须提交的)?交付物|提交文件', heading[2], re.I):
                section = len(heading[1])
                list_indent = None
            elif section and re.search(r'\boptional\b|可选', heading[2], re.I):
                optional_section = depth
            continue
        item = re.match(r'^(\s*)(?:\d+[.)]|[-*+])\s+(.+)', line)
        if not item or section is None or optional_section:
            continue
        if list_indent is None:
            list_indent = len(item[1].expandtabs())
        if len(item[1].expandtabs()) != list_indent:
            continue
        text = item[2].strip()
        if re.search(r'^\**(?:optional\b|可选)|[(（]\s*(?:optional\b|可选)|'
                     r'\b(?:is optional|if desired)\b|无需提交|可选[）)]', text, re.I):
            continue
        result.append({'source_line': index + 1, 'quote': text})
    return result


def _member_names(artifact, submission):
    return [submission if p == '{submission}' else p for p in artifact.get('paths', [])] or [artifact.get('pattern', '')]


def protocol_member_errors(submission, protocol):
    declared = submission.get('required_artifacts', [])
    if protocol.get('required_artifacts') != declared:
        return ['actual scoring config must enforce all declared required artifacts unchanged']
    errors = []
    for unit in protocol.get('units', []):
        if not isinstance(unit, dict):
            continue  # The formal protocol validator reports an invalid evaluation unit.
        path = unit.get('path')
        if isinstance(path, str) and path != '{submission}' and not any(
                path in item.get('paths', []) or (item.get('pattern') and fnmatchcase(path, item['pattern']))
                for item in declared):
            errors.append(f'formal evaluator unit {unit.get("id")} reads undeclared submission member {path}')
    return errors


def deliverable_errors(expected, bindings, markdown, submission, protocol):
    if not expected:
        return []
    if not isinstance(bindings, list) or len(bindings) != len(expected):
        return ['fixed deliverables require a checked artifact and public quote for each original item: ' +
                ', '.join(str(row['source_line']) for row in expected)]
    declared = {a['id']: a for a in submission.get('required_artifacts', [])}
    enforced = {a['id']: a for a in protocol.get('required_artifacts', [])}
    errors = []
    for original, row in zip(expected, bindings):
        prefix = f"fixed deliverable at source line {original['source_line']}"
        if not isinstance(row, dict) or any(row.get(k) != v for k, v in original.items()):
            errors.append(prefix + ' must preserve its original line and quote')
            continue
        artifact_id, quote = row.get('artifact_id'), row.get('instruction_quote')
        artifact = declared.get(artifact_id) if isinstance(artifact_id, str) else None
        if not artifact or enforced.get(artifact_id) != artifact:
            errors.append(prefix + ' must bind an artifact enforced in the formal protocol')
            continue
        paths = _member_names(artifact, submission.get('path', ''))
        if not isinstance(quote, str) or not quote.strip() or quote not in markdown:
            errors.append(prefix + ' must quote Solver instructions naming the bound submission member')
            continue
        if not any(p and p in quote for p in paths):
            errors.append(prefix + ' must quote Solver instructions naming the bound submission member')
        field = row.get('field')
        if field is not None and (not isinstance(field, str) or field not in
                [*artifact.get('columns', []), *artifact.get('json_required_keys', [])] or field not in quote):
            errors.append(prefix + ' must bind the cited field to a formally checked column or JSON key')
        output_phrase = re.split(r'\s+(?:from|using|based on|derived from)\s+', original['quote'],
                                 maxsplit=1, flags=re.I)[0]
        source_files = re.findall(r'(?<![/\w])([A-Za-z0-9_./*-]+\.(?:jsonl?|csv|tsv|bin|zip|md|pdf|txt|npy|npz|h5ad|parquet))\b',
                                  output_phrase, re.I)
        def matches(name, member):
            if member == submission.get('path') and '/' not in name:
                return name == PurePosixPath(member).name
            return fnmatchcase(name, member)
        if source_files and not any(matches(name, p) for name in source_files for p in paths):
            errors.append(prefix + ' names a file outside its bound formal artifact: ' + ', '.join(source_files))
        named_members = [p for item in enforced.values() for p in _member_names(item, submission.get('path', ''))]
        if not re.search(r'\bor\b|或', output_phrase, re.I):
            for name in source_files:
                if not any(matches(name, p) for p in named_members):
                    errors.append(prefix + ' names an unenforced submission member: ' + name)
        if re.search(r'\b(?:methodology|methods description|written explanation)\b|方法说明',
                     original['quote'], re.I) and not field and not any(
                         PurePosixPath(p).suffix.lower() in {'.md', '.txt', '.pdf', '.rst', '.docx', '.ipynb', '.html'}
                         for p in paths) and artifact.get('format') != 'text':
            errors.append(prefix + ' requires a written explanation in a checked document or text field')
    return errors


_FINAL_SCORE = re.compile(r'\b(?:the\s+)?(?:final|overall|benchmark)\s+(?:score|reward)\s*'
                          r'(?:is(?:\s+(?:computed|calculated|defined)\s+as)?|equals|=)\s+(.+)', re.I)


def _stated_components(expression, weights):
    # Collapse a public display name such as "recovery rate" to its metric ID.
    for key in sorted(weights, key=len, reverse=True):
        spelling = re.escape(key).replace('_', r'[\s_-]+')
        expression = re.sub(r'(?<![a-z0-9])' + spelling + r'(?![a-z0-9])',
                            lambda _: key, expression, flags=re.I)
    group_mean = re.fullmatch(r'(?:the\s+)?(?:(?:arithmetic\s+)?(?:mean|average)(?:\s+of)?\s+)?'
                              r'([A-Za-z][\w-]*)(?:\s+(?:across|over|among)\s+[^+*=]+)?', expression, re.I)
    if group_mean and group_mean[1] in weights:
        return {group_mean[1]: 1.0}
    # Only interpret a complete additive expression. A prose aside mentioning a
    # percentage or a comparison with a published score is not a score formula.
    expression = re.sub(r'\s+and\s+', ' + ', expression, flags=re.I).replace('×', '*')
    number = r'(?:\d+(?:\.\d*)?|\.\d+)'
    term = re.compile(r'\s*([+-]?)\s*(?:(' + number + r')\s*(%?)\s*(?:\*|of\b)?\s*)?'
                      r'([A-Za-z][\w-]*)(?:\s*\*\s*(' + number + r')\s*(%?))?\s*', re.I)
    result = {}
    position = 0
    while position < len(expression):
        match = term.match(expression, position)
        if not match or (position and not match[1]) or (match[2] and match[5]):
            return None
        key = match[4]
        coefficient = float(match[2] or match[5] or 1)
        if match[3] or match[6]:
            coefficient /= 100
        if match[1] == '-':
            coefficient = -coefficient
        result[key] = result.get(key, 0) + coefficient
        position = match.end()
    return result if result.keys() & weights.keys() else None


def score_formula_errors(markdown, protocol):
    """Reject unambiguous equalities in Solver prose, including explicit arithmetic."""
    weights = protocol.get('reward_components') or {protocol.get('reward_metric'): 1}
    weights = {k: v for k, v in weights.items() if isinstance(k, str) and v}
    if not weights:
        return []
    errors = []
    for index, line in _visible_lines(markdown):
        if line.lstrip().startswith(('|', '>')):
            continue
        match = _FINAL_SCORE.search(line.replace(chr(96), '').replace('**', ''))
        if not match:
            continue
        expression = re.split(r'[.!?](?:\s|$)', match[1], maxsplit=1)[0].strip()
        stated = _stated_components(expression, weights)
        if stated and any(not isclose(stated.get(k, 0), weights.get(k, 0), rel_tol=1e-9, abs_tol=1e-12)
                          for k in stated.keys() | weights.keys()):
            errors.append(f'line {index + 1}: final-score prose specifies {stated}, '
                          f'while the formal evaluator uses {weights}')
    return errors
