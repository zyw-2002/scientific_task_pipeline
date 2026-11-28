"""Resource identities and explicit authoring/runtime resolution.

Static validation never downloads data or imports task code. The prepare CLI is
for a disposable container with a writable package, before read-only deployment.
"""
from __future__ import annotations

import argparse
import ast
import struct
import copy
import csv
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
import subprocess


def digest(path):
    path = Path(path)
    if path.is_file():
        result = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''): result.update(block)
        return result.hexdigest()
    entries = [(p.relative_to(path).as_posix(), digest(p))
               for p in sorted(path.rglob('*')) if p.is_file()]
    return hashlib.sha256(json.dumps(entries).encode()).hexdigest()


def relative(value):
    return (isinstance(value, str) and bool(value) and '\\' not in value
            and not PurePosixPath(value).is_absolute()
            and '..' not in PurePosixPath(value).parts and value != '.')


def argv(value):
    return isinstance(value, list) and bool(value) and all(isinstance(v, str) and v for v in value)


def valid_digest(value):
    return isinstance(value, str) and bool(re.fullmatch('[0-9a-f]{64}', value))


def has_payload(path):
    def payload_file(item):
        return (item.name not in {'.placeholder', '.gitkeep', '.keep'} and
                not item.is_symlink() and item.is_file() and item.stat().st_size > 0)
    if path.is_symlink():
        return False
    return any(payload_file(p) for p in path.rglob('*')) if path.is_dir() else payload_file(path)


def file_manifest_valid(manifest):
    return bool(isinstance(manifest, dict) and manifest and
                all(relative(path) and PurePosixPath(path).as_posix() == path and valid_digest(checksum)
                    for path, checksum in manifest.items()))


