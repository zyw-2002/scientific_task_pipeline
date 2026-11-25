"""Bind the 02 review of a fixed benchmark to its frozen text and the actual package."""
import csv
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

from contract import atomic_write_json, package_layout, package_path
from resource_contract import _members, digest, selected_unit_ids, valid_digest, validated_study_splits
from task_contract import has_bytes, resource_delivery


RECEIPT = 'fixed_task_alignment.json'

_HOLDOUT = re.compile(
    r'\b(?:held[ -]out|hold[ -]out|leave[ -]one)[ -]+(?:research[ -]+)?(?:stud(?:y|ies)|batch(?:es)?)\b'
    r'|\b(?:stud(?:y|ies)|batch(?:es)?)\s+(?:are\s+|were\s+|is\s+|was\s+)?held[ -]out\b'
    r'|\b(?:unseen|previously unseen)\s+(?:studies|batches)\b'
    r'|\b(?:split|partitioned)\s+by\s+(?:study|batch)\b'
    r'|留出(?:的)?(?:研究|批次)|按(?:研究|批次)留出', re.I)


def _clauses(text):
    rows, paragraph = [], []
    for index, line in enumerate([*text.splitlines(), ''], 1):
        if line.strip():
            paragraph.append(line)
        elif paragraph:
            if any(not s.startswith(('#', '```')) and not re.fullmatch(r'[| :\-]+', s) for s in paragraph):
                rows.append({'start_line': index - len(paragraph), 'end_line': index - 1,
                             'quote': '\n'.join(paragraph)})
            paragraph = []
    return rows


def _split_table(quote):
    lines = quote.splitlines()
    for i, line in enumerate(lines):
        fields = [v.strip().casefold() for v in line.strip().strip('|').split('|')]
        if line.strip().startswith('|') and {'instance', 'reference', 'query'} <= set(fields):
            keys = [fields.index(k) for k in ('instance', 'reference', 'query')]
            groups = {}
            for body in lines[i + 1:]:
                if not body.strip().startswith('|'):
                    break
                cells = [v.strip() for v in body.strip().strip('|').split('|')]
                if len(cells) == len(fields) and cells[keys[0]] and not re.fullmatch(r'[:\- ]+', cells[keys[0]]):
                    name, reference, query = (cells[i] for i in keys)
                    if name in groups:
                        raise ValueError('duplicate instance in fixed split table: ' + name)
                    groups[name] = (reference, query)
            if groups:
                return groups
    return {}


def _explicit_panel(quote):
    # Compare named test datasets or an enumerated evaluation stratum, not output alphabets.
    if groups := _split_table(quote):
        return set(groups)
    for line in quote.splitlines():
        named = re.search(r'\b(?:datasets|evaluation groups|test groups) are:\s*\*\*([^*]+)\*\*', line, re.I)
        if named and ',' in named[1]:
            return {s.strip().removeprefix('and ').strip() for s in named[1].split(',')}
        if not re.search(r'\b(?:test|evaluation)\s+(?:graphs|sets|groups|cases|datasets|instances)\b', line, re.I):
            continue
        match = re.search(r'∈\s*\{\s*(\d+(?:\s*,\s*\d+)+)\s*\}', line)
        if match:
            return {str(int(v)) for v in match[1].split(',')}
    return set()


