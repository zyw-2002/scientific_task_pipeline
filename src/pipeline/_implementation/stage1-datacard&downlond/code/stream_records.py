"""Atomically publish validated paper records, with generation isolation."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid
from datetime import datetime, timezone


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class RecordPublisher:
    def __init__(self, directory, stage, source, records):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.lock = (directory / '.stream-writer.lock').open('a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise ValueError('another search process is publishing to this output directory') from None
        ids = [record['canonical_id'] for record in records]
        if len(set(ids)) != len(ids) or not all(isinstance(value, str) and value for value in ids):
            self.close()
            raise ValueError('stream requires unique nonempty canonical_id values')
        self.ids, self.published = set(ids), set()
        with source.open('rb') as handle:
            sha = hashlib.file_digest(handle, 'sha256').hexdigest()
        self.metadata = {'schema_version': 'paper-record-stream-v1', 'stage': stage,
                         'run_id': uuid.uuid4().hex, 'status': 'running',
                         'input': str(source.resolve()), 'input_sha256': sha,
                         'expected_records': len(ids), 'published_records': 0}
        atomic_json(directory / 'stream_progress.json', self.metadata)

    def publish(self, record):
        key = record['canonical_id']
        if key not in self.ids:
            raise ValueError('record was not selected for this search run')
        name = hashlib.sha256(key.encode()).hexdigest() + '.json'
        atomic_json(self.directory / 'stream_records' / name,
                    {'run_id': self.metadata['run_id'], 'record': record})
        self.published.add(key)
        self.metadata['published_records'] = len(self.published)
        atomic_json(self.directory / 'stream_progress.json', self.metadata)

    def finish(self, status):
        self.metadata['status'] = status
        atomic_json(self.directory / 'stream_progress.json', self.metadata)
        self.close()

    def progress(self, **fields):
        self.metadata.update(fields)
        self.metadata['heartbeat_at'] = datetime.now(timezone.utc).isoformat()
        atomic_json(self.directory / 'stream_progress.json', self.metadata)

    def close(self):
        if not self.lock.closed:
            self.lock.close()


def read_available(directory, stage):
    """Use a single generation; legacy outputs are readable only after summary publication."""
    progress = directory / 'stream_progress.json'
    if progress.is_file():
        metadata = json.loads(progress.read_text())
        if metadata.get('stage') != stage:
            raise ValueError('wrong search stage in stream directory')
        records = []
        for path in sorted((directory / 'stream_records').glob('*.json')):
            payload = json.loads(path.read_text())
            if payload.get('run_id') == metadata['run_id']:
                records.append(payload['record'])
        after = json.loads(progress.read_text())
        if after['run_id'] != metadata['run_id']:
            return [], {'status': 'generation_changed', 'complete': False}
        ids = [r['canonical_id'] for r in records]
        if len(ids) != len(set(ids)):
            raise ValueError('duplicate paper in record stream')
        complete = str(after['status']).startswith('completed') and len(records) == after['expected_records']
        return records, {**after, 'complete': complete}
    prefix = '07_open_source' if stage == '07' else '08_serpapi'
    summary_path, records_path = directory / f'{prefix}_summary.json', directory / f'{prefix}_enriched.jsonl'
    if not summary_path.is_file() or not records_path.is_file():
        return [], {'status': 'waiting_for_published_records', 'complete': False}
    try:
        before = records_path.stat()
        summary = json.loads(summary_path.read_text())
        records = [json.loads(line) for line in records_path.read_text().splitlines() if line.strip()]
        after = records_path.stat()
    except (OSError, json.JSONDecodeError):
        return [], {'status': 'waiting_for_complete_files', 'complete': False}
    ids = [row['canonical_id'] for row in records]
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or len(records) != summary.get('output_records'):
        return [], {'status': 'waiting_for_complete_files', 'complete': False}
    if len(ids) != len(set(ids)):
        raise ValueError('duplicate paper in final output')
    complete = str(summary.get('status')).startswith('completed')
    return (records if complete else []), {'status': summary.get('status'), 'complete': complete}