def source_errors(package, spec, *, preparing=False, construction_only=False):
    """Check recipes and identities, without requiring deferred resources now."""
    errors, seen, locations = [], set(), set()
    sources = spec.get('data', {}).get('sources', [])
    if not isinstance(sources, list):
        return ['data.sources must be an array']
    deferred = set()
    for item in sources:
        if not isinstance(item, dict):
            errors.append('resource must be an object'); continue
        rid = item.get('id')
        if not isinstance(rid, str) or not rid or rid in seen:
            errors.append('resources need unique nonempty id'); continue
        seen.add(rid)
        if item.get('role') not in {'input', 'reference', 'fixture', 'support'}:
            errors.append(f'{rid}: role must be input/reference/fixture/support')
        if item.get('origin') not in {'observation', 'reference_implementation', 'analytical',
                                     'simulation', 'benchmark_policy', 'fixture'}:
            errors.append(f'{rid}: declare the origin of the resource')
        if (item.get('role') == 'fixture') != (item.get('origin') == 'fixture'):
            errors.append(f'{rid}: fixture role and origin must agree')
        for key in ('identity', 'version'):
            if not isinstance(item.get(key), str) or not item[key].strip():
                errors.append(f'{rid}: {key} must describe identity/version (unknown allowed explicitly)')
        public = spec.get('framework', {}).get('public_dir', 'common_task/environment/input')
        path = item.get('package_path')
        if not relative(path):
            errors.append(f'{rid}: package_path must name a safe file or directory'); continue
        p = Path(package) / path
        if path in locations: errors.append(f'{rid}: each resource needs a distinct package_path')
        locations.add(path)
        required = item.get('required', True)
        if type(required) is not bool:
            errors.append(f'{rid}: required must be boolean')
        if required is False and (item.get('role'), item.get('visibility')) != ('support', 'public'):
            errors.append(f'{rid}: only public support resources may be optional')
        protected = item.get('role') in {'reference', 'fixture'}
        if protected and (item.get('visibility') != 'private' or p.is_relative_to(Path(package) / public)):
            errors.append(f'{rid}: reference/fixture resources must remain private')
        if protected and not p.is_relative_to(Path(package) / 'common_task/evaluation'):
            errors.append(f'{rid}: package_path={path!r} resolves to {p}; private scoring resources '
                          'must be under common_task/evaluation/ relative to the package root '
                          '(the directory containing execution.json), not common_task/')
        if not protected and item.get('visibility') == 'public' and not p.is_relative_to(Path(package) / public):
            errors.append(f'{rid}: package_path={path!r} resolves to {p}; public resources must be '
                          f'under {public}/ relative to the package root '
                          '(the directory containing execution.json), not common_task/')
        if item.get('visibility') not in {'public', 'private', 'build_only', 'guided'}:
            errors.append(f'{rid}: explicit visibility is required')
        availability = item.get('availability')
        if construction_only and not preparing and spec.get('scope_contract_version') == 3 and item.get('role') in {'input', 'reference'}:
            deferred.add(rid)
            if p.resolve() != Path(package).resolve() / path:
                errors.append(f'{rid}: formal resource path cannot traverse symlinks')
            if 'sha256' in item and not valid_digest(item['sha256']):
                errors.append(f'{rid}: sha256 must be a valid file digest')
        if availability not in {'bundled', 'preprovided', 'runtime_download', 'unverified', 'unavailable'}:
            errors.append(f'{rid}: explicit availability is required')
        if availability == 'bundled' or item.get('role') == 'fixture':
            if not p.exists() or (p.is_file() and p.stat().st_size == 0):
                hint = f'; a file/directory exists at common_task/{path}; check the missing common_task/ prefix' if (Path(package) / 'common_task' / path).exists() else ''
                errors.append(f'{rid}: bundled/fixture resource is missing or empty at {p} '
                              f'(package_path={path!r}){hint}')
        prep = item.get('prepare', {})
        if not isinstance(prep, dict): prep = {}
        if (required is not False and availability != 'bundled') or availability == 'runtime_download' or 'prepare' in item:
            if not argv(prep.get('command')) or not argv(prep.get('validate')):
                errors.append(f'{rid}: deferred resource needs prepare.command and prepare.validate argv')
            if not isinstance(prep.get('access'), str) or not prep.get('access', '').strip():
                errors.append(f'{rid}: explain access/download/preprovided prerequisites')
            if type(prep.get('timeout_sec')) is not int or prep.get('timeout_sec', 0) <= 0:
                errors.append(f'{rid}: preparation needs a positive timeout_sec')
        schema = item.get('content', {})
        if 'files_sha256' in item and (not isinstance(schema, dict) or schema.get('format') != 'directory'
                                       or not file_manifest_valid(item['files_sha256'])):
            errors.append(f'{rid}: files_sha256 requires a directory and valid relative member digests')
        if not isinstance(schema, dict) or schema.get('format') not in {'csv', 'tsv', 'json', 'text', 'binary', 'directory', 'npy'}:
            errors.append(f'{rid}: content.format is required')
        if isinstance(schema, dict):
            if rid in deferred and p.exists():
                if schema.get('format') == 'directory':
                    if not p.is_dir() or (availability == 'bundled' and not any(p.iterdir())):
                        errors.append(f'{rid}: expected a nonempty bundled directory or a prepared directory at {p}')
                elif not p.is_file():
                    errors.append(f'{rid}: declared file path is not a regular file: {p}')
            if set(schema) - {'format', 'columns', 'group_column', 'row_count', 'id_column', 'item_count', 'items_path', 'shape', 'members', 'forbidden_keys'}:
                errors.append(f'{rid}: unsupported content constraints')
            if 'group_column' in schema and (not isinstance(schema['group_column'], str) or not schema['group_column'].strip()
                    or schema['group_column'] == schema.get('id_column')
                    or (isinstance(schema.get('columns'), list) and schema['group_column'] not in schema['columns'])):
                errors.append(f'{rid}: content.group_column must identify a grouping annotation distinct from the ID')
            if set(schema) & {'columns', 'row_count', 'id_column'} and schema.get('format') not in {'csv', 'tsv'}:
                errors.append(f'{rid}: resource column/count/ID constraints require CSV or TSV')
            if 'row_count' in schema and (type(schema['row_count']) is not int or schema['row_count'] < 0):
                errors.append(f'{rid}: content.row_count must be a nonnegative integer')
            if 'columns' in schema and (not isinstance(schema['columns'], list) or
                    not all(isinstance(v, str) and v for v in schema['columns'])):
                errors.append(f'{rid}: content.columns must be column names')
            if 'id_column' in schema and (not isinstance(schema['id_column'], str) or not schema['id_column']):
                errors.append(f'{rid}: content.id_column must be a column name')
            if schema.get('id_column') and 'columns' in schema and schema['id_column'] not in schema['columns']:
                errors.append(f'{rid}: id_column must occur in columns')
            if 'forbidden_keys' in schema and (schema.get('format') not in {'json', 'csv', 'tsv'} or not isinstance(schema['forbidden_keys'], list) or not all(isinstance(k, str) and k for k in schema['forbidden_keys'])):
                errors.append(f'{rid}: forbidden_keys needs JSON/CSV and a list of field names')
            if 'item_count' in schema and (schema.get('format') != 'json' or type(schema['item_count']) is not int or schema['item_count'] < 0):
                errors.append(f'{rid}: item_count needs JSON and a nonnegative integer')
            if 'items_path' in schema and (schema.get('format') != 'json' or not isinstance(schema['items_path'], list) or any(not isinstance(k, str) for k in schema['items_path'])):
                errors.append(f'{rid}: items_path must be JSON object keys')
            if 'shape' in schema and (schema.get('format') != 'npy' or not isinstance(schema['shape'], list) or not schema['shape'] or any(type(n) is not int or n < 0 for n in schema['shape'])):
                errors.append(f'{rid}: shape needs NPY and nonnegative integer dimensions')
            if 'members' in schema:
                members = schema['members']
                if schema.get('format') != 'directory' or not isinstance(members, list) or not members:
                    errors.append(f'{rid}: members needs a directory and nonempty pattern/count entries')
                else:
                    for member in members:
                        if not isinstance(member, dict) or not relative(member.get('pattern')) or type(member.get('count')) is not int or member['count'] < 0:
                            errors.append(f'{rid}: directory members need safe relative pattern and nonnegative count')
            # Construction validates fixtures. Runtime preparation may replace an incomplete cache.
            prepare_can_replace = preparing and availability != 'bundled' and isinstance(prep, dict) and argv(prep.get('command'))
            if rid not in deferred and not prepare_can_replace and (p.exists() or p.is_symlink()) and not any(e.startswith(rid + ':') for e in errors):
                try: verify_content(p, item)
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    kind = 'bundled' if availability == 'bundled' else 'supplied'
                    errors.append(f'{rid}: {kind} content invalid: {exc}')
    indexed = {s['id']: s for s in sources if isinstance(s, dict) and isinstance(s.get('id'), str)}
    def visit(rid, chain):
        if rid in chain: raise ValueError('resource preparation cycle: ' + ' -> '.join([*chain, rid]))
        prep = indexed[rid].get('prepare', {})
        deps = prep.get('inputs', []) if isinstance(prep, dict) else []
        if not isinstance(deps, list) or any(not isinstance(v, str) or v not in indexed for v in deps):
            raise ValueError(f'{rid}: prepare.inputs must reference declared resources')
        for dep in deps:
            if indexed[rid].get('required', True) and not indexed[dep].get('required', True):
                raise ValueError(f'{rid}: required preparation depends on optional resource {dep}')
            visit(dep, [*chain, rid])
    for rid in indexed:
        try: visit(rid, [])
        except ValueError as exc: errors.append(str(exc))
        alignment = indexed[rid].get('align_with', [])
        if not isinstance(alignment, list) or any(not isinstance(v, str) or v not in indexed or v == rid for v in alignment):
            errors.append(f'{rid}: align_with must reference other declared resources'); continue
        for other in alignment:
            if not indexed[rid].get('required', True) or not indexed[other].get('required', True):
                errors.append(f'{rid}: formal ID alignment cannot use optional resources')
            for target in (rid, other):
                content = indexed[target].get('content', {})
                if not isinstance(content, dict) or content.get('format') not in {'csv', 'tsv'} or not content.get('id_column'):
                    errors.append(f'{rid}: align_with requires CSV/TSV id_column on both resources')
            if (indexed[rid].get('role') == 'fixture') != (indexed[other].get('role') == 'fixture'):
                errors.append(f'{rid}: align fixture resources only with fixtures')
    if not errors:
        available = {rid for rid, item in indexed.items() if item.get('availability') == 'bundled'} - deferred
        aligned = {rid: dict(item, align_with=[other for other in item.get('align_with', []) if other in available]) for rid, item in indexed.items()}
        try: verify_alignment(aligned, lambda rid: Path(package) / indexed[rid]['package_path'], available)
        except (OSError, ValueError, KeyError, TypeError) as exc: errors.append(str(exc))
    return list(dict.fromkeys(errors))


