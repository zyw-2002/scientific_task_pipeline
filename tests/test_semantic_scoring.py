import json
from pathlib import Path
import tempfile
import unittest
from pipeline.orchestration.semantic_scoring import checked_review, evidence_spans
from pipeline.orchestration.trajectory_quality import T_PROCESS_WEIGHTS


class SemanticScoringTests(unittest.TestCase):
    def test_span_pointers_resolve_to_exact_original_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'trajectory.json'
            trajectory={'steps':[{'step_id':7,'message':'Checked the actual samples.\nPreserved the original units.'}]}
            path.write_text(json.dumps(trajectory))
            spans=evidence_spans(trajectory); identifier=next(iter(spans))
            review={'criteria':{'observation_evidence':{'score':4,'reason':'Inspected the actual inputs',
                'evidence':[{'span_id':identifier}]}}}
            result=checked_review(review,path)
            self.assertEqual(result['criteria']['observation_evidence']['score'],4)
            self.assertEqual(result['criteria']['observation_evidence']['evidence'][0]['quote'],spans[identifier]['quote'])
            review['criteria']['observation_evidence']['evidence']=[{'span_id':'invented'}]
            self.assertIsNone(checked_review(review,path)['criteria']['observation_evidence']['score'])

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
