import unittest
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from pipeline._legacy_loader import STAGE2_ROOT, load_file, implementation_paths


class ResourcePreparationPolicyTests(unittest.TestCase):
    def test_bundled_formal_schema_mismatch_is_rejected_during_construction(self):
        from pipeline.stage2_construction.contracts.resource_contract import source_errors
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            path = package/'common_task/environment/input/data/items.json'
            path.parent.mkdir(parents=True)
            path.write_text('{"items": [1, 2]}')
            spec = {'scope_contract_version': 3, 'framework': {'public_dir': 'common_task/environment/input'},
                    'data': {'sources': [{'id': 'items', 'role': 'input', 'origin': 'observation',
                              'visibility': 'public', 'identity': 'bundled control', 'version': '1',
                              'availability': 'bundled', 'package_path': 'common_task/environment/input/data/items.json',
                              'content': {'format': 'json', 'items_path': ['items'], 'item_count': 3}}]}}
            errors = source_errors(package, spec, construction_only=True)
            self.assertTrue(any('item count differs' in error for error in errors), errors)

    def test_runtime_resource_with_a_sample_is_deferred_for_stage3_materialization(self):
        from pipeline._legacy_loader import STAGE2_ROOT, load_file
        module = load_file(STAGE2_ROOT/'code/task_contract.py', '_resource_delivery_contract', stage=2)
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp); (package/'common_task/evaluation/fixtures').mkdir(parents=True)
            (package/'common_task/environment/input/data').mkdir(parents=True)
            (package/'common_task/evaluation/fixtures/sample.txt').write_text('sample')
            # This deliberately schema-compatible placeholder must not count as
            # the formal runtime resource just because it is already present.
            (package/'common_task/environment/input/data/formal.txt').write_text('sample')
            (package/'common_task/evaluation/scoring_smoke.json').write_text(
                json.dumps({'resource_bindings': {'formal': 'sample'}}))
            source = {'id': 'formal', 'role': 'input', 'origin': 'analytical',
                      'visibility': 'public', 'identity': 'runtime materialized control', 'version': '1',
                      'availability': 'runtime_download',
                      'package_path': 'common_task/environment/input/data/formal.txt',
                      'representative_sample': 'sample', 'content': {'format': 'text'},
                      'prepare': {'command': [sys.executable, '{package}/common_task/evaluation/prepare.py'],
                                  'validate': [sys.executable, '{package}/common_task/evaluation/validate.py'],
                                  'access': 'local control', 'timeout_sec': 30}}
            sample = {'id': 'sample', 'role': 'fixture', 'origin': 'fixture', 'visibility': 'private',
                      'identity': 'small control sample', 'version': '1', 'availability': 'bundled',
                      'package_path': 'common_task/evaluation/fixtures/sample.txt', 'content': {'format': 'text'}}
            (package/'common_task/evaluation/prepare.py').write_text('from pathlib import Path\n')
            (package/'common_task/evaluation/validate.py').write_text('from pathlib import Path\n')
            missing, deferred = module.resource_delivery(package, {'data': {'sources': [source, sample]},
                'runtime': {'data_dir': '/task/problem/data'},
                'framework': {'public_dir': 'common_task/environment/input'},
                'verification': {'scoring_smoke': {'config_path': 'common_task/evaluation/scoring_smoke.json'}}})
            self.assertEqual(missing, [], missing)
            self.assertEqual([row['id'] for row in deferred], ['formal'])
            self.assertEqual(deferred[0]['sample_id'], 'sample')

    def test_unpinned_deferred_bytes_are_materialized_even_when_structurally_valid(self):
        from pipeline.stage2_construction.contracts.resource_contract import prepare
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); value = root/'common_task/environment/input/data/value.txt'
            value.parent.mkdir(parents=True); value.write_text('placeholder')
            item = {'id': 'measurements', 'role': 'input', 'origin': 'analytical',
                    'visibility': 'public', 'availability': 'runtime_download',
                    'identity': 'independent arithmetic control', 'version': '1',
                    'package_path': value.relative_to(root).as_posix(), 'content': {'format': 'text'},
                    'prepare': {'command': [sys.executable, '-c',
                                'from pathlib import Path; Path("{output}").write_text(str(2+2))'],
                                'validate': [sys.executable, '-c',
                                'from pathlib import Path; assert Path("{output}").is_file()'],
                                'timeout_sec': 30, 'access': 'Local analytical infrastructure control'}}
            spec = {'data': {'sources': [item]}}
            proof = prepare(root, spec)
            self.assertEqual(value.read_text(), '4')
            self.assertEqual(proof[0]['sha256'], hashlib.sha256(b'4').hexdigest())
            # A pinned, matching resource can be reused without reacquisition.
            item['sha256'] = hashlib.sha256(b'4').hexdigest()
            item['prepare']['command'] = [sys.executable, '-c', 'raise SystemExit(9)']
            prepare(root, spec)
            # An unpinned schema-compatible cache never hides a broken recipe.
            item.pop('sha256')
            with self.assertRaisesRegex(Exception, 'returned non-zero exit status'):
                prepare(root, spec)

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
