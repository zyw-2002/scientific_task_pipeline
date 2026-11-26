"""Text-only final F1--F8 scoring with locally checked trajectory evidence.

This scorer sends only the supplied public task documents and trajectory. It
never runs a Solver or imports task-specific evaluator code. Native workflow
results and source trajectories remain immutable.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit
from typing import Mapping

from pipeline.orchestration.trajectory_quality import T_PROCESS_WEIGHTS, _bound_semantic_scores

ROOT = Path(__file__).resolve().parents[3]


def checked_review(raw: Mapping, trajectory: Path) -> dict:
    if not isinstance(raw, Mapping): raise ValueError('judge output must be an object')
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


def score_process(*, task_spec: dict, trajectory: Path, env_file: Path,
                  output: Path, proxy_url: str | None = None) -> dict:
    if output.exists(): raise ValueError('refusing to overwrite final process review: '+str(output))
    evidence = json.loads(trajectory.read_bytes())
    if not isinstance(evidence, dict) or not isinstance(evidence.get('steps'), list):
        raise ValueError('process review requires a parsed ATIF trajectory')
    # Explicit whitelist: no private evaluation code/data, authoring materials,
    # previous grades or historical scores are attached by the scorer.
    public_roles = {'task_description', 'data_description', 'task_contract', 'provided_method', 'public_validation'}
    documents = [{k: doc[k] for k in ('role', 'path', 'text')}
                 for doc in task_spec['documents'] if doc.get('role') in public_roles]
    if not documents: raise ValueError('process review requires actual public task documents')
    payload_view = {'task': {'task_id': task_spec['task_id'], 'documents': documents,
        'task_text': '\n\n'.join(doc['text'] for doc in documents)},
        'trajectory': {'steps': evidence['steps']}}
    content = json.dumps(payload_view, ensure_ascii=False)
    if len(content.encode()) > 8_000_000:
        raise ValueError('trajectory review exceeds input limit; no silent truncation')
    env = _env(env_file)
    endpoint = (env.get('BASE_URL') or env.get('OPENAI_BASE_URL') or '').rstrip('/')
    parsed = urlsplit(endpoint)
    if (parsed.scheme not in {'https','http'} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('configure a chat API origin without embedded credentials')
    if not endpoint.endswith('/chat/completions'): endpoint += '/chat/completions'
    model = env.get('MODEL_NAME') or env.get('OPENAI_MODEL')
    key = env.get('API_KEY') or env.get('OPENAI_API_KEY') or env.get('OPENROUTER_API_KEY')
    if not model or not key: raise ValueError('configure the final judge model and API credential')
    prompt = (ROOT/'prompts/evaluation/trajectory_process.md').read_text()
    body = {'model': model, 'temperature': 0, 'response_format': {'type': 'json_object'},
        'max_tokens': 6000, 'messages': [{'role':'system','content':prompt}, {'role':'user','content':content}]}
    request = urllib.request.Request(endpoint, data=json.dumps(body).encode(),
        headers={'Content-Type':'application/json','Authorization':'Bearer '+key}, method='POST')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({'http':proxy_url,'https':proxy_url})) if proxy_url else urllib.request.build_opener()
    output.mkdir(parents=True)
    (output/'request-identity.json').write_text(json.dumps({'task_id':task_spec['task_id'],
        'model':model, 'trajectory_sha256':hashlib.sha256(trajectory.read_bytes()).hexdigest(),
        'public_view_sha256':hashlib.sha256(content.encode()).hexdigest(),
        'prompt_sha256':hashlib.sha256(prompt.encode()).hexdigest()},indent=2)+'\n')
    with opener.open(request, timeout=600) as response: data = response.read()
    (output/'api-response.json').write_bytes(data)
    envelope = json.loads(data); message = envelope['choices'][0]['message']
    if message.get('tool_calls') or message.get('function_call'): raise ValueError('judge returned forbidden tool calls')
    raw = json.loads(message['content'])
    result = checked_review(raw, trajectory)
    result['usage'] = envelope.get('usage')
    result['task_id'] = task_spec['task_id']
    (output/'process-review.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    return result


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--task-specs',type=Path,required=True);p.add_argument('--task-id',required=True)
    p.add_argument('--trajectory',type=Path,required=True);p.add_argument('--env-file',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--proxy-url')
    args=p.parse_args(argv); matches=[json.loads(line) for line in args.task_specs.read_text().splitlines() if line.strip() and json.loads(line)['task_id']==args.task_id]
    if len(matches)!=1: raise ValueError('expected exactly one public task specification')
    result=score_process(task_spec=matches[0],trajectory=args.trajectory,env_file=args.env_file,output=args.output,proxy_url=args.proxy_url)
    print(json.dumps({'task_id':args.task_id,'score':result['score'],'unknown_dimensions':result['unknown_dimensions']}))


if __name__=='__main__': main()
