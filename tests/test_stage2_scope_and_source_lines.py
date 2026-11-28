import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pipeline._legacy_loader import STAGE2_ROOT, implementation_paths, load_file


class Stage2ScopeAndSourceTests(unittest.TestCase):
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
            index = author.build_source_evidence_index({'source_bundle': str(bundle)}, output)
            self.assertTrue(index['private_authoring_only'])
            self.assertEqual(index['primary_sources'][0]['sha256'], digest)
            self.assertTrue(output.is_file())
            self.assertTrue(index['captions'])
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
