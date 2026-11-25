#!/usr/bin/env python3
"""Use SerpAPI only for gaps left after local article-content extraction.

This stage never overwrites stage-3 files. It supports a no-network planning
mode and a cached search mode. High-confidence candidates are added to the
record; lower-confidence candidates are retained for human review.

Abstract is optional and never triggers a search. SerpAPI snippets are evidence
only and are never written into the abstract field.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import importlib.util
from html.parser import HTMLParser
from http.client import HTTPException
import json
import os
from pathlib import Path
import random
import re
from threading import BoundedSemaphore, Lock, Event
import tempfile
import time
from typing import Any, Iterable
import urllib.error
import urllib.parse
import urllib.request

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline_runtime import setting







SERPAPI_ENDPOINT = "https://serpapi.com/search.json"
USER_AGENT = "NatureBenchStage1-SerpAPI/1.0"
DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
ARXIV_RE = re.compile(r"(?:arxiv[:./ ]+|arxiv\.org/(?:abs|pdf)/)(\d{4}\.\d{4,5})(?:v\d+)?", re.IGNORECASE)
PMCID_RE = re.compile(r"\bPMC\d+\b", re.IGNORECASE)
YEAR_RE = re.compile(r"\b(18\d{2}|19\d{2}|20\d{2}|21\d{2})\b")

CODE_HOSTS = ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org")
DATA_HOSTS = (
    "zenodo.org",
    "figshare.com",
    "figshare.org",
    "datadryad.org",
    "dryad.org",
    "osf.io",
    "kaggle.com",
    "huggingface.co/datasets",
    "ncbi.nlm.nih.gov/geo",
    "ncbi.nlm.nih.gov/sra",
    "ncbi.nlm.nih.gov/bioproject",
    "ncbi.nlm.nih.gov/biosample",
    "trace.ncbi.nlm.nih.gov",
    "ebi.ac.uk/ena",
    "ebi.ac.uk/biostudies",
    "ebi.ac.uk/pride",
    "ebi.ac.uk/arrayexpress",
    "ebi.ac.uk/emdb",
    "ebi.ac.uk/empiar",
    "rcsb.org",
    "wwpdb.org",
    "emdataresource.org",
    "proteomexchange.org",
    "massive.ucsd.edu",
    "dataverse",
)
DATA_QUERY_SITES = (
    "zenodo.org", "figshare.com", "dryad.org", "osf.io",
    "huggingface.co/datasets", "ncbi.nlm.nih.gov", "ebi.ac.uk",
    "rcsb.org", "wwpdb.org", "emdataresource.org",
    "proteomexchange.org", "massive.ucsd.edu",
)
SUPPLEMENT_URL_MARKERS = (
    "supplement", "supp_info", "supp-info", "suppinfo", "suppl_file",
    "supporting-information", "supporting_information", "additional-file",
    "additional_file", "/esm/", "/mmc", "article-supplement", "/suppl/",
    "supinfo", "suppdata", "suppmat",
)
SUPPLEMENT_FILE_EXTENSIONS = (
    ".pdf", ".zip", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".txt",
    ".tar", ".gz", ".tgz", ".7z", ".rar", ".ppt", ".pptx",
)
RESOURCE_FIELDS = {
    "supplementary": "supplementary_urls",
    "code": "code_urls",
    "data": "data_urls",
}
RESOURCE_STATUSES = {
    "found", "not_found", "partial_search_error", "search_error",
    "needs_review", "not_applicable",
}
PAGE_URL_RE = re.compile(r"https?://[^\s<>\"'\[\]{}]+", re.IGNORECASE)
MAX_LINK_PAGE_BYTES = setting('link_page_max_bytes')
ENGINE_SEMAPHORES = {
    "google": BoundedSemaphore(setting('google_search_workers')),
    "google_scholar": BoundedSemaphore(setting('scholar_search_workers')),
}

PAPER_COLUMNS = [
    "canonical_id",
    "title",
    "year",
    "doi",
    "arxiv",
    "openalex",
    "pmid",
    "pmcid",
    "paper_url",
    "pdf_urls",
    "supplementary_urls",
    "supplementary_status",
    "code_urls",
    "code_status",
    "data_urls",
    "data_status",
    "source_types",
    "changed_fields",
    "unresolved_fields",
    "review_candidate_count",
    "resource_graph_nodes",
    "resource_graph_edges",
    "resource_graph_pages",
    "resource_graph_page_errors",
]


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean(value: Any) -> str:
    return " ".join(re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value or "")).split())


def normalize_title(value: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9\u4e00-\u9fff]+", clean(value).casefold()))


def normalize_doi(value: Any) -> str | None:
    text = urllib.parse.unquote(clean(value))
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.IGNORECASE)
    match = DOI_RE.search(text)
    if not match:
        return None
    doi = match.group(0).rstrip(".,;)]}").casefold()
    for suffix in (".full.pdf", ".abstract", ".full", "/pdf", ".pdf"):
        if doi.endswith(suffix):
            doi = doi[: -len(suffix)]
            break
    return doi or None


def unique(values: Iterable[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        if value in (None, "", []):
            continue
        marker = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if marker not in seen:
            seen.add(marker)
            result.append(value)
    return result


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number} 不是 JSON 对象")
            records.append(value)
    return records


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".json-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def load_env_value(path: Path, key: str) -> str:
    if not path.is_file():
        raise ValueError(f"环境变量文件不存在: {path.resolve()}")
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if text.startswith("export "):
            text = text[7:].strip()
        if "=" not in text:
            continue
        name, value = text.split("=", 1)
        if name.strip() == key:
            result = value.strip().strip("\"").strip("'")
            if result:
                return result
    raise ValueError(f"{path.resolve()} 中没有有效的 {key}")


def source_types(record: dict[str, Any]) -> list[str]:
    return unique(clean(source.get("type")) for source in record.get("sources") or [])


def build_legacy_jina_index(root: Path) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    index: dict[str, dict[str, Any]] = {}
    stats = {"result_files": 0, "discovery_files": 0, "candidates": 0, "errors": 0}
    for result_path in sorted(root.rglob("jina_rerank_result.json")):
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            stats["errors"] += 1
            continue
        stats["result_files"] += 1
        discovery_by_id: dict[str, dict[str, Any]] = {}
        discovery_path = result_path.parent / "resource_discovery.json"
        if discovery_path.is_file():
            try:
                discovery = json.loads(discovery_path.read_text(encoding="utf-8"))
                discovery_by_id = {
                    clean(item.get("candidate_id")): item
                    for item in discovery.get("papers") or []
                    if isinstance(item, dict) and clean(item.get("candidate_id"))
                }
                stats["discovery_files"] += 1
            except (OSError, json.JSONDecodeError):
                stats["errors"] += 1
        for raw_candidate in payload.get("selected_candidates") or []:
            if not isinstance(raw_candidate, dict):
                continue
            candidate = dict(raw_candidate)
            candidate["__jina_result_path__"] = str(result_path)
            candidate["__resource_discovery_path__"] = (
                str(discovery_path) if discovery_path.is_file() else None
            )
            candidate["__resource_discovery__"] = discovery_by_id.get(
                clean(candidate.get("candidate_id")), {}
            )
            stats["candidates"] += 1
            keys = (
                clean(candidate.get("candidate_id")),
                f"doi:{normalize_doi(candidate.get('doi'))}" if normalize_doi(candidate.get("doi")) else "",
                f"title:{normalize_title(candidate.get('title'))}" if normalize_title(candidate.get("title")) else "",
            )
            for key in keys:
                if key:
                    index.setdefault(key, candidate)
    return index, stats


def legacy_resource_urls(candidate: dict[str, Any]) -> dict[str, list[str]]:
    result = {
        "pdf_urls": [],
        "supplementary_urls": [],
        "code_urls": [],
        "data_urls": [],
    }
    for resource in candidate.get("resource_candidates") or []:
        if not isinstance(resource, dict):
            continue
        url = clean(resource.get("url"))
        kind = clean(resource.get("kind")).casefold()
        if not url:
            continue
        if kind in {"pdf", "article_pdf"}:
            result["pdf_urls"].append(url)
        elif "supp" in kind:
            result["supplementary_urls"].append(url)
        elif kind in {"github", "gitlab", "code", "repository"}:
            result["code_urls"].append(url)
        elif kind in {"dataset", "data", "huggingface_dataset"}:
            result["data_urls"].append(url)
    discovery = candidate.get("__resource_discovery__")
    if isinstance(discovery, dict):
        result["pdf_urls"].extend(
            clean(item.get("url"))
            for item in discovery.get("pdf") or []
            if isinstance(item, dict) and clean(item.get("url"))
        )
        result["code_urls"].extend(
            clean(item.get("url"))
            for item in discovery.get("github") or []
            if isinstance(item, dict) and clean(item.get("url"))
        )
        result["data_urls"].extend(
            clean(item.get("url"))
            for bucket in ("dataset", "huggingface")
            for item in discovery.get(bucket) or []
            if isinstance(item, dict) and clean(item.get("url"))
        )
        result["supplementary_urls"].extend(
            clean(item.get("url"))
            for item in discovery.get("supplementary") or []
            if isinstance(item, dict) and clean(item.get("url"))
        )
    return {field: unique(values) for field, values in result.items()}


def sync_paper_content(record: dict[str, Any]) -> None:
    """Keep the stage-04 paper status aligned with later PDF discoveries."""
    document = record.setdefault("document", {})
    document["paper_urls"] = unique([
        document.get("paper_url"), *(document.get("paper_urls") or []),
    ])
    document["pdf_urls"] = unique([
        document.get("pdf_url"), *(document.get("pdf_urls") or []),
    ])
    paper_content = record.setdefault("paper_content", {
        "schema_version": "paper-content-status-v1",
        "collection_status": None,
        "status": "not_found",
        "paper_available": False,
        "text_ready": False,
        "valid_local_artifact_count": 0,
        "text_ready_artifact_count": 0,
        "invalid_artifact_count": 0,
        "coverage": {},
        "local_artifacts": [],
        "request_problem_count": 0,
    })
    paper_content["candidate_paper_urls"] = document["paper_urls"]
    paper_content["candidate_pdf_urls"] = document["pdf_urls"]
    if (
        document["pdf_urls"]
        and paper_content.get("status") in {None, "not_found", "landing_page_only"}
    ):
        paper_content["status"] = "downloadable_pdf"
        paper_content["paper_available"] = True


def hydrate_identifiers_from_document(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Fill missing identifiers from URLs already attached to this paper."""
    identifiers = record.setdefault("identifiers", {})
    document = record.setdefault("document", {})
    urls = unique([
        document.get("paper_url"),
        *(document.get("paper_urls") or []),
        document.get("pdf_url"),
        *(document.get("pdf_urls") or []),
    ])
    changes: list[dict[str, Any]] = []
    for url in urls:
        found = link_identifiers({"link": url})
        for field in ("doi", "arxiv", "openalex", "pmid", "pmcid"):
            if identifiers.get(field) or not found.get(field):
                continue
            identifiers[field] = found[field]
            changes.append({
                "field": f"identifiers.{field}",
                "old_value": None,
                "new_value": found[field],
                "origin": "existing_document_url",
                "evidence_url": url,
            })
    return changes


