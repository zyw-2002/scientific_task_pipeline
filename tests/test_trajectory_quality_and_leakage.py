import unittest
import hashlib
import json
import tempfile
from pathlib import Path

from pipeline.orchestration.trajectory_quality import (T0_WEIGHTS, score_trajectory,
    score_atif_trajectory, summarize_semantic_review)


class TrajectoryQualityTests(unittest.TestCase):
    def test_planning_or_completion_words_are_not_execution_evidence(self):
        report = score_atif_trajectory({'steps': [{'source': 'agent',
            'message': 'Read the data, test the results, all completed and validated.'}]})
        self.assertEqual(report['observed_score'], 0)
        self.assertIn('real_execution', report['unknown_dimensions'])
        actual = score_atif_trajectory({'steps': [{'source': 'agent',
            'tool_calls': [{'tool_call_id': 'read1', 'function_name': 'Read'}],
            'observation': {'results': [{'source_call_id': 'read1', 'content': 'actual file contents'}]}}]})
        self.assertEqual(actual['observed_score'], 12)
        self.assertIn('formal_input_use', actual['unknown_dimensions'])

    def test_semantic_scores_require_current_trajectory_and_exact_quotes(self):
        criterion = 'strategy_choice'
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'trajectory.json'
            path.write_text(json.dumps({'steps': [{'step_id': 2, 'source': 'agent',
                'message': 'I will verify the split before training the model.'}]}))
            row = {'artifacts': {'trajectory': str(path)}, 'semantic_review': {
                'scores': {criterion: 5}}}
            self.assertIsNone(score_trajectory(row)['dimensions'][criterion]['score'])
            row['semantic_review'] = {'trajectory_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'criteria': {criterion: {'score': 5, 'reason': 'Checks the split before fitting',
                    'evidence': [{'step_id': 2, 'quote': 'verify the split before training'}]}}}
            self.assertEqual(score_trajectory(row)['dimensions'][criterion]['score'], 5)
            path.write_text('{"steps": []}')
            self.assertIsNone(score_trajectory(row)['dimensions'][criterion]['score'])

    def test_tqd_v1_trajectory_weights_are_fixed(self):
        self.assertEqual(sum(T0_WEIGHTS.values()), 100.0)
        self.assertEqual(len(T0_WEIGHTS), 14)

    def test_t0_is_separate_and_penalizes_missing_execution_evidence(self):
        report = score_trajectory({'status': 'solver_failed', 'solver': {'status': 'failed'},
                                   'judge': {'failure_class': 'engineering_failure'}})
        self.assertEqual(report['score_kind'], 'trajectory')
        self.assertIsNone(report['score'])
        self.assertLess(report['observed_score'], 50)
        self.assertLess(report['coverage'], 1.0)
        self.assertIn('missing frozen input/run manifest evidence', report['findings'])

    def test_semantic_review_preserves_f1_to_f8_and_exact_quote_status(self):
        report = summarize_semantic_review({'review_status': 'model_proposed_quote_checked',
            'issues': [{'types': ['F4', 'F6'], 'all_quotes_exact': True}, {'types': ['ENV']} ]})
        self.assertEqual(report['issue_type_counts']['F4'], 1)
        self.assertEqual(report['issue_type_counts']['F6'], 1)
        self.assertEqual(report['issue_type_counts']['ENV'], 1)
        self.assertEqual(report['issues_with_exact_quotes'], 1)
        self.assertFalse(report['contributes_to_t0'])

    def test_atif_adapter_keeps_unknown_contract_dimensions_explicit(self):
        report = score_atif_trajectory({'steps': [
            {'source': 'user', 'message': 'solve the task'},
            {'source': 'agent', 'message': 'read data; run evaluator; saved output',
             'tool_calls': [{'function_name': 'run_shell_command', 'arguments': {'command': 'cat data'}}]},
        ]})
        self.assertTrue(report['assessed'])
        self.assertEqual(report['source'], 'atif_external')
        self.assertIn('contract_adherence', report['unknown_dimensions'])


class SolverLeakageTests(unittest.TestCase):
    def test_public_source_locators_and_success_claims_are_rejected(self):
        from pipeline.stage2_construction.validation.instruction import solver_visible_leakage_errors
        text = ('DOI: 10.1038/example.1234; ENA PRJEB19305 and ERR1816980; '
                'GitHub commit 8ee2777; researchers successfully recovered the file.')
        errors = solver_visible_leakage_errors(text, relative_path='instruction.md')
        self.assertGreaterEqual(len(errors), 4)

    def test_abstract_method_text_is_allowed(self):
        from pipeline.stage2_construction.validation.instruction import solver_visible_leakage_errors
        self.assertEqual(solver_visible_leakage_errors(
            'Implement the stated method, validate the schema, and report uncertainty.'), [])


if __name__ == '__main__':
    unittest.main()
