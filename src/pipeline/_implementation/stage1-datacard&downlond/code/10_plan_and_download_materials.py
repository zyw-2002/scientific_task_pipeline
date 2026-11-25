#!/usr/bin/env python3
"""Plan and materialize papers, code, supplementary files, and datasets."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import mimetypes
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
from types import SimpleNamespace
from typing import Any, Iterable
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline_runtime import setting




USER_AGENT = "NatureBenchMaterialDownloader/2.0"
SUCCESS_STATUSES = {"downloaded", "materialized_local"}
CHUNK_BYTES = setting('transfer_chunk_bytes')
NETWORK_OPENER = None
METADATA_CLIENT = None
PAPER_AVAILABLE_STATUSES = {
    "local_text_ready", "local_fulltext_available", "downloadable_pdf",
}
RESOURCE_FIELDS = {
    "code": "code_urls",
    "supplementary": "supplementary_urls",
    "data": "data_urls",
}


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def unique(values: Iterable[Any]) -> list[Any]:
    output: list[Any] = []
    seen: set[str] = set()
    for value in values:
        if value in (None, "", []):
            continue
        marker = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if marker not in seen:
            seen.add(marker)
            output.append(value)
    return output


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number} is not a JSON object")
            rows.append(value)
    return rows


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, "".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        for row in rows
    ))


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".manifest-", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def status_value(record: dict[str, Any], resource_type: str) -> str | None:
    value = (record.get("resource_status") or {}).get(resource_type)
    if isinstance(value, dict):
        return clean(value.get("status")) or None
    return clean(value) or None


def eligible_record(record: dict[str, Any]) -> bool:
    paper = record.get("paper_content") or {}
    paper_available = (
        bool(paper.get("paper_available"))
        and clean(paper.get("status")) in PAPER_AVAILABLE_STATUSES
    )
    code_urls = (record.get("resources") or {}).get("code_urls") or []
    return paper_available and status_value(record, "code") == "found" and bool(code_urls)


def github_repository(url: str) -> tuple[str, str, str | None] | None:
    parsed = urllib.parse.urlsplit(url)
    host = parsed.netloc.casefold().removeprefix("www.")
    if host != "github.com":
        return None
    parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    owner = parts[0]
    repo = re.sub(r"\.git$", "", parts[1], flags=re.IGNORECASE)
    subpath = None
    if len(parts) >= 5 and parts[2] in {"tree", "blob"}:
        subpath = "/".join(parts[4:]) or None
    return owner, repo, subpath


def resource_id(canonical_id: str, kind: str, url: str) -> str:
    digest = hashlib.sha256(f"{canonical_id}|{kind}|{url}".encode()).hexdigest()[:20]
    return f"material-{digest}"


def local_paper_item(record: dict[str, Any]) -> dict[str, Any] | None:
    artifacts = [
        deepcopy(item)
        for item in (record.get("paper_content") or {}).get("local_artifacts") or []
        if item.get("valid_file") and clean(item.get("path"))
    ]
    if not artifacts:
        return None
    canonical_id = clean(record.get("canonical_id"))
    return {
        "schema_version": "material-download-plan-item-v1",
        "resource_id": resource_id(canonical_id, "paper", "local-artifacts"),
        "canonical_id": canonical_id,
        "title": record.get("title"),
        "kind": "paper",
        "required_for_gate": True,
        "action": "copy_local_artifacts",
        "resolver": "local",
        "input_url": None,
        "local_artifacts": artifacts,
    }


def url_plan_item(
    record: dict[str, Any], kind: str, url: str, *, required: bool
) -> dict[str, Any]:
    canonical_id = clean(record.get("canonical_id"))
    resolver = "direct"
    action = "download"
    extra: dict[str, Any] = {}
    github = github_repository(url)
    lower = urllib.parse.unquote(url).casefold()
    if github:
        owner, repo, subpath = github
        resolver = "github_archive"
        parts = [part for part in urllib.parse.urlsplit(url).path.split("/") if part]
        requested_ref = urllib.parse.unquote(parts[3]) if len(parts) >= 4 and parts[2] in {"tree", "blob"} else "HEAD"
        extra.update({
            "requested_ref": requested_ref,
            "repository": f"{owner}/{repo}",
            "repository_root": f"https://github.com/{owner}/{repo}",
            "subpath": subpath,
        })
    elif urllib.parse.urlsplit(url).hostname in {"doi.org", "dx.doi.org"} and re.fullmatch(
        r"/10\.5281/zenodo\.\d+/?", urllib.parse.urlsplit(url).path, re.IGNORECASE
    ):
        resolver = "zenodo_record"
        extra["zenodo_record_id"] = urllib.parse.urlsplit(url).path.rstrip("/").rsplit(".", 1)[-1]
    elif urllib.parse.urlsplit(url).hostname in {"zenodo.org", "www.zenodo.org"} and re.fullmatch(
        r"/records?/\d+/?", urllib.parse.urlsplit(url).path
    ):
        resolver = "zenodo_record"
    elif ((urllib.parse.urlsplit(url).hostname == "figshare.com" or
           (urllib.parse.urlsplit(url).hostname or "").endswith(".figshare.com")) and "/articles/" in lower) or re.search(
        r"^https?://(?:dx\.)?doi\.org/10\.\d+/(?:m9\.)?figshare(?:\.plus)?\.\d+", lower
    ):
        resolver = "figshare_article"
    elif "europepmc/webservices/rest/" in lower and lower.endswith("/supplementaryfiles"):
        resolver = "europepmc_supplementary"
    elif "rcsb.org/structure/" in lower:
        resolver = "pdb_structure"
    elif any(marker in lower for marker in (
        "ncbi.nlm.nih.gov/geo/", "ncbi.nlm.nih.gov/sra/",
        "ncbi.nlm.nih.gov/bioproject/", "ebi.ac.uk/ena/",
    )):
        resolver = "specialized_dataset"
        action = "defer_specialized_download"
    return {
        "schema_version": "material-download-plan-item-v1",
        "resource_id": resource_id(canonical_id, kind, url),
        "canonical_id": canonical_id,
        "title": record.get("title"),
        "kind": kind,
        "required_for_gate": required,
        "action": action,
        "resolver": resolver,
        "input_url": url,
        **extra,
    }


def build_plan(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    selected = [record for record in records if eligible_record(record)]
    ids = [clean(record.get("canonical_id")) for record in selected]
    if len(ids) != len(set(ids)):
        raise ValueError("eligible records contain duplicate canonical_id values")
    if any(not value or value != safe_name(value, "") for value in ids):
        raise ValueError("canonical_id must be a nonempty safe path component")
    plan: list[dict[str, Any]] = []
    for record in selected:
        paper_item = local_paper_item(record)
        if paper_item:
            plan.append(paper_item)
        else:
            document = record.get("document") or {}
            pdf_urls = unique([document.get("pdf_url"), *(document.get("pdf_urls") or [])])
            if pdf_urls:
                plan.append(url_plan_item(record, "paper", pdf_urls[0], required=True))
            else:
                plan.append({"canonical_id": record["canonical_id"], "kind": "paper",
                             "resource_id": resource_id(record["canonical_id"], "paper", "missing"),
                             "required_for_gate": True, "action": "missing_paper_source"})

        resources = record.get("resources") or {}
        code_items: list[dict[str, Any]] = []
        for url in unique(resources.get("code_urls") or []):
            item = url_plan_item(record, "code", url, required=True)
            code_items.append(item)
            plan.append(item)
        code_roots = {
            item.get("repository_root"): item["resource_id"]
            for item in code_items if item.get("repository_root")
        }

        for kind in ("supplementary", "data"):
            field = RESOURCE_FIELDS[kind]
            for url in unique(resources.get(field) or []):
                item = url_plan_item(record, kind, url, required=False)
                if kind == "data" and item.get("repository_root") in code_roots:
                    item["action"] = "reuse_code_repository"
                    item["resolver"] = "github_reuse"
                    item["depends_on"] = code_roots[item["repository_root"]]
                plan.append(item)
    return selected, plan


def safe_name(value: str, fallback: str) -> str:
    value = urllib.parse.unquote(value).replace("\\", "_").replace("/", "_")
    value = re.sub(r"[^0-9A-Za-z._()\-]+", "_", value).strip("._")
    return value[:180] or fallback


def open_url(request: urllib.request.Request, timeout: float):
    if NETWORK_OPENER is not None:
        return NETWORK_OPENER.open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)


def configure_network(args: argparse.Namespace):
    path = Path(__file__).resolve().parent / "network_config.py"
    spec = importlib.util.spec_from_file_location("stage1_download_network_config", path)
    if spec is None or spec.loader is None:
        raise ValueError("network configuration module is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    options: dict[str, Any] = {}
    config_path = getattr(args, "network_config", None)
    network_keys = {"enabled", "inherit_env", "proxy_url", "proxy_env_file", "no_proxy"}
    if config_path is not None:
        def pairs(entries):
            result = {}
            for key, value in entries:
                if key in result:
                    raise ValueError("network JSON contains duplicate fields")
                result[key] = value
            return result
        try:
            document = json.loads(Path(config_path).read_text(encoding="utf-8"), object_pairs_hook=pairs,
                                  parse_constant=lambda value: (_ for _ in ()).throw(ValueError("non-finite JSON")))
        except (OSError, ValueError) as error:
            raise ValueError("network config must be a readable, unambiguous JSON object") from error
        if not isinstance(document, dict):
            raise ValueError("network config must be a JSON object")
        if "network" in document:
            if network_keys.intersection(document):
                raise ValueError("network config mixes nested and top-level network options")
            options = document["network"]
        else:
            if set(document) - network_keys:
                raise ValueError("worker config must include a network object; standalone config accepts network options only")
            options = document
        if not isinstance(options, dict):
            raise ValueError("network config network field must be an object")
        options = {key: value for key, value in options.items() if key in network_keys and value is not None}
        for key in ("enabled", "inherit_env"):
            if key in options and type(options[key]) is not bool:
                raise ValueError("network enabled/inherit_env options must be booleans")
        for key in ("proxy_url", "proxy_env_file", "no_proxy"):
            if key in options and not isinstance(options[key], str):
                raise ValueError("network proxy options must be strings")
        if options.get("proxy_env_file"):
            env_path = Path(options["proxy_env_file"])
            if not env_path.is_absolute():
                options["proxy_env_file"] = str(Path(config_path).absolute().parent / env_path)
    explicit_proxy = getattr(args, "proxy_url", None)
    explicit_file = getattr(args, "proxy_env_file", None)
    if explicit_proxy is not None and explicit_file is None:
        options.pop("proxy_env_file", None)
    if explicit_file is not None and explicit_proxy is None:
        options.pop("proxy_url", None)
    if explicit_proxy is not None or explicit_file is not None:
        options["enabled"] = True
    for key in ("proxy_url", "proxy_env_file", "no_proxy"):
        value = getattr(args, key, None)
        if value is not None:
            options[key] = str(value)
    if getattr(args, "direct", False):
        options["enabled"] = False
    network = module.resolve_network(options)
    global NETWORK_OPENER, METADATA_CLIENT
    NETWORK_OPENER = network.opener()
    METADATA_CLIENT = None
    return network


def load_repository_module():
    name = "stage1_repository_http"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("repository_http.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def configure_metadata(args):
    global METADATA_CLIENT
    module = load_repository_module()
    METADATA_CLIENT = module.MetadataClient(
        open_url, write_json,
        cache_dir=getattr(args, "metadata_cache_dir", None) or args.output_dir / "repository_cache",
        token=module.read_github_token(getattr(args, "github_env_file", None)),
        interval=getattr(args, "github_min_interval", None),
        ttl=0 if getattr(args, "refresh", False) else setting('metadata_cache_ttl_sec'),
        user_agent=USER_AGENT,
    )
    return METADATA_CLIENT


def provider_failure(error, fallback="expansion_error"):
    if isinstance(error, load_repository_module().ProviderError):
        return dict(error.details)
    return {"status": fallback, "error": load_repository_module().redact(error)}


def request_json(url: str, timeout: float, retries: int) -> dict[str, Any]:
    module = load_repository_module()
    client = METADATA_CLIENT or module.MetadataClient(open_url, write_json, token=module.read_github_token())
    return client.get(url, timeout, retries)


def github_commit(repository: str, requested_ref: str, timeout: float, retries: int) -> str:
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repository):
        raise load_repository_module().ProviderError(
            "needs_review_url", "GitHub owner/repository contains invalid characters; verify the original paper URL", retryable=False
        )
    commit = request_json(
        f"https://api.github.com/repos/{repository}/commits/" + urllib.parse.quote(requested_ref or "HEAD", safe=""),
        timeout, retries,
    ).get("sha")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        raise ValueError("GitHub did not return an immutable commit SHA")
    return commit


def public_acs_supplement(item, timeout, retries):
    """Use ACS's public Figshare mirror only when DOI and attachment name agree."""
    original = urllib.parse.urlsplit(clean(item.get("input_url")))
    match = re.fullmatch(r"/doi/suppl/(10\.\d+/[^/]+)/suppl_file/([^/]+)", urllib.parse.unquote(original.path))
    if item.get("kind") != "supplementary" or original.hostname != "pubs.acs.org" or not match:
        return None
    doi, filename = match.groups()
    module = load_repository_module()
    client = METADATA_CLIENT or module.MetadataClient(open_url, write_json)
    search = "https://api.figshare.com/v2/articles?" + urllib.parse.urlencode({"resource_doi": doi, "page_size": 10})
    articles = client.get(search, timeout, retries, allow_list=True)
    if not isinstance(articles, list):
        raise module.ProviderError("metadata_error", "Figshare public article lookup did not return a list", retryable=False)
    for article in articles[:10]:
        article_id = article.get("id") if isinstance(article, dict) else None
        if not str(article_id).isdigit():
            continue
        metadata_url = f"https://api.figshare.com/v2/articles/{article_id}"
        metadata = request_json(metadata_url, timeout, retries)
        if (clean(metadata.get("resource_doi")).lower() != doi.lower()
            or str(metadata.get("id")) != str(article_id)
            or urllib.parse.urlsplit(clean(metadata.get("url_public_html"))).hostname != "acs.figshare.com"):
            continue
        for file_item in metadata.get("files") or []:
            if file_item.get("name") != filename or not file_item.get("download_url"):
                continue
            download = urllib.parse.urlsplit(file_item["download_url"])
            if (download.scheme != "https" or download.username or download.password
                or not (download.hostname == "figshare.com" or (download.hostname or "").endswith(".figshare.com"))):
                continue
            return {
                "url": file_item["download_url"], "name": filename, "expected_bytes": file_item.get("size"),
                "expected_checksum": "md5:" + file_item["computed_md5"] if file_item.get("computed_md5") else None,
                "public_mirror": {"provider": "acs.figshare.com", "metadata_url": metadata_url,
                                  "article_id": article_id, "version": metadata.get("version"),
                                  "matched_paper_doi": doi, "matched_filename": filename,
                                  "original_url": item["input_url"], "original_http_status": 403},
            }
    return None


