"""Bounded data examples. Never fetch a data archive just to sample its contents."""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
import re
import threading
import urllib.parse
import urllib.request
import urllib.error

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.stage1_sources.settings import setting


TABLE_TYPES = {'.csv', '.tsv', '.jsonl', '.ndjson'}
SAMPLE_TYPES = TABLE_TYPES | {
    '.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp', '.gif', '.wav', '.flac',
    '.mp3', '.mp4', '.webm', '.cif', '.pdb', '.sdf', '.mol2', '.xyz',
    '.fasta', '.fa', '.fna', '.fastq', '.fq', '.npy', '.npz', '.h5', '.hdf5',
    '.nc', '.fits', '.fit', '.dcm', '.nii',
}


def attach_policy(plan, args):
    policy = {
        'mode': args.data_mode, 'max_files': args.sample_files,
        'max_rows': args.sample_rows, 'max_file_bytes': args.sample_file_mb * 1024**2,
        'max_resource_bytes': (
            args.max_file_mb * 1024**2
            if args.data_mode == 'full'
            else args.sample_resource_mb * 1024**2
        ),
    }
    for item in plan:
        if item['kind'] != 'data':
            continue
        item['data_policy'] = policy.copy()
        item['data_scope'] = {
            'sample': 'sample',
            'full': 'full',
            'metadata-only': 'metadata_only',
        }[args.data_mode]
        item['dataset_completeness'] = 'not_assessed'
        if args.data_mode == 'sample' and item.get('resolver') in {'github_archive', 'github_reuse'}:
            item.update(action='download', resolver='github_sample')
            item['data_policy']['repository_selection_version'] = 2
            item.pop('depends_on', None)
    return plan


def add_arguments(parser):
    parser.add_argument('--data-mode', choices=('sample', 'full', 'metadata-only'), default=setting('data_mode'))
    parser.add_argument('--sample-files', type=int, default=setting('sample_files'), help='Max data files per resource URL')
    parser.add_argument('--sample-rows', type=int, default=setting('sample_rows'), help='Max CSV/TSV/JSONL records per sample')
    parser.add_argument('--sample-file-mb', type=int, default=setting('sample_file_mb'))
    parser.add_argument('--sample-resource-mb', type=int, default=setting('sample_resource_mb'))


def validate_arguments(args):
    if min(args.sample_files, args.sample_rows, args.sample_file_mb, args.sample_resource_mb) < 1:
        raise ValueError('sample limits must be positive')


class LimitedBudget:
    def __init__(self, parent, maximum):
        self.parent, self.maximum, self.used = parent, maximum, 0

    def reserve(self, amount):
        amount = min(amount, max(0, self.maximum - self.used))
        actual = self.parent.reserve(amount)
        self.used += actual
        return actual

    def refund_unread(self, amount):
        self.parent.refund_unread(amount)
        self.used -= amount


class PersistentBudget:
    """Charge before reading, so a killed watcher cannot reset its byte allowance."""
    def __init__(self, maximum, path, api):
        self.maximum, self.path, self.api = maximum, path, api
        self.lock = threading.Lock()
        self.used = json.loads(path.read_text())['used_bytes'] if path.exists() else 0
        if type(self.used) is not int or self.used < 0 or self.used > maximum:
            raise ValueError('persisted byte usage exceeds budget or is invalid')

    def save(self):
        self.api.write_json(self.path, {'used_bytes': self.used, 'maximum_bytes': self.maximum})

    def reserve(self, amount):
        with self.lock:
            value = min(amount, self.maximum - self.used)
            self.used += value
            self.save()
            return value

    def refund_unread(self, amount):
        with self.lock:
            self.used -= amount
            self.save()


def extension(target):
    return Path(target.get('name') or urllib.parse.urlsplit(target['url']).path).suffix.lower()


def repository_sample_reason(path):
    """Return the reason a repository member cannot serve as a data example."""
    name = Path(urllib.parse.unquote(path)).as_posix().casefold()
    parts = name.split('/')
    tokens = set(re.findall(r'[a-z0-9]+', parts[-1]))
    if tokens & {'logo', 'favicon', 'badge', 'sticker', 'schematic', 'screenshot'}:
        return 'repository branding or illustration'
    if set(parts[:-1]) & {'docs', 'doc', 'documentation', '_static', 'externalpackages', 'node_modules'}:
        return 'documentation or vendored dependency'
    if Path(name).suffix in {'.png', '.jpg', '.jpeg', '.gif', '.bmp'} and not (
        set(parts[:-1]) & {'data', 'datasets', 'dataset', 'samples', 'sample', 'testdata', 'images', 'train', 'test', 'val'}
    ):
        return 'repository image has no dataset location'
    return None


