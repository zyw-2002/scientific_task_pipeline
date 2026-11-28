#!/usr/bin/env python3
"""Prepare editable authoring files and bind pipeline-owned task metadata."""
from __future__ import annotations
import argparse
import hashlib
import json
import re
from pathlib import Path
import shutil
import tempfile

from contract import SCHEMA_VERSION, atomic_write_json, package_manifest

DEFAULT_AUTHORING_PATHS = Path('/task/input/paths.json')


def evaluation_context(*, scientific_controls=False):
    """Resolve no-argument authoring runners without guessing from cwd or GPUs."""
    paths = read_paths(DEFAULT_AUTHORING_PATHS)
    package = Path(paths['output_dir'])
    if package.is_symlink(): raise ValueError('output package must not be a symlink')
    package = package.resolve()
    for key in ('source_bundle', 'draft'):
        if paths.get(key):
            frozen = Path(paths[key]).resolve()
            if package.is_relative_to(frozen) or frozen.is_relative_to(package):
                raise ValueError('output must be separate from frozen inputs')
    if not (package / 'execution.json').is_file():
        raise ValueError('prepare the output package and run preflight before scientific tests')
    evaluation = package / 'common_task/evaluation'
    if evaluation.is_symlink() or not evaluation.resolve().is_relative_to(package):
        raise ValueError('evaluation must stay inside the editable package')
    if scientific_controls:
        spec = json.loads((package / 'execution.json').read_bytes())
        if spec.get('framework_generation_version') == 1:
            binding = spec.get('verification', {}).get('score_evidence', {}).get('evaluation_protocol', {})
            protocol = package / binding.get('path', '')
            if (not protocol.resolve().is_relative_to(evaluation) or not protocol.is_file()
                    or protocol.read_bytes() != Path(__file__).with_name('evaluation_protocol.py').read_bytes()):
                raise ValueError('Preserve any authored evaluation_protocol.py under a distinct name and call its '
                                 'scientific logic from your task metric adapter. Run preflight.py to install the '
                                 'framework runner before calibration and full scoring smoke.')
    return package, evaluation


def new_evidence(package, prefix):
    """Keep each invocation outside the package until its run is finished."""
    return Path(tempfile.mkdtemp(prefix=prefix + '-', dir=package.parent)) / 'evidence'


def preserve_evidence(package, evidence):
    target = package / 'common_task/evaluation/checks/evidence' / evidence.parent.name
    if target.parent.is_symlink() or not target.parent.resolve().is_relative_to(package.resolve()):
        raise ValueError('evidence archive must stay inside the editable package')
    shutil.copytree(evidence, target)
    return target


def build_source_evidence_index(paths: dict, output: Path) -> dict:
    """Write a private, deterministic index before scientific drafting."""
    from paper_caption_audit import captions
    source = Path(paths['source_bundle']).resolve()
    manifest = json.loads((source / 'materials_manifest.json').read_text())
    available = [item for item in manifest.get('artifacts', [])
                 if isinstance(item, dict) and item.get('status') == 'available'
                 and isinstance(item.get('snapshot_path'), str)]
    primary = [item for item in available if item.get('kind') in {
        'paper', 'article', 'paper_text', 'article_text', 'article_html', 'article_xml'}]
    heading = re.compile(r'^\\s*(?:#{1,6}\\s+)?'
                         r'(abstract|introduction|methods?|materials|results?|experiments?|evaluation|dataset|data|metrics?)'
                         r'\\s*:?\\s*$', re.I)
    sections = []
    for item in primary:
        path = source / item['snapshot_path']
        if path.suffix.lower() not in {'.txt', '.md', '.html', '.xml'} or not path.is_file():
            continue
        lines = path.read_text(errors='replace').split('\\n')
        for number, line in enumerate(lines, 1):
            if heading.match(line):
                sections.append({'path': item['snapshot_path'], 'sha256': item.get('sha256'),
                                 'line_start': number, 'line_end': min(len(lines), number + 3),
                                 'heading': line.strip(),
                                 'excerpt': ' '.join(' '.join(lines[number-1:number+3]).split())[:500]})
    index = {'schema_version': 'pipeline-source-evidence-index-v1',
             'canonical_id': manifest.get('canonical_id'),
             'primary_sources': [{'path': item['snapshot_path'], 'sha256': item.get('sha256'),
                                  'kind': item.get('kind')} for item in primary],
             'captions': captions(source, manifest), 'section_anchors': sections,
             'private_authoring_only': True}
    output.write_text(json.dumps(index, ensure_ascii=False, indent=2) + '\\n')
    return index

METADATA_FIELDS = ('task_id', 'canonical_id', 'scientific_acceptance_version',
                   'assessment_contract_version', 'scope_contract_version', 'difficulty_contract_version', 'paper_caption_review_version', 'paper_comparison_version', 'solver_access',
                   'output_language', 'evaluation_protocol_version',
                   'framework_generation_version')
