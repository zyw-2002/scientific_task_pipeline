"""Detect accidental CJK prose in newly authored English task artifacts.

This is a narrow mixed-language regression check, not English-language inference.
Original datasets, identifiers, paths and quotations are not translated. Required non-English source literals may use inline code spans.
"""
from pathlib import Path
import json
import re

CJK = re.compile(r'[\u3400-\u9fff]')


def english_prose_errors(text: str) -> list[str]:
    # Preserve literal field names/paths quoted as code in scientific descriptions.
    prose = re.sub(r'`[^`\n]+`', '', text)
    return ['newly authored prose must be English; CJK text found'] if CJK.search(prose) else []


def _values(value, key=''):
    if key in {'paper', 'canonical_id', 'task_id', 'domain', 'path',
               'package_path', 'url', 'source_path', 'source', 'citation', 'quoted_source'}:
        return
    if isinstance(value, dict):
        for name, item in value.items():
            yield from _values(item, name)
    elif isinstance(value, list):
        for item in value:
            yield from _values(item, key)
    elif isinstance(value, str) and not value.startswith(('/', 'https://', 'http://')):
        yield value


def inspect_language(package: Path, spec: dict) -> list[str]:
    if spec.get('output_language') != 'en':
        return []
    from pipeline.stage2_construction.contracts.contract import package_layout, package_path
    layout = package_layout(spec)
    paths = {package_path(package, layout[key]) for key in ('instruction_path', 'method_path')}
    paths.add(package / 'common_task/environment/environment_requirements.md')
    public = package_path(package, layout['public_dir'])
    paths.update(p for p in public.rglob('*') if p.is_file() and p.suffix in {'.md', '.json'})
    paths.update(package / name for name in ('build_report.json', 'review_report.json', 'review_report.md'))
    errors = []
    for text in _values(spec):
        errors.extend('execution: ' + error for error in english_prose_errors(text))
    for path in sorted(paths):
        if not path.is_file():
            continue
        # Raw scientific inputs are source material, even when stored as JSON.
        data = package_path(package, layout['data_path'])
        if path.resolve().is_relative_to(data.resolve()):
            continue
        try:
            content = path.read_text()
            texts = _values(json.loads(content)) if path.suffix == '.json' else [content]
            for text in texts:
                errors.extend(str(path.relative_to(package)) + ': ' + error for error in english_prose_errors(text))
        except (ValueError, UnicodeError) as exc:
            errors.append(str(path.relative_to(package)) + ': cannot inspect authored text: ' + str(exc))
    return sorted(set(errors))
