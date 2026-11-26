import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from pipeline.orchestration.semantic_scoring import (
    checked_review, evidence_spans, behavior_view, segment_views, checked_segment_facts, score_process,
)
from pipeline.orchestration.trajectory_quality import T_PROCESS_WEIGHTS


class SemanticScoringTests(unittest.TestCase):
    def test_segments_cover_complete_steps_and_reject_oversized_steps(self):
        trace = {'steps': [{'step_id': index, 'message': '完整观察与计算结果' * 20} for index in range(7)]}
        parts = segment_views(trace, max_bytes=900)
        self.assertGreater(len(parts), 1)
        self.assertEqual([s for part in parts for s in part['steps']], trace['steps'])
        self.assertEqual(trace['steps'][0]['message'], '完整观察与计算结果' * 20)
        with self.assertRaisesRegex(ValueError, 'no silent truncation'):
            segment_views(trace, max_bytes=20)

    def test_segment_facts_require_valid_spans_and_never_carry_point_scores(self):
        spans = {'real': {'step_id': 1, 'quote': 'Read the formal input before computing'}}
        raw = {'criteria': {name: {'score': 5, 'observations': [
            {'score': 5, 'reason': 'Observed input inspection', 'evidence': [{'span_id': 'real'}]}]}
            for name in T_PROCESS_WEIGHTS}}
        result = checked_segment_facts(raw, spans)
        self.assertNotIn('score', json.dumps(result))
        raw['criteria']['strategy_choice']['observations'][0]['evidence'] = ['real']
        self.assertEqual(checked_segment_facts(raw, spans), result)
        raw['criteria']['strategy_choice']['observations'][0]['evidence'] = [{'span_id': 'invented'}]
        with self.assertRaisesRegex(ValueError, 'unknown span'):
            checked_segment_facts(raw, spans)

    def test_long_review_reads_all_segments_then_grades_once_without_private_material(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); trace = root/'trace.json'; env = root/'env'
            trajectory = {'steps': [{'step_id': i, 'message': f'Observed complete step {i}: ' + 'x'*230_000}
                                    for i in range(3)]}
            trace.write_text(json.dumps(trajectory)); original = trace.read_bytes()
            env.write_text('BASE_URL=https://judge.example/v1\nMODEL_NAME=test\nAPI_KEY=dummy\n')
            spec = {'task_id': 'test', 'documents': [
                {'role': 'task_description', 'path': 'instruction.md', 'text': 'Compute from formal data'},
                {'role': 'private_reference', 'path': 'hidden', 'text': 'DO_NOT_SEND_PRIVATE_REFERENCE'}]}
            calls = []
            def judge(**kwargs):
                kwargs['output'].mkdir(parents=True, exist_ok=True)
                view = kwargs['content']; calls.append(view)
                ref = {'span_id': next(iter(view['evidence_spans']))}
                if 'trajectory_segment' in view:
                    return {'criteria': {name: {'observations': [
                        {'reason': 'Observed intermediate behavior', 'evidence': [ref]}]}
                        for name in T_PROCESS_WEIGHTS}}, {'input_tokens': 10}
                self.assertIn('chronological_segment_observations', view)
                return {'criteria': {name: {'score': 4, 'reason': 'Global process judgment', 'evidence': [ref]}
                                     for name in T_PROCESS_WEIGHTS}}, {'input_tokens': 20}
            with patch('pipeline.orchestration.semantic_scoring._call_judge', side_effect=judge):
                result = score_process(task_spec=spec, trajectory=trace, env_file=env, output=root/'review')
            self.assertEqual(len(calls), 4)
            self.assertEqual([s for call in calls[:-1] for s in call['trajectory_segment']['steps']], trajectory['steps'])
            self.assertNotIn('DO_NOT_SEND_PRIVATE_REFERENCE', json.dumps(calls))
            self.assertEqual(result['score'], 32)
            self.assertEqual(trace.read_bytes(), original)
            coverage = json.loads((root/'review/segment-coverage.json').read_text())
            self.assertEqual(coverage['covered_step_count'], 3)
            self.assertEqual(result['review_protocol'], 'complete_segments_then_global_judgment_v1')

    def test_behavior_projection_preserves_all_steps_commands_and_results(self):
        text='Observed data and the complete scientific result.\n'*100
        trace={'steps':[{'step_id':9,'source':'agent','message':text,'reasoning_content':'Choose a method',
            'tool_calls':[{'tool_call_id':'call1','function_name':'Bash',
                           'arguments':{'command':'python run.py'},'extra':{'raw_arguments':'duplicate'}}],
            'observation':{'results':[{'source_call_id':'call1','content':text}]},
            'extra':{'provider':'duplicate telemetry'},'metrics':{'tokens':123}}]}
        view=behavior_view(trace)
        self.assertEqual(len(view['steps']),len(trace['steps']))
        self.assertEqual(view['steps'][0]['message'],text)
        self.assertEqual(view['steps'][0]['observation'],trace['steps'][0]['observation'])
        self.assertEqual(view['steps'][0]['tool_calls'][0]['arguments'],{'command':'python run.py'})
        self.assertNotIn('extra',view['steps'][0])
        self.assertNotIn('extra',view['steps'][0]['tool_calls'][0])
        self.assertIn('extra',trace['steps'][0])

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
            review['criteria']['observation_evidence']['evidence'] = [identifier]
            self.assertEqual(checked_review(review, path)['criteria']['observation_evidence']['score'], 4)
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