def sample_targets(item, args, api):
    if item['resolver'] != 'github_sample':
        return api.expand_targets(item, args.timeout, args.retries)
    owner, repo = item['repository'].split('/', 1)
    commit = api.github_commit(item['repository'], item.get('requested_ref') or 'HEAD', args.timeout, args.retries)
    tree = api.request_json(f'https://api.github.com/repos/{owner}/{repo}/git/trees/{commit}?recursive=1', args.timeout, args.retries)
    if tree.get('truncated'):
        raise ValueError('GitHub tree is truncated; select a smaller data path explicitly')
    subpath = (item.get('subpath') or '').strip('/')
    targets = []
    for entry in tree.get('tree', []):
        path = entry.get('path', '')
        if entry.get('type') != 'blob' or entry.get('mode') not in {'100644', '100755'}:
            continue
        if subpath and path != subpath and not path.startswith(subpath + '/'):
            continue
        if repository_sample_reason(path):
            continue
        targets.append({'url': f'https://raw.githubusercontent.com/{owner}/{repo}/{commit}/' + urllib.parse.quote(path, safe='/'),
                        'name': path.rsplit('/', 1)[-1], 'repository_path': path,
                        'expected_bytes': entry.get('size'), 'repository_commit': commit})
    return targets


def table_sample(item, target, index, args, budget, api):
    policy = item['data_policy']
    maximum = min(policy['max_file_bytes'], args.max_file_mb * 1024**2)
    destination = api.material_directory(item, args.output_dir)
    receipt = destination / ('sample-' + api.plan_digest(target) + '.json')
    if receipt.is_file() and not getattr(args, 'refresh', False):
        previous = json.loads(receipt.read_text())
        if previous.get('plan_sha256') == api.plan_digest(item) and api.valid_cached_row(previous['result'], destination):
            return api.result_state({**previous['result'], 'resumed': True})
    last_error = None
    for attempt in range(args.retries + 1):
        try:
            request = urllib.request.Request(target['url'], headers={'User-Agent': api.USER_AGENT, 'Accept-Encoding': 'identity'})
            with api.open_url(request, args.timeout) as response:
                if response.getcode() != 200:
                    raise ValueError('sample request did not return HTTP 200')
                media_type = response.headers.get_content_type()
                if media_type in {'text/html', 'application/xhtml+xml'}:
                    raise ValueError('sample URL returned an HTML page')
                if response.headers.get('Content-Encoding', 'identity').lower() not in {'', 'identity'}:
                    raise ValueError('compressed HTTP sample requires a dedicated adapter')
                received, ended = 0, False

                def lines():
                    nonlocal received, ended
                    first = True
                    while True:
                        allowance = budget.reserve(min(64 * 1024, maximum - received))
                        if not allowance:
                            raise ValueError('sample byte limit exhausted before a complete record')
                        raw = response.readline(allowance)
                        budget.refund_unread(allowance - len(raw))
                        received += len(raw)
                        if not raw:
                            ended = True
                            return
                        if not raw.endswith(b'\n') and len(raw) == allowance:
                            raise ValueError('sample line exceeds bounded read size')
                        text = raw.decode('utf-8-sig' if first else 'utf-8')
                        if first and any(token in text.lstrip().lower() for token in ('<html', '<!doctype html', '<body', '<head')):
                            raise ValueError('sample URL returned disguised HTML')
                        first = False
                        yield text

                out = io.StringIO(newline='')
                count = 0
                ext = extension(target)
                if ext in {'.csv', '.tsv'}:
                    reader = csv.reader(lines(), delimiter='\t' if ext == '.tsv' else ',', strict=True)
                    writer = csv.writer(out, delimiter='\t' if ext == '.tsv' else ',', lineterminator='\n')
                    header = next(reader, None)
                    if header is None:
                        raise ValueError('empty tabular file')
                    writer.writerow(header)
                    while count < policy['max_rows']:
                        row = next(reader, None)
                        if row is None:
                            break
                        writer.writerow(row)
                        count += 1
                else:
                    for line in lines():
                        if not line.strip():
                            continue
                        out.write(json.dumps(json.loads(line), ensure_ascii=False) + '\n')
                        count += 1
                        if count >= policy['max_rows']:
                            break
                if not count:
                    raise ValueError('no complete data records in sample')
                body = out.getvalue().encode('utf-8')
                if len(body) > maximum:
                    raise ValueError('serialized sample exceeds byte limit')
                name = 'sample-' + api.safe_name(target.get('name') or Path(urllib.parse.urlsplit(target['url']).path).name, 'rows' + ext)
                import tempfile
                with tempfile.NamedTemporaryFile(dir=destination, suffix='.part', delete=False) as handle:
                    temporary = Path(handle.name)
                    handle.write(body)
                sha = hashlib.sha256(body).hexdigest()
                try:
                    local = api.publish_immutable(temporary, destination, name, sha)
                finally:
                    temporary.unlink(missing_ok=True)
                result = api.result_state({**item, 'status': 'downloaded', 'download_url': target['url'],
                    'target_index': index, 'final_url': response.geturl(), 'http_status': 200,
                    'local_path': str(local.resolve()), 'bytes': len(body), 'sha256': sha,
                    'data_scope': 'sample', 'sampling_method': 'first_n_records', 'sample_rows': count,
                    'header_assumption': 'first_record' if ext in {'.csv', '.tsv'} else None,
                    'source_exhausted': ended, 'source_bytes_read': received, 'media_type': media_type,
                    'source_expected_checksum': target.get('expected_checksum'),
                    'repository_commit': target.get('repository_commit'),
                    'validation_checks': {'parsed_records': True, 'html_rejected': True,
                                          'upstream_checksum_verified': False, 'sample_sha256_computed': True},
                    'completed_at': api.now_utc()})
                api.write_json(receipt, {'plan_sha256': api.plan_digest(item), 'result': result})
                return result
        except urllib.error.HTTPError as error:
            details = api.load_repository_module().error_details(error, target['url'])
            error.close()
            return api.result_state({**item, **details, 'download_url': target['url'], 'target_index': index})
        except (OSError, ValueError, csv.Error) as error:
            last_error = error
    return api.result_state({**item, 'status': 'sample_error', 'download_url': target['url'],
                             'error': str(last_error), 'target_index': index})


