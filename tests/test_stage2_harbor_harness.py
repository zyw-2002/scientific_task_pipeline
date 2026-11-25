from pathlib import Path
import tempfile
import unittest
from pipeline.stage2_construction.workflow.test_harness import run_harbor_test

ROOT = Path(__file__).resolve().parents[1]


class Stage2HarborHarnessTests(unittest.TestCase):
    def test_construct_verify_and_result_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest = run_harbor_test(
                draft=ROOT / 'tests/fixtures/stage2/draft',
                output_root=temp,
            )
            self.assertEqual(manifest['status'], 'ready')
            self.assertEqual([row['role'] for row in manifest['tasks']],
                             ['construct', 'construct_verify'])
            self.assertTrue(all(row['result']['trial_count'] == 1 for row in manifest['tasks']))
            self.assertEqual(manifest['verification']['status'], 'complete')
            self.assertTrue((Path(temp) / 'harbor_test_manifest.json').is_file())