def generated_resources(spec, evaluation_relative, container_dir):
    """Freeze resource bindings in protocol; never invent a resource or recipe."""
    result = {}
    public = PurePosixPath(spec.get('framework', {}).get('public_dir', 'common_task/environment/input'))
    evaluation = PurePosixPath(evaluation_relative)
    for source in spec.get('data', {}).get('sources', []):
        if not relative(source.get('package_path')):
            raise ValueError(f"{source.get('id')}: resource package_path must stay inside the package")
        rid, p = source['id'], PurePosixPath(source['package_path'])
        if p.is_relative_to(evaluation):
            runtime = str(PurePosixPath(container_dir) / p.relative_to(evaluation))
        elif p.is_relative_to(public):
            runtime = str(PurePosixPath('/task/input') / p.relative_to(public))
        else:
            raise ValueError(f'{rid}: resource is outside runtime mounts')
        result[rid] = {k: copy.deepcopy(source[k]) for k in
                      ('role', 'origin', 'visibility', 'identity', 'version', 'availability', 'content')}
        result[rid].update(package_path=str(p), runtime_path=runtime)
        if p.suffix.lower() == '.h5ad' and isinstance(source.get('schema_description'), str):
            result[rid]['schema_description'] = source['schema_description']
        if source.get('required') is False:
            result[rid]['required'] = False
        if source.get('sha256'): result[rid]['sha256'] = source['sha256']
        if source.get('files_sha256'): result[rid]['files_sha256'] = dict(source['files_sha256'])
        if source.get('align_with'): result[rid]['align_with'] = copy.deepcopy(source['align_with'])
    return result


def bind_protocol_resources(config, spec, evaluation_relative, container_dir):
    config['resources'] = generated_resources(spec, evaluation_relative, container_dir)
    if spec.get('scope_contract_version') == 3:
        config['scope_contract_version'] = 3
    validated_study_splits(config, config['resources'])


