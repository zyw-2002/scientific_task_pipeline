"""Evidence binding for scope v2. Reads materials; never executes authored code.

This gate proves coverage, freshness and links to scored controls. Whether an
adaptation preserves a scientific problem is still a source-based review judgment.
"""
from __future__ import annotations
import argparse
import csv
import json
from pathlib import Path
from resource_contract import digest, relative
from contract import package_layout, package_path


def input_hashes(package, *, exclude_paths=()):
    """Bind code, instructions, bundled data and recipes, excluding review receipts."""
    from contract import RECEIPT_FILES
    package = Path(package)
    paths = sorted(package.rglob('*'))
    result = {}
    evidence = package / 'common_task/evaluation/checks/evidence'
    for path in paths:
        if any(path.is_relative_to(package / name) for name in exclude_paths):
            continue
        if (path.parent == package and path.name in RECEIPT_FILES | {'review_report.json'}) or path.is_relative_to(evidence) or '__pycache__' in path.parts or path.suffix == '.pyc':
            continue
        if path.is_symlink(): raise ValueError('audit input cannot be a symlink: ' + str(path))
        if path.is_file(): result[path.relative_to(package).as_posix()] = digest(path)
    return result


def public_document_errors(package, spec, reviews):
    """Compare claims transcribed by the reviewer to the resource and output manifests."""
    layout = package_layout(spec)
    files = {layout[k]: package_path(package, layout[k])
             for k in ('instruction_path', 'readme_path', 'data_description_path', 'data_sources_path')}
    files = {name: path for name, path in files.items() if path.is_file()}
    if not isinstance(reviews, dict) or set(reviews) != set(files):
        return ['public_documents must review each present instruction, README, data description and source manifest']

    # Fixture IDs and formal IDs remain distinct even if their column names coincide.
    facts = {}
    directories = {}
    required = set()
    for source in spec['data']['sources']:
        subject = 'resource:' + source['id']
        facts[subject] = {**source['content'], **{k: source[k] for k in
            ('origin', 'role', 'availability', 'identity', 'version') if k in source}}
        if source['content'].get('format') == 'directory' and source.get('visibility', 'public') == 'public':
            directories[subject] = source['package_path']
        if source['role'] == 'input' and source.get('visibility', 'public') == 'public':
            required.add((subject, 'origin'))
            for key in ('row_count', 'shape', 'item_count', 'members'):
                if key in source['content']:
                    required.add((subject, key))
    for artifact in spec['submission']['required_artifacts']:
        subject = 'artifact:' + artifact['id']
        facts[subject] = artifact
        for key in ('columns', 'json_required_keys'):
            if key in artifact:
                required.add((subject, key))

    errors = []
    reviewed = set()
    csv_extents = {}
    for name, path in files.items():
        doc = reviews[name]
        if not isinstance(doc, dict) or doc.get('verdict') != 'consistent':
            errors.append(f'{name}: unresolved public documentation conflict')
            continue
        claims = doc.get('claims')
        rationale = doc.get('rationale')
        if not isinstance(claims, list) or not isinstance(rationale, str) or not rationale.strip():
            errors.append(f'{name}: record the claims and why they agree, including any fixture/formal distinction')
            continue
        contents = path.read_text(encoding='utf-8')
        for claim in claims:
            if not isinstance(claim, dict):
                errors.append(f'{name}: public claim must be an object')
                continue
            quote, subject, field = (claim.get(k) for k in ('quote', 'subject', 'field'))
            if not isinstance(quote, str) or not quote.strip() or quote not in contents:
                errors.append(f'{name}: quoted public claim is absent')
                continue
            if not isinstance(subject, str) or not isinstance(field, str):
                errors.append(f'{name}: public claim needs an existing resource or artifact and field')
                continue
            if 'member' in claim:
                member = claim['member']
                if subject not in directories or field != 'csv_shape' or not relative(member) or not member.lower().endswith('.csv'):
                    errors.append(f'{name}: csv_shape needs a CSV member of a public directory resource')
                    continue
                try:
                    item = package_path(package, str(Path(directories[subject]) / member))
                    if item not in csv_extents:
                        with item.open(encoding='utf-8-sig', newline='') as stream:
                            reader = csv.reader(stream)
                            header = next(reader, [])
                            csv_extents[item] = [sum(1 for row in reader if row), len(header)]
                    expected = csv_extents[item]
                except (OSError, ValueError, csv.Error) as exc:
                    errors.append(f'{name}: cannot check bundled CSV member {member}: {exc}')
                    continue
            elif field in facts.get(subject, {}):
                expected = facts[subject][field]
            else:
                errors.append(f'{name}: public claim needs an existing resource or artifact and field')
                continue
            value = claim.get('value')
            # CSV columns are positional; JSON object key order is immaterial.
            if field == 'json_required_keys' and isinstance(value, list) and all(isinstance(k, str) for k in value):
                value, expected = sorted(value), sorted(expected)
            if 'value' not in claim or json.dumps(value, sort_keys=True) != json.dumps(expected, sort_keys=True):
                errors.append(f'{name}: public {subject}.{field} differs from the manifest or bundled content')
            reviewed.add((subject, field))
    for subject, field in sorted(required - reviewed):
        errors.append(f'public_documents lack a cited public declaration of {subject}.{field}')
    return errors


