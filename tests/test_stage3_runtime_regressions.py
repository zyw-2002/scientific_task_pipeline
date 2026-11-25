import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pipeline._legacy_loader import STAGE3_ROOT, implementation_paths, load_file
from pipeline.stage3_execution.evidence.result_accounting import classify, summarize
from pipeline.stage3_execution.runtime.outcome import trial_outcome
from pipeline.stage3_execution.judge.completion import complete_review


class Stage3RuntimeRegressionTests(unittest.TestCase):
    def test_long_running_qwen_settings_reach_harbor(self):
        with implementation_paths(stage=3):
            core = load_file(STAGE3_ROOT / 'code/pipeline.py', '_test_stage3_runtime', stage=3)
            options = core.validate_agent_kwargs('qwen-coder', {
                'skip_loop_detection': True, 'shell_heartbeat_interval_ms': 60000})
            command = core.harbor_command(package=Path('/tmp/task'), jobs_dir=Path('/tmp/jobs'),
                job_name='test', agent='qwen-coder', model='test', agent_kwargs=options)
        self.assertIn('skip_loop_detection=true', command)
        self.assertIn('shell_heartbeat_interval_ms=60000', command)

    def test_trial_exception_is_failure_even_if_harbor_cli_succeeds(self):
        with tempfile.TemporaryDirectory() as temp:
            trial = Path(temp)
            (trial / 'result.json').write_text(json.dumps({
                'finished_at': '2026-11-25T00:00:00Z',
                'exception_info': {'exception_type': 'NonZeroAgentExitCodeError'}}))
            self.assertEqual(trial_outcome(trial)['execution_status'], 'failed')
            (trial / 'result.json').write_text(json.dumps({
                'finished_at': '2026-11-25T00:00:00Z', 'exception_info': None}))
            self.assertEqual(trial_outcome(trial)['execution_status'], 'completed')

    def test_diagnostic_scores_never_become_verified_scores(self):
        result = {'score_status': 'success', 'scientific_score': {'auc': .7},
                  'is_valid': True, 'diagnostic_only': True}
        self.assertFalse(classify(result)['eligible_for_comparison'])
        self.assertEqual(summarize([result])['scored_runs'], 1)
        self.assertEqual(summarize([result])['comparable_runs'], 0)

    def test_late_judge_completion_preserves_run_and_checks_hashes(self):
        import hashlib
        with tempfile.TemporaryDirectory() as temp, implementation_paths(stage=3):
            root = Path(temp)
            bundle = root / '04_run_verify/evidence'
            review = root / '04_run_verify/review'
            bundle.mkdir(parents=True)
            review.mkdir()
            request = bundle / 'request.json'
            request.write_text('{}')
            verdict = review / 'judge_verdict.json'
            verdict.write_text(json.dumps({'score_attempt_used': 'a1'}))
            digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
            manifest = {'status': 'complete', 'mode': 'text_only_api', 'is_valid': True,
                        'score_attempt_used': 'a1', 'provenance': {
                            'request_sha256': digest(request), 'verdict_sha256': digest(verdict)}}
            (review / 'judge_manifest.json').write_text(json.dumps(manifest))
            artifacts = {}
            for name in ('run_manifest', 'run_export', 'trajectory', 'timeline'):
                path = root / name
                path.write_text('{}')
                artifacts[name] = {'path': str(path), 'sha256': digest(path)}
            result = {'run_id': 'original', 'status': 'judge_not_configured',
                      'score_attempt_used': 'a1', 'submission_sha256': 'f' * 64,
                      'diagnostic_only': True, 'score_status': 'success',
                      'scientific_score': {'auc': .7}, 'artifacts': artifacts,
                      'solver': {'execution_status': 'completed'}}
            (root / 'result.json').write_text(json.dumps(result))
            frozen = {'score_attempt_used': 'a1', 'submission': {'sha256': 'f' * 64}}
            with patch('score_attempt.load_frozen', return_value=frozen), patch(
                    'score_attempt.validate_rule_binding', return_value={'status': 'pass'}):
                completed = complete_review(root)
                self.assertEqual(completed['status'], 'complete')
                self.assertEqual(completed['run_id'], 'original')
                self.assertFalse(completed['result_accounting']['eligible_for_comparison'])
                (root / 'trajectory').write_text('changed')
                with self.assertRaisesRegex(ValueError, 'artifact changed'):
                    complete_review(root)


if __name__ == '__main__':
    unittest.main()