def npy_shape(path):
    """Read a bounded NPY header without loading arrays or executing pickle."""
    with Path(path).open('rb') as stream:
        if stream.read(6) != b'\x93NUMPY': raise ValueError('invalid NPY magic')
        version = stream.read(2)
        if version not in (b'\x01\x00', b'\x02\x00', b'\x03\x00'): raise ValueError('unsupported NPY version')
        size = 2 if version[0] == 1 else 4
        raw = stream.read(size)
        if len(raw) != size: raise ValueError('truncated NPY header')
        length = struct.unpack('<H' if size == 2 else '<I', raw)[0]
        if length > 65536: raise ValueError('NPY header too large')
        raw = stream.read(length)
        if len(raw) != length: raise ValueError('truncated NPY header')
        try: header = ast.literal_eval(raw.decode('utf-8' if version[0] == 3 else 'latin1'))
        except (ValueError, SyntaxError) as exc: raise ValueError('invalid NPY header') from exc
        shape = header.get('shape') if isinstance(header, dict) else None
        if not isinstance(shape, tuple) or any(type(n) is not int or n < 0 for n in shape):
            raise ValueError('invalid NPY shape')
        # For ordinary scalar numeric arrays also reject truncated payloads.
        import re
        dtype = header.get('descr')
        match = re.fullmatch(r'[<>=|]([biufc?])(\d+)', dtype) if isinstance(dtype, str) else None
        if not match: raise ValueError('NPY resources require a scalar numeric dtype; use binary plus a reviewed validator for other formats')
        count = 1
        for n in shape: count *= n
        if Path(path).stat().st_size - stream.tell() != count * int(match[2]):
            raise ValueError('NPY payload size differs from header')
        return shape


def verify_content(path, resource):
    if not path.exists(): raise FileNotFoundError(f"resource not prepared: {resource['identity']} at {path}")
    if path.is_symlink() or (path.is_dir() and any(p.is_symlink() for p in path.rglob('*'))):
        raise ValueError('resource cannot contain symlinks: ' + str(path))
    content = resource['content']
    if content['format'] == 'directory':
        if not path.is_dir() or not has_payload(path):
            raise ValueError('resource directory has no nonempty data files: ' + str(path))
        if 'files_sha256' in resource:
            manifest = resource['files_sha256']
            files = {p.relative_to(path).as_posix(): p for p in path.rglob('*') if p.is_file()}
            if not file_manifest_valid(manifest) or files.keys() != manifest.keys():
                raise ValueError('resource directory differs from the declared checksum manifest')
            if any(digest(files[name]) != checksum for name, checksum in manifest.items()):
                raise ValueError('resource directory member checksum mismatch')
        for member in content.get('members', []):
            if not relative(member['pattern']): raise ValueError('unsafe directory member pattern')
            matches = [p for p in path.glob(member['pattern']) if p.is_file()]
            if len(matches) != member['count']:
                raise ValueError(f"resource directory member count for pattern {member['pattern']!r}: "
                                 f"expected {member['count']} files, found {len(matches)} under {path.name}")
            empty = next((p for p in matches if p.stat().st_size == 0), None)
            if empty is not None:
                raise ValueError(f"resource directory pattern {member['pattern']!r}: empty file {empty.relative_to(path)}")
    elif not path.is_file() or not path.stat().st_size:
        raise ValueError('resource file is missing/empty: ' + str(path))
    elif content['format'] in {'csv', 'tsv'}:
        with path.open('rb') as raw:
            if raw.read(8) == b'\x89HDF\r\n\x1a\n':
                raise ValueError(f'{path.name} contains HDF5 bytes but content.format declares CSV; '
                                 'verify the real schema and the preparation output')
        with path.open(newline='', encoding='utf-8') as stream:
            reader = csv.DictReader(stream, delimiter='\t' if content['format'] == 'tsv' else ',')
            if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
                raise ValueError('reference CSV has missing/duplicate columns')
            if 'columns' in content and reader.fieldnames != content['columns']:
                raise ValueError('resource columns differ from declaration')
            if set(content.get('forbidden_keys', [])) & set(reader.fieldnames):
                raise ValueError('resource exposes forbidden fields')
            if content.get('id_column') and content['id_column'] not in reader.fieldnames:
                raise ValueError('resource ID column is missing')
            ids, count = set(), 0
            for row in reader:
                if None in row or None in row.values(): raise ValueError('ragged reference CSV')
                count += 1
                if 'id_column' in content:
                    value = row[content['id_column']]
                    if not value or value in ids: raise ValueError('resource has missing/duplicate IDs')
                    ids.add(value)
            if 'row_count' in content and count != content['row_count']:
                raise ValueError('resource row count differs from declaration')
    elif content['format'] == 'json':
        value = json.loads(path.read_text())
        forbidden = set(content.get('forbidden_keys', []))
        pending = [value] if forbidden else []
        while pending:
            node = pending.pop()
            if isinstance(node, dict):
                if forbidden & node.keys(): raise ValueError('resource exposes forbidden fields')
                pending.extend(node.values())
            elif isinstance(node, list): pending.extend(node)
        for key in content.get('items_path', []):
            if not isinstance(value, dict) or key not in value: raise ValueError('resource JSON items_path missing: ' + key)
            value = value[key]
        if 'item_count' in content and (not isinstance(value, (list, dict)) or len(value) != content['item_count']):
            raise ValueError('resource JSON item count differs from declaration')
    elif content['format'] == 'npy':
        shape = npy_shape(path)
        if 'shape' in content and list(shape) != content['shape']:
            raise ValueError('resource NPY shape differs from declaration')
    if resource.get('sha256') and digest(path) != resource['sha256']:
        raise ValueError('resource checksum mismatch: ' + str(path))


