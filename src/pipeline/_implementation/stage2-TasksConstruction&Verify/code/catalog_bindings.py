"""Compare byte-preserving preparation copies with operator-published source catalogs."""
import hashlib
import json
from pathlib import Path

from contract import package_path


def catalog_copy_errors(spec, bundle):
    copies = {}
    for source in spec.get('data', {}).get('sources', []):
        if not isinstance(source, dict):
            continue
        prep = source.get('prepare')
        cmd = prep.get('command') if isinstance(prep, dict) else None
        if (isinstance(cmd, list) and len(cmd) >= 3 and all(isinstance(v, str) for v in cmd)
                and Path(cmd[0]).name == 'cp' and cmd[-1] == '{output}'):
            copies.setdefault(cmd[-2], []).append(source)
    if not copies:
        return []
    bundle = Path(bundle)
    manifest = bundle/'materials_manifest.json'
    if not manifest.exists():
        return []
    errors = []
    for artifact in json.loads(manifest.read_text()).get('artifacts', []):
        if artifact.get('kind') != 'construction_resource_catalog' or artifact.get('status') != 'available':
            continue
        path = package_path(bundle, artifact['snapshot_path'])
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != artifact.get('sha256'):
            errors.append('Operator resource catalog differs from the Stage1 digest: ' + path.name)
            continue
        catalog = json.loads(raw)
        if catalog.get('canonical_id') != spec.get('canonical_id'):
            errors.append('Operator resource catalog targets another paper: ' + path.name)
            continue
        for row in catalog.get('resources', catalog.get('files', [])):
            mount = row.get('shared_mount') if isinstance(row, dict) else None
            if not isinstance(mount, dict):
                continue
            for source in copies.get(mount.get('path'), []):
                rid = source.get('id', row.get('file', '?'))
                if source['prepare'].get('shared_mount') != mount:
                    errors.append(f'{rid}: direct copy of {mount["path"]} requires its catalogued prepare.shared_mount {mount["name"]}')
                if row.get('rows') is not None and source.get('content', {}).get('row_count') != row['rows']:
                    errors.append(f'{rid}: direct copy of {row["file"]} has {row["rows"]} formal rows according to the frozen catalog')
                for key in ('role', 'visibility', 'sha256'):
                    if key == 'sha256' and source.get(key) is None:
                        continue
                    if row.get(key) is not None and source.get(key) != row[key]:
                        errors.append(f'{rid}: {key} for the direct copy disagrees with the frozen catalog for {row["file"]}')
    return errors
