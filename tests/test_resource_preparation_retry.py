import unittest
from pipeline._legacy_loader import STAGE2_ROOT, load_file


class ResourcePreparationPolicyTests(unittest.TestCase):
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
