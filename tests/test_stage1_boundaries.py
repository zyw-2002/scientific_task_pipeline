from pathlib import Path
import unittest
from pipeline.shared.io.jsonio import read_jsonl
from pipeline.stage1_sources.contracts import validate_records

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'tests/fixtures/stage1'


class Stage1BoundaryTests(unittest.TestCase):
    def test_historical_input_and_outputs_keep_required_fields(self):
        cases = [
            ('input/01_unified_sources.jsonl', 'unified'),
            ('expected/08_serpapi_enriched.jsonl', 'searched'),
            ('expected/10_download_results.jsonl', 'materials'),
        ]
        for relative, boundary in cases:
            errors = validate_records(read_jsonl(FIX / relative), boundary)
            self.assertEqual(errors, [], f'{relative}: {errors}')

    def test_fixture_is_small_and_redacted(self):
        for path in FIX.rglob('*'):
            if path.is_file():
                self.assertLess(path.stat().st_size, 300_000, path)
                self.assertNotIn('/u01/zhengyawen/pipline', path.read_text(errors='ignore'))