def expand_targets(item: dict[str, Any], timeout: float, retries: int) -> list[dict[str, Any]]:
    url = clean(item.get("input_url"))
    resolver = item.get("resolver")
    if resolver == "github_archive":
        owner, repo = str(item["repository"]).split("/", 1)
        requested_ref = clean(item.get("requested_ref")) or "HEAD"
        commit = github_commit(item["repository"], requested_ref, timeout, retries)
        return [{
            "url": f"https://github.com/{owner}/{repo}/archive/{commit}.zip",
            "name": f"{repo}-{commit}.zip", "repository_commit": commit,
            "requested_ref": requested_ref,
        }]
    if resolver == "zenodo_record":
        match = re.search(r"/(?:records?|record)/(\d+)", urllib.parse.urlsplit(url).path)
        record_id = item.get("zenodo_record_id") or (match.group(1) if match else None)
        if not record_id:
            return []
        payload = request_json(
            f"https://zenodo.org/api/records/{record_id}", timeout, retries
        )
        targets = []
        for file_item in payload.get("files") or []:
            links = file_item.get("links") or {}
            download_url = clean(links.get("content") or links.get("self") or links.get("download"))
            if download_url:
                targets.append({
                    "url": download_url,
                    "name": clean(file_item.get("key")),
                    "expected_bytes": file_item.get("size"),
                    "expected_checksum": file_item.get("checksum"),
                })
        return targets
    if resolver == "figshare_article":
        article_id, version = load_repository_module().figshare_identity(url)
        endpoint = f"https://api.figshare.com/v2/articles/{article_id}"
        if version:
            endpoint += f"/versions/{version}"
        payload = request_json(endpoint, timeout, retries)
        if payload.get("id") is not None and str(payload["id"]) != article_id:
            raise ValueError("Figshare returned metadata for a different article")
        if version and payload.get("version") is not None and str(payload["version"]) != version:
            raise ValueError("Figshare returned metadata for a different version")
        return [
            {
                "url": clean(file_item.get("download_url")),
                "name": clean(file_item.get("name")),
                "expected_bytes": file_item.get("size"),
                "expected_checksum": ("md5:" + file_item["computed_md5"])
                if file_item.get("computed_md5") else None,
            }
            for file_item in payload.get("files") or []
            if clean(file_item.get("download_url"))
        ]
    if resolver == "pdb_structure":
        pdb_id = urllib.parse.urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1].upper()
        return [{"url": f"https://files.rcsb.org/download/{pdb_id}.cif", "name": f"{pdb_id}.cif"}]
    return [{"url": url, "name": ""}]


