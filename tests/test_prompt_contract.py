from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ROOT / 'prompts'


class PromptContractTests(unittest.TestCase):
    def test_prompts_use_runtime_paths_only(self):
        for path in PROMPTS.rglob('*.md'):
            text = path.read_text(encoding='utf-8')
            self.assertNotIn('/u01/zhengyawen/pipline', text, path)
            self.assertNotIn('stage1-datacard&downlond', text, path)
            self.assertNotIn('stage2-TasksConstruction&Verify', text, path)
            self.assertNotIn('stage3-TasksRun&Verify', text, path)

    def test_stage2_compatibility_tools_are_named_stably(self):
        text = (PROMPTS / 'stage2/01_construct.md').read_text(encoding='utf-8')
        for name in ('paths.json', 'pipeline_tools/authoring_package.py',
                     'pipeline_tools/preflight.py', 'pipeline_tools/scoring_smoke.py'):
            self.assertIn(name, text)

    def test_solver_prompt_keeps_container_boundary(self):
        text = (PROMPTS / 'stage3/03_solver_instruction.md').read_text(encoding='utf-8')
        self.assertIn('/task/problem/', text)
        self.assertIn('/workspace/', text)
        self.assertIn('assets/solver_guidance.mapping.json', text)


if __name__ == '__main__':
    unittest.main()
