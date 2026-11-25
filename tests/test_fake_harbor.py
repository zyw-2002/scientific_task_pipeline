from pathlib import Path
import csv
import json
import tempfile
import unittest

from pipeline.stage2_construction.harbor import normalize_harbor_result, run_fake_authoring
from pipeline.stage3_execution.runtime.fake_harbor import run_fake_solver
from pipeline.shared.io.jsonio import read_json

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'tests/fixtures'


class FakeHarborTests(unittest.TestCase):
    def test_stage2_authoring_has_one_completed_trial_and_artifact(self):
        source = FIX / 'stage2/draft'
        with tempfile.TemporaryDirectory() as temp:
            def worker(input_dir, workspace):
                artifact = workspace / 'build'
                artifact.mkdir()
                for name in ('execution.json', 'common_task/instruction.md'):
                    target = artifact / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes((input_dir / name).read_bytes())
            result = run_fake_authoring(input_dir=source, artifact_name='build', output_root=temp, worker=worker)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['trial_count'], 1)
            self.assertTrue(Path(result['workspace'], 'build/execution.json').is_file())

    def test_stage2_rejects_incomplete_harbor_result(self):
        with self.assertRaises(ValueError):
            normalize_harbor_result({'status': 'completed', 'stats': {'n_errored_trials': 1}, 'trials': []})

    def test_stage3_solver_input_output_contract(self):
        package = FIX / 'stage3/task_package'
        with tempfile.TemporaryDirectory() as temp:
            def solver(public, output):
                with (public / 'test.csv').open(newline='', encoding='utf-8') as stream:
                    rows = list(csv.DictReader(stream))
                with (output / 'predictions.csv').open('w', newline='', encoding='utf-8') as stream:
                    writer = csv.DictWriter(stream, fieldnames=['sample_id', 'prediction'])
                    writer.writeheader()
                    for row in rows:
                        writer.writerow({'sample_id': row['sample_id'], 'prediction': '1.5'})
            result = run_fake_solver(package=package, output_root=temp, solver=solver)
            self.assertEqual(result['status'], 'passed')
            self.assertEqual(result['errors'], [])
            self.assertTrue(Path(result['submission']).is_file())
