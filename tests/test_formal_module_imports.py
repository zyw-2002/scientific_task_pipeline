import unittest


class FormalModuleImportTests(unittest.TestCase):
    def test_stage1_material_modules(self):
        from pipeline.stage1_sources.materials import data_samples, download_gate, stream_records
        from pipeline.stage1_sources.content import multimodal
        self.assertEqual(data_samples.setting('data_mode'), 'sample')
        self.assertTrue(any(callable(download_gate.__dict__.get(name)) for name in ('valid_url', 'resource_plan', 'filter_plan')))
        self.assertTrue(callable(stream_records.RecordPublisher))
        self.assertTrue(callable(multimodal.prepare_multimodal_data))

    def test_stage2_contract_and_validation_modules(self):
        from pipeline.stage2_construction.contracts import contract, evaluation_protocol, resource_contract
        from pipeline.stage2_construction.validation.instruction import validate_instruction
        self.assertEqual(contract.SCHEMA_VERSION, 'pipeline-execution-v1')
        self.assertTrue(callable(evaluation_protocol.config_errors))
        self.assertTrue(callable(resource_contract.source_errors))
        self.assertTrue(callable(validate_instruction))

    def test_stage3_runtime_evidence_modules(self):
        from pipeline.stage3_execution.evidence.score_attempt import capture_submission
        from pipeline.stage3_execution.judge.reporting import reporting_dimensions
        from pipeline.stage3_execution.submission.rules import check
        self.assertTrue(callable(capture_submission))
        self.assertTrue(callable(reporting_dimensions))
        self.assertTrue(callable(check))
