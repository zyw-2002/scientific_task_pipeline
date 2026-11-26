import json
from pathlib import Path
import tempfile
import unittest
from pipeline.orchestration.semantic_scoring import checked_review
from pipeline.orchestration.trajectory_quality import T_PROCESS_WEIGHTS


class SemanticScoringTests(unittest.TestCase):
    def test_only_exact_step_quotes_contribute_to_process_points(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'trajectory.json'
            p.write_text(json.dumps({'steps':[{'step_id':1,'message':'I checked the actual inputs before computing.'}]}))
            criteria={name:{'score':4,'reason':'Supported process behavior',
                'evidence':[{'step_id':1,'quote':'checked the actual inputs before computing'}]} for name in T_PROCESS_WEIGHTS}
            result=checked_review({'criteria':criteria,'environment':[{'type':'network'}]},p)
            self.assertEqual(result['score'],32)
            self.assertEqual(result['environment'],[{'type':'network'}])
            criteria['strategy_choice']['evidence'][0]['quote']='fabricated evidence never present'
            partial=checked_review({'criteria':criteria},p)
            self.assertIsNone(partial['score'])
            self.assertIn('strategy_choice',partial['unknown_dimensions'])
            self.assertIsNone(partial['criteria']['strategy_choice']['score'])
