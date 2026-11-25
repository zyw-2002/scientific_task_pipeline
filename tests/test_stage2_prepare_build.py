from pathlib import Path
import json
import shutil
import tempfile
import unittest
from pipeline.stage2_construction.entrypoint import main

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'tests/fixtures/stage2'


class Stage2PrepareBuildTests(unittest.TestCase):
    def test_new_facade_builds_harbor_task_input(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            source=root/'source'; shutil.copytree(FIX/'source_bundle', source)
            output=root/'out'
            paths=root/'paths.json'; paths.write_text(json.dumps({'source_bundle':str(source),'output_dir':str(output)}))
            worker=root/'worker.json'; worker.write_text(json.dumps({
                'backend':'command','command':['/bin/true'],'base_image':'naturebench-base:v3',
                'timeout_sec':1,'build_timeout_sec':1,'cpus':1,'memory_mb':512,
                'gpus':0,'network_mode':'no-network'}))
            rc=main(['prepare-build','--paths',str(paths),'--worker-config',str(worker),
                     '--prompt01',str(ROOT/'prompts/stage2/01_construct.md')])
            self.assertEqual(rc, 0)
            for relative in ('environment/input/paths.json','environment/Dockerfile',
                             'environment/docker-compose.yaml','task.toml','worker_snapshot.json'):
                self.assertTrue((output/relative).is_file(), relative)