def _text_id(value):
    if isinstance(value, bytes):
        value = value.decode('utf-8')
    if value is None or str(value).strip().casefold() in {'', 'nan', 'none', '<na>'}:
        raise ValueError('missing ID or held-out group in the formal data')
    return str(value).strip()


def _h5_column(group, name, *, categorical=True):
    import h5py
    if not isinstance(name, str) or not name or '/' in name or not isinstance(group.get(name, getlink=True), h5py.HardLink):
        raise ValueError('missing or externally linked H5AD field ' + str(name))
    node = group[name]
    if isinstance(node, h5py.Dataset):
        if node.ndim != 1 or node.is_virtual or node.external:
            raise ValueError('expected a self-contained one-dimensional H5AD field')
        return node[:]
    if not categorical or node.attrs.get('encoding-type') not in {'categorical', b'categorical'}:
        raise ValueError('unsupported H5AD field encoding: ' + name)
    categories = _h5_column(node, 'categories', categorical=False)
    codes = _h5_column(node, 'codes', categorical=False)
    if codes.dtype.kind not in 'iu' or any(code < 0 or code >= len(categories) for code in codes):
        raise ValueError('missing or invalid H5AD categorical code in ' + name)
    return [categories[code] for code in codes]


def _members(path, id_column, group_column=None):
    if not isinstance(id_column, str) or not id_column or id_column == group_column:
        raise ValueError('identify the formal sample ID separately from its study or batch')
    members = {}

    def add(identifier, group):
        identifier = _text_id(identifier)
        if identifier in members:
            raise ValueError('duplicate observation ID in ' + path.name)
        members[identifier] = _text_id(group).casefold() if group_column else None

    if path.suffix.lower() == '.h5ad':
        try:
            import h5py
        except ImportError as exc:
            raise ValueError('h5py is required to verify the H5AD observation metadata') from exc
        with h5py.File(path, 'r') as handle:
            if not isinstance(handle.get('obs', getlink=True), h5py.HardLink):
                raise ValueError('missing or externally linked H5AD obs dataframe')
            obs = handle['obs']
            index = _text_id(obs.attrs.get('_index', '_index'))
            ids = _h5_column(obs, index if id_column in {'obs_names', '_index'} else id_column)
            groups = _h5_column(obs, group_column) if group_column else None
            if groups is not None and len(groups) != len(ids):
                raise ValueError('H5AD observation columns have different lengths')
            for i, identifier in enumerate(ids):
                add(identifier, None if groups is None else groups[i])
    elif path.suffix.lower() in {'.csv', '.tsv'}:
        with path.open(newline='', encoding='utf-8-sig') as stream:
            reader = csv.DictReader(stream, delimiter='\t' if path.suffix.lower() == '.tsv' else ',')
            fields = reader.fieldnames or []
            if len(fields) != len(set(fields)) or id_column not in fields or group_column and group_column not in fields:
                raise ValueError('missing or ambiguous formal ID or group column in ' + path.name)
            for row in reader:
                add(row[id_column], row[group_column] if group_column else None)
    else:
        raise ValueError('held-out membership requires formal H5AD obs metadata or a CSV/TSV ID table')
    if not members:
        raise ValueError('empty formal membership in ' + path.name)
    return members


def selected_unit_ids(selection, available, field):
    if (not isinstance(selection, list) or not selection
            or not all(isinstance(uid, str) and uid in available for uid in selection)
            or len(set(selection)) != len(selection)):
        raise ValueError(field + ' must list distinct existing evaluator unit IDs')
    return set(selection)


