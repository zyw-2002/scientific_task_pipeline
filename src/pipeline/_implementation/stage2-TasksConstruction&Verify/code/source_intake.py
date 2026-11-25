#!/usr/bin/env python3
"""Freeze one Stage 1 paper's referenced local materials, without downloading.

The paper record is preserved verbatim as JSON data. The separate manifest maps
its host paths to copied files and records unavailable resources without making
a scientific readiness claim. This is private Builder input, not Solver input.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
from typing import Any


SCHEMA_VERSION = "pipline-source-materials-v1"
SUCCESS_STATUSES = {"downloaded", "materialized_local", "reused_verified"}


class SourceIntakeError(ValueError):
    """An ambiguous or unsafe source selection cannot be snapshotted."""


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SourceIntakeError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _invalid_number(value: str) -> None:
    raise SourceIntakeError(f"Non-finite JSON number: {value}")


def _absolute(path: Path) -> Path:
    # Do not resolve symlinks before checking them.
    return Path(os.path.abspath(path))


def _assert_no_symlinks(path: Path) -> None:
    for item in (path, *path.parents):
        if item.is_symlink():
            raise SourceIntakeError(f"Symlink path is not allowed: {item}")


def _open_regular(path: Path):
    """Open every component with O_NOFOLLOW, including the final regular file."""
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
    finally:
        os.close(directory)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise SourceIntakeError(f"Referenced artifact is not a regular file: {path}")
    return os.fdopen(descriptor, "rb")


def _rows(path: Path, canonical_id: str) -> tuple[list[tuple[int, dict[str, Any]]], str]:
    rows: list[tuple[int, dict[str, Any]]] = []
    digest = hashlib.sha256()
    with _open_regular(path) as handle:
        before = os.fstat(handle.fileno())
        for line_number, raw in enumerate(handle, 1):
            digest.update(raw)
            if not raw.strip():
                continue
            try:
                value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_invalid_number)
            except (ValueError, UnicodeError) as error:
                raise SourceIntakeError(f"{path}:{line_number}: {error}") from error
            if not isinstance(value, dict):
                raise SourceIntakeError(f"{path}:{line_number}: expected a JSON object")
            if value.get("canonical_id") == canonical_id:
                rows.append((line_number, value))
        after = os.fstat(handle.fileno())
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_size, after.st_mtime_ns, after.st_ctime_ns
    ):
        raise SourceIntakeError(f"Source index changed while reading: {path}")
    return rows, digest.hexdigest()


def _candidate(value: Any, locator: str, kind: str, *, expected: dict | None = None,
               unavailable: str | None = None) -> dict:
    expected = expected or {}
    return {"source_path": value, "source_locator": locator, "kind": kind,
            "expected_sha256": expected.get("sha256"),
            "expected_bytes": expected.get("bytes"),
            **{key: expected[key] for key in (
                "data_scope", "dataset_completeness", "data_policy", "sampling_method", "sample_rows",
                "source_exhausted", "download_url", "source_expected_checksum", "repository_commit",
            ) if key in expected},
            "unavailable_reason": unavailable}


def _object(value: Any, field: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SourceIntakeError(f"{field} must be an object")
    return value


def _local_candidates(record: dict, downloads: list[dict]) -> list[dict]:
    found = []
    content = _object(record.get("paper_content"), "paper_content")
    local_artifacts = content.get("local_artifacts") or []
    if not isinstance(local_artifacts, list):
        raise SourceIntakeError("paper_content.local_artifacts must be a list")
    for index, artifact in enumerate(local_artifacts):
        if not isinstance(artifact, dict):
            raise SourceIntakeError("paper_content.local_artifacts must contain objects")
        locator = f"paper_content.local_artifacts[{index}]"
        rejected = "Stage 1 marked the source file invalid" if artifact.get("valid_file") is False else None
        if artifact.get("path"):
            found.append(_candidate(artifact["path"], locator + ".path", artifact.get("kind", "paper"),
                                    expected=artifact, unavailable=rejected))
        if artifact.get("text_path"):
            reason = rejected or ("Stage 1 marked extracted text unavailable"
                                  if artifact.get("text_ready") is False else None)
            # A PDF/HTML hash does not describe its independently extracted text.
            found.append(_candidate(artifact["text_path"], locator + ".text_path", "paper_text",
                                    expected={"sha256": artifact.get("text_sha256"),
                                              "bytes": artifact.get("text_bytes")}, unavailable=reason))
    document = _object(record.get("document"), "document")
    described_paths = {item["source_path"] for item in found if isinstance(item["source_path"], str)}
    for field in ("local_path", "local_paths"):
        values = document.get(field) or []
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            raise SourceIntakeError(f"document.{field} must be a path or list of paths")
        for index, value in enumerate(values):
            if value and not (isinstance(value, str) and value in described_paths):
                found.append(_candidate(value, f"document.{field}[{index}]", "paper"))
    for index, item in enumerate(downloads):
        if item.get("local_path"):
            ready = item.get("status") in SUCCESS_STATUSES
            found.append(_candidate(item["local_path"], f"download_results[{index}].local_path",
                                    item.get("kind", "material"), expected=item,
                                    unavailable=None if ready else f"Download status is {item.get('status')!r}"))
    return found


def _source_path(value: Any, records_parent: Path, cwd: Path, material_roots=()) -> Path:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise SourceIntakeError("Artifact path must be a nonempty local path")
    path = Path(value)
    if ".." in path.parts:
        raise SourceIntakeError("Parent traversal is not allowed in artifact paths")
    # Credentials and agent-private configuration are never paper materials.
    if any(part.startswith(".") and part not in {".", ".."} for part in path.parts):
        raise SourceIntakeError("Hidden/private paths are not source materials")
    roots = (records_parent, cwd, *material_roots)
    choices = [path] if path.is_absolute() else [records_parent / path, cwd / path]
    choices = list(dict.fromkeys(_absolute(choice) for choice in choices))
    existing = []
    for choice in choices:
        if not any(choice.is_relative_to(root) for root in roots):
            raise SourceIntakeError("Artifact path escapes the records directory and invocation workspace")
        _assert_no_symlinks(choice)
        if choice.exists():
            existing.append(choice)
    if len(existing) > 1:
        raise SourceIntakeError("Relative artifact path is ambiguous between records directory and cwd")
    if not existing:
        raise SourceIntakeError("Referenced local artifact is missing")
    if not existing[0].is_file():
        raise SourceIntakeError("Referenced directories are not recursively copied")
    return existing[0]


def _copy_artifact(candidate: dict, root: Path, records_parent: Path, cwd: Path,
                   output: Path, material_roots=()) -> dict:
    result = {key: value for key, value in candidate.items() if key != "unavailable_reason"}
    try:
        if candidate["unavailable_reason"]:
            raise SourceIntakeError(candidate["unavailable_reason"])
        source = _source_path(candidate["source_path"], records_parent, cwd, material_roots)
        if source.is_relative_to(output) or output.is_relative_to(source):
            raise SourceIntakeError("Source and output paths overlap")
        expected = candidate["expected_sha256"]
        if expected is not None:
            if not isinstance(expected, str):
                raise SourceIntakeError("Recorded SHA-256 is not a string")
            expected = expected.removeprefix("sha256:").lower()
            if not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise SourceIntakeError("Recorded SHA-256 is malformed")
        expected_bytes = candidate["expected_bytes"]
        if expected_bytes is not None and (type(expected_bytes) is not int or expected_bytes < 0):
            raise SourceIntakeError("Recorded byte count is invalid")
        digest = hashlib.sha256()
        size = 0
        temporary = root / "files" / ".copying"
        try:
            with _open_regular(source) as original, temporary.open("xb") as copied:
                before = os.fstat(original.fileno())
                for chunk in iter(lambda: original.read(1024 * 1024), b""):
                    copied.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                after = os.fstat(original.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns
            ):
                raise SourceIntakeError("Artifact changed while being copied")
            sha256 = digest.hexdigest()
            if expected is not None and expected != sha256:
                raise SourceIntakeError("Recorded SHA-256 does not match actual file bytes")
            if expected_bytes is not None and expected_bytes != size:
                raise SourceIntakeError("Recorded byte count does not match actual file bytes")
            name = re.sub(r"[^A-Za-z0-9._-]", "_", source.name)[:100] or "material"
            relative = Path("files") / f"{sha256}-{name}"
            target = root / relative
            if not target.exists():
                temporary.rename(target)
                target.chmod(0o444)
            result.update(status="available", resolved_source_path=str(source),
                          snapshot_path=relative.as_posix(), sha256=sha256, bytes=size)
        finally:
            temporary.unlink(missing_ok=True)
    except (OSError, SourceIntakeError) as error:
        result.update(status="unavailable", reason=str(error), snapshot_path=None)
    return result


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    path.chmod(0o444)


def prepare_sources(records: Path, canonical_id: str, output: Path,
                    download_results: Path | None = None, *, material_roots=(),
                    review_paper_materials=False) -> dict:
    """Create a new private snapshot of exactly one Stage 1 canonical record.

    Only explicit regular-file references under records.parent or the invocation
    cwd are eligible; relative paths may be based on either, but ambiguities,
    parent traversal, hidden paths and symlinks are rejected. Missing files and
    unavailable downloads remain in the manifest for Builder investigation.
    """
    records, output, cwd = _absolute(Path(records)), _absolute(Path(output)), _absolute(Path.cwd())
    roots = [_absolute(Path(root)) for root in material_roots]
    for root in roots:
        _assert_no_symlinks(root)
        if not root.is_dir():
            raise SourceIntakeError(f'Material root must be an existing directory: {root}')
    _assert_no_symlinks(records)
    _assert_no_symlinks(output)
    if not isinstance(canonical_id, str) or not canonical_id.strip():
        raise SourceIntakeError("canonical_id is required")
    if output.exists():
        raise SourceIntakeError(f"Refusing to overwrite existing output: {output}")
    selected, records_sha256 = _rows(records, canonical_id)
    if len(selected) != 1:
        raise SourceIntakeError(f"Expected exactly one record for {canonical_id!r}; found {len(selected)}")
    line_number, record = selected[0]
    downloads: list[dict] = []
    download_index = None
    if download_results is not None:
        download_results = _absolute(Path(download_results))
        _assert_no_symlinks(download_results)
        download_rows, download_hash = _rows(download_results, canonical_id)
        downloads = [row for _, row in download_rows]
        download_index = {"path": str(download_results), "sha256": download_hash}
        roots.append(download_results.parent)
    provenance = None
    if review_paper_materials:
        stage1 = Path(__file__).resolve().parents[2] / 'stage1-datacard&downlond/code'
        sys.path.insert(0, str(stage1))
        try:
            from paper_material_provenance import review_materials
            record, downloads, decisions = review_materials(record, downloads, roots=[records.parent,cwd,*roots])
        finally:
            sys.path.remove(str(stage1))
        primary = record['paper_content']['local_artifacts']
        provenance = {'primary_paper_files':len(primary),
                      'status':'primary_article_verified' if primary else 'published_article_still_required',
                      'retained_local_resources':len(downloads),
                      'held_candidates':sum(item['decision']=='held' for item in decisions)}
    candidates = _local_candidates(record, downloads)
    output.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_symlinks(output.parent)
    scratch = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    try:
        (scratch / "files").mkdir()
        artifacts = [_copy_artifact(item, scratch, records.parent, cwd, output, roots) for item in candidates]
        resources = []
        source_resources = _object(record.get("resources"), "resources")
        source_statuses = _object(record.get("resource_status"), "resource_status")
        for kind, field in (("code", "code_urls"), ("data", "data_urls"), ("supplementary", "supplementary_urls")):
            urls = source_resources.get(field) or []
            if not isinstance(urls, list):
                raise SourceIntakeError(f"resources.{field} must be a list")
            matching = [item for item in downloads if item.get("kind") == kind]
            resources.append({"kind": kind, "urls": urls,
                              "stage1_status": source_statuses.get(kind), "download_results": matching,
                              "note": "No network checks performed. A URL, Stage 1 status, or download success alone does not establish scientific identity or Solver accessibility."})
        manifest = {
            "schema_version": SCHEMA_VERSION, "canonical_id": canonical_id,
            "private_builder_input": True,
            "record_index": {"path": str(records), "line": line_number, "sha256": records_sha256},
            "download_index": download_index,
            "paper_record_path": "paper_record.json", "artifacts": artifacts,
            "resources": resources, "download_results": downloads,
            "summary": {"referenced_files": len(artifacts),
                        "available_files": sum(item["status"] == "available" for item in artifacts),
                        "unavailable_files": sum(item["status"] == "unavailable" for item in artifacts),
                        "download_records": len(downloads)},
            "readiness": "not_assessed",
            "unresolved_policy": "Unavailable local materials and unresolved URLs remain available for active Builder acquisition; they do not imply scientific rejection or run_ready.",
        }
        if provenance is not None:
            manifest['paper_material_provenance'] = provenance
        _write_json(scratch / "paper_record.json", record)
        _write_json(scratch / "materials_manifest.json", manifest)
        # Payloads are read-only. Directories remain owner-writable so a failed
        # private staging tree can be cleaned without privilege changes.
        if output.exists():
            raise SourceIntakeError(f"Output appeared during preparation: {output}")
        scratch.rename(output)
        return manifest
    except BaseException:
        shutil.rmtree(scratch)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--canonical-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--download-results", type=Path)
    parser.add_argument("--review-paper-materials", action="store_true",
                        help="Check primary paper identity and resource ownership before freezing construction inputs")
    parser.add_argument("--material-root", action="append", type=Path, default=[],
                        help="Additional explicit read-only material source root; never expose credentials")
    args = parser.parse_args()
    try:
        manifest = prepare_sources(args.records, args.canonical_id, args.output, args.download_results,
                                   material_roots=args.material_root, review_paper_materials=args.review_paper_materials)
    except (OSError, SourceIntakeError) as error:
        parser.exit(2, f"source intake failed: {error}\n")
    print(json.dumps({"canonical_id": manifest["canonical_id"], "output": str(args.output.resolve()),
                      **manifest["summary"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
