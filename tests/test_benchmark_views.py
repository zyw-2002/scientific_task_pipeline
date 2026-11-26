import json
from pathlib import Path
import tempfile
import unittest
from pipeline.orchestration.benchmark_views import naturebench_sample


class BenchmarkViewTests(unittest.TestCase):
    def test_scientific_view_excludes_screening_and_private_evaluator(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); jobs=root/'jobs/NatureBench'; jobs.mkdir(parents=True)
            trial=jobs/'batch/science'; trial.mkdir(parents=True)
            package=root/'datasets/science'; (package/'environment/input').mkdir(parents=True)
            (package/'instruction.md').write_text('Read README and data description')
            (package/'environment/input/README.md').write_text('Actual scientific objective')
            (package/'environment/input/data_description.md').write_text('Public schema')
            (package/'tests').mkdir(); (package/'tests/verifier.py').write_text('SECRET_REFERENCE')
            (trial/'config.json').write_text(json.dumps({'task': {'path': 'datasets/science'}}))
            (jobs/'best-manifest.json').write_text(json.dumps([
                {'task': 'screening', 'path': str(root/'jobs/paper-screen/sample')},
                {'task': 'science', 'path': str(trial)}]))
            report=naturebench_sample(jobs,root,seed=12,count=1)
            self.assertEqual(report['eligible_count'],1)
            text=report['tasks'][0]['task_text']
            self.assertIn('Actual scientific objective',text)
            self.assertNotIn('SECRET_REFERENCE',text)
            self.assertEqual(report['evidence'][0]['public_document_errors'],[])
            (package/'environment/input/README.md').unlink()
            missing=naturebench_sample(jobs,root,seed=12,count=1)
            self.assertEqual(missing['selected_task_ids'],report['selected_task_ids'])
            self.assertTrue(missing['evidence'][0]['public_document_errors'])