DEFAULT_FRAMEWORK_FIELDS = {
    'assessment_contract_version': 1, 'scope_contract_version': 3, 'paper_caption_review_version': 3, 'paper_comparison_version': 1, 'solver_access': 'online_search',
    'scientific_acceptance_version': 3, 'difficulty_contract_version': 1, 'output_language': 'en',
    'evaluation_protocol_version': 1, 'framework_generation_version': 1,
}
PATH_FIELDS = {
    'source_bundle', 'output_dir', 'draft', 'preflight', 'execution_schema',
    'construction_instructions', 'runtime_feedback', 'multimodal_overview',
    'multimodal_report',
    'review_report_schema', 'source_inventory', 'fixed_task_instruction',
}


def validate_paths(value: dict) -> dict:
    """Accept file locations only; scientific and runtime settings belong in the task."""
    if not isinstance(value, dict):
        raise ValueError('paths must be an object')
    extra = value.keys() - PATH_FIELDS - {'host_validation_feedback'}
    if extra:
        raise ValueError('paths contains non-path fields: ' + ', '.join(sorted(extra)))
    for key in ('source_bundle', 'output_dir'):
        if key not in value:
            raise ValueError(f'paths.{key} is required')
    for key, item in value.items():
        items = item if key == 'host_validation_feedback' else [item]
        if key == 'host_validation_feedback' and not isinstance(items, list):
            raise ValueError('paths.host_validation_feedback must be an array of paths')
        for path in items:
            if (not isinstance(path, str) or not path or not Path(path).is_absolute()
                    or '..' in Path(path).parts or any(c in path for c in ('\x00', '\n', '\r'))):
                raise ValueError(f'paths.{key} must contain absolute paths')
    return value


def read_paths(path: Path) -> dict:
    return validate_paths(json.loads(path.read_text(encoding='utf-8')))


