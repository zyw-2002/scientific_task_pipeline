import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pipeline._legacy_loader import STAGE2_ROOT, implementation_paths, load_file
with implementation_paths(stage=2):
    from pipeline.stage2_construction.contracts import task_contract
from pipeline.stage2_construction.validation.instruction import (
    solver_contract_errors, solver_visible_leakage_errors,
)


class PublicContractRegressionTests(unittest.TestCase):
    def test_generated_public_manifest_omits_private_resource_metadata(self):
        generator = load_file(STAGE2_ROOT/'code/framework_generator.py', '_public_manifest_generator', stage=2)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = {'data': {'mode': 'referenced', 'sources': [
                {'id': 'public_input', 'name': 'input.json', 'visibility': 'public'},
                {'id': 'hidden_reference', 'visibility': 'private',
                 'package_path': 'common_task/evaluation/reference.json',
                 'identity': 'PRIVATE_SOURCE_REFERENCE'},
                {'id': 'private_fixture', 'visibility': 'private', 'role': 'fixture'},
            ]}}
            generator._normalize_public_support_files(root, spec)
            manifest = json.loads((root/'common_task/environment/input/data_sources.json').read_text())
            self.assertEqual([r['id'] for r in manifest['sources']], ['public_input'])
            self.assertNotIn('PRIVATE_SOURCE_REFERENCE', json.dumps(manifest))
            self.assertEqual(len(spec['data']['sources']), 3)

    def test_private_collector_stays_out_of_public_contract(self):
        source = {'runtime': {'data_dir': '/task/problem/data'},
                  'submission': {'path': '/workspace/output/predictions.csv',
                                 'required_artifacts': [{'id': 'result', 'paths': ['{submission}'], 'format': 'csv'}]},
                  'verification': {'score_evidence': {'service': 'evaluator', 'artifact_source': '/task/evaluation/records'}}}
        task = {'verifier': {'collect': [{'service': 'evaluator',
                                         'command': 'python /task/evaluation/scoring_entry.py'}]}}
        inp = {'id': 'data', 'path': '/task/problem/data/data.csv', 'purpose': 'Scientific input',
               'format': 'csv', 'fields': ['id', 'value'], 'required': True}
        with patch.object(task_contract, '_inputs', return_value=[inp]):
            contract = task_contract.build_contract(Path('.'), source,
                         {'reward_metric': 'accuracy', 'reward_direction': 'higher_is_better'}, task)
        self.assertEqual(task_contract.contract_errors(contract), [])
        self.assertEqual(solver_visible_leakage_errors(json.dumps(contract)), [])
        self.assertNotIn('command', contract['evaluation'])
        self.assertEqual(task['verifier']['collect'][0]['command'], 'python /task/evaluation/scoring_entry.py')
        generator = load_file(STAGE2_ROOT/'code/framework_generator.py', '_public_contract_generator', stage=2)
        text = generator.with_solver_bindings('## Evaluation\n', contract)
        self.assertEqual(solver_contract_errors(text, contract), [])
        self.assertIn('Automatic private verification', text)
        self.assertNotIn('python evaluation_protocol.py', text)
        broken = copy.deepcopy(contract)
        broken['evaluation']['command'] = ['python', '/task/evaluation/private.py']
        self.assertTrue(task_contract.contract_errors(broken))

    def test_smoke_mapping_path_is_normalized_without_changing_expected_scores(self):
        generator = load_file(STAGE2_ROOT/'code/framework_generator.py', '_smoke_config_generator', stage=2)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); evaluation = root/'common_task/evaluation'; evaluation.mkdir(parents=True)
            fixture = evaluation/'fixtures'; fixture.mkdir(); (fixture/'good.csv').write_text('id,value\n1,2\n')
            (evaluation/'protocol.json').write_text('{}')
            smoke = {'schema_version': 'pipeline-scoring-smoke-v1',
                     'cases': [{'id': 'good', 'kind': 'correct',
                                'submission': {'path': 'fixtures/good.csv'},
                                'expected_metrics': {'accuracy': 0.9}}]}
            path = evaluation/'scoring_smoke.json'; path.write_text(json.dumps(smoke))
            generator._normalize_scoring_smoke_config(root, {})
            case = json.loads(path.read_text())['cases'][0]
            self.assertEqual(case['submission'], 'fixtures/good.csv')
            self.assertEqual(case['expected_metrics'], smoke['cases'][0]['expected_metrics'])

    def test_review_template_never_invents_acceptance_or_overwrites_findings(self):
        report_module = load_file(STAGE2_ROOT/'code/review_report.py', '_review_template_regression', stage=2)
        with tempfile.TemporaryDirectory() as tmp, implementation_paths(stage=2):
            root = Path(tmp); evaluation = root/'common_task/evaluation'; evaluation.mkdir(parents=True)
            spec = {'task_id': 'sample', 'scope_contract_version': 3,
                    'data': {'sources': [{'id': 'formal_data', 'role': 'input', 'availability': 'bundled',
                                           'content': {'format': 'json', 'item_count': 10}}]}}
            (root/'execution.json').write_text(json.dumps(spec))
            (root/'common_task/instruction.md').write_text('Scientific task')
            (evaluation/'protocol.json').write_text(json.dumps({'assessment': {'requirements': [
                {'id': 'quality', 'role': 'quality'}]}}))
            self.assertTrue(report_module.initialize_review(root))
            report = json.loads((root/'review_report.json').read_text())
            self.assertEqual(report['status'], 'blocked')
            self.assertFalse(report['definition_complete'])
            self.assertEqual(report['construction_audit']['data_paths'][0]['verdict'], 'not_reviewed')
            self.assertEqual(report['construction_audit']['shortcut_checks'][0]['requirement_ids'], ['quality'])
            self.assertEqual(report['construction_audit']['input_sha256'], {})
            original = (root/'review_report.json').read_bytes()
            self.assertFalse(report_module.initialize_review(root))
            self.assertEqual((root/'review_report.json').read_bytes(), original)
