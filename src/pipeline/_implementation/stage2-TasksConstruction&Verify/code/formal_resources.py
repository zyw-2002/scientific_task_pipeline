"""Bind a reviewed package's formal data to a checked preparation, shared by variants."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import uuid

from contract import atomic_write_json, package_fingerprint, package_path, read_reference
from prepare_resources import prepare_package
from resource_contract import check_available, verify_content


def declared(spec):
    if spec.get('assessment_contract_version') != 1:
        return {}
    return {source['id']: source for source in spec['data']['sources']
            if source['role'] != 'fixture' and source.get('visibility') != 'build_only'}


def inspect_formal(package, resources, *, common=False):
    def locate(rid):
        name = Path(resources[rid]['package_path'])
        if common:
            name = name.relative_to('common_task')
        return package_path(package, name.as_posix())
    return check_available(resources, locate, include_fixtures=False)


def preparation(source, spec, receipt, image, reference_overlay=None, *, solver_proxy_url=None):
    resources = declared(spec)
    if not resources:
        return None
    def locate(rid):
        item = resources[rid]
        directory = reference_overlay if reference_overlay is not None and item['role'] == 'reference' else source
        return package_path(directory, item['package_path'])
    try:
        check_available(resources, locate, include_fixtures=False)
    except (ValueError, OSError) as original_error:
        preflight = read_reference(receipt['preflight'], source)
        deferred = {item['id'] for item in preflight.get('deferred_resources', [])}
        if not deferred:
            raise ValueError(f'formal input failed validation without a reviewed preparation: {original_error}') from original_error
        for rid, item in resources.items():
            if item.get('required', True) and rid not in deferred:
                verify_content(locate(rid), item)
    else:
        # An ID count, schema or binary header does not authenticate the split.
        # In this case the task's validator must execute in its isolated image.
        if not any(item.get('required', True) and item.get('availability') != 'bundled'
                   and not item.get('sha256') and item.get('prepare', {}).get('validate')
                   for item in resources.values()):
            return None
    if not image:
        image = spec['runtime'].get('base_image')
    if not image:
        raise ValueError('bind the task image to prepare its reviewed formal resources')
    source_hash = package_fingerprint(source)
    overlay_hash = package_fingerprint(reference_overlay) if reference_overlay is not None else ''
    cache_key = hashlib.sha256(('validated-formal-data-v2-source-materialization\n' + source_hash + '\n' + image + '\n' + overlay_hash).encode()).hexdigest()
    cache_home = Path(os.environ.get('XDG_CACHE_HOME', Path.home()/'.cache')) / 'pipline' / 'formal-resources'
    cache = Path(os.environ.get('PIPELINE_RESOURCE_CACHE_DIR', cache_home)) / cache_key
    if cache.resolve().is_relative_to(source.resolve()):
        raise ValueError('formal resource cache must be outside the immutable source package')
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (cache/'prepare.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        pointer = cache/'current.json'
        if pointer.is_file():
            try:
                cached = json.loads(pointer.read_text())
                path = Path(cached['package']).resolve()
                if (path.is_relative_to(cache.resolve()) and path.is_dir()
                        and cached.get('source_package_sha256') == source_hash
                        and cached.get('prepared_package_sha256') == package_fingerprint(path)):
                    inspect_formal(path, resources)
                    return path
            except (OSError, KeyError, ValueError, TypeError):
                pass  # A new attempt replaces the pointer only after its own proof passes.
        # A new attempt has its own directory. Only a complete verified attempt
        # is published. Interrupted attempts remain available for diagnosis.
        output = cache / ('attempt-' + uuid.uuid4().hex)
        network = 'bridge' if spec['runtime'].get('network_policy') == 'enabled' else 'none'
        if spec['runtime'].get('network_policy') == 'allowlisted':
            raise ValueError('formal preparation requires a runner enforcing the declared host allowlist')
        options = {'reference_overlay': reference_overlay} if reference_overlay is not None else {}
        if network == 'bridge' and solver_proxy_url:
            options['proxy_url'] = solver_proxy_url
        result = prepare_package(source, output, image=image, network=network, **options)
        if result.get('status') != 'pass':
            raise ValueError(f'formal data preparation failed: {output / "preparation.json"}: {result.get("error")}')
        path = Path(result['package'])
        inspect_formal(path, resources)
        if (result.get('source_package_sha256') != source_hash or package_fingerprint(source) != source_hash
                or result.get('prepared_package_sha256') != package_fingerprint(path)):
            raise ValueError('prepared resource evidence has stale package fingerprints')
        atomic_write_json(pointer, result)
        return path