def _template(fixed, *, scope_v3=False):
    raw = Path(fixed).read_bytes()
    text = raw.decode('utf-8')
    rows = _clauses(text)
    if not rows:
        raise ValueError('fixed task instruction has no scientific text')
    supervised_holdout = all(re.search(pattern, text, re.I) for pattern in
                             (r'\breference(?:[ ._-]+(?:set|data|cells?|h5ad)|\s+and\s+query)\b|参考集|参考数据|已标注参考',
                              r'\bquery[ ._-]+(?:set|data|cells?|h5ad)\b|查询集|查询细胞',
                              r'\blabell?ed\b|\blabels?\b|\bannotations?\b|标注|标签'))
    for row in rows:
        row.update(status='pending', rationale='', resource_ids=[], evidence=[])
        if panel := _explicit_panel(row['quote']):
            row['evaluation_groups'] = ({'protocol_units': sorted(panel)} if scope_v3 else
                                        {'resource_id': '', 'manifest': '', 'rows_path': [], 'group_field': ''})
        if any(not re.search(r'\b(?:no|not|without)\s+$|不(?:按)?$', row['quote'][:match.start()], re.I)
               for match in _HOLDOUT.finditer(row['quote'])):
            if scope_v3:
                if supervised_holdout:
                    row['held_out_groups'] = {'protocol_splits': True}
                else:
                    row['grouped_evaluation'] = True
            else:
                row['held_out_groups'] = {'reference_resource': '', 'query_resource': '', 'scoring_resource': '',
                                          'source_membership': {'snapshot_path': '', 'id_column': '', 'group_column': ''}}
    template = {'schema_version': 1, 'source_sha256': hashlib.sha256(raw).hexdigest(), 'clauses': rows}
    if scope_v3:
        from solver_output_audit import fixed_deliverables
        deliverables = fixed_deliverables(text)
        if deliverables:
            template['deliverables'] = [dict(item, artifact_id='', instruction_quote='') for item in deliverables]
    return template


def initialize_alignment(package, fixed):
    path = Path(package) / RECEIPT
    if path.exists():
        return False
    spec_path = Path(package) / 'execution.json'
    spec = json.loads(spec_path.read_text()) if spec_path.is_file() else {}
    atomic_write_json(path, _template(fixed, scope_v3=spec.get('scope_contract_version') == 3))
    return True


def _inside_resource(path, source):
    relative = source.get('package_path') or source.get('path')
    return bool(relative and PurePosixPath(path).is_relative_to(relative))


def _contains_quote(path, quote):
    needle = quote.encode('utf-8')
    carry = b''
    with path.open('rb') as stream:
        while block := stream.read(65536):
            chunk = carry + block
            if needle in chunk:
                return True
            carry = chunk[-(len(needle)-1):] if len(needle) > 1 else b''
    return False


def _check_groups(package, group, required, resources, scoring_resources, protocol, split_table, formal_resources):
    if not isinstance(group, dict):
        raise ValueError('provide evaluation_groups for the explicit evaluation population')
    selection = group.get('protocol_units')
    if selection is not None and selection is not False:
        from evaluation_protocol import config_errors
        if error := config_errors(protocol):
            raise ValueError('invalid formal evaluator units: ' + '; '.join(error))
        units = {u['id'] for u in protocol['units']}
        panel = units if selection is True else selected_unit_ids(selection, units, 'evaluation_groups.protocol_units')
        if panel != required:
            raise ValueError('formal evaluator unit IDs differ from the fixed evaluation groups')
        if not scoring_resources or not (set(resources) & scoring_resources):
            raise ValueError('cite the formal scoring resources used by the evaluator units')
        if split_table:
            if not protocol.get('held_out_splits'):
                raise ValueError('bind the source table reference and query populations to each evaluator unit')
            validated_study_splits(protocol, formal_resources)
            splits = {s['unit_id']: s for s in protocol['held_out_splits']}
            if not panel <= splits.keys():
                raise ValueError('held_out_splits must include every original fixed reference/query panel group')
            for name, (reference, query) in split_table.items():
                split = splits[name]
                for role, population in [('reference',reference), ('query',query), ('scoring',query)]:
                    rid = split[role + '_resource']
                    if rid not in resources:
                        raise ValueError(f'cite the formal {role} resource for fixed group {name}')
                    identity = resources[rid].get('identity', '')
                    normalized = ' '.join(identity.casefold().split())
                    if not all(' '.join(term.casefold().split()) in normalized for term in (name,population)):
                        raise ValueError(f'{name} {role} resource identity differs from the original split table')
        return
    resource = resources.get(group.get('resource_id'))
    name = group.get('manifest')
    if not resource or not isinstance(name, str) or not _inside_resource(name, resource):
        raise ValueError('evaluation_groups.manifest must belong to the cited formal resource')
    if group['resource_id'] not in scoring_resources:
        raise ValueError('evaluation-group resource is not bound to the formal scoring protocol')
    path = package_path(package, name)
    field, keys = group.get('group_field'), group.get('rows_path')
    if not isinstance(field, str) or not field or not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        raise ValueError('evaluation_groups needs group_field and rows_path')
    if path.suffix.lower() == '.json':
        rows = json.loads(path.read_text())
        for key in keys:
            rows = rows[key]
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            raise ValueError('group manifest must select records')
        values = {str(row[field]) for row in rows}
    elif path.suffix.lower() in {'.csv', '.tsv'} and not keys:
        with path.open(newline='') as stream:
            values = {str(row[field]) for row in csv.DictReader(stream, delimiter='\t' if path.suffix.lower() == '.tsv' else ',')}
    else:
        raise ValueError('provide a JSON, CSV or TSV evaluation-group manifest from the formal resource')
    if values != required:
        raise ValueError(f'evaluation groups in {name}: expected {sorted(required)}, observed {sorted(values)}')