class TotalBudget:
    """A per-invocation I/O budget; failed transfers and retries consume bytes too."""

    def __init__(self, maximum: int) -> None:
        if maximum < 0:
            raise ValueError("budget must not be negative")
        self.maximum = maximum
        self.used = 0
        self.lock = threading.Lock()

    def commit(self, amount: int) -> bool:
        with self.lock:
            if amount < 0 or self.used + amount > self.maximum:
                return False
            self.used += amount
            return True

    def reserve(self, amount: int) -> int:
        with self.lock:
            reserved = min(amount, self.maximum - self.used)
            self.used += reserved
            return reserved

    def refund_unread(self, amount: int) -> None:
        with self.lock:
            self.used -= amount


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


class MaterialError(ValueError):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


def result_state(result: dict[str, Any]) -> dict[str, Any]:
    valid = result.get("status") in SUCCESS_STATUSES
    return {
        **result, "schema_version": "material-download-result-v2",
        "source_access": "local" if result.get("source_path") else (
            "http_response_received" if result.get("http_status") else "unverified"
        ),
        "materialization_status": "validated_local" if valid else "not_materialized",
        "runtime_verified": False, "run_ready": False,
    }


def material_directory(item: dict[str, Any], output_dir: Path) -> Path:
    parts = [str(item[field]) for field in ("canonical_id", "kind", "resource_id")]
    if any(not part or part != safe_name(part, "") for part in parts):
        raise ValueError("material identifiers must be safe path components")
    path = output_dir / "materials"
    for part in parts:
        path = path / part
    # Refuse redirection through symlinks, including existing output parents.
    for parent in (path, *path.absolute().parents):
        if parent.is_symlink():
            raise ValueError("symlink is not allowed in material destination")
    path.mkdir(parents=True, exist_ok=True)
    return path


