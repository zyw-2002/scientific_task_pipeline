"""Executable declared-structure checks. No metric adapter or hidden score feedback."""
import copy
import importlib.util
import json
from pathlib import Path, PurePosixPath
import tempfile
import zipfile


def check(submission, config_path, *, public=False):
    checks = []
    def result(status, reason=None):
        return {'status': status, 'checks': checks, 'reason': reason,
                'scope': 'public_structure' if public else 'formal_structure',
                'scientific_validity': 'not_assessed'}
    try:
        config = json.loads(config_path.read_text())
        spec = importlib.util.spec_from_file_location('submission_protocol_checks', config_path.parent / 'evaluation_protocol.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        artifacts = copy.deepcopy(config.get('required_artifacts', []))
        errors = module.declaration_errors(artifacts) if 'required_artifacts' in config else []
        if errors:
            return result('error', '; '.join(errors))
        if public:
            for item in artifacts:
                if item.pop('id_set', None):
                    checks.append({'id': item['id'] + '.reference_ids', 'status': 'deferred',
                                   'reason': 'Private reference comparison is reserved for 04.'})
                item.pop('_expected_ids', None)
        elif any(item.get('id_set') for item in artifacts):
            # Resolve reference IDs only in the evaluator. Acquisition failures are
            # checker errors, never malformed-submission or cheating findings.
            from pipeline.stage2_construction.contracts.resource_contract import resolve
            resolved, _ = resolve(config, config_path, mode='runtime')
            artifacts = resolved['required_artifacts']
        if not submission.is_file() or submission.is_symlink():
            return result('fail', 'Submission must be an existing regular file')
        checks.append({'id': 'submission_file', 'status': 'pass'})
        with tempfile.TemporaryDirectory(prefix='submission-check-') as directory:
            root = Path(directory)
            archive = zipfile.is_zipfile(submission)
            if archive:
                with zipfile.ZipFile(submission) as bundle:
                    names = bundle.namelist()
                    if len(names) != len(set(names)):
                        return result('fail', 'Duplicate archive members')
                    for info in bundle.infolist():
                        name = PurePosixPath(info.filename)
                        if name.is_absolute() or '..' in name.parts or '\\' in info.filename or (info.external_attr >> 16) & 0o170000 == 0o120000:
                            return result('fail', 'Unsafe archive member')
                    bundle.extractall(root)
                checks.append({'id': 'archive_safety', 'status': 'pass'})
            else:
                checks.append({'id': 'archive_safety', 'status': 'not_applicable'})
            def locate(name):
                if name == '{submission}':
                    return submission
                if not archive:
                    raise ValueError('Required archive member missing')
                path = root / name
                if not path.is_file():
                    raise ValueError('Required member missing: ' + name)
                return path
            names = [p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()] if archive else ['{submission}']
            try:
                checks.extend(module.validate_artifacts(artifacts, locate, names) if artifacts else [])
            except (ValueError, UnicodeError) as exc:
                return result('fail', str(exc))
        if not artifacts:
            checks.append({'id': 'declared_artifacts', 'status': 'not_applicable'})
        return result('pass')
    except (zipfile.BadZipFile, UnicodeError) as exc:
        return result('fail', str(exc))
    except Exception as exc:
        return result('error', str(exc))
