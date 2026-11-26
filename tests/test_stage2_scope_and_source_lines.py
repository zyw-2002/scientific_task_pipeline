import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pipeline._legacy_loader import STAGE2_ROOT, implementation_paths, load_file


class Stage2ScopeAndSourceTests(unittest.TestCase):
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