def publish_immutable(temporary: Path, destination: Path, name: str, digest: str) -> Path:
    directory = destination / digest
    directory.mkdir(exist_ok=True)
    if directory.is_symlink():
        raise ValueError("symlink is not allowed in immutable destination")
    target = directory / name
    try:
        os.link(temporary, target)  # No replace: a snapshot is never silently overwritten.
    except FileExistsError:
        if target.is_symlink() or not target.is_file() or sha256_file(target) != digest:
            raise MaterialError("snapshot_conflict", "immutable artifact already exists with different bytes")
    temporary.unlink(missing_ok=True)
    return target


def stream_material(source: Any, handle: Any, budget: TotalBudget,
                    maximum: int, expected: int | None = None) -> tuple[int, str, bytes]:
    digest = hashlib.sha256()
    written = 0
    prefix = bytearray()
    while expected is None or written < expected:
        allowance = min(CHUNK_BYTES, maximum - written + 1)
        if expected is not None:
            allowance = min(allowance, expected - written)
        reserved = budget.reserve(allowance)
        if not reserved:
            raise MaterialError("skipped_total_budget", "total transfer/copy byte budget exhausted")
        try:
            chunk = source.read(reserved)
        except Exception:
            # The failed read may already have consumed bytes on the wire; charge its reservation.
            raise
        budget.refund_unread(reserved - len(chunk))
        if not chunk:
            break
        written += len(chunk)
        if written > maximum:
            raise MaterialError("skipped_file_budget", "file exceeds max-file-mb")
        if len(prefix) < 8192:
            prefix.extend(chunk[:8192 - len(prefix)])
        handle.write(chunk)
        digest.update(chunk)
    if expected is not None and written != expected:
        raise MaterialError("invalid_content", f"truncated response: expected {expected}, received {written} bytes")
    if not written:
        raise MaterialError("invalid_content", "empty material")
    return written, digest.hexdigest(), bytes(prefix)