def audit_errors(package, spec, report):
    if report.get('status') != 'complete': return []
    package = Path(package).resolve()
    audit = report.get('construction_audit')
    if not isinstance(audit, dict): return ['scope v2 requires construction_audit in review_report']
    errors = []
    if audit.get('input_sha256') != input_hashes(package):
        errors.append('construction audit inputs changed/missing; re-review current instructions, data and preparation/scoring code')
    errors.extend(public_document_errors(package, spec, audit.get('public_documents')))
    protocol = json.loads((package / 'common_task/evaluation/protocol.json').read_text())
    requirements = {r['id']: r for r in protocol['assessment']['requirements']}
    core = {rid for rid, r in requirements.items() if r['role'] in {'quality', 'trajectory'}}
    resources = {r['id']: r for r in spec['data']['sources'] if r['role'] in {'input', 'reference'}}

    def text(row, key, where):
        if not isinstance(row.get(key), str) or not row[key].strip(): errors.append(f'{where}: needs {key}')

    def citations(rows, where):
        if not isinstance(rows, list) or not rows:
            errors.append(f'{where}: needs file evidence with path, sha256 and quote/locator'); return
        for ref in rows:
            try:
                if not isinstance(ref, dict) or not relative(ref.get('path')): raise ValueError('unsafe/missing evidence path')
                p = package / ref['path']
                if not p.resolve().is_relative_to(package) or not p.is_file() or any(v.is_symlink() for v in (p, *p.parents)):
                    raise ValueError('missing/escaping evidence file')
                if ref.get('sha256') != digest(p): raise ValueError('evidence file changed')
                quote = ref.get('quote')
                if quote is not None:
                    if not isinstance(quote, str) or not quote.strip() or quote not in p.read_text():
                        raise ValueError('quoted evidence is absent from file')
                else:
                    text(ref, 'locator', where)
            except (OSError, ValueError, TypeError) as exc: errors.append(f'{where}: {exc}')

    def entries(key):
        value = audit.get(key)
        if not isinstance(value, list): errors.append(f'construction audit needs {key} array'); return []
        if any(not isinstance(v, dict) for v in value):
            errors.append(f'{key}: entries must be objects'); return []
        return value

    def ids(row, key, known, where):
        value = row.get(key)
        if not isinstance(value, list) or any(not isinstance(v, str) or v not in known for v in value):
            errors.append(f'{where}: {key} must reference declared IDs'); return set()
        return set(value)

    scope_resources, scope_requirements = set(), set()
    for i, row in enumerate(entries('scope_decisions')):
        where = f'scope_decisions[{i}]'
        scope_resources |= ids(row, 'resource_ids', resources, where)
        scope_requirements |= ids(row, 'requirement_ids', requirements, where)
        for key in ('retained_capability', 'removed_capability', 'justification', 'difficulty_effect'):
            text(row, key, where)
        citations(row.get('source_evidence'), where)
        if row.get('verdict') != 'justified': errors.append(f'{where}: unresolved scope reduction')
    if scope_resources != resources.keys(): errors.append('scope decisions must cover every formal input/reference')
    if not core <= scope_requirements: errors.append('scope decisions must cover all quality/trajectory requirements')

    reviewed = set()
    for row in entries('data_paths'):
        rid = row.get('resource_id')
        if not isinstance(rid, str) or rid not in resources or rid in reviewed:
            errors.append('data_paths must uniquely cover formal input/reference resources'); continue
        reviewed.add(rid)
        source = resources[rid]
        recipe = source.get('prepare', {})
        for key, expected in (('content', source['content']), ('availability', source['availability']),
                              ('prepare_command', recipe.get('command', [])), ('validate_command', recipe.get('validate', []))):
            if row.get(key) != expected: errors.append(f'{rid}: reviewed {key} differs from actual execution.data.sources')
        for key in ('consistency_reason', 'missing_data_behavior'):
            text(row, key, rid)
        citations(row.get('code_evidence'), rid)
        content = source['content']
        extent_key = {'csv': 'row_count', 'tsv': 'row_count', 'json': 'item_count', 'npy': 'shape', 'directory': 'members'}.get(content['format'])
        if row.get('extent_check') == 'content':
            if not extent_key or extent_key not in content:
                errors.append(f'{rid}: content does not enforce formal extent; declare counts/shape/members or cite a custom validator')
        elif row.get('extent_check') == 'custom_validator':
            if not recipe.get('validate'): errors.append(f'{rid}: custom extent check needs an actual prepare.validate command')
            citations(row.get('extent_evidence'), rid + ' extent validator')
        else: errors.append(f'{rid}: extent_check must be content/custom_validator')
        if row.get('verdict') != 'consistent': errors.append(f'{rid}: unresolved formal data/preparation inconsistency')
    if reviewed != resources.keys(): errors.append('data_paths must cover every formal input/reference')

    smoke_binding = spec.get('verification', {}).get('scoring_smoke', {})
    smoke_path = package / smoke_binding.get('config_path', 'common_task/evaluation/scoring_smoke.json')
    smoke = json.loads(smoke_path.read_text())
    cases = {c['id']: c for c in smoke['cases']}
    covered = set()
    used_cases = set()
    for i, row in enumerate(entries('shortcut_checks')):
        where = f'shortcut_checks[{i}]'
        rids = ids(row, 'requirement_ids', requirements, where)
        covered |= rids
        for key in ('mechanism', 'reason'):
            text(row, key, where)
        citations(row.get('code_evidence'), where)
        disposition = row.get('disposition')
        if disposition == 'not_applicable':
            # A source/code judgment is allowed when a diagnostic cannot sensibly
            # exercise this objective. It is not evidence of full-data robustness.
            if row.get('case_id') is not None: errors.append(f'{where}: not_applicable cannot claim an executed case')
            continue
        case = cases.get(row.get('case_id'))
        if not case or not rids or not rids <= set(case.get('requirement_ids', [])):
            errors.append(f'{where}: must bind a real smoke case covering the reviewed requirements'); continue
        used_cases.add(case['id'])
        if disposition == 'legitimate_method':
            if case['kind'] == 'invalid': errors.append(f'{where}: legitimate method must produce a valid scored submission')
        elif disposition == 'rejected':
            if case['kind'] != 'invalid' or not case.get('expected_error_contains'):
                errors.append(f'{where}: bypass rejection needs an invalid control and specific cause')
        elif disposition == 'bounded_score':
            if case['kind'] == 'invalid' or not (case.get('reward_bounds') or case.get('metric_bounds')):
                errors.append(f'{where}: bypass needs an executed score bound; success alone is insufficient')
            text(row, 'bound_reason', where)
        else: errors.append(f'{where}: unresolved shortcut or unknown disposition')
    if not core <= covered: errors.append('shortcut checks must cover all quality/trajectory requirements, or explain non-applicability')
    if used_cases:
        check = next((c for c in report.get('checks', []) if c.get('name') == smoke_binding.get('runtime_check')), {})
        if check.get('status') != 'pass' or not check.get('artifacts'):
            errors.append('shortcut controls require preserved passing scoring-smoke evidence; findings_errors verifies actual outcomes and freshness')
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, help='Default: current editable output from authoring paths')
    args = parser.parse_args()
    if args.package is None:
        from authoring_package import evaluation_context
        args.package, _ = evaluation_context()
    # Emits only bindings. Never creates scientific decisions or declares pass.
    print(json.dumps(input_hashes(args.package), indent=2))


if __name__ == '__main__': main()