def materialize_sample(item, args, budget, api):
    if item['data_policy']['mode'] == 'metadata-only':
        return [api.result_state({**item, 'status': 'metadata_only', 'reason': 'data URL retained; file transfer disabled'})]
    previous = api.cached_results(item, args.output_dir)
    if previous is not None and not getattr(args, 'refresh', False):
        return previous
    if item.get('resolver') == 'specialized_dataset':
        return [api.result_state({**item, 'status': 'deferred_specialized',
                                 'reason': 'GEO/SRA/ENA require explicit sample/file selection; no dataset fetched'})]
    try:
        targets = sample_targets(item, args, api)
    except (OSError, ValueError) as error:
        return [api.result_state({**item, **api.provider_failure(error), 'completed_at': api.now_utc()})]
    policy = item['data_policy']
    if policy['mode'] == 'full':
        maximum = args.max_file_mb * 1024**2
        if not targets:
            return [api.result_state({**item, 'status': 'no_download_targets'})]
        results = []
        for index, target in enumerate(targets, 1):
            if not target.get('url'):
                results.append(api.result_state({
                    **item, 'status': 'invalid_url', 'target_index': index,
                    'error': 'expanded data target has no download URL',
                }))
                continue
            result = api.download_target(
                item, target, index, args.output_dir, args.timeout, args.retries,
                maximum, budget, resume=not getattr(args, 'refresh', False),
            )
            result.update(data_scope='full', dataset_completeness='full')
            results.append(result)
        if all(row['status'] in api.SUCCESS_STATUSES for row in results):
            api.write_json(api.material_directory(item, args.output_dir) / 'validated-result.json', {
                'plan_sha256': api.plan_digest(item), 'results': results,
            })
        return results
    maximum = min(policy['max_file_bytes'], args.max_file_mb * 1024**2)
    selected = [t for t in targets if extension(t) in SAMPLE_TYPES and (
        extension(t) in TABLE_TYPES or (type(t.get('expected_bytes')) is int and 0 < t['expected_bytes'] <= maximum))]
    selected.sort(key=lambda t: (extension(t) not in TABLE_TYPES, t.get('expected_bytes') or 0, t['url']))
    selected = selected[:policy['max_files']]
    api.write_json(api.material_directory(item, args.output_dir) / 'sample-selection.json', {
        'data_scope': 'sample', 'policy': policy, 'targets': targets, 'selected': selected,
        'note': 'Archives and binary files without a bounded known size are not fetched; tables are read by complete records.',
    })
    if not selected:
        return [api.result_state({**item, 'status': 'sample_unavailable',
            'reason': 'no supported bounded sample; archive/landing page/full dataset download not attempted'})]
    limited = LimitedBudget(budget, policy['max_resource_bytes'])
    results = []
    for index, target in enumerate(selected, 1):
        if extension(target) in TABLE_TYPES:
            result = table_sample(item, target, index, args, limited, api)
        else:
            result = api.download_target(item, target, index, args.output_dir, args.timeout, args.retries,
                                         maximum, limited, resume=not getattr(args, 'refresh', False))
            result.update(data_scope='sample', sampling_method='selected_files', dataset_completeness='not_assessed')
        results.append(result)
    if all(row['status'] in api.SUCCESS_STATUSES for row in results):
        api.write_json(api.material_directory(item, args.output_dir) / 'validated-result.json', {
            'plan_sha256': api.plan_digest(item), 'results': results,
        })
    return results