def validate_download(path: Path, item: dict[str, Any], target: dict[str, Any],
                      media_type: str, prefix: bytes, written: int) -> dict[str, Any]:
    expected = target.get("expected_bytes")
    if expected is not None and written != int(expected):
        raise MaterialError("invalid_content", "download size differs from repository metadata")
    html_prefix = prefix.lstrip(b"\xef\xbb\xbf\x00\t\r\n ").lower()
    if media_type in {"text/html", "application/xhtml+xml"} or any(
        token in html_prefix[:4096] for token in (b"<!doctype html", b"<html", b"<head", b"<body")
    ):
        raise MaterialError("landing_page_not_file", "URL returned an HTML page instead of material")
    if item["kind"] == "paper" and not prefix.startswith(b"%PDF-"):
        raise MaterialError("invalid_content", "paper download is not a PDF")
    if item.get("resolver") == "github_archive" and not prefix.startswith(b"PK"):
        raise MaterialError("invalid_content", "GitHub archive is not a ZIP file")
    checks: dict[str, Any] = {"nonempty": True, "html_rejected": True, "bytes_verified": True}
    checksum = target.get("expected_checksum")
    if checksum:
        algorithm, separator, expected_hash = str(checksum).partition(":")
        algorithm = algorithm.lower()
        if not separator or algorithm not in {"md5", "sha1", "sha256", "sha512"}:
            raise MaterialError("invalid_content", "unsupported/malformed repository checksum")
        digest = hashlib.new(algorithm)
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(CHUNK_BYTES), b""):
                digest.update(chunk)
        if digest.hexdigest().lower() != expected_hash.lower():
            raise MaterialError("checksum_mismatch", "download does not match repository checksum")
        checks["upstream_checksum_verified"] = True
    if item.get("resolver") == "github_archive":
        try:
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                if not entries:
                    raise ValueError("empty archive")
                # Keep archive intact; never extract untrusted paths, symlinks, or zip bombs here.
                checks["archive_entries"] = len(entries)
                checks["archive_preserved"] = True
        except (OSError, ValueError, zipfile.BadZipFile) as error:
            raise MaterialError("invalid_content", f"invalid ZIP archive: {error}") from error
    return checks


def copy_local_artifacts(item: dict[str, Any], output_dir: Path,
                         budget: TotalBudget | None = None,
                         max_file_bytes: int | None = None) -> list[dict[str, Any]]:
    destination = material_directory(item, output_dir)
    if budget is None:
        budget = TotalBudget(int(setting('max_total_gb') * 1024**3))
    if max_file_bytes is None:
        max_file_bytes = setting('max_file_mb') * 1024**2
    sources = []
    for artifact in item.get("local_artifacts") or []:
        sources.append(artifact.get("path"))
        if artifact.get("text_ready") is not False:
            sources.append(artifact.get("text_path"))
    results: list[dict[str, Any]] = []
    for source_value in unique(sources):
        source = Path(str(source_value)).absolute()
        base = {**item, "source_path": str(source)}
        temporary: Path | None = None
        try:
            if any(parent.is_symlink() for parent in (source, *source.parents)) or not source.is_file():
                raise MaterialError("error", "local source must be an existing regular file without symlinks")
            before = source.stat()
            if before.st_size > max_file_bytes:
                raise MaterialError("skipped_file_budget", "local source exceeds max-file-mb")
            source_digest = sha256_file(source)
            existing = destination / source_digest / safe_name(source.name, "paper-file")
            after_hash = source.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after_hash.st_size, after_hash.st_mtime_ns, after_hash.st_ctime_ns
            ):
                raise MaterialError("invalid_content", "local source changed while hashing")
            if valid_cached_row({"status": "materialized_local", "local_path": str(existing.absolute()),
                                 "bytes": before.st_size, "sha256": source_digest}, destination):
                results.append(result_state({
                    **base, "status": "materialized_local", "local_path": str(existing.resolve()),
                    "bytes": before.st_size, "sha256": source_digest,
                    "media_type": mimetypes.guess_type(existing.name)[0], "resumed": True,
                    "revalidated_at": now_utc(), "completed_at": now_utc(),
                }))
                continue
            with source.open("rb") as source_handle, tempfile.NamedTemporaryFile(
                mode="wb", dir=destination, prefix=".local-", suffix=".part", delete=False,
            ) as handle:
                temporary = Path(handle.name)
                written, digest, _ = stream_material(source_handle, handle, budget, max_file_bytes, before.st_size)
            after = source.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns
            ):
                raise MaterialError("invalid_content", "local source changed while copying")
            target = publish_immutable(temporary, destination, safe_name(source.name, "paper-file"), digest)
            results.append(result_state({
                **base, "status": "materialized_local", "local_path": str(target.resolve()),
                "bytes": written, "sha256": digest, "media_type": mimetypes.guess_type(target.name)[0],
                "validation_checks": {"bytes_verified": True, "source_stable": True},
                "completed_at": now_utc(),
            }))
        except (OSError, ValueError) as error:
            results.append(result_state({**base, "status": getattr(error, "status", "error"),
                                        "error": clean(error), "completed_at": now_utc()}))
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
    return results or [result_state({**item, "status": "no_local_artifacts"})]


