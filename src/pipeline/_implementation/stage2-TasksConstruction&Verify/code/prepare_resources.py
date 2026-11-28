"""Prepare data in a disposable container, preserving the original task package.

This is an explicit execution-stage operation, never an automatic construction
step. It does not train a solution, build an image or certify scientific labels.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid

from contract import atomic_write_json, package_fingerprint, package_manifest, package_path, sha256_file, package_layout
from resource_contract import check_available, source_errors, verify_content


def shared_mounts(spec, package, output):
    """Resolve requested dataset aliases against the operator's local mapping."""
    requested = [s['prepare']['shared_mount'] for s in spec['data']['sources']
                 if s.get('required', True) and 'shared_mount' in s.get('prepare', {})]
    if not requested:
        return []
    registry = json.loads(os.environ.get('PIPELINE_RESOURCE_MOUNTS', '{}'))
    if not isinstance(registry, dict):
        raise ValueError('PIPELINE_RESOURCE_MOUNTS must be an object of named dataset mounts')
    mounts = {}
    system = [Path('/' + p) for p in ('bin', 'boot', 'dev', 'etc', 'lib', 'lib64', 'proc', 'root',
                                     'run', 'sbin', 'sys', 'usr', 'var/run')]
    reserved = [*system, Path('/tmp'), Path('/pipeline'), Path('/prepared'), Path('/preparation')]
    for decl in requested:
        if (not isinstance(decl, dict) or set(decl) != {'name', 'path', 'read_only'}
                or decl['read_only'] is not True or not isinstance(decl['name'], str)
                or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]*', decl['name'])):
            raise ValueError('shared_mount must declare a name, path and read_only=true')
        alias = decl['name']
        mapping = registry.get(alias)
        if (not isinstance(mapping, dict) or mapping.get('target') != decl['path']
                or not all(isinstance(mapping.get(k), str) for k in ('source', 'target'))):
            raise ValueError(f'shared mount {alias} has no matching operator-approved source and target')
        source, target = (Path(mapping[k]) for k in ('source', 'target'))
        for path in (source, target):
            if (not path.is_absolute() or str(path).startswith('//') or '..' in path.parts
                    or any(c in str(path) for c in ('"', ',', '\n', '\r', '\x00'))):
                raise ValueError(f'invalid source or target for shared mount {alias}')
        source = source.resolve(strict=True)
        if not source.is_dir() and not source.is_file():
            raise ValueError(f'shared mount {alias} must identify a regular file or directory')
        if alias in mounts:
            if mounts[alias][1] != target:
                raise ValueError(f'inconsistent shared mount target for {alias}')
            continue
        def overlaps(path, others):
            return any(path.is_relative_to(p) or p.is_relative_to(path) for p in others)
        if (overlaps(target, reserved) or overlaps(source, [*system, package, output])
                or Path.home().resolve().is_relative_to(source)):
            raise ValueError(f'shared mount {alias} overlaps a reserved path')
        if 'sha256' in mapping:
            expected = mapping['sha256']
            if (not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected)
                    or not source.is_file() or sha256_file(source) != expected):
                raise ValueError(f'shared mount {alias} differs from the frozen file digest')
        if 'publisher_md5' in mapping:
            expected = mapping['publisher_md5']
            if (not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{32}', expected)
                    or not source.is_file() or mapping.get('bytes') != source.stat().st_size):
                raise ValueError(f'shared mount {alias} differs from the publisher file size or checksum')
            checksum = hashlib.md5()
            with source.open('rb') as stream:
                for block in iter(lambda: stream.read(4 << 20), b''):
                    checksum.update(block)
            if checksum.hexdigest() != expected:
                raise ValueError(f'shared mount {alias} differs from the publisher file checksum')
        mounts[alias] = (source, target)
        reserved.append(target)
    return [(alias, source, target) for alias, (source, target) in mounts.items()]


