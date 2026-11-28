"""Opt-in real Docker check for the Stage3 preparation path contract."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from pipeline._legacy_loader import STAGE2_ROOT, load_file


@unittest.skipUnless(os.environ.get('PIPELINE_TEST_PREPARATION_IMAGE'), 'explicit local preparation image required')
class PreparationContainerIntegrationTests(unittest.TestCase):
    def test_public_environment_and_private_path_write_only_disposable_copy(self):
        module = load_file(STAGE2_ROOT/'code/prepare_resources.py', '_preparation_container_integration', stage=2)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root/'source'; source.mkdir()
            sources = []
            for rid, role, path, target in [
                ('input', 'input', 'common_task/environment/input/data/value.txt',
                 "Path(os.environ['DATA_DIR'])/'value.txt'"),
                ('reference', 'reference', 'common_task/evaluation/reference.txt',
                 "Path('/task/evaluation/reference.txt')"),
            ]:
                sources.append({'id': rid, 'role': role, 'visibility': 'public' if role == 'input' else 'private',
                                'origin': 'analytical', 'identity': 'infrastructure arithmetic control, not benchmark data',
                                'version': '1', 'availability': 'runtime_download', 'package_path': path,
                                'content': {'format': 'text'}, 'sha256': hashlib.sha256(b'4').hexdigest(),
                                'prepare': {'command': ['python', '-c',
                                    'import os; from pathlib import Path; p='+target+
                                    '; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(str(2+2))'],
                                    'validate': ['python', '-c', 'from pathlib import Path; assert Path("{output}").read_text()=="4"'],
                                    'access': 'No external resources: deterministic arithmetic for infrastructure test',
                                    'timeout_sec': 30}})
            spec = {'assessment_contract_version': 1, 'runtime': {}, 'data': {'sources': sources}}
            original = json.dumps(spec).encode(); (source/'execution.json').write_bytes(original)
            result = module.prepare_package(source, root/'run', image=os.environ['PIPELINE_TEST_PREPARATION_IMAGE'], timeout=60)
            self.assertEqual(result['status'], 'pass', (result, (root/'run/prepare.log').read_text()))
            prepared = Path(result['package'])
            for item in sources:
                self.assertFalse((source/item['package_path']).exists())
                self.assertEqual((prepared/item['package_path']).read_text(), '4')
            self.assertEqual((source/'execution.json').read_bytes(), original)
            self.assertEqual({r['id'] for r in result['resources']}, {'input', 'reference'})
            self.assertEqual(result['preparation_paths']['DATA_DIR'], '/task/problem/data')

