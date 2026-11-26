"""Reviewed paper anchors and deterministic relative gaps; no task code execution.

Evidence checks establish bindings, not scientific equivalence. Semantic review
must establish that the selected paper experiment matches the formal task.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
from resource_contract import digest, relative

STATUSES = {'comparable', 'unavailable', 'not_comparable'}


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def bindings(package):
    from construction_audit import input_hashes
    spec = json.loads((Path(package) / 'execution.json').read_text())
    # Deferred data is allowed to materialize during execution. Bind its declared
    # identity/recipe in execution.json, and check actual resources at runtime.
    deferred = [s['package_path'] for s in spec.get('data', {}).get('sources', [])
                if s.get('role') in {'input', 'reference'} and s.get('availability') != 'bundled'
                and relative(s.get('package_path'))]
    return {'protocol_sha256': digest(Path(package) / 'common_task/evaluation/protocol.json'),
            'inputs_sha256': fingerprint(input_hashes(package, exclude_paths=deferred))}


def metric_units(protocol, metric):
    units = {u['id'] for u in protocol.get('units', [])}
    return {uid for a in protocol.get('aggregations', []) if a.get('metric') == metric
            for uid in a.get('unit_ids', units)}


def declaration_errors(protocol, *, required=False):
    ref = protocol.get('assessment', {}).get('paper_comparison')
    if ref is None:
        return ['assessment.paper_comparison is required; explicitly explain unavailable/not_comparable when needed'] if required else []
    if not isinstance(ref, dict): return ['paper_comparison must be an object']
    errors = []
    if ref.get('status') not in STATUSES: errors.append('paper comparison status must be comparable/unavailable/not_comparable')
    for key in ('reason',):
        if not isinstance(ref.get(key), str) or not ref[key].strip(): errors.append('paper comparison needs ' + key)
    if ref.get('status') != 'comparable': return errors
    for key in ('metric', 'metric_unit', 'evaluation_scope'):
        if not isinstance(ref.get(key), str) or not ref[key].strip(): errors.append('paper comparison needs ' + key)
    if ref.get('direction') not in {'higher_is_better', 'lower_is_better'}: errors.append('paper comparison needs explicit direction')
    if ref.get('mode') == 'aggregate' and ref.get('metric') == protocol.get('reward_metric') and ref.get('direction') != protocol.get('reward_direction'):
        errors.append('paper comparison direction disagrees with the same reward metric')
    mode = ref.get('mode')
    if mode == 'aggregate':
        outputs = {a.get('output') for a in protocol.get('aggregations', [])}
        if protocol.get('reward_components'): outputs.add(protocol.get('reward_metric'))
        if ref.get('metric') not in outputs: errors.append('paper comparison metric must be an actual aggregate output')
        rows = [ref]
        if 'instances' in ref: errors.append('aggregate paper comparison must not include per-unit anchors')
    elif mode == 'per_unit':
        if ref.get('metric') not in {a.get('metric') for a in protocol.get('aggregations', [])}:
            errors.append('per-unit paper metric must be a declared measured metric')
        rows = ref.get('instances')
        if not isinstance(rows, list) or not rows or any(not isinstance(r, dict) for r in rows):
            return errors + ['per_unit paper comparison needs instance objects']
        ids = [r.get('unit_id') for r in rows]
        if any(not isinstance(v, str) for v in ids) or len(set(ids)) != len(ids) or set(ids) != metric_units(protocol, ref.get('metric')):
            errors.append('paper instances must exactly cover formal evaluation unit IDs, without omissions or duplicates')
        if 'paper_value' in ref: errors.append('per_unit comparison must not use a pooled paper_value')
    else: return errors + ['paper comparison mode must be aggregate/per_unit']
    for row in rows:
        if not number(row.get('paper_value')): errors.append('paper_value must be a finite number')
        conversion = row.get('conversion')
        if conversion is not None:
            if (not isinstance(conversion, dict) or not number(conversion.get('reported_value')) or
                    not number(conversion.get('factor')) or conversion['factor'] <= 0 or
                    not isinstance(conversion.get('reported_unit'), str) or not conversion['reported_unit'].strip()):
                errors.append('paper unit conversion needs reported_value, positive factor and reported_unit')
            elif not number(row.get('paper_value')) or not math.isclose(row['paper_value'], conversion['reported_value'] * conversion['factor'], rel_tol=1e-12, abs_tol=1e-12):
                errors.append('paper_value disagrees with explicit unit conversion')
        sources = row.get('sources')
        if not isinstance(sources, list) or not sources:
            errors.append('each paper anchor needs original source evidence'); continue
        for source in sources:
            if not isinstance(source, dict): errors.append('paper source evidence must be an object'); continue
            for key in ('paper', 'locator', 'path', 'sha256', 'quote'):
                if not isinstance(source.get(key), str) or not source[key].strip(): errors.append('paper source needs ' + key)
    return errors


def source_errors(package, protocol):
    errors = []
    ref = protocol.get('assessment', {}).get('paper_comparison', {})
    if not isinstance(ref, dict) or ref.get('status') != 'comparable': return errors
    rows = ref.get('instances', []) if ref.get('mode') == 'per_unit' else [ref]
    package = Path(package).resolve()
    for row in rows:
        for source in row.get('sources', []):
            try:
                if not relative(source.get('path')): raise ValueError('unsafe source path')
                path = package / source['path']
                if not path.resolve().is_relative_to(package) or any(p.is_symlink() for p in (path, *path.parents)):
                    raise ValueError('source must be a package-local nonsymlink file')
                if digest(path) != source['sha256']: raise ValueError('source evidence hash differs')
                if source['quote'] not in path.read_text(): raise ValueError('paper quote is absent from original source excerpt')
            except (OSError, ValueError, KeyError, TypeError) as exc: errors.append('paper source: ' + str(exc))
    return errors


def review_errors(package, spec, report):
    required = spec.get('paper_comparison_version') == 1
    path = Path(package) / 'common_task/evaluation/protocol.json'
    if not path.exists() and not required: return []
    protocol = json.loads(path.read_text())
    errors = declaration_errors(protocol, required=required)
    ref = protocol.get('assessment', {}).get('paper_comparison')
    if errors or ref is None: return errors
    errors += source_errors(package, protocol)
    if report.get('status') != 'complete': return errors
    check = report.get('scientific_checks', {}).get('paper_comparison', {})
    if not isinstance(check, dict) or check.get('status') != 'pass' or check.get('conclusion') != ref['status']:
        errors.append('complete review must verify the declared paper comparison status (including justified unavailability)')
        return errors
    if not isinstance(check.get('evidence'), str) or not check['evidence'].strip(): errors.append('paper comparison review needs experiment/metric/split/unit/aggregation evidence')
    for key, value in bindings(package).items():
        if check.get(key) != value: errors.append('paper comparison review is stale or unbound: ' + key)
    return errors


def reference_snapshot(package, spec):
    """Freeze verified anchors with their protocol/review/source provenance."""
    package = Path(package)
    try:
        path = package / 'common_task/evaluation/protocol.json'
        protocol = json.loads(path.read_text())
        ref = protocol.get('assessment', {}).get('paper_comparison')
        if ref is None: return {'status': 'unavailable', 'reason': 'Legacy task has no reviewed paper comparison.'}
        report = json.loads((package / 'review_report.json').read_text())
        errors = review_errors(package, spec, report)
        if report.get('status') != 'complete': errors.append('paper reference has not completed review')
        if errors: return {'status': 'unverified', 'reason': '; '.join(errors)}
        return {'status': 'verified', 'protocol': protocol, 'protocol_sha256': digest(path),
                'review_sha256': digest(package / 'review_report.json'),
                'review': copy.deepcopy(report['scientific_checks']['paper_comparison']),
                'reference': copy.deepcopy(ref)}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {'status': 'unverified', 'reason': str(exc)}


def calculate(reference, event, *, diagnostic=False):
    """Use only one frozen SCORE_ATTEMPT. Eligibility is decided after validity review."""
    out = {'schema_version': 'pipeline-paper-comparison-result-v1', 'status': 'unavailable',
           'attempt_id': event.get('attempt_id'), 'submission_sha256': event.get('submission_sha256'),
           'g': None, 'eligible_for_paper_comparison': False}
    def stop(status, reason):
        return {**out, 'status': status, 'reason': reason}
    if diagnostic: return stop('diagnostic_only', 'Diagnostic execution is not formal paper comparison.')
    if not isinstance(reference, dict) or reference.get('status') != 'verified':
        return stop('unavailable' if not reference or reference.get('status') == 'unavailable' else 'unverified',
                    (reference or {}).get('reason', 'No verified paper reference.'))
    ref, protocol = reference['reference'], reference['protocol']
    out.update(reference_sha256=fingerprint(reference), protocol_sha256=reference['protocol_sha256'])
    if ref['status'] != 'comparable': return stop(ref['status'], ref['reason'])
    out.update(metric=ref['metric'], metric_unit=ref['metric_unit'], direction=ref['direction'], mode=ref['mode'])
    if ref['mode'] == 'aggregate': out['paper_value'] = ref['paper_value']
    if event.get('status') != 'success': return stop('unscored', 'Selected attempt did not produce a successful scientific score.')
    response = event.get('response')
    if not isinstance(response, dict) or response.get('status') != 'success' or response.get('metrics') != event.get('metrics'):
        return stop('evidence_mismatch', 'Scoring response and selected event disagree.')
    if response.get('protocol_sha256') != reference['protocol_sha256']:
        return stop('protocol_mismatch', 'Scored protocol does not match the reviewed paper reference.')
    proof = response.get('resource_evidence')
    if not isinstance(proof, dict) or proof.get('scope') != 'formal' or proof.get('mode') != 'runtime' or proof.get('instance_sizes'):
        return stop('not_formal', 'Only runtime formal resources can be compared; smoke/authoring results are excluded.')
    resources = protocol.get('resources', {})
    used = proof.get('resources', {})
    required = set(protocol.get('resource_bindings', {}).values()) | {
        a['id_set']['resource'] for a in protocol.get('required_artifacts', []) if a.get('id_set')}
    if (not isinstance(used, dict) or not required <= used.keys() or
            any(not isinstance(r, dict) or rid not in resources or r.get('resource_id') != rid or
                r.get('role') != resources[rid].get('role') or r.get('role') == 'fixture' or
                not isinstance(r.get('sha256'), str) or len(r['sha256']) != 64 or
                (resources[rid].get('sha256') and r['sha256'] != resources[rid]['sha256']) for rid, r in used.items())):
        return stop('resource_mismatch', 'Formal resource identities/checksums are incomplete or inconsistent.')
    metric = ref['metric']
    if ref['mode'] == 'aggregate':
        values = [(None, event.get('metrics', {}).get(metric), ref['paper_value'])]
    else:
        actual = response.get('unit_results')
        if (not isinstance(actual, list) or any(not isinstance(r, dict) or not isinstance(r.get('id'), str) or not isinstance(r.get('metrics'), dict) for r in actual) or
                len({r['id'] for r in actual}) != len(actual) or {r['id'] for r in actual} != {u['id'] for u in protocol['units']}
                or {r['unit_id'] for r in ref['instances']} != metric_units(protocol, metric)):
            return stop('unit_mismatch', 'All paper instances must correspond to exactly one scored evaluation unit.')
        actual = {r['id']: r.get('metrics', {}) for r in actual}
        values = [(r['unit_id'], actual[r['unit_id']].get(metric), r['paper_value']) for r in ref['instances']]
    if ref['mode'] == 'aggregate': out['actual_value'] = values[0][1] if number(values[0][1]) else None
    rows = []
    direction = 1 if ref['direction'] == 'higher_is_better' else -1
    for unit, value, paper in values:
        if not number(value): return stop('missing_metric', 'Selected attempt lacks a finite primary metric.')
        if paper == 0: return stop('zero_reference', 'Relative gap is undefined for a zero paper value; no epsilon or failure score is substituted.')
        gap = direction * (value - paper) / abs(paper)
        if not number(gap): return stop('nonfinite_gap', 'Relative gap is not finite.')
        rows.append({'unit_id': unit, 'actual_value': value, 'paper_value': paper, 'g': gap})
    if ref['mode'] == 'aggregate': out.update(actual_value=rows[0]['actual_value'], paper_value=rows[0]['paper_value'])
    gap = sum(r['g'] / len(rows) for r in rows)
    if not number(gap): return stop('nonfinite_gap', 'Aggregated relative gap is not finite.')
    out.update(status='computed', reason='Paper comparison computed from the locked formal attempt; validity is assessed separately.',
               instances=rows, g=gap, aggregation='identity' if ref['mode'] == 'aggregate' else 'mean_of_instance_gaps')
    return out


def main():
    parser = argparse.ArgumentParser(description='Print binding hashes for a completed paper-reference review; never declare scientific pass.')
    parser.add_argument('--package', type=Path)
    args = parser.parse_args()
    if args.package is None:
        from authoring_package import evaluation_context
        args.package, _ = evaluation_context()
    print(json.dumps(bindings(args.package), indent=2))


if __name__ == '__main__': main()


def source_claim_errors(package, spec, source_bundle, *, review=False):
    """Check public-science claims against the frozen Stage1 materials used by this build."""
    if source_bundle is None:
        if spec.get('scope_contract_version') == 3:
            return ['Main experiments require the frozen primary-source bundle']
        return []
    bundle = Path(source_bundle).resolve()
    manifest_path = bundle/'materials_manifest.json'
    if not manifest_path.is_file():
        return ['Frozen source bundle lacks materials_manifest.json']
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('schema_version') != 'pipline-source-materials-v1':
        if spec.get('scope_contract_version') == 3:
            return ['Main experiments require a frozen primary-source inventory']
        # Earlier standalone input bundles have no hash-indexed primary material.
        return []
    if manifest.get('canonical_id') != spec.get('canonical_id'):
        return ['Source manifest canonical_id differs from execution.json']
    protocol = json.loads((Path(package)/'common_task/evaluation/protocol.json').read_text())
    scope = protocol.get('assessment', {}).get('scope', {})
    scope = scope if isinstance(scope, dict) else {}
    claims = scope.get('source_claims', {})
    dimensions = ('objective', 'scale', 'split', 'metric')
    claim_shape_errors = []
    if not isinstance(claims, dict) or not all(k in claims for k in dimensions):
        claim_shape_errors.append('protocol.json assessment.scope.source_claims must be a JSON object keyed by '
                                  'objective, scale, split and metric, with one claim object per key '
                                  f'(received {type(claims).__name__})')
        if not isinstance(claims, dict):
            claims = {}
    indexed = {a['snapshot_path']: a for a in manifest.get('artifacts', [])
               if isinstance(a, dict) and a.get('status') == 'available' and isinstance(a.get('snapshot_path'), str)}
    from catalog_bindings import catalog_copy_errors
    errors, verified = catalog_copy_errors(spec, bundle), {}
    errors.extend(claim_shape_errors)
    experiments = scope.get('main_experiments', {}) if spec.get('scope_contract_version') == 3 else {}
    experiment_claims = {}
    if spec.get('scope_contract_version') == 3:
        from assessment_contract import main_experiment_errors
        errors.extend(main_experiment_errors(protocol))
        if isinstance(experiments, dict):
            experiment_claims = {f'main experiment {key}': item for key, item in experiments.items()}
    def norm(text):
        return ' '.join(text.split())
    for key, claim in [(key, claims[key]) for key in dimensions if key in claims] + list(experiment_claims.items()):
        if not isinstance(claim, dict) or not isinstance(claim.get('statement'), str) or not claim['statement'].strip():
            errors.append(f'{key}: describe the actual task decision')
            continue
        origin = claim.get('origin')
        if origin in ('task_policy', 'not_specified') and key in ('scale', 'split', 'metric'):
            if not isinstance(claim.get('rationale'), str) or not claim['rationale'].strip():
                errors.append(f'{key}: explain the source gap or adaptation and its scientific effect')
            continue
        if origin not in ('paper', 'author_artifact'):
            errors.append(f'{key}: ground the objective in paper/author_artifact; mark other adaptations task_policy or not_specified')
            continue
        refs = claim.get('source_evidence')
        if not isinstance(refs, list) or not refs:
            errors.append(f'{key}: source_evidence needs a quote and locator from a frozen primary source')
            continue
        for ref in refs:
            try:
                if not isinstance(ref, dict):
                    raise ValueError('source_evidence must contain source reference objects')
                source_name = ref.get('path', ref.get('snapshot_path'))
                if 'snapshot_path' in ref and ref['snapshot_path'] != source_name:
                    raise ValueError('source reference path and snapshot_path differ')
                if not relative(source_name) or source_name not in indexed:
                    raise ValueError('path must be a Stage1 materials_manifest snapshot_path')
                record = indexed[source_name]
                kind = record.get('kind')
                allowed = ({'article', 'paper', 'article_text', 'paper_text', 'article_pdf', 'article_html', 'article_xml'}
                           if origin == 'paper' else {'code', 'code_documentation', 'source_archive', 'data', 'supplementary'})
                if kind not in allowed:
                    raise ValueError('source kind does not match claim origin')
                path = (bundle/source_name).resolve()
                if not path.is_relative_to(bundle) or not path.is_file():
                    raise ValueError('missing or escaping primary source')
                if path not in verified:
                    verified[path] = digest(path)
                if verified[path] != ref.get('sha256') or verified[path] != record.get('sha256'):
                    raise ValueError('primary source hash changed or does not match frozen manifest')
                quote, locator = ref.get('quote'), ref.get('locator')
                if not isinstance(quote, str) or not quote.strip() or not isinstance(locator, str) or not locator.strip():
                    raise ValueError('provide the original excerpt and section, figure, table, schema or code locator')
                contents = _source_excerpt_text(path, ref)
                if norm(quote) not in norm(contents):
                    raise ValueError('quoted excerpt does not occur in the cited source. For primary paper text, '
                                     'use authoring_package.py --find-quote "short distinctive phrase" '
                                     'and select the returned exact quote, path, SHA-256 and lines')
                if 'line_start' in ref or 'line_end' in ref:
                    start, end = ref.get('line_start'), ref.get('line_end')
                    lines = contents.split('\n')
                    if (type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(lines)
                            or norm(quote) not in norm(' '.join(lines[start-1:end]))):
                        raise ValueError('stated lines do not contain the quoted excerpt')
            except (OSError, ValueError, KeyError, TypeError, ImportError) as exc:
                errors.append(f'{key}: {exc}')
    check_captions = spec.get('scope_contract_version') == 3 and spec.get('paper_caption_review_version') in {1, 2, 3}
    if check_captions and not review:
        from paper_caption_audit import audit
        errors.extend(audit(bundle, manifest, protocol, version=spec['paper_caption_review_version'])['errors'])
    if review:
        report = json.loads((Path(package)/'review_report.json').read_text())
        if report.get('status') == 'complete':
            fidelity = report.get('scientific_checks', {}).get('scope_fidelity', {})
            checks = fidelity.get('claim_verdicts', {})
            if check_captions:
                from paper_caption_audit import audit
                errors.extend(audit(bundle, manifest, protocol, fidelity, version=spec['paper_caption_review_version'])['errors'])
            if spec.get('scope_contract_version') == 3:
                decisions = fidelity.get('main_experiments')
                if not isinstance(experiments, dict) or not isinstance(decisions, dict) or set(decisions) != set(experiments):
                    errors.append('Independent 02 main_experiments must match the full inventory of main experiments from the sources')
                elif any(not isinstance(d, dict) or d.get('status') != 'pass'
                         or not isinstance(d.get('rationale'), str) or not d['rationale'].strip()
                         for d in decisions.values()):
                    errors.append('Each main experiment needs a passing independent source-to-evaluator review and rationale')
            for key in dimensions:
                decision = checks.get(key) if isinstance(checks, dict) else None
                if not isinstance(decision, dict) or decision.get('status') != 'pass' or not isinstance(decision.get('rationale'), str) or not decision['rationale'].strip():
                    errors.append(f'{key}: final reviewer must explain how the source supports the claim or why the declared adaptation is appropriate')
    return errors


def _source_excerpt_text(path, ref):
    member = ref.get('member')
    if member:
        import tarfile
        import zipfile
        if not relative(member):
            raise ValueError('unsafe archive member')
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                entry = archive.getinfo(member)
                if entry.file_size > 16 * 1024 * 1024:
                    raise ValueError('cite a bounded text member or materialized excerpt')
                return archive.read(entry).decode('utf-8')
        if tarfile.is_tarfile(path):
            with tarfile.open(path) as archive:
                entry = archive.getmember(member)
                if not entry.isfile() or entry.size > 16 * 1024 * 1024:
                    raise ValueError('cite a bounded regular text member')
                return archive.extractfile(entry).read().decode('utf-8')
        raise ValueError('member specified for unsupported source archive')
    if path.suffix.lower() == '.pdf':
        from pypdf import PdfReader
        page = ref.get('page')
        if type(page) is not int or page < 1:
            raise ValueError('provide the one-based PDF page')
        reader = PdfReader(str(path))
        if page > len(reader.pages):
            raise ValueError('PDF page is out of bounds')
        return reader.pages[page-1].extract_text() or ''
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError('cite a bounded primary-source excerpt in the frozen manifest')
    return path.read_text(encoding='utf-8')