def prepare_package(package, output, *, image, network='none', timeout=3600,
                    docker='docker', runner=subprocess.run, reference_overlay=None, proxy_url=None):
    package, output = Path(package).resolve(), Path(output).resolve()
    if output.exists() or output.is_relative_to(package) or package.is_relative_to(output):
        raise ValueError('output must be a new directory separate from the input package')
    if not image or network not in {'none', 'bridge'} or timeout <= 0:
        raise ValueError('provide an image, positive timeout and network none/bridge')
    spec = json.loads((package / 'execution.json').read_text())
    if spec.get('assessment_contract_version') != 1:
        raise ValueError('prepare-data requires assessment_contract_version=1')
    errors = source_errors(package, spec, preparing=True)
    if errors: raise ValueError('; '.join(errors))
    transport = None
    if network == 'bridge':
        from worker_runtime import ENV_NAMES, network_for_policy
        config = {'inherit_env': False, **(spec['runtime'].get('network') or {})}
        transport = network_for_policy(config, 'enabled', proxy_url=proxy_url)
    mounts = shared_mounts(spec, package, output)
    before = package_manifest(package)  # Reject symlinks before copying/mounting.
    output.mkdir(parents=True)
    prepared = output / 'package'
    shutil.copytree(package, prepared)
    if reference_overlay is not None:
        for item in spec['data']['sources']:
            if item.get('role') != 'reference':
                continue
            seed = package_path(reference_overlay, item['package_path'])
            verify_content(seed, item)
            target = package_path(prepared, item['package_path'])
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            if seed.is_dir():
                shutil.copytree(seed, target)
            else:
                shutil.copyfile(seed, target)
    for name in ('readiness.json', 'runtime_evidence.json'):
        (prepared / name).unlink(missing_ok=True)
    # Recipes use the same documented task paths as execution. Both aliases
    # refer only to the disposable prepared copy, never the reviewed package.
    public = package_path(prepared, package_layout(spec)['public_dir'])
    evaluation = prepared / 'common_task/evaluation'
    public.mkdir(parents=True, exist_ok=True)
    evaluation.mkdir(parents=True, exist_ok=True)
    helper = Path(__file__).with_name('resource_contract.py').resolve()
    name = 'pipeline-data-' + uuid.uuid4().hex[:12]
    command = [docker, 'run', '--rm', '--name', name, '--user', f'{os.getuid()}:{os.getgid()}',
               '--network', network, '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
               '--read-only', '--tmpfs', '/tmp:rw,nosuid,nodev',
               '--mount', f'type=bind,source={prepared},target=/prepared',
               '--mount', f'type=bind,source={public},target=/task/problem',
               '--mount', f'type=bind,source={evaluation},target=/task/evaluation',
               '--mount', f'type=bind,source={output},target=/preparation',
               '--mount', f'type=bind,source={helper},target=/pipeline/resource_contract.py,readonly',
               '--env', 'DATA_DIR=/task/problem/data']
    for alias, source, target in mounts:
        command.extend(['--mount', f'type=bind,source={source},target={target},readonly'])
    if transport is not None:
        for key in ENV_NAMES:
            command.extend(['--env', key])
    command.extend(['--workdir', '/prepared', '--entrypoint', 'python', image,
               '/pipeline/resource_contract.py', '--package', '/prepared', '--prepare',
               '--receipt', '/preparation/resources.json'])
    receipt = {'status': 'fail', 'source_package_sha256': package_fingerprint(package),
               'image': image, 'network': network, 'package': str(prepared),
               'preparation_paths': {'package': '/prepared', 'public': '/task/problem',
                                     'private_evaluation': '/task/evaluation', 'DATA_DIR': '/task/problem/data'},
               'network_transport': transport.report if transport is not None else None,
               'shared_mounts': [{'name': alias, 'path': str(target), 'read_only': True} for alias, _, target in mounts]}
    try:
        with (output / 'prepare.log').open('w') as log:
            runner(command, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=timeout,
                   env=transport.process_env() if transport is not None else None)
        proof_path = output / 'resources.json'
        if proof_path.is_symlink() or not proof_path.is_file():
            raise ValueError('missing regular preparation receipt')
        proof = json.loads(proof_path.read_text())
        if proof.get('status') != 'pass': raise ValueError('resource preparation did not pass')
        resources = {s['id']: s for s in spec['data']['sources']}
        verified = check_available(resources, lambda rid: prepared / resources[rid]['package_path'], include_fixtures=True)
        expected = {r['id']: {'id': r['id'], 'path': resources[r['id']]['package_path'],
                              'sha256': r['sha256'], 'status': 'pass'}
                    for r in verified if resources[r['id']].get('required', True)}
        rows = proof.get('resources')
        if (not isinstance(rows, list) or len(rows) != len(expected)
                or any(not isinstance(r, dict) or not isinstance(r.get('id'), str) for r in rows)
                or {r['id']: r for r in rows} != expected):
            raise ValueError('container resource evidence differs from the verified IDs, paths or digests')
        after = package_manifest(prepared)
        data_paths = [s['package_path'] for s in spec['data']['sources']]
        def data_file(path):
            return any(path == p or path.startswith(p.rstrip('/') + '/') for p in data_paths)
        changed = {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
        if any(not data_file(p) for p in changed):
            raise ValueError('preparation changed non-resource task files; review changes before deployment')
        receipt.update(status='pass', prepared_package_sha256=package_fingerprint(prepared),
                       changed_resources=sorted(changed), resources=rows, resources_sha256=sha256_file(proof_path))
    except Exception as exc:
        receipt['error'] = str(exc)
    finally:
        # Only this command's named container; never restart or prune Docker.
        try:
            runner([docker, 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   check=False, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            receipt.update(status='fail', cleanup_error=str(exc))
        atomic_write_json(output / 'preparation.json', receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--image', required=True, help='Already built image with preparation dependencies')
    parser.add_argument('--network', choices=['none', 'bridge'], default='none')
    parser.add_argument('--proxy-url', help='HTTP(S) download proxy for bridge network preparation')
    parser.add_argument('--timeout', type=int, default=3600)
    parser.add_argument('--docker', default='docker')
    args = parser.parse_args()
    result = prepare_package(args.package, args.output, image=args.image,
        network=args.network, timeout=args.timeout, docker=args.docker, proxy_url=args.proxy_url)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] == 'pass' else 1


if __name__ == '__main__':
    raise SystemExit(main())