def reuse_existing_jina(
    original: dict[str, Any], index: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    record = deepcopy(original)
    candidate: dict[str, Any] | None = None
    for source in record.get("sources") or []:
        if source.get("type") != "jina":
            continue
        candidate = index.get(clean(source.get("record_id")))
        if candidate:
            break
    if not candidate:
        doi = normalize_doi((record.get("identifiers") or {}).get("doi"))
        candidate = index.get(f"doi:{doi}") if doi else None
    if not candidate:
        title = normalize_title(record.get("title"))
        candidate = index.get(f"title:{title}") if title else None
    if not candidate:
        changes = hydrate_identifiers_from_document(record)
        sync_paper_content(record)
        return record, changes

    changes: list[dict[str, Any]] = []

    def reuse_change(field: str, old_value: Any, new_value: Any) -> None:
        changes.append({
            "field": field,
            "old_value": old_value,
            "new_value": new_value,
            "origin": "existing_jina",
            "evidence_path": candidate.get("__jina_result_path__"),
            "resource_discovery_path": candidate.get("__resource_discovery_path__"),
        })

    identifiers = record.setdefault("identifiers", {})
    candidate_ids = {
        "doi": normalize_doi(candidate.get("doi")),
        "arxiv": clean(candidate.get("arxiv_id")) or None,
        "openalex": clean(candidate.get("openalex_id")) or None,
        "pmid": clean(candidate.get("pmid")) or None,
        "pmcid": clean(candidate.get("pmcid")) or None,
    }
    for field, value in candidate_ids.items():
        if not identifiers.get(field) and value:
            identifiers[field] = value
            reuse_change(f"identifiers.{field}", None, value)
    if not record.get("title") and clean(candidate.get("title")):
        record["title"] = clean(candidate.get("title"))
        reuse_change("title", None, record["title"])
    if not record.get("abstract") and clean(candidate.get("abstract")):
        record["abstract"] = clean(candidate.get("abstract"))
        reuse_change("abstract", None, record["abstract"])
    if not record.get("year") and candidate.get("year"):
        record["year"] = candidate.get("year")
        reuse_change("year", None, record["year"])

    document = record.setdefault("document", {})
    document.setdefault("paper_urls", unique([document.get("paper_url")]))
    document.setdefault("pdf_urls", unique([document.get("pdf_url")]))
    paper_url = clean(candidate.get("paper_url"))
    if paper_url and paper_url not in document["paper_urls"]:
        document["paper_urls"].append(paper_url)
        reuse_change("document.paper_urls", None, paper_url)
    if not document.get("paper_url") and paper_url:
        document["paper_url"] = paper_url
        reuse_change("document.paper_url", None, paper_url)

    legacy_urls = legacy_resource_urls(candidate)
    direct_pdf = clean(candidate.get("pdf_url"))
    pdf_urls = unique([direct_pdf, *legacy_urls["pdf_urls"]])
    for url in pdf_urls:
        if url not in document["pdf_urls"]:
            document["pdf_urls"].append(url)
            reuse_change("document.pdf_urls", None, url)
    if not document.get("pdf_url") and document["pdf_urls"]:
        document["pdf_url"] = document["pdf_urls"][0]
        reuse_change("document.pdf_url", None, document["pdf_url"])

    resources = record.setdefault("resources", {})
    for field in ("supplementary_urls", "code_urls", "data_urls"):
        values = resources.setdefault(field, [])
        expected_kind = {
            "supplementary_urls": "supplementary",
            "code_urls": "code",
            "data_urls": "data",
        }[field]
        for url in legacy_urls[field]:
            if url_kind({"link": url}) != expected_kind:
                continue
            if url not in values:
                values.append(url)
                reuse_change(f"resources.{field}", None, url)

    record["existing_source_reuse"] = {
        "origin": "jina_rerank_result_and_resource_discovery",
        "changes": changes,
    }
    changes.extend(hydrate_identifiers_from_document(record))
    sync_paper_content(record)
    return record, changes


def metadata_incomplete(record: dict[str, Any]) -> bool:
    identifiers = record.get("identifiers") or {}
    document = record.get("document") or {}
    return any((
        not record.get("year"),
        not identifiers.get("doi") and not identifiers.get("arxiv"),
        not document.get("paper_url"),
    ))


def current_resource_status(record: dict[str, Any], resource_type: str) -> str | None:
    raw = (record.get("resource_status") or {}).get(resource_type)
    if isinstance(raw, dict):
        value = clean(raw.get("status"))
    else:
        value = clean(raw)
    return value if value in RESOURCE_STATUSES else None


def short_title(title: Any) -> str:
    """Return the title prefix before the first ASCII or full-width colon."""
    value = clean(title)
    if not value:
        return ""
    prefix = re.split(r"[:：]", value, maxsplit=1)[0].strip(" -–—")
    if len(prefix) < 3 or prefix.casefold() == value.casefold():
        return ""
    return prefix


def build_search_attempts(
    record: dict[str, Any],
    query_type: str,
    qualifier: str = "",
    *,
    engine: str = "google",
    profile: str = "balanced",
) -> list[dict[str, str]]:
    title = clean(record.get("title"))
    title_prefix = short_title(title)
    identifiers = record.get("identifiers") or {}
    doi = normalize_doi(identifiers.get("doi"))
    arxiv = clean(identifiers.get("arxiv"))
    attempts: list[dict[str, str]] = []

    def add_attempt(
        basis: str,
        search_value: str,
        *,
        attempt_engine: str = engine,
        attempt_qualifier: str = qualifier,
        quoted: bool = True,
    ) -> None:
        if not search_value:
            return
        value = f'"{search_value}"' if quoted else search_value
        query_text = f"{value} {attempt_qualifier}".strip()
        attempt = {
            "query_type": query_type,
            "engine": attempt_engine,
            "q": query_text,
            "search_basis": basis,
        }
        if not any(
            item["engine"] == attempt["engine"] and item["q"] == attempt["q"]
            for item in attempts
        ):
            attempts.append(attempt)

    if query_type == "metadata":
        add_attempt("doi_scholar", doi, attempt_engine="google_scholar")
        add_attempt("arxiv_scholar", f"arXiv:{arxiv}" if arxiv else "", attempt_engine="google_scholar")
        add_attempt("title_scholar", title, attempt_engine="google_scholar")
        add_attempt("short_title_scholar", title_prefix, attempt_engine="google_scholar")
        add_attempt("doi_google", doi, attempt_engine="google")
        add_attempt("title_google", title, attempt_engine="google")
        add_attempt("short_title_google", title_prefix, attempt_engine="google")
    elif query_type == "pdf":
        add_attempt("doi_scholar", doi, attempt_engine="google_scholar")
        add_attempt("arxiv_scholar", f"arXiv:{arxiv}" if arxiv else "", attempt_engine="google_scholar")
        add_attempt("title_scholar", title, attempt_engine="google_scholar")
        add_attempt("short_title_scholar", title_prefix, attempt_engine="google_scholar")
        add_attempt("doi_google_pdf", doi, attempt_engine="google", attempt_qualifier="filetype:pdf")
        add_attempt("title_google_pdf", title, attempt_engine="google", attempt_qualifier="filetype:pdf")
        add_attempt("short_title_google_pdf", title_prefix, attempt_engine="google", attempt_qualifier="filetype:pdf")
        add_attempt("title_google", title, attempt_engine="google")
    elif query_type == "code":
        add_attempt("title_github", title, attempt_qualifier="github")
        add_attempt("title_github_site", title, attempt_qualifier="site:github.com")
        add_attempt("short_title_github", title_prefix, attempt_qualifier="github")
        add_attempt("short_title_github_site", title_prefix, attempt_qualifier="site:github.com")
        add_attempt("doi_github", doi, attempt_qualifier="github")
        add_attempt("arxiv_github", f"arXiv:{arxiv}" if arxiv else "", attempt_qualifier="github")
        if profile == "thorough":
            add_attempt("title_gitlab_site", title, attempt_qualifier="site:gitlab.com")
            add_attempt("short_title_gitlab_site", title_prefix, attempt_qualifier="site:gitlab.com")
            add_attempt("title_source_code", title, attempt_qualifier='"source code"')
    elif query_type == "data":
        # Scholar can expose cited datasets, but ordinary Google is still needed
        # for repository landing pages and downloadable records.
        add_attempt("doi_dataset_scholar", doi, attempt_engine="google_scholar", attempt_qualifier="dataset")
        add_attempt("title_dataset_scholar", title, attempt_engine="google_scholar", attempt_qualifier="dataset")
        add_attempt("doi_data_google", doi, attempt_qualifier="dataset")
        add_attempt("title_data_availability", title, attempt_qualifier='"data availability"')
        add_attempt("title_dataset", title, attempt_qualifier="dataset")
        add_attempt("title_repository", title, attempt_qualifier="repository accession")
        add_attempt("short_title_dataset", title_prefix, attempt_qualifier="dataset")
        if profile == "thorough":
            for site in DATA_QUERY_SITES:
                add_attempt(
                    f"title_site_{site}",
                    title,
                    attempt_qualifier=f"site:{site}",
                )
    elif query_type == "supplementary":
        add_attempt("doi_supplementary", doi, attempt_qualifier="supplementary")
        add_attempt("title_supplementary", title, attempt_qualifier="supplementary")
        add_attempt("short_title_supplementary", title_prefix, attempt_qualifier="supplementary")
        if profile == "thorough":
            add_attempt("title_supporting_information", title, attempt_qualifier='"supporting information"')
    else:
        if doi:
            add_attempt("doi", doi)
        elif arxiv:
            add_attempt("arxiv", f"arXiv:{arxiv}")
        add_attempt("title", title)
        if profile == "thorough":
            add_attempt("title_unquoted", title, quoted=False)
    return attempts


def logical_query(
    record: dict[str, Any],
    query_type: str,
    qualifier: str = "",
    *,
    engine: str = "google",
    profile: str = "balanced",
) -> dict[str, Any] | None:
    attempts = build_search_attempts(
        record, query_type, qualifier, engine=engine, profile=profile
    )
    if not attempts:
        return None
    return {
        "query_type": query_type,
        "engine": (
            attempts[0]["engine"]
            if len({attempt["engine"] for attempt in attempts}) == 1
            else "mixed"
        ),
        "q": attempts[0]["q"],
        "attempts": attempts,
        "record_context": {
            "title": record.get("title"),
            "identifiers": record.get("identifiers") or {},
            "document": record.get("document") or {},
        },
    }


def build_query_plan(record: dict[str, Any], profile: str) -> list[dict[str, Any]]:
    title = clean(record.get("title"))
    if not title:
        return []
    queries: list[dict[str, Any]] = []

    def add(query: dict[str, Any] | None) -> None:
        if query:
            queries.append(query)

    if metadata_incomplete(record):
        add(logical_query(
            record, "metadata", profile=profile
        ))
    document = record.get("document") or {}
    resources = record.get("resources") or {}
    missing_pdf = not document.get("pdf_url") and not document.get("pdf_urls")
    missing_supplementary = (
        not resources.get("supplementary_urls")
        and current_resource_status(record, "supplementary") != "not_applicable"
    )
    missing_code = (
        not resources.get("code_urls")
        and current_resource_status(record, "code") != "not_applicable"
    )
    missing_data = (
        not resources.get("data_urls")
        and current_resource_status(record, "data") != "not_applicable"
    )
    if missing_pdf:
        add(logical_query(record, "pdf", profile=profile))
    if missing_supplementary:
        add(logical_query(
            record,
            "supplementary",
            profile=profile,
        ))
    if missing_code:
        add(logical_query(record, "code", profile=profile))
    if missing_data:
        add(logical_query(record, "data", profile=profile))
    return queries


def query_cache_key(query: dict[str, Any]) -> str:
    raw = json.dumps(query, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def redact_secret(value: Any, secret: str) -> Any:
    if isinstance(value, dict):
        return {key: redact_secret(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_secret(item, secret) for item in value]
    if isinstance(value, str) and secret:
        return value.replace(secret, "<redacted>")
    return value


class SerpAPISearchError(ValueError):
    def __init__(self, message: str, api_calls: int) -> None:
        super().__init__(message)
        self.api_calls = api_calls


class ApiBudget:
    def __init__(self, limit: int | None) -> None:
        self.limit = limit
        self.used = 0
        self._lock = Lock()

    def consume(self) -> None:
        with self._lock:
            if self.limit is not None and self.used >= self.limit:
                raise SerpAPISearchError(
                    f"已达到 --max-api-queries={self.limit} 的请求上限", 0
                )
            self.used += 1


def serpapi_search(
    query: dict[str, Any],
    *,
    api_key: str,
    cache_dir: Path,
    reuse_cache_dir: Path | None = None,
    timeout: float,
    retries: int,
    delay: float,
    api_budget: ApiBudget,
    stop_event: Event | None = None,
) -> tuple[dict[str, Any], bool, int]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{query_cache_key(query)}.json"
    paths = [cache_path]
    if reuse_cache_dir is not None:
        paths.append(reuse_cache_dir / cache_path.name)
    for cached_path in paths:
        if not cached_path.is_file():
            continue
        try:
            payload = json.loads(cached_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("cache payload is not an object")
        except (OSError, ValueError):
            print(json.dumps({"event":"invalid_search_cache", "path":str(cached_path),
                              "action":"reuse_other_cache_or_refetch"}), flush=True)
            continue
        if cached_path != cache_path:
            write_json(cache_path, payload)
        return payload, True, 0

    params = {
        "engine": query["engine"],
        "q": query["q"],
        "api_key": api_key,
        "hl": "en",
        "num": "10",
        "output": "json",
    }
    url = f"{SERPAPI_ENDPOINT}?{urllib.parse.urlencode(params)}"
    last_error: Exception | None = None
    api_calls = 0
    for attempt in range(retries + 1):
        if stop_event is not None and stop_event.is_set():
            raise SerpAPISearchError("search cancelled during shutdown", api_calls)
        try:
            api_budget.consume()
            api_calls += 1
            request = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
            semaphore = ENGINE_SEMAPHORES.get(query["engine"])
            if semaphore is None:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            else:
                with semaphore:
                    if stop_event is not None and stop_event.is_set():
                        raise SerpAPISearchError("search cancelled during shutdown", api_calls)
                    with urllib.request.urlopen(request, timeout=timeout) as response:
                        payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("SerpAPI 返回的不是 JSON 对象")
            if payload.get("error"):
                error_text = clean(payload["error"])
                if "hasn't returned any results" not in error_text.casefold():
                    raise ValueError(f"SerpAPI error: {error_text}")
            sanitized = redact_secret(payload, api_key)
            write_json(cache_path, sanitized)
            if delay > 0:
                time.sleep(delay)
            return sanitized, False, api_calls
        except SerpAPISearchError:
            raise
        except urllib.error.HTTPError as error:
            last_error = error
            transient = error.code in {429, 500, 502, 503, 504}
            if attempt >= retries or not transient:
                break
            retry_after = clean(error.headers.get("Retry-After")) if error.headers else ""
            try:
                wait_seconds = float(retry_after)
            except ValueError:
                wait_seconds = min(2 ** attempt, 8) + random.uniform(0.2, 1.0)
            time.sleep(max(0, min(wait_seconds, 30)))
        except (OSError, ValueError, HTTPException, urllib.error.URLError) as error:
            last_error = error
            if attempt >= retries:
                break
            time.sleep(min(2 ** attempt, 8) + random.uniform(0.2, 1.0))
    raise SerpAPISearchError(redact_secret(f"SerpAPI 查询失败: {last_error}", api_key), api_calls)


def is_absolute_http_url(value: Any) -> bool:
    link = clean(value)
    if not link:
        return False
    lower = link.casefold()
    if any(marker in lower for marker in (
        "\\", "%5c", "\\u003c", "\\u003e", "%3c", "%3e", "<", ">",
    )):
        return False
    parsed = urllib.parse.urlsplit(link)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return False
    if parsed.path.rstrip("/").casefold() == "/goto" and "url=" in parsed.query.casefold():
        return False
    return True


def result_candidates(payload: dict[str, Any], query: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for fallback_position, result in enumerate(payload.get("organic_results") or [], 1):
        if not isinstance(result, dict):
            continue
        base = {
            "query_type": query["query_type"],
            "engine": query["engine"],
            "position": result.get("position") or fallback_position,
            "title": clean(result.get("title")),
            "link": clean(result.get("link")),
            "snippet": clean(result.get("snippet")),
            "publication_info": clean((result.get("publication_info") or {}).get("summary")),
            "file_format": clean(result.get("file_format")),
            "parent_result": True,
            "search_basis": query.get("search_basis"),
        }
        if is_absolute_http_url(base["link"]):
            candidates.append(base)
        for resource in result.get("resources") or []:
            if not isinstance(resource, dict):
                continue
            link = clean(resource.get("link"))
            if not is_absolute_http_url(link):
                continue
            candidates.append({
                **base,
                "title": clean(resource.get("title")) or base["title"],
                "link": link,
                "file_format": clean(resource.get("file_format")),
                "parent_result": False,
            })
    return candidates


def title_score(paper_title: str, candidate: dict[str, Any]) -> float:
    target = normalize_title(paper_title)
    candidate_title = normalize_title(candidate.get("title"))
    if not target or not candidate_title:
        return 0.0
    sequence = SequenceMatcher(None, target, candidate_title).ratio()
    target_tokens = set(target.split())
    evidence_tokens = set(normalize_title(
        f"{candidate.get('title', '')} {candidate.get('snippet', '')}"
    ).split())
    coverage = len(target_tokens & evidence_tokens) / max(1, len(target_tokens))
    return round(max(sequence, coverage), 6)


def identity_title_score(paper_title: str, candidate: dict[str, Any]) -> float:
    """Compare only the result title, excluding snippets that may cite another paper."""
    target = normalize_title(paper_title)
    candidate_title = normalize_title(candidate.get("title"))
    if not target or not candidate_title:
        return 0.0
    sequence = SequenceMatcher(None, target, candidate_title).ratio()
    target_tokens = set(target.split())
    candidate_tokens = set(candidate_title.split())
    coverage = len(target_tokens & candidate_tokens) / max(1, len(target_tokens))
    return round(max(sequence, coverage), 6)


def candidate_text(candidate: dict[str, Any]) -> str:
    return " ".join(clean(candidate.get(field)) for field in (
        "title", "link", "snippet", "publication_info"
    ))


def is_supplementary_url(link: str) -> bool:
    if not is_absolute_http_url(link):
        return False
    parsed = urllib.parse.urlsplit(link)
    host = parsed.netloc.casefold().removeprefix("www.")
    if host in {
        "researchgate.net", "academia.edu", "reddit.com",
        "academia.stackexchange.com",
    }:
        return False
    path_and_query = urllib.parse.unquote(
        f"{parsed.path}?{parsed.query}"
    ).casefold()
    filename = Path(parsed.path).name.casefold()
    if (
        host == "ebi.ac.uk"
        and re.fullmatch(
            r"/europepmc/webservices/rest/PMC\d+/supplementaryFiles/?",
            parsed.path,
            re.IGNORECASE,
        )
    ):
        return True
    numbered_supplement = bool(re.search(
        r"(?:^|[-_.])(?:supp|suppl|s|mmc|moesm)\d*(?:[-_.]|$)", filename
    ))
    has_marker = any(marker in path_and_query for marker in SUPPLEMENT_URL_MARKERS)
    if not (has_marker or numbered_supplement):
        return False
    path = parsed.path.casefold()
    explicit_attachment_path = any(marker in path_and_query for marker in (
        "/doi/suppl/", "/content/suppl/", "/article-supplement/", "/esm/",
        "/mmc", "downloadsupplement", "suppl_file", "type=supplementary",
        "/supplemental/",
    ))
    return bool(
        path.endswith(SUPPLEMENT_FILE_EXTENSIONS) or explicit_attachment_path
    )


def is_code_repository_url(link: str) -> bool:
    if not is_absolute_http_url(link):
        return False
    parsed = urllib.parse.urlsplit(link)
    host = parsed.netloc.casefold().removeprefix("www.")
    parts = [part for part in parsed.path.split("/") if part]
    if host not in CODE_HOSTS or len(parts) < 2:
        return False
    reserved = {
        "about", "blog", "collections", "contact", "enterprise", "events",
        "explore", "features", "issues", "login", "marketplace", "new",
        "open-source", "resources", "site-policy", "solutions", "sponsors",
        "organizations", "orgs", "pricing", "search", "security", "shop",
        "skills", "support", "topics", "trending", "users",
    }
    if parts[0].casefold() in reserved or parts[1].casefold() in reserved:
        return False
    if host == "github.com":
        owner = parts[0]
        repository = re.sub(r"\.git$", "", parts[1], flags=re.IGNORECASE)
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", owner):
            return False
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repository):
            return False
    if len(parts) >= 4 and parts[2].casefold() in {"blob", "raw"}:
        if Path(parts[-1]).suffix.casefold() in {".bib", ".cff", ".ris"}:
            return False
    return True


def is_data_record_url(link: str) -> bool:
    if not is_absolute_http_url(link):
        return False
    parsed = urllib.parse.urlsplit(link)
    host = parsed.netloc.casefold().removeprefix("www.")
    path = urllib.parse.unquote(parsed.path).casefold()
    parts = [part for part in path.split("/") if part]
    query = urllib.parse.parse_qs(parsed.query)
    data_doi = normalize_doi(link)
    exact_data_doi = bool(data_doi and (
        re.fullmatch(r"10\.5281/zenodo\.\d+", data_doi)
        or re.fullmatch(r"10\.6084/m9\.figshare\.\d+", data_doi)
        or re.fullmatch(r"10\.5061/dryad\.[-a-z0-9]+", data_doi)
    ))
    if host in {"doi.org", "dx.doi.org"} and exact_data_doi:
        return True
    if host == "zenodo.org":
        if any(key.casefold() in {"download", "preview"} for key in query):
            return False
        return (
            len(parts) == 2
            and parts[0] in {"record", "records"}
            and parts[1].isdigit()
        )
    if "figshare." in host:
        return bool(
            len(parts) == 4
            and parts[0] == "articles"
            and parts[1] in {"dataset", "media"}
            and parts[3].isdigit()
        ) or bool(
            len(parts) == 3
            and parts[0] == "collections"
            and parts[2].isdigit()
        )
    if host in {"dryad.org", "datadryad.org"}:
        return "/stash/dataset/" in path or "/dataset/doi:" in path
    if host == "osf.io":
        return (
            len(parts) == 1 and bool(re.fullmatch(r"[a-z0-9]{4,8}", parts[0]))
        ) or "/registrations/" in path
    if host == "kaggle.com":
        return len(parts) >= 3 and parts[0] == "datasets"
    if host == "huggingface.co":
        return len(parts) >= 3 and parts[0] == "datasets"
    lower = link.casefold()
    if host == "ncbi.nlm.nih.gov":
        if path.startswith("/geo/query/acc.cgi"):
            return any(
                re.fullmatch(r"(?:GDS|GSE|GSM|GPL)\d+", value, re.I)
                for value in query.get("acc", [])
            )
        if path.rstrip("/") == "/sra":
            return any(
                re.fullmatch(
                    r"(?:(?:SR|ER|DR)[APRSX]\d+|PRJ(?:NA|EB|DB)\d+)(?:\[accn\])?",
                    clean(value),
                    re.I,
                )
                for value in query.get("term", [])
            )
        if re.fullmatch(r"/sra/(?:SR|ER|DR)[APRSX]\d+/?", parsed.path, re.I):
            return True
        if path.rstrip("/") in {"/bioproject", "/biosample"}:
            return any(
                re.fullmatch(
                    r"(?:PRJ(?:NA|EB|DB)\d+|SAM(?:N|EA|D)\d+)(?:\[accn\])?",
                    clean(value),
                    re.I,
                )
                for values in query.values()
                for value in values
            )
        return False
    if any(item in lower for item in (
        "trace.ncbi.nlm.nih.gov", "ebi.ac.uk/ena", "ebi.ac.uk/biostudies",
        "ebi.ac.uk/pride", "ebi.ac.uk/arrayexpress", "ebi.ac.uk/emdb",
        "ebi.ac.uk/empiar", "proteomexchange.org/project/",
        "massive.ucsd.edu/proteosafe/",
    )):
        return True
    if host in {"rcsb.org", "www.rcsb.org"}:
        return bool(
            len(parts) == 2
            and parts[0] == "structure"
            and re.fullmatch(r"[A-Za-z0-9]{4}", parts[1])
        )
    if host.endswith("wwpdb.org"):
        return len(parts) >= 1 and parts[0] not in {"", "about", "documentation"}
    if host == "emdataresource.org":
        return len(parts) >= 1 and parts[0] not in {"", "about", "news"}
    return "dataverse" in host and len(parts) >= 1


def extracted_identifiers(candidate: dict[str, Any]) -> dict[str, str | None]:
    text = candidate_text(candidate)
    link = clean(candidate.get("link"))
    doi = normalize_doi(text)
    arxiv_match = ARXIV_RE.search(text)
    pmcid_match = PMCID_RE.search(text)
    pmid_match = re.search(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)", link, re.IGNORECASE)
    openalex_match = re.search(r"openalex\.org/(W\d+)", link, re.IGNORECASE)
    return {
        "doi": doi,
        "arxiv": arxiv_match.group(1) if arxiv_match else None,
        "openalex": f"https://openalex.org/{openalex_match.group(1).upper()}" if openalex_match else None,
        "pmid": pmid_match.group(1) if pmid_match else None,
        "pmcid": pmcid_match.group(0).upper() if pmcid_match else None,
    }


def link_identifiers(candidate: dict[str, Any]) -> dict[str, str | None]:
    """Extract identifiers from the candidate URL, never from its snippet."""
    link = clean(candidate.get("link"))
    doi = normalize_doi(link)
    arxiv_match = ARXIV_RE.search(link)
    pmcid_match = PMCID_RE.search(link)
    openalex_match = re.search(r"openalex\.org/(W\d+)", link, re.IGNORECASE)
    pmid_match = re.search(
        r"(?:pubmed\.ncbi\.nlm\.nih\.gov/|[?&]pmid=)(\d+)",
        link,
        re.IGNORECASE,
    )
    return {
        "doi": doi,
        "arxiv": arxiv_match.group(1) if arxiv_match else None,
        "openalex": openalex_match.group(1).upper() if openalex_match else None,
        "pmid": pmid_match.group(1) if pmid_match else None,
        "pmcid": pmcid_match.group(0).upper() if pmcid_match else None,
    }


def candidate_year(candidate: dict[str, Any]) -> int | None:
    match = YEAR_RE.search(clean(candidate.get("publication_info")))
    return int(match.group(1)) if match else None


def url_kind(candidate: dict[str, Any]) -> str:
    link = clean(candidate.get("link"))
    if not is_absolute_http_url(link):
        return "invalid"
    lower = link.casefold()
    if is_code_repository_url(link):
        return "code"
    if is_data_record_url(link):
        return "data"
    if is_supplementary_url(link):
        return "supplementary"
    if (
        lower.endswith(".pdf")
        or "/pdf/" in lower
        or lower.startswith("https://arxiv.org/pdf/")
        or clean(candidate.get("file_format")).casefold() == "pdf"
    ):
        return "pdf"
    return "paper"


def doi_matches(record: dict[str, Any], candidate: dict[str, Any]) -> bool:
    record_doi = normalize_doi((record.get("identifiers") or {}).get("doi"))
    if not record_doi:
        return False
    kind = url_kind(candidate)
    if kind in {"paper", "pdf"}:
        return link_identifiers(candidate).get("doi") == record_doi
    evidence = urllib.parse.unquote(candidate_text(candidate)).casefold()
    return record_doi in evidence


def confidence(record: dict[str, Any], candidate: dict[str, Any]) -> float:
    kind = url_kind(candidate)
    if kind in {"paper", "pdf"}:
        score = identity_title_score(clean(record.get("title")), candidate)
    else:
        score = title_score(clean(record.get("title")), candidate)
    if doi_matches(record, candidate):
        score = 1.0
    evidence_score = title_score(clean(record.get("title")), candidate)
    basis = clean(candidate.get("search_basis"))
    if (
        kind == "pdf"
        and clean(candidate.get("query_type")) == "pdf"
        and (basis.startswith("doi_") or basis == "doi")
        and evidence_score >= 0.85
    ):
        score = max(score, 0.9)
    document = record.get("document") or {}
    known_urls = unique([
        document.get("paper_url"), *(document.get("paper_urls") or []),
        document.get("pdf_url"), *(document.get("pdf_urls") or []),
    ])
    link = clean(candidate.get("link"))
    if link and link in known_urls:
        score = 1.0
    query_type = clean(candidate.get("query_type"))
    if kind in RESOURCE_FIELDS and query_type == kind:
        paper_url = clean((record.get("document") or {}).get("paper_url"))
        if paper_url and paper_url.casefold() in candidate_text(candidate).casefold():
            score = max(score, 0.95)
    return score


def evidence_matches(record: dict[str, Any], candidate: dict[str, Any]) -> list[str]:
    matches: list[str] = []
    if doi_matches(record, candidate):
        matches.append("doi_in_result_or_indexed_page")
    paper_url = clean((record.get("document") or {}).get("paper_url"))
    evidence = candidate_text(candidate).casefold()
    if paper_url and paper_url.casefold() in evidence:
        matches.append("paper_url_in_result_or_indexed_page")
    if title_score(clean(record.get("title")), candidate) >= 0.72:
        matches.append("paper_title_in_result_or_indexed_page")
    if "readme" in evidence:
        matches.append("readme_mentioned_in_search_evidence")
    basis = clean(candidate.get("search_basis"))
    if basis.startswith("doi_") or basis == "doi":
        matches.append("returned_by_doi_targeted_search")
    if basis.startswith("arxiv_") or basis == "arxiv":
        matches.append("returned_by_arxiv_targeted_search")
    return unique(matches)


def candidate_allowed_for_query(candidate: dict[str, Any], query_type: str) -> bool:
    kind = url_kind(candidate)
    if query_type == "metadata":
        return kind in {"paper", "pdf"}
    if query_type == "pdf":
        return kind == "pdf"
    if query_type in RESOURCE_FIELDS:
        return kind == query_type
    return False


def candidate_plausible_for_query(
    record: dict[str, Any], candidate: dict[str, Any], query_type: str
) -> bool:
    if not candidate_allowed_for_query(candidate, query_type):
        return False
    if query_type not in RESOURCE_FIELDS:
        return True
    matches = evidence_matches(record, candidate)
    strong_matches = {
        "doi_in_result_or_indexed_page",
        "paper_url_in_result_or_indexed_page",
        "paper_title_in_result_or_indexed_page",
    }
    if strong_matches & set(matches):
        return True
    return title_score(clean(record.get("title")), candidate) >= 0.45


def candidate_verified_for_query(
    record: dict[str, Any], candidate: dict[str, Any], query_type: str
) -> bool:
    """Require auto-accept-level evidence before stopping fallback searches."""
    if not candidate_allowed_for_query(candidate, query_type):
        return False
    if doi_matches(record, candidate):
        return True
    score = confidence(record, candidate)
    threshold = {
        "metadata": 0.85,
        "pdf": 0.82,
        "supplementary": 0.82,
        "code": 0.72,
        "data": 0.72,
    }.get(query_type, 1.0)
    return score >= threshold


class LinkEvidenceParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: list[str] = []
        self.text_parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"script", "style", "noscript", "svg"}:
            self._ignored_depth += 1
            return
        attributes = dict(attrs)
        href = clean(attributes.get("href"))
        if href:
            absolute = urllib.parse.urljoin(self.base_url, href)
            if is_absolute_http_url(absolute):
                self.links.append(absolute)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "noscript", "svg"}:
            self._ignored_depth = max(0, self._ignored_depth - 1)

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth and data.strip():
            self.text_parts.append(data)


def canonical_resource_url(url: Any, kind: str | None = None) -> str:
    link = clean(url)
    if not is_absolute_http_url(link):
        return ""
    parsed = urllib.parse.urlsplit(link)
    host = parsed.netloc.casefold().removeprefix("www.")
    path = re.sub(r"/{2,}", "/", parsed.path).rstrip("/")
    resource_kind = kind or url_kind({"link": link})
    if resource_kind == "data" and host in {"doi.org", "dx.doi.org"}:
        doi = normalize_doi(link)
        zenodo_match = re.fullmatch(r"10\.5281/zenodo\.(\d+)", doi or "")
        if zenodo_match:
            return f"https://zenodo.org/records/{zenodo_match.group(1)}"
    if resource_kind == "code" and host in CODE_HOSTS:
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 2:
            repository_name = re.sub(r'\.git$', '', parts[1], flags=re.I)
        path = f"/{parts[0]}/{repository_name}"
    filtered_query = urllib.parse.urlencode([
        (key, value)
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_")
        and key.casefold() not in {"ref", "source", "stixel_click"}
    ])
    return urllib.parse.urlunsplit(("https", host, path or "/", filtered_query, ""))


def fetch_link_evidence(url: str, cache_dir: Path, timeout: float) -> dict[str, Any]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    cache_path = cache_dir / f"{cache_key}.json"
    if cache_path.is_file():
        return json.loads(cache_path.read_text(encoding="utf-8"))
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/json,application/xml,text/plain;q=0.9,*/*;q=0.2",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final_url = response.geturl()
            content_type = clean(response.headers.get("Content-Type")).casefold()
            raw = response.read(MAX_LINK_PAGE_BYTES + 1)
        truncated = len(raw) > MAX_LINK_PAGE_BYTES
        raw = raw[:MAX_LINK_PAGE_BYTES]
        textual = any(marker in content_type for marker in (
            "text/", "json", "xml", "javascript", "xhtml",
        )) or raw.lstrip().startswith((b"<", b"{", b"["))
        if not textual:
            result = {
                "status": "unsupported_content",
                "url": url,
                "final_url": final_url,
                "content_type": content_type,
                "text": "",
                "links": [],
                "truncated": truncated,
            }
        else:
            charset_match = re.search(r"charset=([\w.-]+)", content_type)
            charset = charset_match.group(1) if charset_match else "utf-8"
            try:
                decoded = raw.decode(charset, errors="replace")
            except LookupError:
                decoded = raw.decode("utf-8", errors="replace")
            links: list[str] = []
            text = decoded
            if "html" in content_type or decoded.lstrip().startswith(b"<".decode()):
                parser = LinkEvidenceParser(final_url)
                parser.feed(decoded)
                links.extend(parser.links)
                text = " ".join(parser.text_parts)
            links.extend(PAGE_URL_RE.findall(decoded))
            result = {
                "status": "ok",
                "url": url,
                "final_url": final_url,
                "content_type": content_type,
                "text": clean(text)[:750_000],
                "links": unique(
                    urllib.parse.urljoin(final_url, item.rstrip(".,;)]}"))
                    for item in links
                    if is_absolute_http_url(
                        urllib.parse.urljoin(final_url, item.rstrip(".,;)]}"))
                    )
                ),
                "truncated": truncated,
            }
        write_json(cache_path, result)
        return result
    except (OSError, ValueError, urllib.error.URLError) as error:
        return {
            "status": "error",
            "url": url,
            "final_url": None,
            "content_type": None,
            "text": "",
            "links": [],
            "error": clean(error),
        }


def page_paper_evidence(record: dict[str, Any], page: dict[str, Any]) -> list[str]:
    text = clean(page.get("text"))
    lower = text.casefold()
    normalized_page = normalize_title(text)
    identifiers = record.get("identifiers") or {}
    evidence: list[str] = []
    doi = normalize_doi(identifiers.get("doi"))
    if doi and doi in lower:
        evidence.append("paper_doi_in_resource_page")
    arxiv = clean(identifiers.get("arxiv"))
    if arxiv and arxiv.casefold() in lower:
        evidence.append("paper_arxiv_in_resource_page")
    title = normalize_title(record.get("title"))
    if len(title) >= 20 and title in normalized_page:
        evidence.append("full_paper_title_in_resource_page")
    document = record.get("document") or {}
    known_urls = unique([
        document.get("paper_url"), *(document.get("paper_urls") or []),
        document.get("pdf_url"), *(document.get("pdf_urls") or []),
    ])
    page_links = {
        canonical_resource_url(item) for item in page.get("links") or []
    }
    if any(canonical_resource_url(url) in page_links for url in known_urls):
        evidence.append("paper_url_linked_from_resource_page")
    return unique(evidence)


def graph_relation(source_type: str, target_type: str) -> str:
    relations = {
        ("paper", "code"): "has_code",
        ("paper", "data"): "has_data",
        ("paper", "supplementary"): "has_supplementary",
        ("code", "paper"): "implements",
        ("data", "paper"): "supports",
        ("supplementary", "paper"): "supplements",
        ("code", "data"): "links_data",
        ("data", "code"): "links_code",
        ("supplementary", "code"): "links_code",
        ("supplementary", "data"): "links_data",
        ("code", "supplementary"): "links_supplementary",
        ("data", "supplementary"): "links_supplementary",
    }
    return relations.get((source_type, target_type), "related_to")


def build_resource_graph(
    record: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    cache_dir: Path,
    timeout: float,
    max_rounds: int,
    max_pages: int,
    max_nodes: int,
    page_workers: int | None = None,
) -> dict[str, Any]:
    page_workers = setting('graph_page_workers') if page_workers is None else page_workers
    identifiers = record.get("identifiers") or {}
    paper_key = (
        f"doi:{normalize_doi(identifiers.get('doi'))}"
        if normalize_doi(identifiers.get("doi"))
        else f"arxiv:{clean(identifiers.get('arxiv'))}"
        if clean(identifiers.get("arxiv"))
        else f"paper:{clean(record.get('canonical_id'))}"
    )
    nodes: dict[str, dict[str, Any]] = {
        paper_key: {
            "id": paper_key,
            "type": "paper",
            "url": clean((record.get("document") or {}).get("paper_url")) or None,
            "status": "accepted",
            "confidence": 1.0,
            "evidence": ["input_paper_record"],
        }
    }
    edges: list[dict[str, Any]] = []
    edge_keys: set[tuple[str, str, str, str]] = set()

    def add_edge(
        source: str,
        target: str,
        relation: str,
        evidence_source: str,
        confidence_value: float,
    ) -> None:
        key = (source, target, relation, evidence_source)
        if key in edge_keys:
            return
        edge_keys.add(key)
        edges.append({
            "from": source,
            "to": target,
            "relation": relation,
            "evidence_source": evidence_source,
            "confidence": round(confidence_value, 6),
        })

    def add_node(
        url: str,
        resource_type: str,
        status: str,
        confidence_value: float,
        evidence: str,
    ) -> str:
        canonical = canonical_resource_url(url, resource_type)
        if not canonical:
            return ""
        node_id = f"{resource_type}:{canonical}"
        existing = nodes.get(node_id)
        if existing:
            if status == "accepted" and existing["status"] != "accepted":
                existing["status"] = "accepted"
            existing["confidence"] = max(existing["confidence"], confidence_value)
            existing["evidence"] = unique([*existing["evidence"], evidence])
            return node_id
        nodes[node_id] = {
            "id": node_id,
            "type": resource_type,
            "url": canonical,
            "status": status,
            "confidence": round(confidence_value, 6),
            "evidence": [evidence],
            "inspection": None,
        }
        return node_id

    resources = record.get("resources") or {}
    for resource_type, field in RESOURCE_FIELDS.items():
        for url in resources.get(field) or []:
            if url_kind({"link": url}) != resource_type:
                continue
            node_id = add_node(url, resource_type, "accepted", 1.0, "upstream_validated_url")
            if not node_id:
                continue
            add_edge(
                paper_key, node_id, graph_relation("paper", resource_type),
                "upstream_validated_url", 1.0,
            )

    review_floor = {"code": 0.3, "data": 0.4, "supplementary": 0.55}
    for raw_candidate in candidates:
        if len(nodes) >= max_nodes:
            break
        candidate = dict(raw_candidate)
        resource_type = url_kind(candidate)
        if resource_type not in RESOURCE_FIELDS:
            continue
        score = confidence(record, candidate)
        if score < review_floor[resource_type]:
            continue
        node_id = add_node(
            clean(candidate.get("link")), resource_type, "needs_review", score,
            f"serpapi:{clean(candidate.get('search_basis')) or 'search'}",
        )
        if not node_id:
            continue
        add_edge(
            paper_key, node_id, graph_relation("paper", resource_type),
            "serpapi_search_candidate", score,
        )

    inspected: set[str] = set()
    page_errors = 0
    pages_fetched = 0
    for depth in range(max_rounds):
        pending = [
            node for node in nodes.values()
            if node["type"] in RESOURCE_FIELDS
            and node["id"] not in inspected
            and node.get("url")
        ]
        pending.sort(key=lambda node: (
            node["status"] != "accepted", -float(node.get("confidence") or 0)
        ))
        remaining = max_pages - pages_fetched
        if remaining <= 0 or not pending:
            break
        batch = pending[:remaining]
        with ThreadPoolExecutor(max_workers=min(page_workers, len(batch))) as executor:
            futures = {
                executor.submit(fetch_link_evidence, node["url"], cache_dir, timeout): node
                for node in batch
            }
            fetched = [(futures[future], future.result()) for future in as_completed(futures)]
        for node, page in fetched:
            inspected.add(node["id"])
            pages_fetched += 1
            node["inspection"] = {
                "status": page.get("status"),
                "final_url": page.get("final_url"),
                "content_type": page.get("content_type"),
                "error": page.get("error"),
                "round": depth + 1,
            }
            if page.get("status") != "ok":
                if page.get("status") == "error":
                    page_errors += 1
                continue
            paper_evidence = page_paper_evidence(record, page)
            if paper_evidence:
                node["status"] = "accepted"
                node["confidence"] = max(float(node["confidence"]), 0.95)
                node["evidence"] = unique([*node["evidence"], *paper_evidence])
                add_edge(
                    node["id"], paper_key,
                    graph_relation(node["type"], "paper"),
                    "+".join(paper_evidence), 0.95,
                )
            if node["status"] != "accepted":
                continue
            for link in page.get("links") or []:
                if len(nodes) >= max_nodes:
                    break
                target_type = url_kind({"link": link})
                if target_type not in RESOURCE_FIELDS:
                    continue
                if target_type == node["type"]:
                    continue
                target_url = canonical_resource_url(link, target_type)
                if not target_url or target_url == node["url"]:
                    continue
                target_status = "needs_review" if target_type == "code" else "accepted"
                target_id = add_node(
                    target_url, target_type, target_status,
                    0.75 if target_status == "needs_review" else 0.9,
                    f"explicit_link_from_accepted_{node['type']}",
                )
                if not target_id:
                    continue
                add_edge(
                    node["id"], target_id,
                    graph_relation(node["type"], target_type),
                    "explicit_page_link", 0.9,
                )

    return {
        "schema_version": "resource-graph-v1",
        "generated_at": now_utc(),
        "paper_node": paper_key,
        "nodes": sorted(nodes.values(), key=lambda node: node["id"]),
        "edges": edges,
        "stats": {
            "nodes": len(nodes),
            "edges": len(edges),
            "accepted_nodes": sum(node["status"] == "accepted" for node in nodes.values()),
            "review_nodes": sum(node["status"] == "needs_review" for node in nodes.values()),
            "pages_fetched_or_cached": pages_fetched,
            "page_errors": page_errors,
            "rounds": max((
                (node.get("inspection") or {}).get("round", 0) for node in nodes.values()
            ), default=0),
        },
    }


def execute_logical_query(
    query: dict[str, Any],
    *,
    api_key: str,
    cache_dir: Path,
    reuse_cache_dir: Path | None = None,
    timeout: float,
    retries: int,
    delay: float,
    api_budget: ApiBudget,
    stop_event: Event | None = None,
) -> dict[str, Any]:
    query_type = clean(query.get("query_type"))
    record_context = query.get("record_context") or {}
    attempts = query.get("attempts") or [query]
    all_candidates: list[dict[str, Any]] = []
    attempt_logs: list[dict[str, Any]] = []
    total_api_calls = 0
    total_cache_hits = 0
    successful_attempts = 0

    for attempt_number, attempt in enumerate(attempts, 1):
        if stop_event is not None and stop_event.is_set():
            raise SerpAPISearchError("search cancelled during shutdown", total_api_calls)
        attempt_log = {
            "attempt": attempt_number,
            "engine": attempt.get("engine"),
            "query": attempt.get("q"),
            "search_basis": attempt.get("search_basis"),
            "status": "pending",
            "cache_hit": False,
            "candidate_count": 0,
            "target_candidate_count": 0,
            "verified_target_count": 0,
            "error": None,
        }
        try:
            payload, cache_hit, api_calls = serpapi_search(
                attempt,
                api_key=api_key,
                cache_dir=cache_dir,
                reuse_cache_dir=reuse_cache_dir,
                timeout=timeout,
                retries=retries,
                delay=delay,
                api_budget=api_budget,
                stop_event=stop_event,
            )
            total_api_calls += api_calls
            total_cache_hits += int(cache_hit)
            successful_attempts += 1
            candidates = result_candidates(payload, attempt)
            target_count = sum(
                candidate_plausible_for_query(record_context, candidate, query_type)
                for candidate in candidates
            )
            verified_count = sum(
                candidate_verified_for_query(record_context, candidate, query_type)
                for candidate in candidates
            )
            all_candidates.extend(candidates)
            attempt_log.update({
                "status": "ok",
                "cache_hit": cache_hit,
                "candidate_count": len(candidates),
                "target_candidate_count": target_count,
                "verified_target_count": verified_count,
            })
            attempt_logs.append(attempt_log)
            if verified_count > 0:
                break
        except SerpAPISearchError as error:
            total_api_calls += error.api_calls
            attempt_log.update({"status": "error", "error": clean(error)})
            attempt_logs.append(attempt_log)

    errors = [item["error"] for item in attempt_logs if item.get("error")]
    if not successful_attempts:
        status = "error"
    elif errors:
        status = "completed_with_errors"
    else:
        status = "ok"
    return {
        "status": status,
        "candidates": all_candidates,
        "attempts": attempt_logs,
        "attempt_count": len(attempt_logs),
        "api_calls": total_api_calls,
        "cache_hits": total_cache_hits,
        "fallback_used": len(attempt_logs) > 1,
        "candidate_count": len(all_candidates),
        "target_candidate_count": sum(
            candidate_plausible_for_query(record_context, candidate, query_type)
            for candidate in all_candidates
        ),
        "verified_target_count": sum(
            candidate_verified_for_query(record_context, candidate, query_type)
            for candidate in all_candidates
        ),
        "error": " | ".join(errors) if errors else None,
        "attempt_errors": len(errors),
    }


def add_change(
    changes: list[dict[str, Any]],
    field: str,
    old_value: Any,
    new_value: Any,
    candidate: dict[str, Any],
    score: float,
) -> None:
    changes.append({
        "field": field,
        "old_value": old_value,
        "new_value": new_value,
        "confidence": score,
        "query_type": candidate.get("query_type"),
        "search_basis": candidate.get("search_basis"),
        "evidence_matches": candidate.get("evidence_matches") or [],
        "evidence_title": candidate.get("title"),
        "evidence_url": candidate.get("link"),
    })


def append_url_change(
    record: dict[str, Any],
    field: str,
    url: str,
    candidate: dict[str, Any],
    score: float,
    changes: list[dict[str, Any]],
) -> None:
    resource_type = {
        "supplementary_urls": "supplementary",
        "code_urls": "code",
        "data_urls": "data",
    }[field]
    url = canonical_resource_url(url, resource_type)
    if not url:
        return
    resources = record.setdefault("resources", {})
    values = resources.setdefault(field, [])
    if url not in values:
        old_value = list(values)
        values.append(url)
        add_change(changes, f"resources.{field}", old_value, list(values), candidate, score)


def normalize_existing_resource_urls(
    record: dict[str, Any],
) -> list[dict[str, Any]]:
    resources = record.setdefault("resources", {})
    changes: list[dict[str, Any]] = []
    for resource_type, field in RESOURCE_FIELDS.items():
        old_values = list(resources.get(field) or [])
        normalized: list[str] = []
        for raw_url in old_values:
            url = clean(raw_url)
            canonical = canonical_resource_url(url, resource_type)
            value = canonical or url
            if value and value not in normalized:
                normalized.append(value)
        resources[field] = normalized
        if normalized != old_values:
            changes.append({
                "field": f"resources.{field}",
                "old_value": old_values,
                "new_value": normalized,
                "confidence": 1.0,
                "query_type": resource_type,
                "search_basis": "input_resource_normalization",
                "evidence_matches": ["upstream_validated_url"],
                "evidence_title": None,
                "evidence_url": None,
            })

    for item in record.get("resource_provenance") or []:
        if not isinstance(item, dict):
            continue
        resource_type = clean(item.get("resource_type"))
        if resource_type not in RESOURCE_FIELDS:
            continue
        canonical = canonical_resource_url(item.get("url"), resource_type)
        if canonical:
            item["url"] = canonical
    return changes


def apply_candidates(
    original: dict[str, Any],
    candidates: list[dict[str, Any]],
    query_outcomes: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    record = deepcopy(original)
    query_outcomes = query_outcomes or {}
    identifiers = record.setdefault("identifiers", {})
    document = record.setdefault("document", {})
    document.setdefault("paper_urls", unique([document.get("paper_url")]))
    document.setdefault("pdf_urls", unique([document.get("pdf_url")]))
    resources = record.setdefault("resources", {})
    for field in ("supplementary_urls", "code_urls", "data_urls"):
        resources.setdefault(field, [])
    changes: list[dict[str, Any]] = normalize_existing_resource_urls(record)

    ranked: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate = dict(candidate)
        candidate["url_kind"] = url_kind(candidate)
        candidate["confidence"] = confidence(record, candidate)
        candidate["extracted_identifiers"] = extracted_identifiers(candidate)
        candidate["link_identifiers"] = link_identifiers(candidate)
        candidate["evidence_matches"] = evidence_matches(record, candidate)
        ranked.append(candidate)
    ranked.sort(key=lambda item: (-item["confidence"], int(item.get("position") or 999)))

    review_candidates: list[dict[str, Any]] = []
    accepted_resources: Counter[str] = Counter()
    review_resources: Counter[str] = Counter()
    used_urls: set[str] = set()
    for candidate in ranked:
        score = candidate["confidence"]
        link = clean(candidate.get("link"))
        if not link or link in used_urls:
            continue
        kind = candidate["url_kind"]
        query_type = clean(candidate.get("query_type"))
        if not candidate_allowed_for_query(candidate, query_type):
            continue
        used_urls.add(link)
        threshold = {
            "paper": 0.85,
            "pdf": 0.82,
            "supplementary": 0.82,
            "code": 0.72,
            "data": 0.72,
        }[kind]
        accepted = score >= threshold or doi_matches(record, candidate)
        if not accepted:
            review_floor = {
                "paper": 0.45,
                "pdf": 0.45,
                "supplementary": 0.55,
                "code": 0.3,
                "data": 0.4,
            }[kind]
            if score >= review_floor:
                candidate["review_reason"] = (
                    f"recognized_{kind}_url_but_confidence_below_{threshold}"
                )
                review_candidates.append(candidate)
                if kind in RESOURCE_FIELDS:
                    review_resources[kind] += 1
            continue

        ids = candidate["link_identifiers"]
        for field in ("doi", "arxiv", "openalex", "pmid", "pmcid"):
            if not identifiers.get(field) and ids.get(field):
                old_value = identifiers.get(field)
                identifiers[field] = ids[field]
                add_change(changes, f"identifiers.{field}", old_value, ids[field], candidate, score)
        if not record.get("year"):
            year = candidate_year(candidate)
            if year:
                record["year"] = year
                add_change(changes, "year", None, year, candidate, score)

        if kind == "paper":
            if not document.get("paper_url"):
                document["paper_url"] = link
                add_change(changes, "document.paper_url", None, link, candidate, score)
            if link not in document["paper_urls"]:
                document["paper_urls"].append(link)
        elif kind == "pdf":
            if not document.get("pdf_url"):
                document["pdf_url"] = link
                add_change(changes, "document.pdf_url", None, link, candidate, score)
            if link not in document["pdf_urls"]:
                document["pdf_urls"].append(link)
        elif kind == "supplementary":
            append_url_change(record, "supplementary_urls", link, candidate, score, changes)
            accepted_resources[kind] += 1
        elif kind == "code":
            append_url_change(record, "code_urls", link, candidate, score, changes)
            accepted_resources[kind] += 1
        elif kind == "data":
            append_url_change(record, "data_urls", link, candidate, score, changes)
            accepted_resources[kind] += 1

    resource_status = record.setdefault("resource_status", {})
    for resource_type, field in RESOURCE_FIELDS.items():
        existing_status = current_resource_status(original, resource_type)
        urls = resources.get(field) or []
        outcome = query_outcomes.get(resource_type)
        if existing_status == "not_applicable" and not urls:
            status = "not_applicable"
            reason = "preserved_manual_or_upstream_status"
        elif urls:
            status = "found"
            reason = "one_or_more_validated_urls"
        elif review_resources[resource_type] or (
            outcome and outcome.get("target_candidate_count", 0) > 0
        ):
            status = "needs_review"
            reason = "target_type_candidates_did_not_meet_auto_accept_threshold"
        elif outcome and outcome.get("status") == "error":
            status = "search_error"
            reason = "all_required_search_attempts_failed"
        elif outcome and outcome.get("status") == "completed_with_errors":
            status = "partial_search_error"
            reason = "some_search_attempts_failed_after_partial_completion"
        elif outcome:
            status = "not_found"
            reason = "serpapi_search_completed_without_target_type_candidate"
        else:
            status = "needs_review"
            reason = "resource_type_was_not_searched"
        resource_status[resource_type] = {
            "status": status,
            "reason": reason,
            "url_count": len(urls),
            "accepted_in_this_run": accepted_resources[resource_type],
            "review_candidate_count": review_resources[resource_type],
            "searched": bool(outcome),
            "search_attempts": outcome.get("attempt_count", 0) if outcome else 0,
            "search_errors": outcome.get("attempt_errors", 0) if outcome else 0,
        }

    unresolved: list[str] = []
    if not record.get("year"):
        unresolved.append("year")
    if not identifiers.get("doi") and not identifiers.get("arxiv"):
        unresolved.append("primary_identifier")
    if not document.get("paper_url"):
        unresolved.append("paper_url")
    if not document.get("pdf_url") and not document.get("pdf_urls"):
        unresolved.append("pdf_url")
    for field in ("supplementary_urls", "code_urls", "data_urls"):
        if not resources.get(field):
            unresolved.append(field)

    record["serpapi_enrichment"] = {
        "generated_at": now_utc(),
        "changed_fields": unique(change["field"] for change in changes),
        "changes": changes,
        "unresolved_fields": unresolved,
        "review_candidate_count": len(review_candidates),
        "resource_status": resource_status,
    }
    sync_paper_content(record)
    return record, changes, review_candidates, unresolved


def apply_resource_graph(
    record: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    cache_dir: Path,
    timeout: float,
    max_rounds: int,
    max_pages: int,
    max_nodes: int,
    page_workers: int | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    graph = build_resource_graph(
        record,
        candidates,
        cache_dir=cache_dir,
        timeout=timeout,
        max_rounds=max_rounds,
        max_pages=max_pages,
        max_nodes=max_nodes,
        page_workers=page_workers,
    )
    changes: list[dict[str, Any]] = []
    resources = record.setdefault("resources", {})
    provenance = record.setdefault("resource_provenance", [])
    existing_provenance = {
        (clean(item.get("resource_type")), canonical_resource_url(item.get("url")))
        for item in provenance
        if isinstance(item, dict)
    }
    for node in graph["nodes"]:
        resource_type = clean(node.get("type"))
        if node.get("status") != "accepted" or resource_type not in RESOURCE_FIELDS:
            continue
        field = RESOURCE_FIELDS[resource_type]
        values = resources.setdefault(field, [])
        url = clean(node.get("url"))
        if url and url not in values:
            old_value = list(values)
            values.append(url)
            changes.append({
                "field": f"resources.{field}",
                "old_value": old_value,
                "new_value": list(values),
                "confidence": node.get("confidence"),
                "query_type": resource_type,
                "search_basis": "resource_graph",
                "evidence_matches": node.get("evidence") or [],
                "evidence_title": None,
                "evidence_url": url,
            })
        provenance_key = (resource_type, canonical_resource_url(url, resource_type))
        if url and provenance_key not in existing_provenance:
            provenance.append({
                "resource_type": resource_type,
                "url": url,
                "decision": "accepted",
                "origin": "resource_graph",
                "confidence": node.get("confidence"),
                "evidence": node.get("evidence") or [],
            })
            existing_provenance.add(provenance_key)

    resource_status = record.setdefault("resource_status", {})
    for resource_type, field in RESOURCE_FIELDS.items():
        urls = resources.get(field) or []
        if not urls:
            continue
        current = resource_status.get(resource_type)
        detail = current if isinstance(current, dict) else {}
        detail.update({
            "status": "found",
            "reason": "one_or_more_urls_validated_by_resource_graph",
            "url_count": len(urls),
            "accepted_by_resource_graph": sum(
                node.get("type") == resource_type
                and node.get("status") == "accepted"
                for node in graph["nodes"]
            ),
        })
        resource_status[resource_type] = detail

    unresolved: list[str] = []
    identifiers = record.get("identifiers") or {}
    document = record.get("document") or {}
    if not record.get("year"):
        unresolved.append("year")
    if not identifiers.get("doi") and not identifiers.get("arxiv"):
        unresolved.append("primary_identifier")
    if not document.get("paper_url"):
        unresolved.append("paper_url")
    if not document.get("pdf_url") and not document.get("pdf_urls"):
        unresolved.append("pdf_url")
    for field in RESOURCE_FIELDS.values():
        if not resources.get(field):
            unresolved.append(field)

    enrichment = record.setdefault("serpapi_enrichment", {})
    enrichment.setdefault("changes", [])
    enrichment["changed_fields"] = unique([
        *(enrichment.get("changed_fields") or []),
        *(change["field"] for change in changes),
    ])
    enrichment["unresolved_fields"] = unresolved
    record["resource_graph"] = graph
    sync_paper_content(record)
    return record, changes


def select_records(records: list[dict[str, Any]], start_index: int, max_records: int | None) -> list[dict[str, Any]]:
    selected = records[start_index:]
    if max_records is not None:
        selected = selected[:max_records]
    return selected


def safe_cell(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, ensure_ascii=False)
    if isinstance(value, str):
        value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)
        if len(value) > 32760:
            return value[:32730] + " [TRUNCATED_FOR_XLSX]"
    return value


def style_header(sheet: Any) -> None:
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    fill = PatternFill("solid", fgColor="1F4E78")
    for cell in sheet[1]:
        cell.font = Font(color="FFFFFF", bold=True)
        cell.fill = fill


def paper_row(record: dict[str, Any]) -> dict[str, Any]:
    identifiers = record.get("identifiers") or {}
    document = record.get("document") or {}
    resources = record.get("resources") or {}
    resource_status = record.get("resource_status") or {}
    enrichment = record.get("serpapi_enrichment") or {}
    graph_stats = (record.get("resource_graph") or {}).get("stats") or {}

    def status_value(resource_type: str) -> str | None:
        value = resource_status.get(resource_type)
        return value.get("status") if isinstance(value, dict) else value

    return {
        "canonical_id": record.get("canonical_id"),
        "title": record.get("title"),
        "year": record.get("year"),
        "doi": identifiers.get("doi"),
        "arxiv": identifiers.get("arxiv"),
        "openalex": identifiers.get("openalex"),
        "pmid": identifiers.get("pmid"),
        "pmcid": identifiers.get("pmcid"),
        "paper_url": document.get("paper_url"),
        "pdf_urls": json.dumps(document.get("pdf_urls") or [], ensure_ascii=False),
        "supplementary_urls": json.dumps(resources.get("supplementary_urls") or [], ensure_ascii=False),
        "supplementary_status": status_value("supplementary"),
        "code_urls": json.dumps(resources.get("code_urls") or [], ensure_ascii=False),
        "code_status": status_value("code"),
        "data_urls": json.dumps(resources.get("data_urls") or [], ensure_ascii=False),
        "data_status": status_value("data"),
        "source_types": json.dumps(source_types(record), ensure_ascii=False),
        "changed_fields": json.dumps(enrichment.get("changed_fields") or [], ensure_ascii=False),
        "unresolved_fields": json.dumps(enrichment.get("unresolved_fields") or [], ensure_ascii=False),
        "review_candidate_count": enrichment.get("review_candidate_count", 0),
        "resource_graph_nodes": graph_stats.get("nodes", 0),
        "resource_graph_edges": graph_stats.get("edges", 0),
        "resource_graph_pages": graph_stats.get("pages_fetched_or_cached", 0),
        "resource_graph_page_errors": graph_stats.get("page_errors", 0),
    }


def write_xlsx(
    path: Path,
    records: list[dict[str, Any]],
    existing_changes: list[dict[str, Any]],
    all_changes: list[dict[str, Any]],
    all_reviews: list[dict[str, Any]],
    query_logs: list[dict[str, Any]],
) -> None:
    workbook = Workbook()
    papers = workbook.active
    papers.title = "补全后论文"
    papers.append(PAPER_COLUMNS)
    for record in records:
        row = paper_row(record)
        papers.append([safe_cell(row[column]) for column in PAPER_COLUMNS])
    style_header(papers)
    papers.column_dimensions["A"].width = 32
    papers.column_dimensions["B"].width = 65

    reused = workbook.create_sheet("已有Jina复用")
    reuse_columns = [
        "canonical_id", "title", "field", "old_value", "new_value",
        "origin", "evidence_path", "resource_discovery_path",
    ]
    reused.append(reuse_columns)
    for item in existing_changes:
        reused.append([safe_cell(item.get(column)) for column in reuse_columns])
    style_header(reused)

    changes = workbook.create_sheet("自动写回")
    change_columns = [
        "canonical_id", "title", "field", "old_value", "new_value",
        "confidence", "query_type", "search_basis", "evidence_matches",
        "evidence_title", "evidence_url",
    ]
    changes.append(change_columns)
    for item in all_changes:
        changes.append([safe_cell(item.get(column)) for column in change_columns])
    style_header(changes)

    reviews = workbook.create_sheet("候选待审核")
    review_columns = [
        "canonical_id", "paper_title", "query_type", "url_kind", "confidence",
        "search_basis", "evidence_matches", "review_reason", "position",
        "candidate_title", "link", "snippet",
    ]
    reviews.append(review_columns)
    for item in all_reviews:
        reviews.append([safe_cell(item.get(column)) for column in review_columns])
    style_header(reviews)

    queries = workbook.create_sheet("查询记录")
    query_columns = [
        "canonical_id", "title", "query_type", "engine", "query",
        "status", "attempt_count", "api_calls", "cache_hits", "fallback_used",
        "candidate_count", "target_candidate_count", "verified_target_count",
        "search_bases", "attempts", "error",
    ]
    queries.append(query_columns)
    for item in query_logs:
        queries.append([safe_cell(item.get(column)) for column in query_columns])
    style_header(queries)

    graph_nodes_sheet = workbook.create_sheet("资源图节点")
    graph_node_columns = [
        "canonical_id", "paper_title", "node_id", "type", "url", "status",
        "confidence", "evidence", "inspection",
    ]
    graph_nodes_sheet.append(graph_node_columns)
    for record in records:
        for node in (record.get("resource_graph") or {}).get("nodes") or []:
            row = {
                "canonical_id": record.get("canonical_id"),
                "paper_title": record.get("title"),
                "node_id": node.get("id"),
                **node,
            }
            graph_nodes_sheet.append([
                safe_cell(row.get(column)) for column in graph_node_columns
            ])
    style_header(graph_nodes_sheet)

    graph_edges_sheet = workbook.create_sheet("资源图关系")
    graph_edge_columns = [
        "canonical_id", "paper_title", "from", "to", "relation",
        "evidence_source", "confidence",
    ]
    graph_edges_sheet.append(graph_edge_columns)
    for record in records:
        for edge in (record.get("resource_graph") or {}).get("edges") or []:
            row = {
                "canonical_id": record.get("canonical_id"),
                "paper_title": record.get("title"),
                **edge,
            }
            graph_edges_sheet.append([
                safe_cell(row.get(column)) for column in graph_edge_columns
            ])
    style_header(graph_edges_sheet)

    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan or execute SerpAPI enrichment for selected NatureBench papers"
    )
    parser.add_argument("--mode", choices=("plan", "search"), required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=setting('stage1_env_file'))
    parser.add_argument("--jina-runs-root", type=Path, default=setting('jina_runs_root'))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--reuse-search-cache-dir",
        type=Path,
        help="只复用旧运行的 SerpAPI JSON 缓存；不会复用旧资源图缓存",
    )
    parser.add_argument("--profile", choices=("balanced", "thorough"), default=setting('search_profile'))
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--max-records", type=int)
    parser.add_argument("--max-api-queries", type=int, default=setting('max_search_queries'))
    parser.add_argument("--allow-large-run", action="store_true")
    parser.add_argument("--google-concurrency", type=int, default=setting('google_search_workers'))
    parser.add_argument("--scholar-concurrency", type=int, default=setting('scholar_search_workers'))
    parser.add_argument("--resource-graph-page-workers", type=int, default=setting('graph_page_workers'))
    parser.add_argument("--timeout", type=float, default=setting('search_timeout'))
    parser.add_argument("--retries", type=int, default=setting('search_retries'))
    parser.add_argument("--delay", type=float, default=setting('search_delay'))
    parser.add_argument("--progress-seconds", type=float, default=setting('search_progress_seconds'))
    parser.add_argument(
        "--resource-graph-rounds",
        type=int,
        default=setting('graph_rounds'),
        help="论文、代码、数据和补充材料交叉关联的最大轮数；默认值来自run_pipeline.sh",
    )
    parser.add_argument(
        "--resource-graph-max-pages",
        type=int,
        default=setting('graph_pages'),
        help="每篇论文最多检查的资源页面数；默认值来自run_pipeline.sh",
    )
    parser.add_argument(
        "--resource-graph-max-nodes",
        type=int,
        default=setting('graph_nodes'),
        help="每篇论文资源图最多保留的节点数；默认值来自run_pipeline.sh",
    )
    parser.add_argument(
        "--resource-graph-timeout",
        type=float,
        default=setting('graph_timeout'),
        help="资源页面检查超时秒数；默认值来自run_pipeline.sh",
    )
    parser.add_argument(
        "--disable-resource-graph",
        action="store_true",
        default=not setting('graph_enabled'),
        help="只执行 SerpAPI 搜索，不检查资源之间的交叉关联",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=setting('search_workers'),
        help="并发执行的 SerpAPI 查询数，范围 1-16；默认值来自run_pipeline.sh",
    )
    return parser


@contextmanager
def query_batch(queries, worker, workers, interval, heartbeat, on_abort):
    """Keep only one batch in flight; report fatal collection errors before joining."""
    executor = ThreadPoolExecutor(max_workers=workers)
    stopping = Event()
    pending = {}
    source = iter(queries.items())

    def submit_one():
        entry = next(source, None)
        if entry is not None:
            key, query = entry
            pending[executor.submit(worker, query, stopping)] = key

    def completed():
        for _ in range(workers):
            submit_one()
        while pending:
            done, _ = wait(pending, timeout=interval, return_when=FIRST_COMPLETED)
            if not done:
                heartbeat(len(pending))
            for future in done:
                key = pending.pop(future)
                yield key, future
                submit_one()

    try:
        yield completed()
    except BaseException as error:
        stopping.set()
        on_abort(error)
        raise
    finally:
        stopping.set()
        executor.shutdown(wait=True, cancel_futures=True)


def failed_query_result(message):
    return {
        "status":"error", "candidates":[], "attempts":[], "attempt_count":0,
        "api_calls":0, "cache_hits":0, "attempt_errors":1, "fallback_used":False,
        "candidate_count":0, "target_candidate_count":0, "verified_target_count":0,
        "error":message, "failure_source":"worker_exception",
    }


def failed_record(record, message):
    fallback = deepcopy(record)
    info = fallback.get("serpapi_enrichment") or {}
    fallback["serpapi_enrichment"] = {**info, "processing_status":"error", "processing_error":message}
    statuses = fallback.get("resource_status") or {}
    fallback["resource_status"] = statuses
    for kind, field in RESOURCE_FIELDS.items():
        if not (fallback.get("resources") or {}).get(field):
            statuses[kind] = {"status":"search_error", "reason":"record_processing_failed"}
    return fallback


def main() -> None:
    args = build_parser().parse_args()
    publisher = None
    api_key = ""
    if args.max_api_queries == 0:
        args.max_api_queries = None
    if not args.input.is_file():
        raise SystemExit(f"输入不存在: {args.input.resolve()}")
    if args.start_index < 0:
        raise SystemExit("--start-index 不能小于 0")
    if args.max_records is not None and args.max_records < 1:
        raise SystemExit("--max-records 必须大于 0")
    if args.max_api_queries is not None and args.max_api_queries < 1:
        raise SystemExit("--max-api-queries 必须大于 0")
    if not 1 <= args.concurrency <= 16:
        raise SystemExit("--concurrency 必须在 1 到 16 之间")
    if args.progress_seconds <= 0:
        raise SystemExit("--progress-seconds 必须大于 0")
    if any(not 1 <= value <= 16 for value in (args.google_concurrency, args.scholar_concurrency, args.resource_graph_page_workers)):
        raise SystemExit("搜索引擎及页面并发必须在1到16之间")
    ENGINE_SEMAPHORES.update(google=BoundedSemaphore(args.google_concurrency), google_scholar=BoundedSemaphore(args.scholar_concurrency))
    if args.resource_graph_rounds < 1:
        raise SystemExit("--resource-graph-rounds 必须大于 0")
    if args.resource_graph_max_pages < 1:
        raise SystemExit("--resource-graph-max-pages 必须大于 0")
    if args.resource_graph_max_nodes < 2:
        raise SystemExit("--resource-graph-max-nodes 必须至少为 2")
    if args.resource_graph_timeout <= 0:
        raise SystemExit("--resource-graph-timeout 必须大于 0")
    if (
        args.reuse_search_cache_dir is not None
        and not args.reuse_search_cache_dir.is_dir()
    ):
        raise SystemExit(
            f"--reuse-search-cache-dir 不存在或不是目录: "
            f"{args.reuse_search_cache_dir.resolve()}"
        )

    try:
        all_input_records = read_jsonl(args.input)
        raw_selected_records = select_records(
            all_input_records, args.start_index, args.max_records
        )
        jina_index, jina_index_stats = build_legacy_jina_index(args.jina_runs_root)
        selected_records: list[dict[str, Any]] = []
        existing_changes: list[dict[str, Any]] = []
        for raw_record in raw_selected_records:
            reused_record, changes = reuse_existing_jina(raw_record, jina_index)
            selected_records.append(reused_record)
            for change in changes:
                existing_changes.append({
                    "canonical_id": raw_record.get("canonical_id"),
                    "title": raw_record.get("title"),
                    **change,
                })
        plans = [
            {
                "canonical_id": record.get("canonical_id"),
                "title": record.get("title"),
                "queries": build_query_plan(record, args.profile),
            }
            for record in selected_records
        ]
        planned_queries = sum(len(item["queries"]) for item in plans)
        planned_search_attempts = sum(
            len(query.get("attempts") or [query])
            for item in plans
            for query in item["queries"]
        )
        output_dir = args.output_dir.resolve()
        plan_summary = {
            "schema_version": "serpapi-query-plan-v1",
            "generated_at": now_utc(),
            "mode": args.mode,
            "profile": args.profile,
            "input": str(args.input.resolve()),
            "input_records": len(all_input_records),
            "start_index": args.start_index,
            "selected_records": len(selected_records),
            "planned_queries": planned_queries,
            "planned_search_attempts_max": planned_search_attempts,
            "concurrency": args.concurrency,
            "engine_concurrency": {"google": args.google_concurrency, "google_scholar": args.scholar_concurrency},
            "reuse_search_cache_dir": (
                str(args.reuse_search_cache_dir.resolve())
                if args.reuse_search_cache_dir else None
            ),
            "resource_graph": {
                "enabled": not args.disable_resource_graph,
                "max_rounds": args.resource_graph_rounds,
                "max_pages_per_record": args.resource_graph_max_pages,
                "max_nodes_per_record": args.resource_graph_max_nodes,
                "page_workers": args.resource_graph_page_workers,
                "timeout": args.resource_graph_timeout,
            },
            "existing_jina_reused_values": len(existing_changes),
            "jina_index": jina_index_stats,
            "query_types": dict(sorted(Counter(
                query["query_type"]
                for item in plans
                for query in item["queries"]
            ).items())),
            "note": (
                "Paper/PDF searches use Google Scholar first and ordinary Google as fallback. "
                "Code and data use multiple DOI/full-title/short-title searches; data also uses "
                "Scholar for dataset citations. Fallback stops only after a verified target URL. "
                "Accepted paper/code/data/supplementary nodes are then cross-linked for up to "
                f"{args.resource_graph_rounds} rounds. Cached attempts do not call SerpAPI."
            ),
        }
        write_jsonl(output_dir / "08_query_plan.jsonl", plans)
        write_jsonl(output_dir / "08_existing_jina_reuse.jsonl", existing_changes)
        write_json(output_dir / "08_query_plan_summary.json", plan_summary)

        if args.mode == "plan":
            print(json.dumps({"status": "planned", **plan_summary}, ensure_ascii=False, indent=2))
            return

        if len(selected_records) > 100 and not args.allow_large_run:
            raise ValueError(
                "本次超过 100 篇。先使用 --max-records 做试验；确认后全量运行时显式加入 --allow-large-run"
            )
        if (
            args.max_api_queries is not None
            and planned_search_attempts > args.max_api_queries
        ):
            raise ValueError(
                f"最多可能执行 {planned_search_attempts} 个搜索尝试，"
                f"超过 --max-api-queries={args.max_api_queries}"
            )

        api_key = load_env_value(args.env_file, "SERPAPI_API_KEY")
        stream_spec = importlib.util.spec_from_file_location("stage08_record_stream", Path(__file__).with_name("stream_records.py"))
        stream_module = importlib.util.module_from_spec(stream_spec)
        stream_spec.loader.exec_module(stream_module)
        publisher = stream_module.RecordPublisher(output_dir, "08", args.input, selected_records)
        for record in selected_records:
            publisher.publish(record)
        cache_dir = output_dir / "cache"
        api_budget = ApiBudget(args.max_api_queries)
        enriched_records: list[dict[str, Any]] = []
        query_logs: list[dict[str, Any]] = []
        all_changes: list[dict[str, Any]] = []
        all_reviews: list[dict[str, Any]] = []
        api_calls = 0
        cache_hits = 0
        query_errors = 0
        attempt_errors = 0
        graph_nodes = 0
        graph_edges = 0
        graph_accepted_nodes = 0
        graph_pages = 0
        graph_page_errors = 0

        unique_queries: dict[str, dict[str, Any]] = {}
        for plan in plans:
            for query in plan["queries"]:
                unique_queries.setdefault(query_cache_key(query), query)

        query_results: dict[str, dict[str, Any]] = {}
        processing_errors = []
        last_published = {index:record for index, record in enumerate(selected_records)}
        progress_state = {"last_report":0.0, "graph_records_completed":0}

        def report_error(phase, error, **context):
            message = redact_secret(f"{type(error).__name__}: {clean(error)}", api_key)
            message = message.encode("utf-8", errors="backslashreplace").decode("utf-8")
            event = {"event":"processing_error", "generated_at":now_utc(), "phase":phase,
                     "run_id":publisher.metadata["run_id"], "error":message, **context}
            processing_errors.append(event)
            print(json.dumps(event, ensure_ascii=True), flush=True)
            with (output_dir / "08_processing_errors.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=True) + "\n")
                handle.flush()
            return message

        def report_progress(phase="searching", *, force=False, in_flight=None):
            if not force and time.monotonic() - progress_state["last_report"] < args.progress_seconds:
                return
            fields = {"phase":phase, "queries_completed":len(query_results),
                      "queries_total":len(unique_queries), "query_errors":query_errors,
                      "processing_errors":len(processing_errors), "api_calls":api_budget.used,
                      "cache_hits":cache_hits, "graph_records_completed":progress_state["graph_records_completed"]}
            if in_flight is not None:
                fields["in_flight_queries"] = in_flight
            publisher.progress(**fields)
            print(json.dumps({"event":"search_progress", "generated_at":now_utc(), **fields}), flush=True)
            progress_state["last_report"] = time.monotonic()

        def abort_search(error):
            message = report_error("collector_fatal", error)
            publisher.progress(status="failed", phase="stopping_workers", fatal_error=message,
                               queries_completed=len(query_results), queries_total=len(unique_queries))

        def execute(query, stopping):
            return execute_logical_query(query, api_key=api_key, cache_dir=cache_dir,
                reuse_cache_dir=args.reuse_search_cache_dir, timeout=args.timeout, retries=args.retries,
                delay=args.delay, api_budget=api_budget, stop_event=stopping)

        query_dependents: dict[str, set[int]] = {}
        for index, plan in enumerate(plans):
            for query in plan["queries"]:
                query_dependents.setdefault(query_cache_key(query), set()).add(index)
        report_progress(force=True, in_flight=0)
        with query_batch(unique_queries, execute, args.concurrency, args.progress_seconds,
                         lambda count: report_progress(force=True, in_flight=count), abort_search) as completed:
            for cache_key, future in completed:
                try:
                    result = future.result()
                except Exception as error:
                    message = report_error("query_worker", error, query_key=cache_key,
                                           query_type=unique_queries[cache_key]["query_type"])
                    result = failed_query_result(message)
                query_results[cache_key] = result
                api_calls += result["api_calls"]
                cache_hits += result["cache_hits"]
                attempt_errors += result["attempt_errors"]
                if result["status"] != "ok":
                    query_errors += 1
                for index in sorted(query_dependents[cache_key]):
                    available_candidates = []
                    available_outcomes = {}
                    pending_types = []
                    for query in plans[index]["queries"]:
                        outcome = query_results.get(query_cache_key(query))
                        if outcome is None:
                            pending_types.append(query["query_type"])
                        else:
                            available_candidates.extend(outcome["candidates"])
                            available_outcomes[query["query_type"]] = outcome
                    try:
                        partial, _, _, _ = apply_candidates(selected_records[index], available_candidates, available_outcomes)
                        json.dumps(partial, ensure_ascii=False).encode("utf-8")
                    except Exception as error:
                        message = report_error("partial_record", error, canonical_id=selected_records[index]["canonical_id"],
                                               query_key=cache_key)
                        partial = failed_record(last_published[index], message)
                    for kind in pending_types:
                        if kind in RESOURCE_FIELDS and current_resource_status(partial, kind) != "found":
                            partial.setdefault("resource_status", {})[kind] = {
                                "status": "pending_search", "reason": "query_has_not_completed",
                            }
                    partial["resource_discovery_stream"] = {"phase": "searching", "pending_query_types": pending_types}
                    publisher.publish(partial)
                    last_published[index] = partial
                report_progress()

        report_progress("resource_graph", force=True, in_flight=0)
        for index, (record, plan) in enumerate(zip(selected_records, plans)):
            candidates: list[dict[str, Any]] = []
            record_outcomes: dict[str, dict[str, Any]] = {}
            for query in plan["queries"]:
                result = query_results[query_cache_key(query)]
                candidates.extend(result["candidates"])
                record_outcomes[query["query_type"]] = result
                query_logs.append({
                    "canonical_id": record.get("canonical_id"),
                    "title": record.get("title"),
                    "query_type": query["query_type"],
                    "engine": query["engine"],
                    "query": query["q"],
                    "status": result["status"],
                    "attempt_count": result["attempt_count"],
                    "api_calls": result["api_calls"],
                    "cache_hits": result["cache_hits"],
                    "fallback_used": result["fallback_used"],
                    "candidate_count": result["candidate_count"],
                    "target_candidate_count": result["target_candidate_count"],
                    "verified_target_count": result["verified_target_count"],
                    "search_bases": json.dumps([
                        item.get("search_basis") for item in result["attempts"]
                    ], ensure_ascii=False),
                    "attempts": json.dumps(result["attempts"], ensure_ascii=False),
                    "error": result["error"],
                })

            record_failed = False
            try:
                enriched, changes, reviews, _ = apply_candidates(record, candidates, record_outcomes)
                json.dumps(enriched, ensure_ascii=False).encode("utf-8")
            except Exception as error:
                message = report_error("final_record", error, canonical_id=record["canonical_id"])
                enriched, changes, reviews = failed_record(last_published[index], message), [], []
                record_failed = True
            if not args.disable_resource_graph and not record_failed:
                try:
                    enriched, graph_changes = apply_resource_graph(
                        deepcopy(enriched), candidates, cache_dir=output_dir / "resource_graph_cache",
                        timeout=args.resource_graph_timeout, max_rounds=args.resource_graph_rounds,
                        max_pages=args.resource_graph_max_pages, max_nodes=args.resource_graph_max_nodes,
                        page_workers=args.resource_graph_page_workers,
                    )
                except Exception as error:
                    message = report_error("resource_graph", error, canonical_id=record["canonical_id"])
                    enriched["resource_graph"] = {"error":message, "stats":{"page_errors":1}}
                    graph_changes = []
                changes.extend(graph_changes)
                graph = enriched.get("resource_graph") or {}
                graph_stats = graph.get("stats") or {}
                graph_nodes += int(graph_stats.get("nodes") or 0)
                graph_edges += int(graph_stats.get("edges") or 0)
                graph_accepted_nodes += int(graph_stats.get("accepted_nodes") or 0)
                graph_pages += int(graph_stats.get("pages_fetched_or_cached") or 0)
                graph_page_errors += int(graph_stats.get("page_errors") or 0)
                accepted_graph_urls = {
                    canonical_resource_url(node.get("url"), node.get("type"))
                    for node in graph.get("nodes") or []
                    if node.get("status") == "accepted"
                    and node.get("type") in RESOURCE_FIELDS
                }
                reviews = [
                    candidate for candidate in reviews
                    if canonical_resource_url(
                        candidate.get("link"), candidate.get("url_kind")
                    ) not in accepted_graph_urls
                ]
                enrichment = enriched.setdefault("serpapi_enrichment", {})
                enrichment["review_candidate_count"] = len(reviews)
            enriched_records.append(enriched)
            publisher.publish(enriched)
            progress_state["graph_records_completed"] = index + 1
            report_progress("resource_graph")
            for change in changes:
                all_changes.append({
                    "canonical_id": record.get("canonical_id"),
                    "title": record.get("title"),
                    **change,
                })
            for candidate in reviews:
                all_reviews.append({
                    "canonical_id": record.get("canonical_id"),
                    "paper_title": record.get("title"),
                    "query_type": candidate.get("query_type"),
                    "url_kind": candidate.get("url_kind"),
                    "confidence": candidate.get("confidence"),
                    "search_basis": candidate.get("search_basis"),
                    "evidence_matches": json.dumps(
                        candidate.get("evidence_matches") or [], ensure_ascii=False
                    ),
                    "review_reason": candidate.get("review_reason"),
                    "position": candidate.get("position"),
                    "candidate_title": candidate.get("title"),
                    "link": candidate.get("link"),
                    "snippet": candidate.get("snippet"),
                })

        write_jsonl(output_dir / "08_serpapi_enriched.jsonl", enriched_records)
        write_jsonl(output_dir / "08_serpapi_candidates_for_review.jsonl", all_reviews)
        write_jsonl(output_dir / "08_serpapi_query_log.jsonl", query_logs)
        if query_errors or processing_errors:
            completion_status = "completed_with_errors"
        elif graph_page_errors:
            completion_status = "completed_with_warnings"
        else:
            completion_status = "completed"
        summary = {
            "schema_version": "serpapi-enrichment-summary-v1",
            "generated_at": now_utc(),
            "status": completion_status,
            "profile": args.profile,
            "input": str(args.input.resolve()),
            "output_records": len(enriched_records),
            "planned_queries": planned_queries,
            "planned_search_attempts_max": planned_search_attempts,
            "unique_queries": len(unique_queries),
            "concurrency": args.concurrency,
            "resource_graph": {
                "enabled": not args.disable_resource_graph,
                "nodes": graph_nodes,
                "edges": graph_edges,
                "accepted_nodes": graph_accepted_nodes,
                "pages_fetched_or_cached": graph_pages,
                "page_errors": graph_page_errors,
                "max_rounds": args.resource_graph_rounds,
                "max_pages_per_record": args.resource_graph_max_pages,
                "max_nodes_per_record": args.resource_graph_max_nodes,
            },
            "api_calls": max(api_calls, api_budget.used),
            "cache_hits": cache_hits,
            "query_errors": query_errors,
            "attempt_errors": attempt_errors,
            "processing_errors":len(processing_errors),
            "automatic_changes": len(all_changes),
            "existing_jina_reused_values": len(existing_changes),
            "review_candidates": len(all_reviews),
            "changed_fields": dict(sorted(Counter(
                change["field"] for change in all_changes
            ).items())),
            "remaining_unresolved_fields": dict(sorted(Counter(
                field
                for record in enriched_records
                for field in (record.get("serpapi_enrichment") or {}).get("unresolved_fields") or []
            ).items())),
            "resource_statuses": {
                resource_type: dict(sorted(Counter(
                    current_resource_status(record, resource_type) or "missing"
                    for record in enriched_records
                ).items()))
                for resource_type in RESOURCE_FIELDS
            },
            "abstract_required": False,
            "security": "API key was read from env and was not written to outputs.",
        }
        write_json(output_dir / "08_serpapi_summary.json", summary)
        write_xlsx(
            output_dir / "08_serpapi_enriched.xlsx",
            enriched_records,
            existing_changes,
            all_changes,
            all_reviews,
            query_logs,
        )
        report_progress("completed", force=True, in_flight=0)
        publisher.finish(summary["status"])
    except BaseException as error:
        message = redact_secret(f"{type(error).__name__}: {clean(error)}", api_key)
        print(json.dumps({"event":"stage08_failed", "error":message}, ensure_ascii=True), flush=True)
        if publisher is not None:
            try:
                publisher.progress(fatal_error=message)
                publisher.finish("interrupted" if isinstance(error, KeyboardInterrupt) else "failed")
            finally:
                publisher.close()
        raise SystemExit(message) from error

    print(json.dumps({
        "status": summary["status"],
        "output_dir": str(output_dir),
        "records": summary["output_records"],
        "api_calls": summary["api_calls"],
        "cache_hits": summary["cache_hits"],
        "automatic_changes": summary["automatic_changes"],
        "review_candidates": summary["review_candidates"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
