import unittest
import hashlib
import json
import tempfile
from pathlib import Path

from pipeline.orchestration.trajectory_quality import (T0_WEIGHTS, score_trajectory,
    score_atif_trajectory, summarize_semantic_review)


class TrajectoryQualityTests(unittest.TestCase):
    def test_external_process_points_and_coverage_share_the_same_bound_dimensions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'trajectory.json'
            trajectory = {'steps': [{'step_id': 1, 'source': 'agent',
                'message': 'Checked actual inputs before computation.',
                'tool_calls': [{'tool_call_id': 'read1'}],
                'observation': {'results': [{'source_call_id': 'read1', 'content': 'observed actual inputs'}]}}]}
            path.write_text(json.dumps(trajectory))
            review = {'trajectory_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'criteria': {
                name: {'score': 4, 'reason': 'Supported process behavior',
                       'evidence': [{'step_id': 1, 'quote': 'Checked actual inputs before computation'}]}
                for name in T0_WEIGHTS if name not in {'formal_input_use', 'data_integrity', 'contract_adherence',
                    'real_execution', 'validation_and_recovery', 'claim_support'}}}
            report = score_atif_trajectory(trajectory, trajectory_path=path, semantic_review=review)
            self.assertEqual(report['observed_score'], 44)
            self.assertEqual(report['observed_max_score'], 52)
            self.assertEqual(report['coverage'], 0.52)
            self.assertIsNone(report['score'])
            self.assertIn('formal_input_use', report['unknown_dimensions'])
            path.write_text('{"steps":[]}')
            changed = score_atif_trajectory(trajectory, trajectory_path=path, semantic_review=review)
            self.assertEqual(changed['observed_max_score'], 12)

    def test_native_receipts_verify_data_and_accept_zero_metric(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root/'data.json'; data.write_text('[1,2,3]')
            manifest = root/'input.json'
            manifest.write_text(json.dumps({'task_id': 'native', 'formal_resources_verified': True,
                'formal_resources': [{'id': 'input', 'path': str(data),
                    'sha256': hashlib.sha256(data.read_bytes()).hexdigest()}]}))
            run = root/'run.json'
            run.write_text(json.dumps({'task_id': 'native', 'diagnostic_only': False,
                'effective_input': str(manifest),
                'effective_input_sha256': hashlib.sha256(manifest.read_bytes()).hexdigest()}))
            bundle = root/'bundle'; bundle.mkdir(); (bundle/'manifest.json').write_text('{}')
            row = {'solver': {'status': 'evidence_exported'}, 'judge': {'is_valid': True},
                'score_status': 'success', 'scientific_score': 0.0,
                'artifacts': {'input_manifest': str(manifest), 'run_manifest': str(run),
                    'bundle': {'path': str(bundle), 'manifest_sha256':
                        hashlib.sha256((bundle/'manifest.json').read_bytes()).hexdigest()}}}
            report = score_trajectory(row)
            self.assertEqual(report['dimensions']['formal_input_use']['score'], 12)
            self.assertEqual(report['dimensions']['data_integrity']['score'], 12)
            self.assertEqual(report['dimensions']['validation_and_recovery']['score'], 9)
            data.write_text('[999]')
            report = score_trajectory(row)
            self.assertIsNone(report['dimensions']['data_integrity']['score'])
            (bundle/'manifest.json').write_text('{"tampered":true}')
            report = score_trajectory(row)
            self.assertIsNone(report['dimensions']['validation_and_recovery']['score'])
            run.write_text('{"task_id":"different"}')
            self.assertIsNone(score_trajectory(row)['dimensions']['formal_input_use']['score'])

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
    def test_cell_schema_is_not_a_journal_citation_but_citation_still_fails(self):
        from pipeline.stage2_construction.validation.instruction import solver_visible_leakage_errors
        from pipeline._legacy_loader import STAGE2_ROOT, load_file
        compat = load_file(STAGE2_ROOT/'code/instruction_checks.py', '_cell_citation_compat', stage=2)
        for check in [solver_visible_leakage_errors, compat.solver_visible_leakage_errors]:
            self.assertEqual(check('obs contains cell-type label and batch columns. Shape: 2017 cells.'), [])
            self.assertTrue(check('Source: Science. 2017;355(6328):950-954.'))

    def test_bare_and_json_encoded_doi_are_source_locators(self):
        from pipeline.stage2_construction.validation.instruction import solver_visible_leakage_errors
        for text in ('10.12345/source.paper', '{"doi": "10.12345/source.paper"}'):
            self.assertTrue(solver_visible_leakage_errors(text))

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
