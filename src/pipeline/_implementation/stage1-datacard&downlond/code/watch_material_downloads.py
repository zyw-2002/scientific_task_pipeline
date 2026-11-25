#!/usr/bin/env python3
"""Consume checked Stage07/08 records while discovery continues independently."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from copy import deepcopy
import fcntl
import importlib.util
import json
from pathlib import Path
import sys
import time

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline_runtime import setting


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


api = load_module('stream_material_downloader', '10_plan_and_download_materials.py')
streams = load_module('stream_material_records', 'stream_records.py')
gate = load_module('stream_material_gate', 'download_gate.py')
samples = api.load_sample_module()


def snapshot(base_records, args):
    records = {r['canonical_id']: deepcopy(r) for r in base_records}
    sources = []
    for stage, directory in [('07', args.open_source_dir), ('08', args.serpapi_dir)]:
        if directory is None:
            continue
        available, info = streams.read_available(directory, stage)
        sources.append({'stage': stage, 'directory': str(directory), **info})
        for row in available:
            key = row['canonical_id']
            if key not in records:
                continue
            old = (records[key].get('identifiers') or {}).get('doi')
            new = (row.get('identifiers') or {}).get('doi')
            if old and new and old.strip().lower() != new.strip().lower():
                raise ValueError(f'paper DOI changed across discovery sources: {key}')
            records[key] = row
    values = list(records.values())
    plan, checks = gate.filter_plan(gate.resource_plan(values, api), values, api)
    samples.attach_policy(plan, args)
    return values, plan, checks, sources


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode', choices=('plan', 'watch'), required=True)
    p.add_argument('--input', type=Path, required=True, help='Stable Stage06 or later paper JSONL')
    p.add_argument('--open-source-dir', type=Path)
    p.add_argument('--serpapi-dir', type=Path)
    p.add_argument('--output-dir', type=Path, required=True)
    p.add_argument('--max-records', type=int)
    p.add_argument('--all-records', action='store_true')
    p.add_argument('--once', action='store_true', help='Consume current snapshot only')
    p.add_argument('--retry-failed', action='store_true', help='Retry persisted failures once this invocation')
    p.add_argument('--retry-only-failed', action='store_true', help='Only retry previously failed resources, then exit')
    p.add_argument('--retry-status', action='append', default=[], help='Limit explicit retries to these result statuses; repeatable')
    p.add_argument('--max-deferred-retries', type=int, default=setting('max_deferred_retries'), help='Maximum automatic cooldown retries per resource')
    p.add_argument('--poll-seconds', type=float, default=setting('watch_poll_seconds'))
    p.add_argument('--report-seconds', type=float, default=setting('watch_report_seconds'))
    p.add_argument('--concurrency', type=int, default=setting('download_workers'))
    p.add_argument('--timeout', type=float, default=setting('download_timeout'))
    p.add_argument('--retries', type=int, default=setting('download_retries'))
    p.add_argument('--max-file-mb', type=int, default=setting('max_file_mb'))
    p.add_argument('--max-total-gb', type=float, default=setting('max_total_gb'))
    p.add_argument('--network-config', type=Path)
    p.add_argument('--proxy-url')
    p.add_argument('--proxy-env-file', type=Path)
    p.add_argument('--no-proxy')
    p.add_argument('--direct', action='store_true')
    samples.add_arguments(p)
    api.load_repository_module().add_arguments(p)
    return p


def task_key(item):
    return item['resource_id'] + '-' + api.plan_digest(item)


FAILURE_STATUSES = {'error', 'sample_error', 'http_error', 'expansion_error', 'provider_unavailable',
                    'rate_limited', 'authentication_failed', 'access_denied', 'not_found_http',
                    'metadata_error', 'dependency_unavailable', 'skipped_total_budget'}


def should_retry(rows, args):
    selected = set(args.retry_status) if args.retry_status else FAILURE_STATUSES
    return any(row.get('status') in selected for row in rows)


def retry_deadline(rows):
    deadlines = [row.get('retry_at') for row in rows
                 if row.get('retryable') and row.get('status') in {'rate_limited', 'provider_unavailable'}]
    valid = [value for value in deadlines if type(value) in {int, float} and 0 < value < 10**11]
    return max(valid) if valid else None


def persist_snapshot(args, records, plan, checks):
    api.write_jsonl(args.output_dir / '10_stream_records.jsonl', records)
    api.write_jsonl(args.output_dir / '10_download_plan.jsonl', plan)
    api.write_jsonl(args.output_dir / '10_resource_gate.jsonl', checks)


def run(args):
    args.output_dir = args.output_dir.resolve()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with (args.output_dir / '.watch.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError('another plan/downloader process owns this output directory') from None
        base = api.read_jsonl(args.input)
        if args.max_records:
            base = base[:args.max_records]
        signature = {'input_sha256': api.sha256_file(args.input), 'selected_ids': [r['canonical_id'] for r in base],
                     'open_source_dir': str(args.open_source_dir.resolve()) if args.open_source_dir else None,
                     'serpapi_dir': str(args.serpapi_dir.resolve()) if args.serpapi_dir else None,
                     'data_mode': args.data_mode, 'sample_files': args.sample_files,
                     'sample_rows': args.sample_rows, 'sample_file_mb': args.sample_file_mb,
                     'sample_resource_mb': args.sample_resource_mb, 'max_file_mb': args.max_file_mb}
        config = args.output_dir / 'stream_config.json'
        if config.is_file() and json.loads(config.read_text()) != signature:
            raise ValueError('input, scope or sample policy changed; use a new output directory')
        api.write_json(config, signature)
        records, plan, checks, sources = snapshot(base, args)
        persist_snapshot(args, records, plan, checks)
        if args.mode == 'plan':
            report = {'status': 'planned', 'records': len(records), 'resources_ready': len(plan),
                      'gate_decisions': dict(Counter(row['decision'] for row in checks)),
                      'resource_kinds': dict(Counter(row['kind'] for row in plan)),
                      'data_mode': args.data_mode, 'sample_files_per_resource': args.sample_files,
                      'sample_rows': args.sample_rows, 'sample_file_mb': args.sample_file_mb,
                      'sample_resource_mb': args.sample_resource_mb, 'sources': sources,
                      'concurrency': args.concurrency, 'network_calls': 0}
            api.write_json(args.output_dir / '10_stream_summary.json', report)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return
        api.configure_network(args)
        metadata = api.configure_metadata(args)
        budget = samples.PersistentBudget(int(args.max_total_gb * 1024**3), args.output_dir / 'transfer_budget.json', api)
        task_dir = args.output_dir / 'stream_tasks'
        task_dir.mkdir(exist_ok=True)
        completed, pending, current = {}, {}, {task_key(item): item for item in plan}
        checked_saved = set()
        retry_rounds, previous_failures = {}, set()
        if args.retry_only_failed:
            for path in task_dir.glob('*.json'):
                prior = json.loads(path.read_text())
                if should_retry(prior['results'], args):
                    previous_failures.update(row['resource_id'] for row in prior['results'])

        def restore(key, item):
            if key in checked_saved:
                return
            checked_saved.add(key)
            path = task_dir / f'{key}.json'
            if not path.is_file():
                return
            prior = json.loads(path.read_text())
            rows = prior['results']
            retry_rounds[key] = prior.get('retry_round', 0)
            if (args.retry_failed or args.retry_only_failed) and should_retry(rows, args):
                retry_rounds[key] = 0
                return
            if all(r.get('status') not in api.SUCCESS_STATUSES or api.valid_cached_row(r, api.material_directory(item, args.output_dir)) for r in rows):
                completed[key] = rows
        for key, item in current.items():
            restore(key, item)
        next_poll, next_flush = time.monotonic() + args.poll_seconds, 0
        stop = None
        waiting = {}

        def flush(status):
            # Old downloaded artifacts remain on disk; only currently admitted resources enter the handoff file.
            results = [row for key in current for row in completed.get(key, [])]
            api.write_jsonl(args.output_dir / '10_download_results.jsonl', results)
            report = {'status': status, 'records': len(records), 'resources_ready': len(current),
                      'resources_finished': sum(key in completed for key in current), 'in_flight': len(pending),
                      'result_statuses': dict(Counter(r['status'] for r in results)),
                      'transfer_and_copy_budget_used_bytes': budget.used, 'max_total_bytes': budget.maximum,
                      'data_mode': args.data_mode, 'sources': sources,
                      'resources_waiting_for_retry': len(waiting),
                      'next_retry_at': min(waiting.values()) if waiting else None,
                      'repository_requests': metadata.report(),
                      'run_ready': False, 'updated_at': api.now_utc()}
            api.write_json(args.output_dir / '10_stream_summary.json', report)
            print(json.dumps(report, ensure_ascii=False), flush=True)

        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            while True:
                now = time.monotonic()
                if not args.once and not args.retry_only_failed and now >= next_poll:
                    records, plan, checks, sources = snapshot(base, args)
                    current = {task_key(item): item for item in plan}
                    for key, item in current.items():
                        restore(key, item)
                    persist_snapshot(args, records, plan, checks)
                    next_poll = now + args.poll_seconds
                running_keys = set(pending.values())
                waiting = {}
                for key, rows in list(completed.items()):
                    if key not in current or retry_rounds.get(key, 0) >= args.max_deferred_retries:
                        continue
                    if args.retry_only_failed and current[key]['resource_id'] not in previous_failures:
                        continue
                    deadline = retry_deadline(rows)
                    if deadline is not None:
                        if deadline <= time.time():
                            completed.pop(key)
                            retry_rounds[key] = retry_rounds.get(key, 0) + 1
                        else:
                            waiting[key] = deadline
                available = [key for key in current if key not in completed and key not in running_keys
                             and (not args.retry_only_failed or current[key]['resource_id'] in previous_failures)]
                if budget.used < budget.maximum:
                    for key in available[:max(0, args.concurrency - len(pending))]:
                        pending[pool.submit(api.execute_item, current[key], args, budget)] = key
                if pending:
                    done, _ = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
                    for future in done:
                        key = pending.pop(future)
                        try:
                            rows = future.result()
                        except Exception as error:
                            rows = [api.result_state({**current.get(key, {}), **api.provider_failure(error, 'error')})]
                        completed[key] = rows
                        task_path = task_dir / f'{key}.json'
                        if task_path.is_file():
                            api.write_json(task_dir / 'history' / key / f'{time.time_ns()}.json', json.loads(task_path.read_text()))
                        api.write_json(task_path, {'results': rows, 'retry_round': retry_rounds.get(key, 0)})
                else:
                    if budget.used >= budget.maximum and (available or waiting):
                        stop = 'budget_exhausted'
                    elif not available and not waiting:
                        if args.retry_only_failed:
                            stop = 'retry_batch_completed'
                        elif args.once:
                            stop = 'snapshot_completed'
                        elif all(source.get('complete') for source in sources):
                            stop = 'completed'
                        elif any(source.get('status') == 'failed' for source in sources):
                            stop = 'upstream_failed'
                    if stop:
                        flush(stop)
                        break
                    time.sleep(min(1, args.poll_seconds))
                if time.monotonic() >= next_flush:
                    flush('running')
                    next_flush = time.monotonic() + args.report_seconds


def main():
    args = parser().parse_args()
    samples.validate_arguments(args)
    api.load_repository_module().validate_arguments(args)
    if args.max_deferred_retries < 0:
        raise SystemExit('max-deferred-retries must be nonnegative')
    if not 1 <= args.concurrency <= 16 or args.poll_seconds <= 0 or args.report_seconds <= 0 or args.timeout <= 0 or args.retries < 0:
        raise SystemExit('invalid concurrency, polling, timeout or retry setting')
    if args.max_file_mb < 1 or args.max_total_gb <= 0 or (args.max_records is not None and args.max_records < 1):
        raise SystemExit('budgets and record limits must be positive')
    if args.mode == 'watch' and not (args.max_records or args.all_records):
        raise SystemExit('select --max-records or --all-records explicitly')
    if not args.open_source_dir and not args.serpapi_dir:
        raise SystemExit('specify at least one discovery output directory')
    run(args)


if __name__ == '__main__':
    main()
