import hashlib
import json
import shutil
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pipeline._legacy_loader import STAGE2_ROOT, STAGE3_ROOT, implementation_paths, load_file


class Stage2ScopeAndSourceTests(unittest.TestCase):
    def test_real_smoke_receipt_survives_package_relative_fixture_and_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as tmp, implementation_paths(stage=2):
            load_file(STAGE3_ROOT/'code/score_attempt.py', 'score_attempt', stage=3)
            import scoring_smoke, assessment_contract, framework_generator
            package = Path(tmp)/'package'; evaluation = package/'common_task/evaluation'
            fixtures = evaluation/'fixtures'; fixtures.mkdir(parents=True)
            for name in ('evaluation_protocol.py', 'scoring_entry.py'):
                shutil.copyfile(STAGE2_ROOT/'code'/name, evaluation/name)
            (evaluation/'metric_adapter.py').write_text(
                'from pathlib import Path\nfrom evaluation_protocol import InvalidSubmission\n'
                'def measure(path, context):\n'
                '    text = Path(path).read_text()\n'
                '    if text == "bad": raise InvalidSubmission("invalid numeric control")\n'
                '    return {"value": float(text)}\n')
            protocol = {'adapter': 'metric_adapter.py', 'units': [{'id': 'one', 'path': '{submission}'}],
                        'aggregations': [{'output': 'value', 'metric': 'value', 'operation': 'identity', 'unit_ids': ['one']}],
                        'reward_metric': 'value', 'reward_direction': 'higher_is_better'}
            (evaluation/'protocol.json').write_text(json.dumps(protocol))
            cases = []
            for name, kind, text in [('correct', 'correct', '1'), ('weak', 'weak_valid', '0.25'), ('invalid', 'invalid', 'bad')]:
                (fixtures/(name+'.txt')).write_text(text)
                case = {'id': name, 'kind': kind, 'description': 'Independent numeric control',
                        'oracle': 'Read independent known fixture value',
                        'submission': 'common_task/evaluation/fixtures/'+name+'.txt',
                        'expected_status': 'invalid_submission' if kind == 'invalid' else 'success'}
                if kind == 'invalid': case['expected_error_contains'] = 'invalid numeric control'
                else: case['expected_metrics'] = {'value': float(text)}
                cases.append(case)
            config = {'schema_version': 'pipeline-scoring-smoke-v1', 'protocol': 'protocol.json', 'cases': cases,
                      'weak_valid_applicability': {'applicable': True, 'reason': 'Lower known scalar'}}
            (evaluation/'scoring_smoke.json').write_text(json.dumps(config))
            spec = {'verification': {'score_evidence': {'service': 'evaluator', 'evaluation_protocol': {
                'config_path': 'common_task/evaluation/protocol.json', 'config_container': '/task/evaluation/protocol.json'}}}}
            self.assertTrue(framework_generator._scoring_smoke_binding(package, spec, evaluation))
            self.assertEqual(spec['verification']['commands']['scientific_handoff'],
                             spec['verification']['commands']['scientific_scoring_smoke'])
            saved = scoring_smoke.run_smoke(evaluation/'scoring_smoke.json', Path(tmp)/'run', resource_mode='authoring')
            self.assertEqual(saved['status'], 'pass', saved)
            archive = evaluation/'checks/evidence/smoke'; archive.mkdir(parents=True)
            proof = archive/'report.json'; proof.write_text(json.dumps(saved))
            report = {'checks': [{'name': 'scientific_scoring_smoke', 'status': 'pass', 'artifacts': [{
                'path': proof.relative_to(package).as_posix(), 'sha256': hashlib.sha256(proof.read_bytes()).hexdigest()}]}]}
            self.assertEqual(assessment_contract.check_evidence_errors(package, spec, report), [])
            (fixtures/'correct.txt').write_text('0.5')
            self.assertIn('scoring smoke submission changed after test: correct',
                          assessment_contract.check_evidence_errors(package, spec, report))

    def test_smoke_package_relative_paths_precede_duplicate_prefix(self):
        smoke = load_file(STAGE2_ROOT/'code/scoring_smoke.py', '_smoke_relative_roots', stage=2)
        with tempfile.TemporaryDirectory() as tmp, implementation_paths(stage=2):
            root = Path(tmp); (root/'fixtures').mkdir(); (root/'fixtures/good.json').write_text('{}')
            self.assertEqual(smoke._file(root, 'common_task/evaluation/fixtures/good.json'),
                             (root/'fixtures/good.json').resolve())

    def test_source_index_is_private_and_hash_bound(self):
        author = load_file(STAGE2_ROOT/'code/authoring_package.py', '_source_index', stage=2)
        with tempfile.TemporaryDirectory() as tmp, implementation_paths(stage=2):
            root = Path(tmp); bundle = root/'bundle'; bundle.mkdir()
            paper = bundle/'paper.txt'; paper.write_text('Abstract\nA source-grounded objective.\nFigure 1: Caption.\n')
            import hashlib
            digest = hashlib.sha256(paper.read_bytes()).hexdigest()
            (bundle/'materials_manifest.json').write_text(json.dumps({'canonical_id': 'paper', 'artifacts': [
                {'status': 'available', 'kind': 'paper_text', 'snapshot_path': 'paper.txt', 'sha256': digest}]}))
            output = root/'source_evidence_index.json'
            paths = {'source_bundle': str(bundle), 'output_dir': str(root)}
            index = author.build_source_evidence_index(paths, output)
            self.assertTrue(index['private_authoring_only'])
            self.assertEqual(index['primary_sources'][0]['sha256'], digest)
            self.assertTrue(output.is_file())
            self.assertTrue(index['captions'])
            self.assertEqual(json.loads(output.read_text()), index)
            self.assertEqual(index['section_anchors'][0]['line_start'], 1)
            self.assertEqual(index['section_anchors'][0]['heading'], 'Abstract')
            self.assertIn('source-grounded objective', index['section_anchors'][0]['excerpt'])
            paper.write_text('Changed material')
            with self.assertRaisesRegex(ValueError, 'changed or unsafe primary source'):
                author.build_source_evidence_index(paths, output)
    def test_frozen_source_title_is_blocked_in_public_docs_and_guided_methods(self):
        comparison = load_file(STAGE2_ROOT/'code/paper_comparison.py', '_source_title_boundary', stage=2)
        with tempfile.TemporaryDirectory() as tmp, implementation_paths(stage=2):
            root = Path(tmp); bundle = root/'source'; bundle.mkdir()
            (bundle/'paper_record.json').write_text(json.dumps({
                'canonical_id': 'paper', 'title': 'A General Method for Measuring Scientific Quantities'}))
            package = root/'package'; public = package/'common_task/environment/input'; public.mkdir(parents=True)
            method = package/'method.md'; method.write_text('A GENERAL METHOD—FOR MEASURING SCIENTIFIC QUANTITIES')
            instruction = package/'common_task/instruction.md'; instruction.write_text('Implement a measurement algorithm.')
            manifest = {'paper_record_path': 'paper_record.json'}
            # Resolve the same method location as the production boundary.
            from contract import package_layout
            method_path = package/package_layout({})['method_path']; method_path.parent.mkdir(parents=True, exist_ok=True)
            method.rename(method_path)
            errors = comparison.public_source_title_errors(package, {'canonical_id': 'paper'}, bundle, manifest)
            self.assertEqual(len(errors), 1)
            self.assertIn(str(method_path.relative_to(package)), errors[0])
            method_path.write_text('Use a general measurement algorithm with independent validation.')
            (public/'README.md').write_text('A General Method for Measuring\nScientific Quantities')
            self.assertEqual(len(comparison.public_source_title_errors(package, {'canonical_id': 'paper'}, bundle, manifest)), 1)
            (public/'README.md').write_text('The supplied measurements have the documented schema.')
            self.assertEqual(comparison.public_source_title_errors(package, {'canonical_id': 'paper'}, bundle, manifest), [])

    def test_stage2_defaults_to_construction_and_preserves_explicit_runtime_modes(self):
        module = load_file(STAGE2_ROOT/'code/pipeline.py', '_stage2_scope_defaults', stage=2)
        for flags, expected in [([], True), (['--probe-runtime'], False),
                                (['--no-construction-only'], False), (['--construction-only'], True)]:
            seen = []
            def workflow(args):
                seen.append(args.construction_only)
                return {'status': 'ready'}
            with implementation_paths(stage=2), patch.object(module, '_enter_snapshot'), patch.object(module, 'workflow', side_effect=workflow):
                self.assertEqual(module.main(['run', *flags]), 0)
            self.assertEqual(seen, [expected])

    def test_source_lookup_caption_index_and_claim_validation_share_physical_lines(self):
        captions = load_file(STAGE2_ROOT/'code/paper_caption_audit.py', '_physical_source_lines', stage=2)
        comparison = load_file(STAGE2_ROOT/'code/paper_comparison.py', '_physical_claim_lines', stage=2)
        with tempfile.TemporaryDirectory() as tmp, implementation_paths(stage=2):
            root = Path(tmp); bundle = root/'source'; bundle.mkdir()
            paper = bundle/'paper.txt'
            paper.write_bytes('Intro text.\n\fFigure 1: Exact source experiment.\u2028Same physical line.\nMethods describe the measured objective.\n'.encode())
            sha = hashlib.sha256(paper.read_bytes()).hexdigest()
            manifest = {'schema_version': 'pipline-source-materials-v1', 'canonical_id': 'paper',
                        'artifacts': [{'status': 'available', 'kind': 'paper_text', 'snapshot_path': 'paper.txt', 'sha256': sha}]}
            (bundle/'materials_manifest.json').write_text(json.dumps(manifest))
            indexed = captions.captions(bundle, manifest)
            quote = captions.find_source_quotes(bundle, phrase='Exact source experiment')[0]
            self.assertEqual(indexed[0]['line'], 2)
            self.assertEqual(quote['line_start'], 2)
            self.assertEqual(quote['line_end'], 2)
            self.assertEqual(captions.evidence_lines(quote, {'paper.txt': (paper, sha)}, {}), ('paper.txt', 2, 2))
            selected = captions.find_source_quotes(bundle, source_path='paper.txt', line_range=(3, 3))[0]
            self.assertIn('Methods describe', selected['quote'])
            package = root/'package'; evaluation = package/'common_task/evaluation'; evaluation.mkdir(parents=True)
            claims = {key: {'statement': 'Source-grounded task decision', 'origin': 'paper', 'source_evidence': [quote]}
                      for key in ('objective', 'scale', 'split', 'metric')}
            protocol = {'assessment': {'scope': {'source_claims': claims}}}
            (evaluation/'protocol.json').write_text(json.dumps(protocol))
            self.assertEqual(comparison.source_claim_errors(package, {'canonical_id': 'paper'}, bundle), [])
            quote['line_start'] = quote['line_end'] = 3
            (evaluation/'protocol.json').write_text(json.dumps(protocol))
            self.assertTrue(comparison.source_claim_errors(package, {'canonical_id': 'paper'}, bundle))
