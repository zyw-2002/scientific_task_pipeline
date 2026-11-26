import unittest
import tempfile
from pathlib import Path
from pipeline._legacy_loader import STAGE2_ROOT, load_file, implementation_paths


class ResourcePreparationPolicyTests(unittest.TestCase):
    def test_timed_out_worker_can_only_salvage_a_unique_safe_partial_package(self):
        with implementation_paths(stage=2):
            from worker_runtime import AuthoringWorkerError
            from compact_outputs import worker_failure
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp); jobs=root/'jobs'; job=jobs/'job'
                candidate=job/'trial/artifacts/workspace/build'; candidate.mkdir(parents=True)
                (candidate/'unfinished.txt').write_text('unfinished scientific authoring')
                failure=AuthoringWorkerError('harbor_trial_failed',job,details={
                    'trial_count':1,'job_finished':True,'exception_types':['AgentTimeoutError']})
                _,record=worker_failure(jobs,root/'output','01','build',failure)
                self.assertTrue(record['retryable'])
                self.assertEqual(record['status'],'failed')
                self.assertEqual(len(record['packages']),1)
                other=job/'second/artifacts/workspace/build'; other.mkdir(parents=True)
                (other/'unfinished.txt').write_text('ambiguous second package')
                _,ambiguous=worker_failure(jobs,root/'other-output','01','build',failure)
                self.assertFalse(ambiguous['retryable'])
                arbitrary=AuthoringWorkerError('harbor_trial_failed',job,details={
                    'trial_count':1,'job_finished':True,'exception_types':['ValueError']})
                self.assertFalse(arbitrary.retryable)

    def test_absent_blobs_still_get_one_preparation_repair_within_budget(self):
        module = load_file(STAGE2_ROOT/'code/pipeline.py', '_resource_retry_policy', stage=2)
        blocked = {'contract_status': 'BLOCKED',
                   'missing_resources': ['formal data missing', 'invalid runtime preparation']}
        self.assertTrue(module.resource_preparation_retry(blocked, 0, 2))
        self.assertFalse(module.resource_preparation_retry(blocked, 1, 2))
        self.assertFalse(module.resource_preparation_retry(blocked, 0, 1))
        self.assertTrue(module.resource_preparation_retry(blocked, 0, 0))
        self.assertFalse(module.resource_preparation_retry({'contract_status': 'BLOCKED'}, 0, 2))
        self.assertFalse(module.resource_preparation_retry({'contract_status': 'PASS'}, 0, 2))
