"""Text-only final F1--F8 scoring with locally checked trajectory evidence.

This scorer sends only the supplied public task documents and trajectory. It
never runs a Solver or imports task-specific evaluator code. Native workflow
results and source trajectories remain immutable.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit
from typing import Mapping

from pipeline.orchestration.trajectory_quality import T_PROCESS_WEIGHTS, _bound_semantic_scores

ROOT = Path(__file__).resolve().parents[3]


def behavior_view(trajectory: dict) -> dict:
    """Keep all ATIF behavior text; omit duplicate provider/usage telemetry."""
    steps = []
    for original in trajectory['steps']:
        if not isinstance(original, Mapping):
            raise ValueError('ATIF steps must be objects')
        step = {key: copy.deepcopy(original[key]) for key in
                ('step_id', 'source', 'role', 'message', 'reasoning_content', 'observation')
                if key in original}
        if 'tool_calls' in original:
            step['tool_calls'] = [{key: copy.deepcopy(value) for key, value in call.items()
                                   if key != 'extra'} for call in original['tool_calls']]
        steps.append(step)
    return {'steps': steps}


def evidence_spans(trajectory: dict) -> dict:
    """Offer exact pointers so reviewers need not retype long escaped logs."""
    def strings(value):
        if isinstance(value, str): yield value
        elif isinstance(value, Mapping):
            for item in value.values(): yield from strings(item)
        elif isinstance(value, list):
            for item in value: yield from strings(item)
    spans = {}
    for step in trajectory.get('steps', []):
        if not isinstance(step, Mapping) or step.get('step_id') is None: continue
        texts = [s for s in strings(step) if len(s.strip()) >= 10][:8]
        for index, text in enumerate(texts):
            for offset in sorted({0, max(0, len(text)//2-80), max(0, len(text)-160)}):
                quote = text[offset:offset+160]
                if len(quote.strip()) < 10: continue
                identifier = f"s{step['step_id']}-{index}-{offset}"
                spans[identifier] = {'step_id': step['step_id'], 'quote': quote}
        if len(spans) >= 8000: break
    return spans


def _criterion_rows(criteria: Mapping) -> dict:
    """Flatten misplaced criterion objects only when their names are unique."""
    found = {}
    def visit(value):
        if isinstance(value, Mapping):
            for name, item in value.items():
                if name in T_PROCESS_WEIGHTS and isinstance(item, Mapping):
                    if name in found:
                        raise ValueError('ambiguous duplicate criterion: '+name)
                    found[name] = item
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(criteria)
    return found


def checked_review(raw: Mapping, trajectory: Path, *, span_map: dict | None = None) -> dict:
    if not isinstance(raw, Mapping): raise ValueError('judge output must be an object')
    raw = json.loads(json.dumps(raw))
    if isinstance(raw.get('criteria'), Mapping):
        raw['criteria'] = _criterion_rows(raw['criteria'])
    spans = span_map if span_map is not None else evidence_spans(json.loads(trajectory.read_bytes()))
    criteria = raw.get('criteria', {})
    for row in criteria.values() if isinstance(criteria, Mapping) else []:
        if not isinstance(row, Mapping) or not isinstance(row.get('evidence'), list): continue
        row['evidence'] = [dict(spans[identifier], span_id=identifier) if identifier in spans else ref
                           for ref in row['evidence']
                           for identifier in [ref if isinstance(ref, str) else
                                              ref.get('span_id') if isinstance(ref, Mapping) else None]]
    review = {'trajectory_sha256': hashlib.sha256(trajectory.read_bytes()).hexdigest(),
              'criteria': raw.get('criteria', {})}
    accepted = _bound_semantic_scores(review, trajectory)
    unknown = [name for name in T_PROCESS_WEIGHTS if name not in accepted]
    criteria = {}
    for name in T_PROCESS_WEIGHTS:
        row = review['criteria'].get(name, {}) if isinstance(review['criteria'], Mapping) else {}
        criteria[name] = dict(row) if isinstance(row, Mapping) else {}
        criteria[name]['score'] = accepted.get(name)
        if name in unknown:
            criteria[name]['validation_status'] = 'unknown_or_unbound_evidence'
    return {**review, 'schema_version': 'pipeline-process-review-v1', 'criteria': criteria,
            'observed_score': sum(accepted.values()), 'observed_max_score': 5*len(accepted),
            'score': sum(accepted.values()) if not unknown else None,
            'unknown_dimensions': unknown, 'environment': raw.get('environment', []),
            'review_status': 'external_scored_exact_quotes_checked'}


def _env(path):
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line: continue
        key, value = line.split('=', 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def segment_views(projected: dict, *, max_bytes: int = 400_000) -> list[dict]:
    """Partition complete behavior steps, with no sampling or text truncation."""
    if max_bytes < 1:
        raise ValueError('segment byte budget must be positive')
    groups, current, size = [], [], 0
    for step in projected['steps']:
        needed = len(json.dumps(step, ensure_ascii=False).encode())
        if needed > max_bytes:
            raise ValueError('one trajectory step exceeds the segment budget; no silent truncation')
        if current and size + needed > max_bytes:
            groups.append({'steps': current})
            current, size = [], 0
        current.append(step)
        size += needed
    if current:
        groups.append({'steps': current})
    return groups


def checked_segment_facts(raw: Mapping, spans: dict) -> dict:
    """Accept only evidence-bound observations, never provisional point scores."""
    if not isinstance(raw, Mapping) or not isinstance(raw.get('criteria'), Mapping):
        raise ValueError('segment reviewer must return criterion observations')
    criteria, rows = {}, _criterion_rows(raw['criteria'])
    for name in T_PROCESS_WEIGHTS:
        row = rows.get(name)
        if not isinstance(row, Mapping) or not isinstance(row.get('observations'), list):
            raise ValueError('segment review omitted observations for '+name)
        observations = []
        for fact in row['observations']:
            if (not isinstance(fact, Mapping) or not isinstance(fact.get('reason'), str)
                    or not fact['reason'].strip() or not isinstance(fact.get('evidence'), list)
                    or not fact['evidence']):
                raise ValueError('segment observation needs a rationale and exact span references')
            references = []
            for ref in fact['evidence']:
                identifier = ref if isinstance(ref, str) else ref.get('span_id') if isinstance(ref, Mapping) else None
                if not isinstance(identifier, str) or identifier not in spans:
                    raise ValueError('segment observation cites an unknown span')
                references.append({'span_id': identifier})
            observations.append({'reason': fact['reason'], 'evidence': references})
        criteria[name] = {'observations': observations}
    return {'criteria': criteria}


def _call_judge(*, content: dict, prompt: str, env: dict, output: Path,
                proxy_url: str | None, use_cache: bool = False) -> tuple[dict, dict | None]:
    endpoint = (env.get('BASE_URL') or env.get('OPENAI_BASE_URL') or '').rstrip('/')
    parsed = urlsplit(endpoint)
    if (parsed.scheme not in {'https','http'} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('configure a chat API origin without embedded credentials')
    if not endpoint.endswith('/chat/completions'): endpoint += '/chat/completions'
    model = env.get('MODEL_NAME') or env.get('OPENAI_MODEL')
    key = env.get('API_KEY') or env.get('OPENAI_API_KEY') or env.get('OPENROUTER_API_KEY')
    if not model or not key: raise ValueError('configure the final judge model and API credential')
    content_text = json.dumps(content, ensure_ascii=False)
    request_identity = {'model':model,
        'public_view_sha256':hashlib.sha256(content_text.encode()).hexdigest(),
        'prompt_sha256':hashlib.sha256(prompt.encode()).hexdigest()}
    if use_cache and (output/'request-identity.json').is_file():
        if (json.loads((output/'request-identity.json').read_text()) != request_identity
                or (output/'request-view.json').read_text() != content_text+'\n'):
            raise ValueError('saved judge response has a different request identity')
    if use_cache and (output/'api-response.json').is_file():
        response_bytes = (output/'api-response.json').read_bytes()
        envelope = json.loads(response_bytes)
        message = envelope['choices'][0]['message']
        if message.get('tool_calls') or message.get('function_call'):
            raise ValueError('saved judge response contains forbidden tool calls')
        (output/'cache-reuse.json').write_text(json.dumps({
            'request_identity': request_identity,
            'response_sha256': hashlib.sha256(response_bytes).hexdigest(),
            'reason': 'identical request reused while resuming an unfinished review'}, indent=2)+'\n')
        return json.loads(message['content']), envelope.get('usage')
    body = {'model': model, 'temperature': 0, 'response_format': {'type': 'json_object'},
        'max_tokens': 6000, 'messages': [{'role':'system','content':prompt}, {'role':'user','content':content_text}]}
    request = urllib.request.Request(endpoint, data=json.dumps(body, ensure_ascii=False).encode(),
        headers={'Content-Type':'application/json','Authorization':'Bearer '+key}, method='POST')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({'http':proxy_url,'https':proxy_url})) if proxy_url else urllib.request.build_opener()
    output.mkdir(parents=True, exist_ok=True)
    # The archived request contains only the explicit public view and behavior;
    # credentials are attached as transport headers and are never archived.
    (output/'request-view.json').write_text(content_text+'\n')
    (output/'request-identity.json').write_text(json.dumps(request_identity,indent=2)+'\n')
    transport = []
    for attempt in range(3):
        try:
            with opener.open(request, timeout=600) as response: data = response.read()
            break
        except urllib.error.HTTPError as exc:
            error_body = exc.read()
            (output/f'http-error-{attempt+1}.bin').write_bytes(error_body)
            lowered = error_body.lower()
            transport.append({'attempt': attempt+1, 'http_status': exc.code,
                'response_sha256': hashlib.sha256(error_body).hexdigest(),
                'context_limit': any(token in lowered for token in
                    (b'context length', b'context window', b'maximum context', b'too many tokens',
                     b'range of input length'))})
            (output/'transport-events.json').write_text(json.dumps(transport,indent=2)+'\n')
            if exc.code != 429 or attempt == 2: raise
            try: retry_after = float(exc.headers.get('Retry-After', '0'))
            except (ValueError, TypeError): retry_after = 0
            time.sleep(min(60, max(20*(2**attempt), retry_after)))
    (output/'api-response.json').write_bytes(data)
    envelope = json.loads(data); message = envelope['choices'][0]['message']
    if message.get('tool_calls') or message.get('function_call'): raise ValueError('judge returned forbidden tool calls')
    return json.loads(message['content']), envelope.get('usage')


def _segment_observations(*, content, prompt, env, output, proxy_url, use_cache=False):
    usages = []
    raw, usage = _call_judge(content=content, prompt=prompt, env=env, output=output,
                             proxy_url=proxy_url, use_cache=use_cache)
    usages.append(usage)
    try:
        return checked_segment_facts(raw, content['evidence_spans']), usages
    except ValueError as error:
        # One citation-only repair is triggered by validation, never by points.
        # Observation statements and their order must remain unchanged.
        repair_content = {**content, 'citation_repair': {
            'previous_output': raw, 'validation_error': str(error),
            'instruction': 'Correct only evidence references to actual supplied span IDs. '
                           'Preserve every observation reason and its order exactly. '
                           'Do not remove, soften or replace observations. If no supporting span '
                           'exists, leave the unresolved citation unchanged; do not invent evidence.'}}
        repaired, usage = _call_judge(content=repair_content, prompt=prompt, env=env,
            output=output/'citation-repair-01', proxy_url=proxy_url, use_cache=use_cache)
        usages.append(usage)
        def statements(value):
            rows = _criterion_rows(value['criteria'])
            return {name: [fact['reason'] for fact in rows[name]['observations']]
                    for name in T_PROCESS_WEIGHTS}
        if statements(raw) != statements(repaired):
            raise ValueError('citation repair changed the observation statements')
        return checked_segment_facts(repaired, content['evidence_spans']), usages


def score_process(*, task_spec: dict, trajectory: Path, env_file: Path,
                  output: Path, proxy_url: str | None = None,
                  segment_bytes: int = 400_000, resume: bool = False) -> dict:
    if output.exists() and (not resume or (output/'process-review.json').exists()):
        raise ValueError('refusing to overwrite final process review: '+str(output))
    original = trajectory.read_bytes()
    evidence = json.loads(original)
    if not isinstance(evidence, dict) or not isinstance(evidence.get('steps'), list):
        raise ValueError('process review requires a parsed ATIF trajectory')
    public_roles = {'task_description', 'data_description', 'task_contract', 'provided_method', 'public_validation'}
    documents = [{k: doc[k] for k in ('role', 'path', 'text')}
                 for doc in task_spec['documents'] if doc.get('role') in public_roles]
    if not documents: raise ValueError('process review requires actual public task documents')
    projected = behavior_view(evidence)
    spans = evidence_spans(projected)
    task_view = {'task_id': task_spec['task_id'], 'documents': documents}
    payload_view = {'task': task_view, 'trajectory': projected, 'evidence_spans': spans}
    content = json.dumps(payload_view, ensure_ascii=False)
    if len(content.encode()) > 8_000_000:
        raise ValueError('trajectory review exceeds input limit; no silent truncation')
    env = _env(env_file)
    prompt = (ROOT/'prompts/evaluation/trajectory_process_spans.md').read_text()
    output.mkdir(parents=True, exist_ok=resume)
    segmented = len(content.encode()) > 600_000
    identity = {'task_id': task_spec['task_id'],
        'model': env.get('MODEL_NAME') or env.get('OPENAI_MODEL'),
        'trajectory_sha256': hashlib.sha256(original).hexdigest(),
        'trajectory_view': 'all ATIF behavior fields; provider/usage telemetry omitted',
        'original_step_count': len(evidence['steps']), 'transmitted_step_count': len(projected['steps']),
        'public_view_sha256': hashlib.sha256(content.encode()).hexdigest(),
        'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
        'review_protocol': 'complete_segments_then_global_judgment_v1' if segmented else 'full_behavior_v1'}
    if resume and (output/'request-identity.json').is_file():
        if json.loads((output/'request-identity.json').read_text()) != identity:
            raise ValueError('unfinished review identity differs from the requested review')
    (output/'request-identity.json').write_text(json.dumps(identity,indent=2)+'\n')
    usages = []
    if segmented:
        groups = segment_views(projected, max_bytes=segment_bytes)
        segment_prompt = (ROOT/'prompts/evaluation/trajectory_segment_evidence.md').read_text()
        segment_facts, selected_spans, coverage = [], {}, []
        offset = 0
        for index, group in enumerate(groups):
            segment_spans = evidence_spans(group)
            view = {'task': task_view, 'trajectory_segment': group, 'evidence_spans': segment_spans,
                    'position': {'segment_index': index, 'segment_count': len(groups),
                                 'first_step_index': offset, 'step_count': len(group['steps']),
                                 'total_step_count': len(projected['steps'])}}
            directory = output/'segments'/f'{index:04d}'
            facts, segment_usages = _segment_observations(content=view, prompt=segment_prompt,
                env=env, output=directory, proxy_url=proxy_url, use_cache=resume)
            facts['position'] = view['position']
            (directory/'bound-facts.json').write_text(json.dumps(facts,ensure_ascii=False,indent=2)+'\n')
            segment_facts.append(facts);usages.extend(segment_usages)
            for row in facts['criteria'].values():
                for fact in row['observations']:
                    for ref in fact['evidence']:
                        selected_spans[ref['span_id']] = segment_spans[ref['span_id']]
            coverage.append(view['position']);offset += len(group['steps'])
        if offset != len(projected['steps']) or trajectory.read_bytes() != original:
            raise ValueError('trajectory changed or segment coverage is incomplete')
        (output/'segment-coverage.json').write_text(json.dumps({
            'trajectory_sha256': identity['trajectory_sha256'], 'covered_step_count': offset,
            'total_step_count': len(projected['steps']), 'segments': coverage,
            'segment_prompt_sha256': hashlib.sha256(segment_prompt.encode()).hexdigest()},indent=2)+'\n')
        payload_view = {'task':task_view, 'chronological_segment_observations':segment_facts,
                        'evidence_spans':selected_spans}
        prompt += ('\nThe entire original trajectory has been reviewed in ordered complete segments. '
                   'The attached observations are evidence extraction, not grades. Judge the whole process '
                   'once, accounting for later recovery and cross-segment state. Do not average segments, '
                   'deduct twice for repeated consequences, or infer success/failure from missing observations. '
                   'Use null when the extracted evidence cannot support a criterion. Cite only supplied span IDs.\n')
        spans = selected_spans
    raw, usage = _call_judge(content=payload_view, prompt=prompt, env=env,
                            output=output/'final-review', proxy_url=proxy_url, use_cache=resume)
    if trajectory.read_bytes() != original:
        raise ValueError('trajectory changed during process review')
    result = checked_review(raw, trajectory, span_map=spans)
    result['usage'] = usage;result['segment_usage'] = usages
    result['task_id'] = task_spec['task_id'];result['review_protocol'] = identity['review_protocol']
    (output/'process-review.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    return result


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--task-specs',type=Path,required=True);p.add_argument('--task-id',required=True)
    p.add_argument('--trajectory',type=Path,required=True);p.add_argument('--env-file',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--proxy-url')
    p.add_argument('--resume-incomplete', action='store_true',
                   help='Reuse identical archived requests only; completed reviews are never overwritten')
    args=p.parse_args(argv); matches=[json.loads(line) for line in args.task_specs.read_text().splitlines() if line.strip() and json.loads(line)['task_id']==args.task_id]
    if len(matches)!=1: raise ValueError('expected exactly one public task specification')
    result=score_process(task_spec=matches[0],trajectory=args.trajectory,env_file=args.env_file,output=args.output,
                         proxy_url=args.proxy_url,resume=args.resume_incomplete)
    print(json.dumps({'task_id':args.task_id,'score':result['score'],'unknown_dimensions':result['unknown_dimensions']}))


if __name__=='__main__': main()
