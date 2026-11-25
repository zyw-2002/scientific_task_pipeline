#!/usr/bin/env python3
"""Materialize only explicitly public task inputs and create Qwen inspection files.

This helper is copied into the final task by Stage2. It never chooses a dataset,
changes scientific inputs, or promotes Stage1's private bundle to public data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request

PUBLIC_ROLES = {'input', 'training', 'validation', 'test_input', 'public_auxiliary', 'model_input'}
MANAGED_FILES = {'PROVISIONING.json', 'PROVISIONING.json.tmp', '.provisioning.lock'}


def sha256(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def local_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or '\\' in relative:
        raise ValueError('manifest paths must be nonempty normalized relative paths')
    pure = PurePosixPath(relative)
    if pure.is_absolute() or '..' in pure.parts or pure.as_posix() != relative or relative == '.':
        raise ValueError('manifest path traverses outside data root')
    target = root / relative
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError('manifest path escapes data root')
    for path in (target, *target.parents):
        if path.is_symlink():
            raise ValueError('symlinks are not valid data materialization paths')
        if path == root:
            break
    return target


def allowed_url(url, hosts):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('manifest download URL must be HTTP(S), without embedded credentials')
    host = parsed.hostname.lower()
    if not any(host == rule or (rule.startswith('*.') and host.endswith(rule[1:]) and host != rule[2:]) for rule in hosts):
        raise ValueError('download/redirect host absent from public manifest allowed_hosts: ' + host)


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts):
        super().__init__()
        self.hosts = hosts

    def redirect_request(self, request, fp, code, message, headers, newurl):
        allowed_url(newurl, self.hosts)
        return super().redirect_request(request, fp, code, message, headers, newurl)


def validate_manifest(manifest):
    if not isinstance(manifest, dict) or manifest.get('schema_version') != 'pipeline-public-data-v1':
        raise ValueError('manifest requires schema_version=pipeline-public-data-v1')
    files = manifest.get('files')
    if not isinstance(files, list):
        raise ValueError('manifest.files must be an array')
    hosts = manifest.get('allowed_hosts', [])
    if not isinstance(hosts, list) or not all(isinstance(host, str) and host and '://' not in host for host in hosts):
        raise ValueError('allowed_hosts must be hostname patterns')
    paths = set()
    for item in files:
        if not isinstance(item, dict) or item.get('visibility') != 'public' or item.get('role') not in PUBLIC_ROLES:
            raise ValueError('only explicitly public scientific input roles may be provisioned')
        path = item.get('path')
        local_path(Path('/workspace/data'), path)
        if path in paths or path.split('/')[0] in MANAGED_FILES | {'.qwen'}:
            raise ValueError('duplicate or reserved output path')
        paths.add(path)
        if bool(item.get('url')) == bool(item.get('local_path')):
            raise ValueError('each input needs exactly one URL or local_path')
        if item.get('url'):
            allowed_url(item['url'], hosts)
        if item.get('sha256') is not None and not re.fullmatch('[a-f0-9]{64}', str(item['sha256'])):
            raise ValueError('provided SHA256 is malformed; omit unknown values')
        if item.get('bytes') is not None and (type(item['bytes']) is not int or item['bytes'] < 0):
            raise ValueError('provided file size must be nonnegative; omit unknown values')
    for path in paths:
        pure = PurePosixPath(path)
        if any(str(parent) in paths for parent in pure.parents):
            raise ValueError('manifest file paths conflict with parent directories')
        if path + '.partial' in paths:
            raise ValueError('manifest path conflicts with another input staging file')
    return files, hosts


def _check_existing_scope(output, paths):
    """A reused output may contain current public inputs and owned aids only."""
    found = []
    for directory, dirs, names in os.walk(output, followlinks=False):
        base = Path(directory)
        for name in list(dirs):
            target = base / name
            if target.is_symlink():
                raise ValueError('symlink directory in public output')
            if target == output / '.qwen':
                dirs.remove(name)  # Converter separately verifies ownership.
        for name in names:
            target = base / name
            relative = target.relative_to(output).as_posix()
            if target.is_symlink() or not target.is_file():
                raise ValueError('symlink/nonregular file in public output')
            if relative in MANAGED_FILES:
                continue
            if relative in paths:
                found.append(relative)
            elif relative.endswith('.partial') and relative[:-8] in paths:
                continue  # Failed acquisition is retried from byte zero.
            else:
                raise ValueError('unlisted input in public output: ' + relative)
    return found


def materialize(manifest_path: Path, output: Path, *, timeout=60, retries=2,
                max_file_bytes=1024**3, max_total_bytes=10*1024**3,
                model_capability='text', convert=True, opener=None) -> dict:
    if timeout <= 0 or retries < 0 or max_file_bytes <= 0 or max_total_bytes <= 0:
        raise ValueError('invalid acquisition time or byte limits')
    manifest = json.loads(manifest_path.read_text())
    files, hosts = validate_manifest(manifest)
    output = output.absolute()
    if output != output.resolve():
        raise ValueError('data output path cannot contain symlinks')
    output.mkdir(parents=True, exist_ok=True)
    state = {'schema_version': 'pipeline-public-provisioning-v1', 'status': 'running',
             'manifest_sha256': sha256(manifest_path), 'files': [], 'transferred_bytes': 0,
             'scientific_identity_verified': False, 'run_ready': False}
    for name in MANAGED_FILES:
        local_path(output, name)
    state_file = output / 'PROVISIONING.json'

    def save():
        temp = state_file.with_suffix('.json.tmp')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
        temp.replace(state_file)

    def matches(path, item):
        return (path.is_file() and path.stat().st_size <= max_file_bytes
                and (item.get('bytes') is None or path.stat().st_size == item['bytes'])
                and (item.get('sha256') is None or sha256(path) == item['sha256']))

    # A matching prior receipt gives content identity when the public source did
    # not publish an expected digest. Mere same filename/length is insufficient.
    import fcntl
    lock_fd = os.open(output / '.provisioning.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = {}
        existing = _check_existing_scope(output, {item['path'] for item in files})
        if state_file.is_file():
            state_fd = os.open(state_file, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(state_fd, encoding='utf-8') as stream:
                old = json.load(stream)
            if old.get('manifest_sha256') == state['manifest_sha256']:
                previous = {row['path']: row for row in old.get('resume_receipts', []) + old.get('files', [])}
            elif existing:
                raise ValueError('public manifest changed in a nonempty output; use a fresh data directory')
        # Retain old content receipts until each input has been revisited, so an
        # interrupted repair does not force unrelated SHA-less data to download
        # again. Every reuse still hashes current bytes before trusting a receipt.
        state['resume_receipts'] = list(previous.values())
        save()
        # ProxyHandler reads explicit environment delivered by the worker/runner;
        # it is independent of bridge networking or model/search API endpoints.
        opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler(), PublicRedirect(hosts))
        total_materialized = 0
        try:
            for item in files:
                if (item.get('bytes') or 0) > max_file_bytes:
                    raise ValueError('declared public input size exceeds per-file budget')
                target = local_path(output, item['path'])
                target.parent.mkdir(parents=True, exist_ok=True)
                prior = previous.get(item['path'], {})
                reused = matches(target, item) and (item.get('sha256') is not None or
                    (prior.get('sha256') and sha256(target) == prior['sha256']))
                if not reused:
                    partial = target.with_name(target.name + '.partial')
                    if partial.is_symlink():
                        raise ValueError('partial file cannot be a symlink')
                    error = None
                    for attempt in range(retries + 1):
                        try:
                            if item.get('local_path'):
                                source = local_path(manifest_path.parent, item['local_path'])
                                stream = source.open('rb')
                            else:
                                request = urllib.request.Request(item['url'], headers={'User-Agent': 'Pipeline-public-input/1'})
                                stream = opener.open(request, timeout=timeout)
                            written = 0
                            with stream, partial.open('wb') as destination:
                                if not item.get('local_path'):
                                    allowed_url(stream.geturl(), hosts)
                                while True:
                                    block = stream.read(min(1024 * 1024, max_file_bytes - written + 1))
                                    if not block:
                                        break
                                    written += len(block)
                                    state['transferred_bytes'] += len(block)
                                    if written > max_file_bytes or state['transferred_bytes'] > max_total_bytes:
                                        raise ValueError('public download budget exhausted')
                                    destination.write(block)
                            if not matches(partial, item):
                                raise ValueError('public file size or SHA256 mismatch')
                            suffix = target.suffix.lower()
                            with partial.open('rb') as check:
                                header = check.read(512).lstrip().lower()
                            if suffix not in {'.html', '.htm', '.txt', '.xml'} and (header.startswith(b'<!doctype html') or header.startswith(b'<html')):
                                raise ValueError('download returned an HTML page instead of the declared data file')
                            partial.replace(target)
                            error = None
                            break
                        except (OSError, ValueError) as exc:
                            error = exc
                            partial.unlink(missing_ok=True)
                            if state['transferred_bytes'] >= max_total_bytes:
                                break
                            if attempt < retries:
                                time.sleep(min(2 ** attempt, 2))
                    if error:
                        raise ValueError(f'cannot acquire public input {item["path"]}: {error}')
                else:
                    # A verified target supersedes an interrupted copy of that
                    # same input; never expose stale staging bytes to the model.
                    target.with_name(target.name + '.partial').unlink(missing_ok=True)
                size = target.stat().st_size
                total_materialized += size
                if size > max_file_bytes or total_materialized > max_total_bytes:
                    raise ValueError('materialized public inputs exceed declared resource budget')
                state['files'].append({'path': item['path'], 'sha256': sha256(target), 'bytes': size,
                                       'status': 'reused_verified' if reused else 'materialized'})
                save()
            if convert:
                try:
                    from prepare_multimodal_data import prepare_multimodal_data
                except ImportError:
                    from multimodal_data import prepare_multimodal_data
                conversion = prepare_multimodal_data(
                    output, output / '.qwen', model_capability=model_capability,
                    max_file_bytes=max_file_bytes,
                    max_archive_sample_bytes=min(max_file_bytes, 256 * 1024 * 1024),
                    max_archive_total_bytes=min(max_total_bytes, 1024 * 1024 * 1024),
                )
                state['multimodal'] = conversion.get('compatibility', {})
                if conversion['summary']['unsupported_count']:
                    raise ValueError('required data formats could not be converted; inspect .qwen/DATA_OVERVIEW.json')
            state['status'] = 'materialized'
            state.pop('resume_receipts', None)
        except Exception as error:
            state.update(status='failed', error=str(error))
            save()
            raise
        save()
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--retries', type=int, default=2)
    parser.add_argument('--max-file-mb', type=float, default=1024)
    parser.add_argument('--max-total-gb', type=float, default=10)
    parser.add_argument('--model-capability', choices=['text', 'vision', 'unknown'], default='text')
    args = parser.parse_args()
    try:
        result = materialize(args.manifest, args.output, timeout=args.timeout, retries=args.retries,
            max_file_bytes=int(args.max_file_mb * 1024**2), max_total_bytes=int(args.max_total_gb * 1024**3),
            model_capability=args.model_capability)
        print(json.dumps({'status': result['status'], 'files': len(result['files']), 'run_ready': False}))
        return 0
    except (OSError, ValueError, KeyError) as error:
        print(json.dumps({'status': 'failed', 'error': str(error)}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
