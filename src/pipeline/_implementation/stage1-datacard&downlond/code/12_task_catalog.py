#!/usr/bin/env python3
"""Freeze the three-source paper pool, classify candidate tasks, export reviewed plans.

Inventory uses observed metadata only. Classification produces proposals, never
scientific certification. Existing active search/download outputs are read only.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

from stream_records import read_available
from task_taxonomy import classify_packet, validate_classification

CODE = Path(__file__).resolve().parent
SUCCESS = {'downloaded', 'materialized_local', 'reused_verified'}


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def object_hash(value):
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False))


def read_json(path):
    return json.loads(Path(path).read_text(), parse_constant=lambda v: (_ for _ in ()).throw(ValueError(v)))


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def snapshot_rows(path):
    """Read one opened inode; atomic publisher replacement cannot mix generations."""
    h = hashlib.sha256(); result = []
    with Path(path).open('rb') as stream:
        before = os.fstat(stream.fileno())
        for n, raw in enumerate(stream, 1):
            h.update(raw)
            if raw.strip():
                row = json.loads(raw)
                if not isinstance(row, dict):
                    raise ValueError(f'{path}:{n}: expected object')
                result.append(row)
        after = os.fstat(stream.fileno())
    if (before.st_size,before.st_mtime_ns) != (after.st_size,after.st_mtime_ns):
        raise ValueError('input was written in place during snapshot; retry: '+str(path))
    return result, {'path':str(Path(path).resolve()), 'sha256':h.hexdigest(), 'rows':len(result)}


def write_rows(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp=path.with_name('.'+path.name+'.tmp')
    temp.write_text(''.join(json.dumps(v,ensure_ascii=False,allow_nan=False)+'\n' for v in values))
    temp.replace(path)


def csv_value(value):
    if isinstance(value,(dict,list)):
        value=json.dumps(value,ensure_ascii=False)
    text='' if value is None else str(value)
    # Spreadsheet formula protection for untrusted paper titles and model text.
    return "'"+text if text.lstrip().startswith(('=','+','-','@')) else text


def write_csv(path, rows, fields):
    temp=path.with_name('.'+path.name+'.tmp')
    with temp.open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields,extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            writer.writerow({key:csv_value(row.get(key)) for key in fields})
    temp.replace(path)


def safe_id(value):
    return (re.sub(r'[^A-Za-z0-9_.-]','-',value).strip('.-')[:90] or 'paper')+'-'+digest(value)[:10]


PAPER_FIELDS=['paper_id','title','sources','doi','year','source_domains','source_difficulties',
              'paper_content_status','code_source_status','data_source_status','reported_local_files',
              'reported_sample_files','download_failures','provisional_main_domain','contribution_classification','classification_status',
              'candidate_count','scientific_evidence_status','baseline_status','run_ready']
TASK_FIELDS=['candidate_id','paper_id','sources','paper_title','title','main_domain','contribution_type',
             'secondary_tags','testable_claim','deliverable','proposed_validation','proposed_baseline',
             'limitations','evidence','classification_status','human_review','run_ready']


def inventory(args):
    out=args.catalog.resolve()
    if out.exists():
        if (out/'catalog.json').is_file():
            return {'status':'existing_frozen_inventory','catalog':str(out),
                    'created_at':read_json(out/'catalog.json')['created_at'],
                    'note':'Choose another --catalog directory for a newer source snapshot.'}
        raise ValueError('output directory exists without catalog.json')
    base, provenance=snapshot_rows(args.records)
    index={}
    for row in base:
        key=row.get('canonical_id')
        if not isinstance(key,str) or not key or key in index:
            raise ValueError('base records require unique canonical_id after cross-source deduplication')
        index[key]=row
    inputs={'base':provenance}; snapshots_started=now()
    for path in args.enriched_records:
        records, info=snapshot_rows(path)
        inputs.setdefault('enriched',[]).append(info)
        for row in records:
            if row.get('canonical_id') in index:
                previous=index[row['canonical_id']]
                merged={**previous,**row}
                # Preserve every original provenance record, even if an overlay
                # only retains a subset of the three source memberships.
                merged['sources']=list({object_hash(x):x for x in [*previous.get('sources',[]),*row.get('sources',[])]}.values())
                index[row['canonical_id']]=merged
    if args.stream_dir:
        records, stream=read_available(args.stream_dir,'08')
        if stream.get('status')=='generation_changed':
            raise ValueError('search publisher generation changed; retry snapshot')
        inputs['search_stream']=stream
        for row in records:
            if row.get('canonical_id') in index:
                previous=index[row['canonical_id']]
                merged={**previous,**row}
                merged['sources']=list({object_hash(x):x for x in [*previous.get('sources',[]),*row.get('sources',[])]}.values())
                index[row['canonical_id']]=merged
    downloads=[]
    if args.download_results:
        data,info=snapshot_rows(args.download_results);inputs['downloads']=info
        downloads=[{k:row.get(k) for k in ('canonical_id','resource_id','kind','status','local_path','bytes','sha256',
                    'data_scope','dataset_completeness','source_access','materialization_status','error','reason')}
                   for row in data if row.get('canonical_id') in index]
    by_paper=defaultdict(list)
    for item in downloads: by_paper[item['canonical_id']].append(item)
    records=list(index.values()); papers=[]
    for record in records:
        local=by_paper[record['canonical_id']]
        context=record.get('selection_context') or {}
        sources=sorted({s.get('type') for s in record.get('sources',[]) if s.get('type')})
        statuses=record.get('resource_status') or {}
        def status(kind):
            value=statuses.get(kind,{})
            return value.get('status','unknown') if isinstance(value,dict) else value or 'unknown'
        papers.append({'paper_id':record['canonical_id'],'title':record.get('title'), 'sources':sources,
            'doi':(record.get('identifiers') or {}).get('doi') or record.get('doi'),'year':record.get('year'),
            'source_domains':context.get('domains',[]),'source_difficulties':context.get('difficulties',[]),
            'paper_content_status':(record.get('paper_content') or {}).get('status','unknown'),
            'code_source_status':status('code'),'data_source_status':status('data'),
            'reported_local_files':sum(r['status'] in SUCCESS and bool(r.get('local_path')) for r in local),
            'reported_sample_files':sum(r['status'] in SUCCESS and r.get('data_scope')=='sample' for r in local),
            'download_failures':dict(Counter(r['status'] for r in local if r['status'] not in SUCCESS)),
            'contribution_classification':None,'classification_status':'not_classified','candidate_count':None,
            'scientific_evidence_status':'not_assessed','baseline_status':'not_certified','run_ready':False})
    out.mkdir(parents=True)
    write_rows(out/'work/papers.jsonl',records)
    write_rows(out/'work/material_states.jsonl',downloads)
    write_rows(out/'work/inventory.jsonl',papers)
    write_csv(out/'paper_inventory.csv',papers,PAPER_FIELDS)
    write_csv(out/'candidate_tasks.csv',[],TASK_FIELDS)
    descriptor={'schema_version':'scientific-task-catalog-v1','created_at':now(),'snapshot_started_at':snapshots_started,
        'paper_count':len(papers),'sources':dict(Counter(s for r in papers for s in r['sources'])),
        'inputs':inputs,'papers_snapshot_sha256':object_hash(records),'material_snapshot_sha256':object_hash(downloads),
        'status':'inventory_only','classification_status':'not_started','scientific_certification':'not_performed',
        'notes':['Source memberships overlap; do not add source counts to obtain unique papers.',
                 'Live search may still be running; published records do not mean completed search.',
                 'Local file availability counts reflect downloader reports, not rehashed dataset verification.',
                 'No task has been scientifically certified by this inventory.']}
    atomic_json(out/'catalog.json',descriptor)
    return {'status':'inventory_created','catalog':str(out),'paper_count':len(papers),'sources':descriptor['sources']}


def packet_for(record,args):
    fields={'title':record.get('title'),'abstract':record.get('abstract'),'year':record.get('year'),
            'identifiers':record.get('identifiers'), 'source_domains':(record.get('selection_context') or {}).get('domains')}
    text=json.dumps(fields,ensure_ascii=False,indent=2)
    text=text[:args.max_input_chars]
    documents=[{'id':'metadata','text':text,'sha256':digest(text),'scope':'paper_metadata_not_full_paper'}]
    remaining=args.max_input_chars-len(text)
    candidates=[]
    for item in (record.get('paper_content') or {}).get('local_artifacts',[]):
        if item.get('valid_file') is False or item.get('text_ready') is False:
            continue
        if item.get('text_path'): candidates.append(item['text_path'])
        if item.get('path') and str(item['path']).lower().endswith(('.txt','.md')): candidates.append(item['path'])
    document=record.get('document') or {}
    for value in [document.get('local_path'),*(document.get('local_paths') or [])]:
        if isinstance(value,str) and value.lower().endswith(('.txt','.md')): candidates.append(value)
    roots=[Path(p).resolve() for p in args.material_root]
    rejected=[]
    for raw in dict.fromkeys(candidates):
        if remaining<=0 or len(documents)>args.max_documents: break
        path=Path(raw)
        if not path.is_absolute(): path=args.records_parent/path
        if any(part.startswith('.') for part in path.parts if part not in {'.','..','/'}):
            rejected.append({'path':str(path),'reason':'private_or_hidden_path'});continue
        if any(p.is_symlink() for p in (path,*path.parents)) or not path.is_file() or not any(path.resolve().is_relative_to(root) for root in roots):
            rejected.append({'path':str(path),'reason':'missing_or_outside_explicit_material_roots'});continue
        with path.open('rb') as stream:
            before=os.fstat(stream.fileno());raw_bytes=stream.read(remaining*4+4);after=os.fstat(stream.fileno())
        if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
            rejected.append({'path':str(path),'reason':'changed_during_read'});continue
        content=raw_bytes.decode('utf-8',errors='replace')
        truncated=len(content)>remaining or len(raw_bytes)<before.st_size
        content=content[:remaining];remaining-=len(content)
        documents.append({'id':f'document_{len(documents)}','text':content,'sha256':digest(content),
                          'scope':'bounded_excerpt_not_full_document' if truncated else 'complete_text_document',
                          'path':str(path.resolve())})
    return {'paper_id':record['canonical_id'],'sources':[s.get('type') for s in record.get('sources',[])],
            'documents':documents,'max_tasks':args.max_tasks_per_paper,
            'acquisition_note':'No live retrieval; absent scientific evidence must stay unknown. Material availability does not certify a baseline.',
            'excluded_documents':rejected}


def snapshot_catalog(args):
    descriptor=read_json(args.catalog/'catalog.json')
    records,_=snapshot_rows(args.catalog/'work/papers.jsonl')
    if object_hash(records)!=descriptor['papers_snapshot_sha256']:
        raise ValueError('frozen paper snapshot changed')
    return descriptor,records


def export_views(args):
    papers,_=snapshot_rows(args.catalog/'work/inventory.jsonl')
    by_id={r['paper_id']:r for r in papers}; candidates=[]
    for path in sorted((args.catalog/'work/classifications').glob('*.json')):
        value=read_json(path);paper_id=value['paper_id']
        if paper_id not in by_id: raise ValueError('classification references unknown paper')
        paper=by_id[paper_id]
        paper['classification_status']=value['status']
        if value['status']!='classified_provisional': continue
        normalized=value['validated']
        request_path=args.catalog/'work/requests'/(path.stem+'.json')
        packet=read_json(request_path)
        if object_hash({'packet':packet,'settings':value['settings']})!=value['identity_sha256']:
            raise ValueError('classification request/settings changed: '+paper_id)
        checked=validate_classification(value['response'],packet)
        if checked!=normalized or not checked['valid']:
            raise ValueError('classification evidence/normalized fields changed: '+paper_id)
        paper['provisional_main_domain']=normalized['paper']['main_domain']
        paper['contribution_classification']=sorted({t['contribution_type'] for t in normalized['tasks']})
        paper['candidate_count']=len(normalized['tasks'])
        for item in normalized['tasks']:
            candidates.append({**item,'sources':paper['sources'],'paper_title':paper['title'],'paper_id':paper_id})
    write_csv(args.catalog/'paper_inventory.csv',papers,PAPER_FIELDS)
    write_csv(args.catalog/'candidate_tasks.csv',candidates,TASK_FIELDS)
    write_rows(args.catalog/'work/candidate_tasks.jsonl',candidates)
    summary={'papers':len(papers),'paper_classification_statuses':dict(Counter(p['classification_status'] for p in papers)),
             'candidate_tasks':len(candidates),'domain_by_contribution':dict(Counter(t['main_domain']+' / '+t['contribution_type'] for t in candidates)),
             'human_approved_contracts':0,'run_ready_tasks':0,'updated_at':now()}
    atomic_json(args.catalog/'classification_summary.json',summary)
    return summary


def _classify(args):
    descriptor,records=snapshot_catalog(args)
    if not args.all and args.limit is None and not args.paper_id:
        raise ValueError('select --limit, --paper-id or explicit --all before model classification')
    args.records_parent=Path(descriptor['inputs']['base']['path']).parent
    if args.paper_id:
        selected=[r for r in records if r['canonical_id'] in args.paper_id]
        missing=set(args.paper_id)-{r['canonical_id'] for r in selected}
        if missing: raise ValueError('paper IDs absent from frozen catalog: '+', '.join(sorted(missing)))
    else: selected=records
    if args.limit is not None: selected=selected[:args.limit]
    if args.workers<1 or args.workers>16 or args.max_input_chars<1000 or args.max_documents<0 or not 1<=args.max_tasks_per_paper<=20:
        raise ValueError('invalid classification limits')
    settings={k:str(getattr(args,k)) for k in ('model','endpoint','api_key_env','env_file','max_input_chars','max_documents','max_tasks_per_paper','max_output_tokens')}
    settings['classifier_sha256']=hashlib.sha256((CODE/'task_taxonomy.py').read_bytes()).hexdigest()
    selected_paths=[]
    def one(record):
        packet=packet_for(record,args)
        key=safe_id(record['canonical_id'])
        path=args.catalog/'work/classifications'/f'{key}.json'
        identity=object_hash({'packet':packet,'settings':settings})
        if path.is_file():
            previous=read_json(path)
            if previous['identity_sha256']!=identity:
                raise ValueError('classification input/config changed; use a fresh catalog: '+record['canonical_id'])
            if previous['status']=='classified_provisional' or not args.retry_failed:
                return previous
        atomic_json(args.catalog/'work/requests'/f'{key}.json',packet)
        if args.prepare_only:
            return {'paper_id':record['canonical_id'],'status':'prepared_only','request_chars':sum(len(x['text']) for x in packet['documents'])}
        result={'paper_id':record['canonical_id'],'identity_sha256':identity,'settings':settings,'created_at':now(),
                'request_path':str((args.catalog/'work/requests'/f'{key}.json').resolve())}
        try:
            response=classify_packet(packet,endpoint=args.endpoint,model=args.model,api_key_env=args.api_key_env,
                timeout_sec=args.timeout_sec,max_output_tokens=args.max_output_tokens,env_file=args.env_file)
            validation=validate_classification(response,packet)
            result.update(response=response,validated=validation,
                          status='classified_provisional' if validation['valid'] else 'rejected_classification')
        except Exception as error:
            result.update(status='classification_failed',error_type=type(error).__name__,error=str(error))
        atomic_json(path,result)
        return result
    statuses=Counter()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures=[pool.submit(one,r) for r in selected]
        for future in as_completed(futures):
            result=future.result();statuses[result['status']]+=1
            print(json.dumps({'paper_id':result['paper_id'],'status':result['status']},ensure_ascii=False),flush=True)
    summary=export_views(args)
    return {'status':'prepared' if args.prepare_only else 'classified_provisional','selected_papers':len(selected),
            'results':dict(statuses),'summary':summary,'human_review':'pending'}


def classify(args):
    import fcntl
    if not (args.catalog/'catalog.json').is_file():
        raise ValueError('create the inventory before classification')
    with (args.catalog/'work/classification.lock').open('a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('another classifier is working on this catalog; wait or choose a new catalog') from None
        return _classify(args)


def review_template(args):
    values,_=snapshot_rows(args.catalog/'work/candidate_tasks.jsonl')
    path=args.output or args.catalog/'candidate_selection_review.csv'
    if path.exists(): raise ValueError('review file already exists; never overwrite human decisions')
    fields=['candidate_id','paper_id','title','contribution_type','testable_claim','candidate_fingerprint','decision','reviewer','review_reason']
    write_csv(path,[{**r,'candidate_fingerprint':object_hash(r),'decision':'pending','reviewer':'','review_reason':''} for r in values],fields)
    return {'status':'review_template_created','path':str(path),'note':'Candidate selection only; this is not scientific contract certification.'}


def export_plan(args):
    values,_=snapshot_rows(args.catalog/'work/candidate_tasks.jsonl');index={r['candidate_id']:r for r in values}
    selected=[];seen=set()
    with args.review_csv.open(encoding='utf-8-sig',newline='') as stream:
        for row in csv.DictReader(stream):
            if row.get('decision')!='approve_for_construction': continue
            key=row.get('candidate_id')
            if key in seen or key not in index: raise ValueError('duplicate or unknown reviewed candidate')
            seen.add(key);item=index[key]
            if row.get('candidate_fingerprint')!=object_hash(item): raise ValueError('candidate changed since human selection review')
            for field in ('paper_id','title','contribution_type','testable_claim'):
                if row.get(field)!=csv_value(item.get(field)):
                    raise ValueError('review changed protected candidate fields; create a corrected draft and review it: '+field)
            if not row.get('reviewer','').strip() or not row.get('review_reason','').strip(): raise ValueError('human selection requires reviewer and reason')
            selected.append({'paper_id':item['paper_id'],'experiment_id':key,'target_experiment':item['testable_claim'],
                'domain':item['main_domain'] if item['main_domain'] in {'physics','geography','chemistry','biology','materials','astronomy'} else 'general',
                'problem_type':item['contribution_type'],'candidate_title':item['title'],
                'proposed_deliverable':item['deliverable'],'proposed_validation':item['proposed_validation'],
                'candidate_selection_review':{'reviewer':row['reviewer'],'reason':row['review_reason'],'candidate_fingerprint':row['candidate_fingerprint']},
                'contract_review_status':'not_performed','run_ready':False})
    if not selected: raise ValueError('no candidates explicitly approved for construction')
    if args.output.exists(): raise ValueError('task plan output already exists')
    write_rows(args.output,selected)
    return {'status':'task_plan_exported','tasks':len(selected),'path':str(args.output),'run_ready':False}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest='action',required=True)
    inv=sub.add_parser('inventory');inv.add_argument('--records',type=Path,required=True)
    inv.add_argument('--enriched-records',type=Path,action='append',default=[])
    inv.add_argument('--stream-dir',type=Path);inv.add_argument('--download-results',type=Path)
    classify_parser=sub.add_parser('classify')
    classify_parser.add_argument('--model',required=True);classify_parser.add_argument('--endpoint',required=True)
    classify_parser.add_argument('--api-key-env',default='OPENROUTER_API_KEY');classify_parser.add_argument('--env-file',type=Path)
    classify_parser.add_argument('--limit',type=int);classify_parser.add_argument('--all',action='store_true')
    classify_parser.add_argument('--paper-id',action='append',default=[])
    classify_parser.add_argument('--workers',type=int,default=2);classify_parser.add_argument('--timeout-sec',type=float,default=180)
    classify_parser.add_argument('--max-input-chars',type=int,default=30000);classify_parser.add_argument('--max-documents',type=int,default=3)
    classify_parser.add_argument('--max-tasks-per-paper',type=int,default=3);classify_parser.add_argument('--max-output-tokens',type=int,default=6000)
    classify_parser.add_argument('--material-root',type=Path,action='append',default=[])
    classify_parser.add_argument('--prepare-only',action='store_true');classify_parser.add_argument('--retry-failed',action='store_true')
    review=sub.add_parser('review-template');review.add_argument('--output',type=Path)
    export=sub.add_parser('export-plan');export.add_argument('--review-csv',type=Path,required=True);export.add_argument('--output',type=Path,required=True)
    for command in (inv,classify_parser,review,export):command.add_argument('--catalog',type=Path,required=True)
    args=p.parse_args();args.catalog=args.catalog.resolve()
    try:
        if getattr(args,'limit',None) is not None and args.limit<1: raise ValueError('--limit must be positive')
        result={'inventory':inventory,'classify':classify,'review-template':review_template,'export-plan':export_plan}[args.action](args)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 2 if any(result.get('results',{}).get(k) for k in ('classification_failed','rejected_classification')) else 0
    except (OSError,ValueError,KeyError,TypeError) as error:
        print(json.dumps({'status':'error','error':str(error)},ensure_ascii=False),file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