def _held_out_groups(package, spec, bundle, proof, resources, protocol, fixed_text, deferred, *, construction_only=False):
    if not isinstance(proof, dict):
        raise ValueError('held_out_groups must verify the reference and query groups from original observations')
    if proof.get('protocol_splits') is True:
        if not protocol.get('held_out_splits'):
            raise ValueError('declare a formal evaluator held_out_splits rule for each study split')
        validated_study_splits(protocol, resources)
        trusted_hashes = None
        for split in protocol['held_out_splits']:
            present = []
            for role in ('reference', 'query', 'scoring'):
                rid = split[role + '_resource']
                item = resources[rid]
                path = package_path(package, item['package_path'])
                if (role in {'reference', 'query'} and re.search(r'\b' + role + r'\.h5ad\b', fixed_text, re.I)
                        and path.name.casefold() != role + '.h5ad'):
                    raise ValueError('bind the original ' + role + '.h5ad Solver input')
                if rid not in deferred:
                    if trusted_hashes is None:
                        inventory = json.loads((bundle / 'materials_manifest.json').read_text())
                        if (inventory.get('schema_version') != 'pipline-source-materials-v1'
                                or inventory.get('canonical_id') != spec.get('canonical_id')):
                            raise ValueError('bundled formal split needs matching frozen source inventory')
                        trusted_hashes = {s.get('sha256') for s in inventory.get('artifacts', [])
                                          if s.get('status') == 'available' and s.get('kind') in {'data','dataset','supplementary'}
                                          and s.get('data_scope') != 'sample' and not s.get('sampling_method')}
                    checksum = item.get('sha256') if construction_only else digest(path) if path.is_file() else None
                    if not valid_digest(checksum) or checksum not in trusted_hashes:
                        raise ValueError('bundled study resources need independently recorded original bytes or a formal Stage 3 delivery')
                    present.append(rid)
            if len(present) == 3 and not construction_only:
                one = dict(protocol, held_out_splits=[split], held_out_unit_ids=[split['unit_id']])
                validated_study_splits(one, resources, lambda rid: package_path(package, resources[rid]['package_path']))
        return
    if construction_only:
        raise ValueError('bind held_out_groups.protocol_splits to the formal held_out_splits evaluator rule for runtime membership verification')
    manifest = json.loads((bundle / 'materials_manifest.json').read_text())
    if manifest.get('schema_version') != 'pipline-source-materials-v1' or manifest.get('canonical_id') != spec.get('canonical_id'):
        raise ValueError('held-out groups need the matching frozen original material inventory')
    originals = {a['snapshot_path']: a for a in manifest.get('artifacts', [])
                 if a.get('status') == 'available' and a.get('kind') in {'data', 'dataset', 'supplementary'}
                 and valid_digest(a.get('sha256')) and a.get('data_scope') not in {'sample', 'metadata-only'}
                 and (not a.get('sampling_method') or a.get('data_scope') == 'full')
                 and a.get('dataset_completeness') not in {'partial', 'sample'}}
    paths = {}

    def original(name):
        if name not in originals:
            raise ValueError('cite a frozen formal data artifact for held-out membership')
        if name not in paths:
            path = package_path(bundle, name)
            if not path.is_file() or digest(path) != originals[name]['sha256']:
                raise ValueError('frozen group source checksum differs')
            paths[name] = path
        return paths[name]

    def recorded_resource(source):
        # A full runtime download can already exist in the prepared package. Verify it
        # against an operator catalog copied into the frozen bundle, independent of authoring.
        item = resources.get(source.get('resource_id'), {})
        if not item or item.get('role') == 'fixture' or item.get('origin') == 'fixture':
            raise ValueError('identify the formally prepared observation resource')
        path = package_path(package, item['package_path'])
        if not path.is_file():
            raise ValueError('materialize the original catalogued observations before checking held-out groups')
        checksum = digest(path)
        for a in manifest.get('artifacts', []):
            if a.get('kind') != 'construction_resource_catalog' or a.get('status') != 'available':
                continue
            catalog = package_path(bundle, a['snapshot_path'])
            if digest(catalog) != a.get('sha256'):
                raise ValueError('frozen preparation catalog checksum differs')
            catalog = json.loads(catalog.read_text())
            if catalog.get('canonical_id') == spec.get('canonical_id') and any(
                    r.get('sha256') == checksum and r.get('data_scope') != 'sample'
                    for r in catalog.get('files', catalog.get('resources', []))):
                return path
        raise ValueError('prepared original membership bytes lack an independent operator catalog checksum')

    formal_groups = {}

    def formal(key, column=None):
        rid = proof[key]
        resource = resources.get(rid, {})
        if (not resource or resource.get('required', True) is not True or resource.get('role') == 'fixture'
                or resource.get('origin') == 'fixture'):
            raise ValueError('identify a required formal resource for ' + key)
        if key != 'scoring_resource' and (resource.get('role'), resource.get('visibility')) != ('input', 'public'):
            raise ValueError('reference and query must be formal Solver inputs')
        path = package_path(package, resource['package_path'])
        split_role = key.removesuffix('_resource')
        if split_role in {'reference', 'query'} and re.search(r'\b' + split_role + r'\.h5ad\b', fixed_text, re.I):
            if path.name.casefold() != split_role + '.h5ad':
                raise ValueError('bind the original ' + split_role + '.h5ad Solver input')
        if not path.is_file():
            # A deferred byte-identical source can be verified here from the host's frozen inventory.
            match = next((n for n, a in originals.items() if a['sha256'] == resource.get('sha256')), None)
            if not match:
                if rid in deferred and valid_digest(resource.get('sha256')):
                    return None
                raise ValueError('formal split needs materialized or independently checksum-bound prepared data: ' + rid)
            path = original(match)
        elif valid_digest(resource.get('sha256')) and digest(path) != resource['sha256']:
            raise ValueError('materialized formal split checksum differs from the pinned preparation: ' + rid)
        field = column or ('obs_names' if path.suffix.lower() == '.h5ad' else resource.get('content', {}).get('id_column'))
        obs_group = re.search(r"""\.obs\[['"]([\w.-]+)['"]\][^\n]{0,35}(?:study|batch)""", fixed_text, re.I)
        if split_role in {'reference', 'query'} and path.suffix.lower() == '.h5ad' and obs_group:
            observations = _members(path, field, obs_group[1])
            formal_groups.update(observations)
            return set(observations)
        return set(_members(path, field))

    if proof.get('reference_resource') == proof.get('query_resource'):
        raise ValueError('reference and query must be distinct formal Solver inputs')
    score_columns = {binding['column'] for a in protocol.get('required_artifacts', [])
                     if isinstance(binding := a.get('id_set'), dict) and binding.get('resource') == proof.get('scoring_resource')}
    if len(score_columns) != 1:
        raise ValueError('bind scoring_resource to the evaluator required artifact ID set')
    splits = {'reference_resource': formal('reference_resource'), 'query_resource': formal('query_resource'),
              'scoring_resource': formal('scoring_resource', score_columns.pop())}
    sources = proof.get('source_membership')
    sources = sources if isinstance(sources, list) else [sources]
    members = {}
    split_roles = {}
    bound = {key: set() for key, ids in splits.items() if ids is None}
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError('provide original group membership and the ID and group columns')
        field = source.get('group_column')
        if not isinstance(field, str) or not field.strip() or field == source.get('id_column'):
            raise ValueError('identify the original study or batch annotation column')
        path = recorded_resource(source) if 'resource_id' in source else original(source.get('snapshot_path'))
        rows = _members(path, source.get('id_column'), field)
        if all(key.casefold() == group for key, group in rows.items()):
            raise ValueError('individual observation IDs cannot serve as held-out groups')
        if rows.keys() & members.keys():
            raise ValueError('ambiguous repeated observation IDs across original membership sources')
        members.update(rows)
        if 'split_column' in source:
            split_roles.update(_members(path, source.get('id_column'), source['split_column']))
        for key in bound:
            field = source.get('binding_columns', {}).get(key)
            if field:
                checksum = resources[proof[key]]['sha256']
                identities = _members(path, source.get('id_column'), field)
                bound[key].update(i for i, candidate in identities.items() if candidate == checksum)
    if split_roles:
        if split_roles.keys() != members.keys():
            raise ValueError('declare the published split column on each cited membership source')
        published_reference = {i for i, role in split_roles.items() if role in {'train', 'training', 'reference'}}
        published_query = {i for i, role in split_roles.items() if role in {'test', 'testing', 'query', 'evaluation'}}
        for key, ids in bound.items():
            expected = published_reference if key == 'reference_resource' else published_query
            if ids != expected or not expected:
                raise ValueError('frozen split manifest must bind all planned IDs to the prepared SHA-256 for ' + key)
            splits[key] = ids
        if splits['reference_resource'] != published_reference or splits['query_resource'] != published_query:
            raise ValueError('formal Solver splits differ from the published reference/query assignments')
    elif bound:
        raise ValueError('deferred study split needs published split_column and per-resource SHA-256 binding_columns')
    reference, query = splits['reference_resource'], splits['query_resource']
    if reference & query or splits['scoring_resource'] != query:
        raise ValueError('reference and query IDs must be disjoint and the scored IDs must exactly equal query IDs')
    if not split_roles and reference | query != members.keys():
        raise ValueError('the two formal splits must cover exactly the cited original observation IDs')
    if {members[i] for i in query} & {members[i] for i in reference}:
        raise ValueError('held-out study or batch appears on both sides of the formal split')
    if any(members[i] != label for i, label in formal_groups.items()):
        raise ValueError('formal H5AD study/batch annotations disagree with the original observations')