def download_target(
    item: dict[str, Any], target: dict[str, Any], target_index: int,
    output_dir: Path, timeout: float, retries: int, max_file_bytes: int, budget: TotalBudget,
    *, resume: bool = True,
) -> dict[str, Any]:
    base = {**item, "download_url": target.get("url"), "target_index": target_index,
            "started_at": now_utc()}
    for field in ("repository_commit", "expected_checksum", "expected_bytes", "requested_ref", "public_mirror"):
        if target.get(field) is not None:
            base[field] = target[field]
    expected = target.get("expected_bytes")
    if isinstance(expected, int) and expected > max_file_bytes:
        return result_state({**base, "status": "skipped_file_budget", "expected_bytes": expected})
    if urllib.parse.urlsplit(str(target.get("url"))).scheme not in {"http", "https"}:
        return result_state({**base, "status": "invalid_url", "error": "only HTTP(S) downloads are supported"})
    destination = material_directory(item, output_dir)
    receipt = destination / ("target-" + str(target_index) + "-" + plan_digest(target) + ".json")
    if resume and receipt.is_file() and not receipt.is_symlink():
        try:
            cached = json.loads(receipt.read_text())
            if cached.get("plan_sha256") == plan_digest(item) and valid_cached_row(cached["result"], destination):
                return result_state({**cached["result"], "resumed": True, "revalidated_at": now_utc()})
        except (OSError, ValueError, TypeError, KeyError):
            pass
    for attempt in range(retries + 1):
        temporary: Path | None = None
        try:
            request = urllib.request.Request(str(target["url"]), headers={
                "User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Encoding": "identity",
            })
            with open_url(request, timeout) as response:
                status = response.getcode()
                base.update({"http_status": status, "attempts": attempt + 1})
                if status != 200:
                    raise MaterialError("http_error", f"unexpected HTTP status {status}")
                if response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
                    raise MaterialError("invalid_content", "unexpected encoded HTTP response")
                length_text = response.headers.get("Content-Length")
                length = int(length_text) if length_text and length_text.isdigit() else None
                if length is not None and length > max_file_bytes:
                    raise MaterialError("skipped_file_budget", "Content-Length exceeds max-file-mb")
                final_url = response.geturl()
                base["final_url"] = final_url
                filename = safe_name(clean(target.get("name")) or Path(urllib.parse.urlsplit(final_url).path).name,
                                     f"target-{target_index}.bin")
                media_type = response.headers.get_content_type()
                base["media_type"] = media_type
                with tempfile.NamedTemporaryFile(mode="wb", dir=destination, prefix=".download-",
                                                 suffix=".part", delete=False) as handle:
                    temporary = Path(handle.name)
                    written, digest, prefix = stream_material(response, handle, budget, max_file_bytes, length)
                checks = validate_download(temporary, item, target, media_type, prefix, written)
                target_path = publish_immutable(temporary, destination, filename, digest)
                result = result_state({
                    **base, "status": "downloaded", "local_path": str(target_path.resolve()),
                    "bytes": written, "sha256": digest, "validation_checks": checks,
                    "archive_format": "zip" if checks.get("archive_preserved") else None,
                    "completed_at": now_utc(),
                })
                write_json(receipt, {"plan_sha256": plan_digest(item), "result": result})
                return result
        except MaterialError as error:
            return result_state({**base, "status": error.status, "error": clean(error), "completed_at": now_utc()})
        except urllib.error.HTTPError as error:
            details = load_repository_module().error_details(error, str(target["url"]))
            error.close()
            if details["http_status"] == 403 and urllib.parse.urlsplit(str(target["url"])).hostname == "pubs.acs.org" and item.get("kind") == "supplementary" and not target.get("public_mirror"):
                try:
                    mirror = public_acs_supplement(item, timeout, retries)
                except (OSError, ValueError) as mirror_error:
                    return result_state({**base, **provider_failure(mirror_error),
                                         "original_access_error": details, "completed_at": now_utc()})
                if mirror is not None:
                    return download_target(item, mirror, target_index, output_dir, timeout, retries,
                                           max_file_bytes, budget, resume=resume)
                details["public_mirror_status"] = "no_exact_doi_and_filename_match"
            return result_state({**base, **details, "completed_at": now_utc()})
        except (OSError, ValueError, urllib.error.URLError) as error:
            last_error = error
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
    return result_state({**base, "status": "error", "error": clean(last_error), "completed_at": now_utc()})


def valid_cached_row(result: dict[str, Any], destination: Path) -> bool:
    try:
        path = Path(result["local_path"])
        return bool(
            result.get("status") in SUCCESS_STATUSES
            and re.fullmatch(r"[0-9a-f]{64}", str(result["sha256"]))
            and path.is_absolute() and path.is_relative_to(destination.absolute())
            and not any(parent.is_symlink() for parent in (path, *path.parents))
            and path.is_file() and path.stat().st_size == result["bytes"]
            and sha256_file(path) == result["sha256"]
        )
    except (OSError, ValueError, TypeError, KeyError):
        return False