def framework_metadata(package: Path, paths: dict) -> dict:
    """Restore mechanical fields without supplying a scientific plan or budget."""
    metadata = dict(DEFAULT_FRAMEWORK_FIELDS)
    source = Path(paths['source_bundle'])
    manifest = source / 'materials_manifest.json'
    if manifest.is_file():
        identity = json.loads(manifest.read_text(encoding='utf-8')).get('canonical_id')
        if isinstance(identity, str) and identity.strip():
            metadata['canonical_id'] = identity
    try:
        existing = json.loads((Path(package) / 'execution.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        existing = {}
    if not isinstance(existing, dict) or 'task_id' not in existing:
        identity = metadata.get('canonical_id', str(source.resolve()))
        metadata['task_id'] = 'task-' + hashlib.sha256(identity.encode()).hexdigest()[:20]
    return metadata


def bind_framework_fields(package: Path, request: dict) -> list[str]:
    """Bind host metadata and the user-selected Solver access policy; preserve science and budgets.

    Missing/malformed task files stay repairable and are reported by preflight.
    Call only on the editable worker output or a newly collected host revision.
    """
    path = Path(package) / 'execution.json'
    if path.is_symlink() or not path.exists():
        return []
    try:
        spec = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    if not isinstance(spec, dict):
        return []
    fields = {'schema_version': SCHEMA_VERSION,
              **{key:request[key] for key in METADATA_FIELDS if key in request}}
    changed = [key for key, value in fields.items() if spec.get(key) != value]
    spec.update(fields)
    data = spec.get('data')
    if isinstance(data, dict) and isinstance(data.get('sources'), list):
        for source in data['sources']:
            if not isinstance(source, dict) or source.get('availability') not in {'bundled', 'preprovided'}:
                continue
            relative = source.get('package_path')
            if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or '..' in Path(relative).parts:
                continue
            aliases = ('/workspace/build/' + relative, '/workspace/verified/' + relative)
            if source.get('path') in aliases and (Path(package)/relative).exists():
                source['path'] = relative
                changed.append('data.sources.' + str(source.get('id', relative)) + '.path')
    from solver_access import apply_policy
    changed.extend(apply_policy(Path(package), spec))
    if changed:
        atomic_write_json(path, spec)
    return changed


def initialize(paths: dict) -> dict:
    validate_paths(paths)
    output = Path(paths['output_dir'])
    if output.is_symlink():
        raise ValueError('output directory must not be a symlink')
    output = output.resolve()
    draft = Path(paths['draft']).resolve() if paths.get('draft') else None
    for source in (draft, Path(paths['source_bundle']).resolve()):
        if source and (output == source or output.is_relative_to(source) or source.is_relative_to(output)):
            raise ValueError('output must be separate from frozen inputs')
    copied = False
    if not output.exists() or not any(output.iterdir()):
        if draft is not None:
            package_manifest(draft)  # Reject symlinks and nonregular inputs before copying.
            shutil.copytree(draft, output, dirs_exist_ok=True)
            copied = True
        else:
            output.mkdir(parents=True, exist_ok=True)
    package_manifest(output)
    for directory in ('environment/input', 'evaluation/checks', 'tests'):
        (output/'common_task'/directory).mkdir(parents=True, exist_ok=True)
    protocol_path = output/'common_task/evaluation/protocol.json'
    new_protocol = not draft and not protocol_path.exists()
    if new_protocol:
        from instruction_checks import REQUIRED_INSTRUCTION_HEADINGS
        instruction = output/'common_task/instruction.md'
        if not instruction.exists():
            slots = {'Environment': 'runtime facts', 'Output': 'output artifacts', 'Evaluation': 'assessment tables'}
            sections = []
            for title in REQUIRED_INSTRUCTION_HEADINGS:
                section = '## ' + title + '\n\n{{' + title.upper().replace(' ', '_') + '}}'
                if title in slots:
                    section += '\n\n<!-- ' + slots[title] + ' -->'
                sections.append(section)
            instruction.write_text('\n\n'.join(sections) + '\n', encoding='utf-8')
        readme = output/'common_task/environment/input/README.md'
        if not readme.exists():
            readme.write_text('# Materials and usage\n\n{{MATERIALS_AND_USAGE}}\n\n'
                              '## Submission\n\n<!-- submission path -->\n', encoding='utf-8')
        # Keep scientific values empty for the author. These objects mirror the
        # formal schema to make the paper inventory editable from the first turn.
        # Expose the actual claim ABI without supplying a scientific decision,
        # origin, quotation or acceptance. The author must ground every value.
        claims = {key: {'statement': '', 'origin': '', 'source_evidence': [], 'rationale': ''}
                  for key in ('objective', 'scale', 'split', 'metric')}
        inventory = json.loads(Path(paths['source_inventory']).read_text()) if paths.get('source_inventory') else {}
        displays = {item['id']: {'caption_id': item['id']} for item in inventory.get('paper_captions', [])}
        scope = {'source_basis': '', 'paper_scope': '', 'task_scope': '', 'difficulty_impact': '',
                 # Explicit policy keeps the historical full-paper gate as the
                 # safe default while allowing a source-backed bounded task to
                 # opt in without an interactive per-case decision.
                 'scope_policy': 'full',
                 'data_coverage': [], 'source_claims': claims, 'main_experiments': {},
                 'figure_table_decisions': displays,
                 # A deliberately incomplete scaffold makes the production
                 # difficulty gate visible to the author from the first turn.
                 # Scientific levels and evidence must come from the paper.
                 'difficulty_profile': {
                     'version': 1, 'paper': {}, 'task': {}, 'weights': {},
                     'overall_gap_limit': 0.2, 'split_drop_limit': 1,
                     'source_evidence': [],
                 }}
        assessment = {'mode': '', 'basis': '', 'reference_policy': '', 'adaptations': [],
                      'requirements': [], 'scope': scope}
        evaluation = protocol_path.parent
        (evaluation/'checks/fixtures').mkdir(exist_ok=True)
        (output/'common_task/environment/input/data').mkdir(exist_ok=True)
        adapter = evaluation/'metric_adapter.py'
        if not adapter.exists():
            adapter.write_text(
                'def measure(path, context):\n'
                '    """Compute the paper metrics for this scoring unit and its bound resources."""\n'
                '    raise NotImplementedError("Implement the scientific metric for the declared scoring unit")\n',
                encoding='utf-8')
        smoke = evaluation/'scoring_smoke.json'
        if not smoke.exists():
            atomic_write_json(smoke, {'schema_version': 'pipeline-scoring-smoke-v1', 'protocol': protocol_path.name,
                                      'resource_bindings': {}, 'cases': [
                {'id': 'correct_control', 'kind': 'correct', 'expected_status': 'success',
                 'description': '', 'submission': '', 'oracle': '', 'expected_metrics': {}, 'requirement_ids': []},
                {'id': 'weak_valid_control', 'kind': 'weak_valid', 'expected_status': 'success',
                 'description': '', 'submission': '', 'expected_metrics': {}, 'requirement_ids': []},
                {'id': 'invalid_control', 'kind': 'invalid', 'expected_status': 'invalid_submission',
                 'description': '', 'submission': '', 'expected_error_contains': '', 'requirement_ids': []},
            ]})
        atomic_write_json(protocol_path, {'adapter': 'metric_adapter.py', 'context': {}, 'resource_bindings': {},
                                          'units': [], 'aggregations': [], 'reward_metric': '',
                                          'reward_direction': '', 'assessment': assessment})
    spec_path = output/'execution.json'
    if not spec_path.exists():
        spec = {}
        if not draft:
            from framework_generator import DEFAULT_EVIDENCE_ROOT
            spec = {
                'runtime': {'data_dir': '/task/problem/data', 'image_policy': 'deferred',
                            'network_policy': 'enabled', 'search_policy': 'direct',
                            'network': {'inherit_env': True}},
                'submission': {'path': '/workspace/output/submission', 'required_artifacts': []},
                'verification': {'score_evidence': {
                    'service': 'evaluator',
                    'artifact_source': DEFAULT_EVIDENCE_ROOT,
                    'evaluation_protocol': {'config_path': 'common_task/evaluation/protocol.json',
                                            'config_container': '/task/evaluation/protocol.json'},
                }},
            }
        atomic_write_json(spec_path, spec)
    bound = bind_framework_fields(output, framework_metadata(output, paths))
    result = {'output_dir':str(output), 'copied_previous_files':copied,
              'metadata_fields_updated':bound, 'preflight':paths.get('preflight')}
    if new_protocol:
        result['authoring'] = {
            'common_task/instruction.md': 'Fill each section with task-specific text. Preflight fills the marked tables from the declarations.',
            'common_task/environment/input/README.md': 'Describe the resources and how to use them. Preflight fills the marked submission path.',
            'execution.json': 'Set the submission filename and required_artifacts from the task. '
                              'Keep required names, formats and packaging. Set resources and budgets from the paper.',
            'common_task/evaluation/protocol.json': 'Complete the experiment inventory, scoring units, aggregations and reward.',
            'common_task/evaluation/metric_adapter.py': 'Implement measure(path, context) for those units.',
            'common_task/evaluation/scoring_smoke.json': 'Bind fixtures, add independent expected scores and run scoring_smoke.py --case CASE_ID.',
            'preflight.py': 'Generates task.toml, Compose mounts, evaluation_protocol.py, task_contract.json and tests/test.sh from these declarations.',
        }
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--paths', default=DEFAULT_AUTHORING_PATHS, type=Path)
    parser.add_argument('--review-template', action='store_true', help='Also create an incomplete review_report.json if absent; never overwrite findings')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--check-scope', action='store_true', help='Check the scientific plan against the frozen paper before implementing the scorer')
    action.add_argument('--find-quote', metavar='PHRASE', help='Find a phrase in the frozen primary paper and return exact source_evidence objects')
    action.add_argument('--quote-lines', nargs=2, type=int, metavar=('FIRST','LAST'), help='Return the exact source_evidence for the chosen inclusive lines')
    action.add_argument('--source-index', action='store_true', help='Write a private deterministic source evidence index before drafting')
    parser.add_argument('--source-path', help='Select a primary text by its materials_manifest snapshot_path')
    args = parser.parse_args(argv)
    paths = read_paths(args.paths)
    if args.source_index:
        output = Path(paths['output_dir']) / 'source_evidence_index.json'
        build_source_evidence_index(paths, output)
        print(json.dumps({'index': str(output), 'private_authoring_only': True}, ensure_ascii=False))
        return 0
    if args.find_quote is not None or args.quote_lines is not None:
        from paper_caption_audit import find_source_quotes
        try:
            matches = find_source_quotes(paths['source_bundle'], phrase=args.find_quote,
                                         source_path=args.source_path, line_range=args.quote_lines)
            print(json.dumps({'matches':matches},ensure_ascii=False))
            return 0 if matches else 1
        except (OSError, ValueError, KeyError) as error:
            print(json.dumps({'matches':[], 'error':str(error)},ensure_ascii=False))
            return 1
    if args.source_path:
        parser.error('--source-path needs --find-quote or --quote-lines')
    if args.check_scope:
        from paper_comparison import source_claim_errors
        package = Path(paths['output_dir'])
        try:
            spec = json.loads((package / 'execution.json').read_text())
            errors = source_claim_errors(package, spec, paths['source_bundle'])
        except (OSError, ValueError, TypeError, KeyError) as error:
            errors = [str(error)]
        print(json.dumps({'check':'PAPER-SOURCE-CLAIMS', 'status':'fail' if errors else 'pass',
                          'errors':errors}, ensure_ascii=False))
        return int(bool(errors))
    result = initialize(paths)
    if args.review_template or paths.get('review_report_schema') or paths.get('construction_instructions'):
        from review_report import initialize_review
        result['review_template_created'] = initialize_review(Path(result['output_dir']))
        if paths.get('fixed_task_instruction'):
            from fixed_task_alignment import initialize_alignment
            result['alignment_template_created'] = initialize_alignment(Path(result['output_dir']), paths['fixed_task_instruction'])
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    raise SystemExit(main())