def alignment_errors(package: Path, spec: dict, fixed_instruction: Path, *, construction_only=False) -> list[str]:
    """Read-only acceptance check. The caller supplies the independently frozen source."""
    package = Path(package)
    defer_formal = construction_only and spec.get('scope_contract_version') == 3
    errors = []
    try:
        expected = _template(fixed_instruction, scope_v3=spec.get('scope_contract_version') == 3)
        path = package_path(package, RECEIPT)
        actual = json.loads(path.read_text())
        if actual.get('schema_version') != 1 or actual.get('source_sha256') != expected['source_sha256']:
            return ['fixed-task receipt version or frozen instruction digest differs']
        rows = actual.get('clauses')
        if not isinstance(rows, list) or len(rows) != len(expected['clauses']):
            return ['fixed-task receipt must cover every source paragraph in order']
        layout = package_layout(spec)
        protocol_path = spec['verification']['score_evidence']['evaluation_protocol']['config_path']
        protocol = json.loads(package_path(package, protocol_path).read_text())
        if expected.get('deliverables'):
            from solver_output_audit import deliverable_errors
            items = [{k: row[k] for k in ('source_line', 'quote')} for row in expected['deliverables']]
            errors.extend(deliverable_errors(items, actual.get('deliverables'),
                package_path(package, layout['instruction_path']).read_text(), spec.get('submission', {}), protocol))
        adapter = str(PurePosixPath(protocol_path).parent / protocol['adapter'])
        scoring_resources = set(protocol.get('resource_bindings', {}).values())
        for artifact in protocol.get('required_artifacts', []):
            if isinstance(artifact.get('id_set'), dict):
                scoring_resources.add(artifact['id_set'].get('resource'))
        resources = {s['id']: s for s in spec.get('data', {}).get('sources', [])
                     if s.get('role') != 'fixture' and s.get('visibility') != 'build_only'}
        deferred = {s['id'] for s in resource_delivery(package, spec)[1]}
        roles = set()
        checked_quotes = {}
        checked_holdouts = set()
        fixed_text = '\n'.join(row['quote'] for row in expected['clauses'])
        bind_split_table = (spec.get('scope_contract_version') != 3 or protocol.get('held_out_splits')
                            or any('held_out_groups' in clause for clause in expected['clauses']))
        for source, row in zip(expected['clauses'], rows):
            prefix = f"fixed instruction lines {source['start_line']}-{source['end_line']}"
            try:
                if not isinstance(row, dict) or any(row.get(k) != source[k] for k in ('start_line', 'end_line', 'quote')):
                    raise ValueError('line numbers and quote must identify the original paragraph')
                if row.get('status') != 'aligned' or not isinstance(row.get('rationale'), str) or not row['rationale'].strip():
                    raise ValueError('unresolved benchmark requirement; explain the actual scientific correspondence')
                ids = row.get('resource_ids')
                if not isinstance(ids, list) or not all(isinstance(v, str) and v in resources for v in ids):
                    raise ValueError('resource_ids must identify declared formal resources')
                linked = {i: resources[i] for i in ids}
                for key, resource in linked.items():
                    if key not in deferred and not has_bytes(package, resource.get('package_path') or resource.get('path')):
                        raise ValueError(f'formal resource {key} has no bytes or verified preparation binding')
                citations = row.get('evidence')
                if not isinstance(citations, list) or not citations:
                    raise ValueError('cite the implementation with evidence entries {role,path,quote}')
                row_roles = set()
                for item in citations:
                    role, name, quote = item['role'], item['path'], item['quote']
                    if role == 'instruction':
                        allowed = name == layout['instruction_path']
                    elif role == 'evaluation':
                        allowed = name in {adapter, protocol_path}
                    elif role == 'resource':
                        allowed = bool(linked) and (name == 'execution.json' or any(_inside_resource(name, s) for s in linked.values()))
                        if defer_formal and name != 'execution.json' and any(
                                s.get('role') in {'input', 'reference'} and _inside_resource(name, s) for s in linked.values()):
                            raise ValueError('cite the linked declaration in execution.json for construction review of formal resources')
                    else:
                        allowed = False
                    if not allowed or not isinstance(quote, str) or not quote.strip():
                        raise ValueError(f'invalid {role} evidence path or empty quote: {name}')
                    if (name, quote) not in checked_quotes:
                        checked_quotes[name, quote] = _contains_quote(package_path(package, name), quote)
                    if not checked_quotes[name, quote]:
                        raise ValueError(f'cited quote is absent from {name}')
                    if name == 'execution.json' and not any(
                        ' '.join(quote.split()) in ' '.join(json.dumps(s, indent=2, ensure_ascii=False).split())
                        for s in linked.values()
                    ):
                        raise ValueError('execution.json quote must belong to a cited resource declaration')
                    row_roles.add(role)
                roles.update(row_roles)
                required = _explicit_panel(source['quote'])
                if required:
                    if defer_formal and not (row.get('evaluation_groups') or {}).get('protocol_units'):
                        raise ValueError('bind evaluation_groups.protocol_units to the evaluator units for the fixed panel')
                    _check_groups(package, row.get('evaluation_groups'), required, linked, scoring_resources, protocol,
                                  _split_table(source['quote']) if bind_split_table else {}, resources)
                if source.get('grouped_evaluation') and not {'resource', 'evaluation'} <= row_roles:
                    raise ValueError('cite the resources and evaluator that implement the published group split')
                if 'held_out_groups' in source or (source.get('grouped_evaluation') and protocol.get('held_out_splits')):
                    proof = row.get('held_out_groups')
                    key = json.dumps(proof, sort_keys=True)
                    if key not in checked_holdouts:
                        _held_out_groups(package, spec, Path(fixed_instruction).parent, proof, resources, protocol, fixed_text,
                                         deferred, construction_only=defer_formal)
                        checked_holdouts.add(key)
                if re.search(r'\b(?:primary metric|evaluated using|final score|overall score)\b',
                             source['quote'], re.I) and 'evaluation' not in row_roles:
                    raise ValueError('cite the formal evaluator for the metric or scoring requirement')
            except (OSError, KeyError, ValueError, TypeError, AttributeError, IndexError) as exc:
                errors.append(f'{prefix}: {exc}')
        needed = {'instruction', 'evaluation'} | ({'resource'} if resources else set())
        if needed - roles:
            errors.append('fixed-task receipt lacks verified evidence for ' + ', '.join(sorted(needed - roles)))
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError) as exc:
        errors.append(f'fixed-task receipt {RECEIPT}: {exc}')
    return errors