def validated_study_splits(config, resources, locate=None):
    """Validate declared study holdouts for their evaluator units and, when available, their bytes."""
    splits = config.get('held_out_splits')
    if splits is None and 'held_out_unit_ids' not in config:
        return []
    declared_units = config.get('units', [])
    if (not isinstance(declared_units, list) or not all(isinstance(u, dict) and isinstance(u.get('id'), str)
            and u['id'] and isinstance(u.get('path'), str) and u['path'] for u in declared_units)):
        raise ValueError('held_out_splits requires evaluator units with IDs and submission member paths')
    units = {u['id']: u['path'] for u in declared_units}
    if len(units) != len(declared_units):
        raise ValueError('held_out_splits requires unique evaluator unit IDs')
    panel = (selected_unit_ids(config['held_out_unit_ids'], units, 'held_out_unit_ids')
             if 'held_out_unit_ids' in config else set(units))
    if (not isinstance(splits, list) or not panel or len(splits) != len(panel)
            or not all(isinstance(s, dict) and isinstance(s.get('unit_id'), str) for s in splits)
            or {s['unit_id'] for s in splits} != panel):
        raise ValueError('held_out_splits must cover each designated formal evaluator unit exactly once')
    panel_paths = [units[uid] for uid in panel]
    if len(set(panel_paths)) != len(panel_paths):
        raise ValueError('independent held-out splits need distinct submission members or separate task executions')
    declared = config.get('required_artifacts', [])
    for path in panel_paths:
        if not any(path in a.get('paths', []) or ('pattern' in a and Path(path).match(a['pattern'])) for a in declared):
            raise ValueError('held-out unit path is absent from the required submission artifacts')
    records = []
    instances = set()
    for split in splits:
        unit = split['unit_id']
        rids = [split.get(k) for k in ('reference_resource', 'query_resource', 'scoring_resource')]
        if len(set(rids)) != 3 or any(r not in resources for r in rids):
            raise ValueError('each study split needs three distinct declared resources')
        ref, query, score = (resources[r] for r in rids)
        if (any(resources[r].get('required', True) is not True or resources[r].get('origin') == 'fixture' for r in rids)
                or any((x.get('role'), x.get('visibility')) != ('input', 'public') for x in (ref, query))
                or (score.get('role'), score.get('visibility')) != ('reference', 'private')):
            raise ValueError('study splits need two formal Solver inputs and private scoring observations')
        if tuple(rids) in instances:
            raise ValueError('different evaluator units must not reuse the identical study split')
        instances.add(tuple(rids))
        field = split.get('group_column')
        if not isinstance(field, str) or not field.strip():
            raise ValueError('identify the source annotation used to separate held-out groups')
        bound_columns = {b['column'] for a in config.get('required_artifacts', [])
                         if isinstance(b := a.get('id_set'), dict) and b.get('resource') == rids[2]
                         and units[unit] in a.get('paths', [])}
        scoring_id = split.get('scoring_id_column') or (next(iter(bound_columns)) if len(bound_columns) == 1 else None)
        if (not scoring_id or scoring_id not in score.get('content', {}).get('columns', [])
                or (scoring_id not in bound_columns and rids[2] not in config.get('resource_bindings', {}).values())):
            raise ValueError('bind the study split to the actual evaluator and its private ID column')
        ids = []
        for source in (ref, query):
            content = source.get('content', {})
            if 'group_column' in content and content['group_column'] != field:
                raise ValueError('held-out group field differs from the declared resource grouping column')
            if field in {scoring_id, content.get('id_column')}:
                raise ValueError('identify a group annotation distinct from sample IDs')
            if source['package_path'].lower().endswith('.h5ad'):
                obs_field = r"\bobs(?:\." + re.escape(field) + r"(?![\w.-])|\[['\"]" + re.escape(field) + r"['\"]\])"
                if (field in {'obs_names', '_index'} or
                        (content.get('group_column') != field and not re.search(obs_field, str(source.get('schema_description', ''))))):
                    raise ValueError('declare the H5AD grouping field in content.group_column or its obs schema')
                ids.append('obs_names')
            else:
                if field not in content.get('columns', []) or not content.get('id_column'):
                    raise ValueError('formal tabular study inputs must declare their ID and study columns')
                ids.append(content['id_column'])
        if locate is not None:
            paths = [Path(locate(r)) for r in rids]
            before, after = (_members(path, identifier, field) for path, identifier in zip(paths[:2], ids))
            if all(key.casefold() == group for rows in (before, after) for key, group in rows.items()):
                raise ValueError('individual observation IDs cannot serve as held-out groups')
            scored = _members(paths[2], scoring_id)
            if before.keys() & after.keys() or set(before.values()) & set(after.values()):
                raise ValueError('reference and query share observations or held-out study groups: ' + unit)
            if (after.keys() != scored.keys() or not bound_columns and list(after) != list(scored)):
                raise ValueError('query observations differ from evaluator membership/order: ' + unit)
            records.append(dict(unit_id=unit, reference_count=len(before), query_count=len(after),
                                reference_groups=len(set(before.values())), query_groups=len(set(after.values()))))
    return records


def verify_alignment(resources, locate, selected=None):
    """Compare complete ID sets, not row order or the shorter prefix."""
    def ids(rid):
        with Path(locate(rid)).open(newline='', encoding='utf-8') as stream:
            content = resources[rid]['content']
            delimiter = '\t' if content.get('format', 'csv') == 'tsv' else ','
            values = [row[content['id_column']] for row in csv.DictReader(stream, delimiter=delimiter)]
        if any(not v for v in values) or len(set(values)) != len(values):
            raise ValueError(f'{rid}: alignment requires unique nonempty IDs')
        return set(values)
    for rid in (resources if selected is None else selected):
        for other in resources[rid].get('align_with', []):
            if ids(rid) != ids(other):
                raise ValueError(f'resource ID sets differ: {rid} vs {other}')