def cached_results(item: dict[str, Any], output_dir: Path) -> list[dict[str, Any]] | None:
    cache = material_directory(item, output_dir) / "validated-result.json"
    if not cache.is_file() or cache.is_symlink():
        return None
    try:
        payload = json.loads(cache.read_text())
        if payload.get("plan_sha256") != plan_digest(item):
            return None
        results = payload["results"]
        if not results:
            return None
        for result in results:
            if not valid_cached_row(result, material_directory(item, output_dir)):
                return None
        return [result_state({**row, "resumed": True, "revalidated_at": now_utc()}) for row in results]
    except (OSError, ValueError, TypeError, KeyError):
        return None


def plan_digest(item: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def load_sample_module():
    name = "stage1_data_samples"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("data_samples.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def execute_item(item: dict[str, Any], args: argparse.Namespace, budget: TotalBudget) -> list[dict[str, Any]]:
    if item.get("kind") == "data" and item.get("data_policy"):
        return load_sample_module().materialize_sample(item, args, budget, SimpleNamespace(**globals()))
    action = item.get("action")
    if action == "copy_local_artifacts":
        return copy_local_artifacts(item, args.output_dir, budget, args.max_file_mb * 1024**2)
    if action == "reuse_code_repository":
        return [result_state({**item, "status": "pending_dependency", "completed_at": now_utc()})]
    if action == "defer_specialized_download":
        return [result_state({**item, "status": "deferred_specialized",
                             "reason": "repository data requires an explicitly selected specialized downloader",
                             "completed_at": now_utc()})]
    if action == "missing_paper_source":
        return [result_state({**item, "status": "missing_paper_source"})]
    previous = cached_results(item, args.output_dir)
    if previous is not None and not getattr(args, "refresh", False):
        return previous
    try:
        targets = expand_targets(item, args.timeout, args.retries)
    except Exception as error:
        return [result_state({**item, **provider_failure(error), "completed_at": now_utc()})]
    if not targets:
        return [result_state({**item, "status": "no_download_targets"})]
    results = [download_target(item, target, index, args.output_dir, args.timeout, args.retries,
                               args.max_file_mb * 1024**2, budget,
                               resume=not getattr(args, "refresh", False))
               for index, target in enumerate(targets, 1)]
    if all(row["status"] in SUCCESS_STATUSES for row in results):
        cache = material_directory(item, args.output_dir) / "validated-result.json"
        write_json(cache, {"plan_sha256": plan_digest(item), "results": results})
    return results


def resolve_dependencies(results: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    by_resource: dict[str, list[dict[str, Any]]] = {}
    for row in results:
        by_resource.setdefault(row["resource_id"], []).append(row)
    output = []
    for row in results:
        if row["status"] != "pending_dependency":
            output.append(row)
            continue
        dependencies = by_resource.get(row.get("depends_on"), [])
        valid = [item for item in dependencies if item.get("status") in SUCCESS_STATUSES and item.get("local_path")]
        if len(valid) != 1:
            output.append(result_state({**row, "status": "dependency_unavailable"}))
            continue
        dependency = valid[0]
        try:
            requested_ref = row.get("requested_ref", "HEAD")
            if requested_ref != dependency.get("requested_ref", "HEAD"):
                targets = expand_targets({**row, "resolver": "github_archive"}, args.timeout, args.retries)
                if targets[0].get("repository_commit") != dependency.get("repository_commit"):
                    raise ValueError("data reference is a different commit from the code archive")
            subpath = clean(row.get("subpath"))
            if not subpath:
                raise ValueError("data URL names the repository without an identifiable data subpath")
            with zipfile.ZipFile(dependency["local_path"]) as archive:
                members = [name.partition("/")[2] for name in archive.namelist()]
                if not any(name == subpath or name.startswith(subpath.rstrip("/") + "/") for name in members):
                    raise ValueError("data subpath is absent from the downloaded code archive")
            output.append(result_state({
                **row, "status": "materialized_local", "reused_dependency": True,
                **{field: dependency[field] for field in ("local_path", "bytes", "sha256", "repository_commit")},
                "archive_format": "zip", "validation_checks": {"archive_subpath_present": True},
            }))
        except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
            output.append(result_state({**row, "status": "dependency_unavailable", "error": clean(error)}))
    return output

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plan and download task materials")
    parser.add_argument("--mode", choices=("plan", "download"), required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-records", type=int)
    parser.add_argument("--canonical-id", action="append", default=[], help="Select one paper; repeatable")
    parser.add_argument("--all-records", action="store_true", help="Explicitly select all eligible records for downloads")
    parser.add_argument("--refresh", action="store_true", help="Resolve upstream versions again; retain earlier immutable files")
    parser.add_argument("--max-file-mb", type=int, default=setting('max_file_mb'))
    parser.add_argument("--max-total-gb", type=float, default=setting('max_total_gb'))
    parser.add_argument("--timeout", type=float, default=setting('download_timeout'))
    parser.add_argument("--retries", type=int, default=setting('download_retries'))
    parser.add_argument("--concurrency", type=int, default=setting('download_workers'))
    parser.add_argument("--network-config", type=Path, help="Worker JSON with a network object, or standalone network JSON")
    parser.add_argument("--proxy-url", help="Explicit HTTP(S) forward proxy URL")
    parser.add_argument("--proxy-env-file", type=Path, help="Read proxy variables from this env file without executing it")
    parser.add_argument("--no-proxy", help="Comma separated proxy bypass hosts")
    parser.add_argument("--direct", action="store_true", help="Explicit direct connection, ignore configured proxies")
    load_sample_module().add_arguments(parser)
    load_repository_module().add_arguments(parser)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.input = args.input.resolve()
    args.output_dir = args.output_dir.resolve()
    if not args.input.is_file():
        raise SystemExit(f"input does not exist: {args.input}")
    if args.max_records is not None and args.max_records < 1:
        raise SystemExit("--max-records must be positive")
    if not 1 <= args.concurrency <= 16:
        raise SystemExit("--concurrency must be between 1 and 16")
    if args.max_file_mb < 1 or args.max_total_gb <= 0:
        raise SystemExit("download budgets must be positive")
    if args.timeout <= 0 or args.retries < 0:
        raise SystemExit("timeout must be positive and retries nonnegative")
    load_sample_module().validate_arguments(args)
    load_repository_module().validate_arguments(args)
    if args.mode == "download" and not (args.max_records or args.canonical_id or args.all_records):
        raise SystemExit("select download scope using --canonical-id, --max-records, or --all-records")

    all_records = read_jsonl(args.input)
    eligible = [record for record in all_records if eligible_record(record)]
    if args.canonical_id:
        selected_names = set(args.canonical_id)
        eligible = [record for record in eligible if clean(record.get("canonical_id")) in selected_names]
        missing = selected_names - {clean(record.get("canonical_id")) for record in eligible}
        if missing:
            raise SystemExit(f"requested IDs missing or ineligible: {sorted(missing)}")
    if args.max_records is not None:
        eligible = eligible[:args.max_records]
    selected, plan = build_plan(eligible)
    gate_spec = importlib.util.spec_from_file_location("stage1_download_gate", Path(__file__).with_name("download_gate.py"))
    gate_module = importlib.util.module_from_spec(gate_spec)
    gate_spec.loader.exec_module(gate_module)
    plan, gate_checks = gate_module.filter_plan(plan, selected, SimpleNamespace(**globals()))
    load_sample_module().attach_policy(plan, args)
    output_dir = args.output_dir
    write_jsonl(output_dir / "10_resource_gate.jsonl", gate_checks)
    write_jsonl(output_dir / "10_download_plan.jsonl", plan)
    plan_summary = {
        "schema_version": "material-download-plan-summary-v1",
        "generated_at": now_utc(),
        "mode": args.mode,
        "input": str(args.input),
        "input_records": len(all_records),
        "eligible_records": len(eligible),
        "planned_items": len(plan),
        "gate_decisions": dict(sorted(Counter(item["decision"] for item in gate_checks).items())),
        "planned_kinds": dict(sorted(Counter(item["kind"] for item in plan).items())),
        "planned_actions": dict(sorted(Counter(item["action"] for item in plan).items())),
        "required_items": sum(bool(item.get("required_for_gate")) for item in plan),
        "concurrency": args.concurrency,
        "max_file_mb": args.max_file_mb,
        "max_total_gb": args.max_total_gb,
        "data_mode": args.data_mode,
        "sample_files_per_resource": args.sample_files,
        "sample_rows": args.sample_rows,
        "sample_file_mb": args.sample_file_mb,
        "sample_resource_mb": args.sample_resource_mb,
    }
    write_json(output_dir / "10_download_plan_summary.json", plan_summary)
    if args.mode == "plan":
        print(json.dumps({"status": "planned", **plan_summary}, ensure_ascii=False, indent=2))
        return

    network = configure_network(args)
    metadata = configure_metadata(args)
    budget = TotalBudget(int(args.max_total_gb * 1024**3))
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = {
            executor.submit(execute_item, item, args, budget): item
            for item in plan
        }
        for future in as_completed(futures):
            try:
                results.extend(future.result())
            except Exception as error:
                results.append({
                    **futures[future], "status": "error",
                    "error": clean(error),
                })
    results = [result_state(row) for row in resolve_dependencies(results, args)]
    results.sort(key=lambda item: (
        clean(item.get("canonical_id")), clean(item.get("kind")),
        clean(item.get("resource_id")), int(item.get("target_index") or 0),
        clean(item.get("local_path")),
    ))
    write_jsonl(output_dir / "10_download_results.jsonl", results)
    status_counts = Counter(clean(item.get("status")) or "missing" for item in results)
    summary = {
        "schema_version": "material-download-summary-v2",
        "generated_at": now_utc(),
        "status": "completed_with_unresolved_materials"
        if any(row["status"] not in SUCCESS_STATUSES for row in results) else "completed",
        "run_ready": False, "runtime_verified": False,
        "required_unresolved": sum(bool(row.get("required_for_gate"))
                                   and row["status"] not in SUCCESS_STATUSES for row in results),
        "input": str(args.input),
        "eligible_records": len(selected),
        "planned_items": len(plan),
        "result_items": len(results),
        "statuses": dict(sorted(status_counts.items())),
        "downloaded_bytes": sum(row.get("bytes", 0) for row in results
                                if row["status"] == "downloaded" and not row.get("resumed")),
        "transfer_and_copy_budget_used_bytes": budget.used,
        "budget_semantics": "artifact bytes received/copied this invocation, including failed attempts; verified resume consumes zero",
        "resumed_items": sum(bool(row.get("resumed")) for row in results),
        "output_dir": str(output_dir),
        "network": network.report,
        "repository_requests": metadata.report(),
    }
    write_json(output_dir / "10_download_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary["required_unresolved"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