def resolve(config, config_path, *, mode='runtime', test_bindings=None):
    """Resolve a test instance without changing the stored formal protocol.

    Only resource-bound instance sizes may vary; formulas, weights, adapter and
    independent structural constraints remain identical to formal evaluation.
    """
    if mode not in {'runtime', 'authoring'}: raise ValueError('unknown resource mode')
    config = copy.deepcopy(config)
    resources = config.get('resources', {})
    swaps = test_bindings or {}
    if not isinstance(swaps, dict): raise ValueError('test bindings must map resource IDs to fixture IDs')
    for original, fixture in swaps.items():
        if original not in resources or resources[original]['role'] == 'fixture' or fixture not in resources or resources[fixture]['role'] != 'fixture':
            raise ValueError(
                f'test binding {original!r} -> {fixture!r} must map a formal resource '
                'to a declared fixture; declare both IDs in execution.json.data.sources '
                '(fixture: role=fixture, origin=fixture, visibility=private, '
                'availability=bundled, package_path under common_task/evaluation/), '
                'then regenerate protocol.json; protocol.resources is generated')
        # A test instance may have fewer records, but retains the formal interface.
        a, b = resources[original].get('content', {}), resources[fixture].get('content', {})
        for key in ('format', 'columns', 'id_column', 'items_path', 'forbidden_keys'):
            # A fixture may omit optional shape metadata, but it must retain
            # every formal answer-exposure prohibition.  Treat an omitted
            # ``forbidden_keys`` field as a mismatch when the formal resource
            # declares one, so authoring smoke cannot silently expose labels.
            if key in a and (key in b or key == 'forbidden_keys') and a[key] != b.get(key):
                raise ValueError(f'test fixture changes declared resource {key}: {original}')
    root = Path(config_path).resolve().parent
    package = root.parent.parent
    paths, evidence, instance_sizes = {}, {}, []
    memberships = []
    def get(rid):
        if rid in paths: return paths[rid]
        if mode == 'authoring' and test_bindings is not None and config.get('scope_contract_version') == 3 and rid not in swaps:
            raise ValueError(f'authoring smoke needs a representative fixture binding for {rid}; formal data is checked at runtime')
        actual = swaps.get(rid, rid)
        if actual not in resources: raise ValueError('unknown resource: ' + str(actual))
        resource = resources[actual]
        formal_resource = resources[rid]
        if not resources[rid].get('required', True):
            raise ValueError(f'formal scoring cannot depend on optional resource {rid}')
        if resources[rid]['role'] == 'fixture': raise ValueError('formal evaluation cannot directly use fixture resources')
        if mode == 'authoring':
            relative_path = PurePosixPath(resource.get('package_path') or resource.get('path', ''))
            if not relative_path.as_posix() or relative_path.as_posix() == '.':
                raise ValueError(f'resource has no package path: {actual}')
            # Evaluation-only calibration copies keep private fixture paths valid.
            private = PurePosixPath('common_task/evaluation')
            p = root / relative_path.relative_to(private) if relative_path.is_relative_to(private) else (
                root / relative_path if resource.get('role') == 'fixture' else package / relative_path)
        else:
            p = Path(resource['runtime_path'])
        validation_resource = resource
        if resource.get('role') == 'fixture' and not resource.get('content'):
            structural = {key: formal_resource.get('content', {}).get(key)
                          for key in ('format', 'columns', 'id_column', 'items_path', 'forbidden_keys')
                          if key in formal_resource.get('content', {})}
            validation_resource = {**resource, 'identity': resource.get('description', actual),
                                   'content': structural}
        verify_content(p, validation_resource)
        paths[rid] = str(p.resolve())
        evidence[rid] = {'resource_id': actual, 'sha256': digest(p), 'path': str(p), 'role': resource['role']}
        return paths[rid]
    for key, rid in config.get('resource_bindings', {}).items(): config.setdefault('context', {})[key] = get(rid)
    formal_ids = [rid for rid, r in resources.items() if r['role'] != 'fixture']
    verify_alignment(resources, get, formal_ids)
    for artifact in config.get('required_artifacts', []):
        binding = artifact.get('id_set')
        if binding:
            with Path(get(binding['resource'])).open(newline='', encoding='utf-8') as stream:
                fmt = resources[binding['resource']]['content']['format']
                rows = csv.DictReader(stream, delimiter='\t' if fmt == 'tsv' else ',')
                values = [row[binding['column']] for row in rows]
            if len(values) != len(set(values)) or any(not v for v in values):
                raise ValueError('reference ID set must be unique and nonempty')
            artifact['_expected_ids'] = values
            resource = evidence[binding['resource']]
            encoded = json.dumps(sorted(values), ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            memberships.append({'artifact_id': artifact['id'], 'binding': 'id_set',
                                'resource_id': resource['resource_id'], 'resource_sha256': resource['sha256'],
                                'id_column': binding['column'], 'count': len(values),
                                'sorted_ids_sha256': hashlib.sha256(encoded).hexdigest()})
            if binding['resource'] in swaps:
                # Change only sizes explicitly bound to this ID set. Never resize
                # fixed paths, evaluation units or unrelated per-file dimensions.
                key = 'count' if artifact.get('member_id_pattern') else 'row_count'
                if key in artifact:
                    formal_count = resources[binding['resource']]['content'].get('row_count')
                    if artifact[key] != len(values) and (formal_count is None or artifact[key] != formal_count):
                        raise ValueError(f"{artifact['id']}: test size needs an unambiguous formal ID-count binding")
                    instance_sizes.append({'artifact_id': artifact['id'], 'constraint': key,
                                           'formal': artifact[key], 'test': len(values)})
                    artifact[key] = len(values)
    fixture_smoke = mode == 'authoring' and test_bindings is not None and config.get('scope_contract_version') == 3
    study_splits = validated_study_splits(config, resources, None if swaps or fixture_smoke else get)
    return config, {'mode': mode, 'scope': 'fixture' if swaps else 'formal',
                    'resources': evidence, 'instance_sizes': instance_sizes,
                    **({'held_out_splits': study_splits} if study_splits else {}),
                    'required_id_membership': memberships}


def prepare(package, spec, selected=None):
    errors = source_errors(package, spec, preparing=True)
    if errors: raise ValueError('; '.join(errors))
    sources = {s['id']: s for s in spec['data']['sources']}
    done, receipt = set(), []
    def run(rid):
        if rid in done: return
        item = sources[rid]
        recipe = item.get('prepare', {})
        for dep in recipe.get('inputs', []): run(dep)
        output = Path(package) / item['package_path']
        if recipe:
            output.parent.mkdir(parents=True, exist_ok=True)
            replacements = {'{package}': str(Path(package).resolve()), '{output}': str(output.resolve()),
                            '{execution}': str((Path(package) / 'execution.json').resolve()), '{resource_id}': rid}
            def execute(phase):
                command = list(recipe[phase])
                for key, value in replacements.items(): command = [v.replace(key, value) for v in command]
                subprocess.run(command, cwd=package, check=True, timeout=recipe['timeout_sec'])
            if item['availability'] == 'bundled':
                execute('validate')
            elif recipe.get('shared_mount') and not (item.get('sha256') or item.get('files_sha256')):
                execute('command')
                execute('validate')
            else:
                try:
                    verify_content(output, item)
                    execute('validate')
                except (FileNotFoundError, ValueError, csv.Error, subprocess.CalledProcessError):
                    execute('command')
                    execute('validate')
        verify_content(output, item)
        receipt.append({'id': rid, 'sha256': digest(output), 'path': item['package_path'], 'status': 'pass'})
        done.add(rid)
    requested = selected if selected is not None else [rid for rid, item in sources.items()
                                                       if item.get('required', True)]
    for rid in requested: run(rid)
    def locate(rid):
        run(rid)
        return Path(package) / sources[rid]['package_path']
    verify_alignment(sources, locate, list(done))
    if selected is None:
        config_rel = spec.get('verification', {}).get('score_evidence', {}).get('evaluation_protocol', {}).get('config_path')
        if config_rel:
            if not relative(config_rel):
                raise ValueError('evaluation protocol must be inside the prepared package')
            protocol = json.loads((Path(package) / config_rel).read_text())
            validated_study_splits(protocol, sources, locate)
    return receipt


def check_available(resources, locate, *, include_fixtures):
    records = []
    prepared = []
    for rid, item in resources.items():
        if item.get('required') is False and (item.get('role'), item.get('visibility')) != ('support', 'public'):
            raise ValueError(f'{rid}: only public support resources may be optional')
        if not include_fixtures and item['role'] == 'fixture':
            continue
        path = Path(locate(rid))
        if item.get('required') is False and not path.exists() and not path.is_symlink():
            records.append({'id': rid, 'path': str(path), 'status': 'optional_missing'})
            continue
        verify_content(path, item)
        records.append({'id': rid, 'sha256': digest(path), 'path': str(path)})
        prepared.append(rid)
    verify_alignment(resources, locate, prepared)
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path)
    parser.add_argument('--config', type=Path, help='Check formal runtime resources from generated protocol')
    parser.add_argument('--prepare', action='store_true', help='Execute recipes INSIDE a disposable preparation container only')
    parser.add_argument('--resource', action='append')
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.config:
            if args.prepare or args.package: raise ValueError('--config is a read-only runtime check')
            config = json.loads(args.config.read_text())
            resources = config['resources']
            records = check_available(resources, lambda rid: resources[rid]['runtime_path'], include_fixtures=False)
            validated_study_splits(config, resources, lambda rid: resources[rid]['runtime_path'])
        else:
            if not args.package: raise ValueError('--package or --config is required')
            spec = json.loads((args.package / 'execution.json').read_text())
            if args.prepare: records = prepare(args.package, spec, args.resource)
            else:
                errors = source_errors(args.package, spec)
                if errors: raise ValueError('; '.join(errors))
                indexed = {s['id']: s for s in spec['data']['sources']}
                records = check_available(indexed, lambda rid: args.package / indexed[rid]['package_path'], include_fixtures=True)
        result = {'status': 'pass', 'resources': records}
    except Exception as exc: result = {'status': 'fail', 'error': str(exc)}
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))
    return 0 if result['status'] == 'pass' else 1


if __name__ == '__main__':
    raise SystemExit(main())
